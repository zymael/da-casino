import asyncio

import discord

import achievements
import cards_render
import db
import moon
from game import Deck, hand_value, is_blackjack
from holdem_view import busy_players

JOIN_SECONDS = 45
ACTION_SECONDS = 45
BETWEEN_HANDS_SECONDS = 120  # how long to wait for everyone who just played to decide before standing them up
EPHEMERAL_DELETE_AFTER = 15  # auto-clean up ephemeral (only-you-can-see-it) replies after this long

OUTCOME_LABELS = {
    "blackjack": "🂡 Blackjack! You win",
    "win": "🎉 You win!",
    "push": "🤝 Push — bet returned",
    "lose": "💥 You lose",
}
OUTCOME_PAYOUT_MULTIPLIERS = {"blackjack": 2.5, "win": 2, "push": 1, "lose": 0}

# channel_id -> BlackjackTable, so only one table can be running per channel
active_tables: dict[int, "BlackjackTable"] = {}


class BlackjackSeat:
    def __init__(self, member: discord.abc.User, bet: int):
        self.member = member
        self.bet = bet  # wagered every round until changed
        self.standing = False  # leave once the current/next round wraps up


class BlackjackTable:
    def __init__(self, channel: discord.abc.Messageable, channel_id: int, guild_id: int):
        self.channel = channel
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.seats: list[BlackjackSeat] = []
        self.control_message: discord.Message | None = None
        # One message, reused/edited for the table's entire life -- every round's turns,
        # settlement, and between-hands prompt all land here instead of flooding the channel
        # with a new message each time. See _send_or_edit_round.
        self.round_message: discord.Message | None = None
        # Last settlement's result embeds, kept around so the between-hands prompt can be
        # appended alongside them instead of replacing/clearing them off round_message.
        self.last_result_embeds: list[discord.Embed] = []
        # Lets a seated player skip the rest of the join window and deal immediately -- unlike
        # roulette/horserace there's no shared betting window worth waiting out here.
        self.start_event = asyncio.Event()
        # The shoe persists across rounds and is only reshuffled once it runs out, so
        # the running count carries between hands -- that's what makes counting possible.
        self.shoe = Deck()
        # The round currently in progress (or just-settled, kept until the next round starts) --
        # None only before the table's first round ever deals. Populated by play_round as it runs.
        self.round: "RoundState | None" = None

    def seat_for(self, user_id: int) -> BlackjackSeat | None:
        return next((s for s in self.seats if s.member.id == user_id), None)

    def draw(self):
        if not self.shoe.cards:
            self.shoe = Deck()
        return self.shoe.draw()

    # --- Mutations shared by the Discord buttons/modals -----------------------------------
    # Simple, single-statement mutations -- no await in between, so no locking need beyond
    # asyncio's own cooperative scheduling already provides. Extracted for one code path per
    # action, not because these specifically were race-prone (unlike turn actions -- see
    # RoundState.turn_lock for the one mutation path that actually spans an await).

    def join(self, member: discord.abc.User, bet: int) -> BlackjackSeat:
        seat = BlackjackSeat(member, bet)
        self.seats.append(seat)
        return seat

    def quit_seat(self, user_id: int) -> bool:
        seat = self.seat_for(user_id)
        if seat is None:
            return False
        seat.standing = True
        return True

    def set_bet(self, user_id: int, bet: int) -> bool:
        seat = self.seat_for(user_id)
        if seat is None:
            return False
        seat.bet = bet
        return True

    def start(self):
        self.start_event.set()


class BlackjackHand:
    def __init__(self, member: discord.abc.User, bet: int):
        self.member = member
        self.bet = bet
        self.cards: list = []
        self.busted = False


class RoundState:
    """Live state for one round, held on BlackjackTable.round for the round's whole life
    (including the between-hands window right after it settles) -- see BlackjackTable.round."""

    def __init__(self, hands: list[BlackjackHand], dealer: list, dealer_natural: bool):
        self.hands = hands
        self.dealer = dealer
        self.dealer_natural = dealer_natural
        self.active_hand_index: int | None = None  # None between turns, or once the round's done
        self.phase = "playing"  # "playing" -> "dealer_turn" -> "settled"
        # The live BetweenHandsView once this round has settled and run_between_hands is waiting
        # on keep-bet/change-bet/quit decisions -- None otherwise.
        self.between_hands_view: "BetweenHandsView | None" = None
        # Guards hit/stand/double_down/on_timeout against each other. One lock per round (not per
        # turn) since only one hand can validly be acting at a time anyway.
        self.turn_lock = asyncio.Lock()
        # The live BlackjackTurnView for whichever hand is currently acting, or None between turns.
        self.active_view: "BlackjackTurnView | None" = None


DRAW_BIASED_WINDOW = 3


def draw_biased(table: "BlackjackTable", favor: str | None) -> "Card":
    """Like table.draw(), but on a secret moon night (see moon.py) peeks at the next
    DRAW_BIASED_WINDOW cards still in the shoe and swaps in whichever is most extreme in the
    dealer's favor/disfavor -- highest value for a "house" night, lowest for "player" -- instead
    of strictly the top card. Only reorders within that small window; the shoe's actual card
    composition never changes, and this never touches the player's own hand or the initial deal
    at all -- only how often the dealer busts on their own forced hit-until-17 draws (see
    play_round). Falls back to a plain table.draw() when favor is None or the shoe's too shallow
    to safely peek, so it never interferes with Deck's own reshuffle-on-empty logic."""
    if favor is None or len(table.shoe.cards) < DRAW_BIASED_WINDOW:
        return table.draw()
    peek = table.shoe.cards[-DRAW_BIASED_WINDOW:]  # Deck.draw() pops from the end
    chosen = max(peek, key=lambda c: c.value) if favor == "house" else min(peek, key=lambda c: c.value)
    table.shoe.cards.remove(chosen)
    return chosen


def outcome_for(hand: BlackjackHand, dealer: list, dealer_natural: bool) -> str:
    if hand.busted:
        return "lose"
    player_natural = is_blackjack(hand.cards)
    if player_natural and dealer_natural:
        return "push"
    if player_natural:
        return "blackjack"
    if dealer_natural:
        return "lose"
    player_total, dealer_total = hand_value(hand.cards), hand_value(dealer)
    if dealer_total > 21 or player_total > dealer_total:
        return "win"
    if player_total < dealer_total:
        return "lose"
    return "push"


def build_control_embed(table: BlackjackTable) -> discord.Embed:
    embed = discord.Embed(title="🃏 Blackjack Table", color=discord.Color.dark_green())
    if not table.seats:
        embed.description = "No one's seated yet."
    else:
        currency = db.get_currency_name(table.guild_id)
        lines = []
        for s in table.seats:
            tag = " — *standing up after this round*" if s.standing else ""
            lines.append(f"**{s.member.display_name}** — betting {s.bet} {currency}{tag}")
        embed.description = "\n".join(lines)
    embed.set_footer(
        text=f"Shoe: {len(table.shoe.cards)} cards left — only reshuffles when it runs out. "
        "Join Table to sit down, Quit to leave whenever. Between each round, everyone who just "
        "played gets asked to keep their bet or change it before the next one deals."
    )
    return embed


async def update_control_message(table: BlackjackTable):
    if table.control_message is not None:
        try:
            await table.control_message.edit(embed=build_control_embed(table))
        except discord.HTTPException:
            pass


async def _send_or_edit_round(
    table: BlackjackTable, ctx, *, embeds: list[discord.Embed],
    files: list[discord.File] | None = None, view: discord.ui.View | None,
) -> discord.Message:
    """Sends table.round_message the first time it's needed, then reuses the same message for
    every later call -- this is what keeps a whole table's lifetime (every round's turns,
    settlement, and between-hands prompt) to one persistent, continuously-edited message instead
    of a new one flooding the channel each time. Recreates it if it was deleted out from under us
    (discord.NotFound), so that doesn't silently kill the table's visible UI while it keeps
    dealing hands and moving money in the background.

    `files` controls attachments explicitly, since editing otherwise leaves them untouched:
    a list (possibly empty) replaces/clears them; `None` (the default) leaves whatever's already
    attached alone -- used when moving from settlement into the between-hands prompt, so the
    result images stay visible instead of vanishing the instant the prompt appears."""
    if table.round_message is None:
        table.round_message = await ctx.send(embeds=embeds, files=files or [], view=view)
        return table.round_message
    edit_kwargs = {"embeds": embeds, "view": view}
    if files is not None:
        edit_kwargs["attachments"] = files
    try:
        await table.round_message.edit(**edit_kwargs)
    except discord.NotFound:
        table.round_message = await ctx.send(embeds=embeds, files=files or [], view=view)
    except discord.HTTPException:
        pass
    return table.round_message


class JoinModal(discord.ui.Modal):
    def __init__(self, table: BlackjackTable):
        super().__init__(title="Join Table")
        self.table = table
        self.amount_input = discord.ui.TextInput(label="Bet each round", placeholder="e.g. 50")
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message("Enter a whole number.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
            return
        if bet <= 0:
            await interaction.response.send_message("Bet must be positive.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
            return

        self.table.join(interaction.user, bet)
        await interaction.response.send_message("You're seated! You'll be dealt into the next round.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
        await update_control_message(self.table)


class JoinButton(discord.ui.Button):
    """Sit down at the table for the first time. An existing seat changes its bet between
    hands instead (see BetweenHandsView) -- there's no "queue this for later" anymore, it's a
    dedicated step the table actually waits on."""

    def __init__(self, table: BlackjackTable):
        super().__init__(label="Join Table", style=discord.ButtonStyle.success)
        self.table = table

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.bot:
            await interaction.response.send_message("Bots can't play.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
            return
        if self.table.seat_for(interaction.user.id) is not None:
            await interaction.response.send_message(
                "You're already seated — change your bet between hands, after the current round.",
                ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER,
            )
            return
        await interaction.response.send_modal(JoinModal(self.table))


class QuitButton(discord.ui.Button):
    """Leave the table after the current round -- available any time (unlike bet changes,
    which are a between-hands-only decision) so a seated player who isn't in a round this
    time around, e.g. too broke to be dealt in, always has a way out."""

    def __init__(self, table: BlackjackTable):
        super().__init__(label="Quit", style=discord.ButtonStyle.danger)
        self.table = table

    async def callback(self, interaction: discord.Interaction):
        if not self.table.quit_seat(interaction.user.id):
            await interaction.response.send_message("You're not seated at this table.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
            return
        await interaction.response.send_message("Noted — you'll stand up after the current round.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
        await update_control_message(self.table)


class StartButton(discord.ui.Button):
    """Skips the rest of the join window and deals the first round right away. Unlike
    roulette/horserace, where waiting out the window lets more bets land on one shared round,
    blackjack has nothing to gain from a forced wait once the seated players are ready."""

    def __init__(self, table: BlackjackTable):
        super().__init__(label="Start Game", style=discord.ButtonStyle.success, row=1)
        self.table = table

    async def callback(self, interaction: discord.Interaction):
        if self.table.seat_for(interaction.user.id) is None:
            await interaction.response.send_message("You're not seated at this table.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
            return
        self.table.start()
        await interaction.response.send_message("Starting the first round now!", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)


class TableControlView(discord.ui.View):
    def __init__(self, table: BlackjackTable):
        super().__init__(timeout=None)
        self.table = table
        self.add_item(JoinButton(table))
        self.add_item(QuitButton(table))
        self.start_button = StartButton(table)
        self.add_item(self.start_button)


def apply_hit(table: BlackjackTable, hand: BlackjackHand) -> bool:
    """Draws one card into hand, marking it busted if that pushes it over 21. Returns whether it
    busted."""
    hand.cards.append(table.draw())
    if hand_value(hand.cards) > 21:
        hand.busted = True
    return hand.busted


async def apply_double_down(table: BlackjackTable, hand: BlackjackHand) -> tuple[bool, str | None]:
    """Escrows and doubles the bet, then draws one card. Returns (busted, error) -- error is set
    (and nothing is mutated) if the player can't afford it, same affordability check the Discord
    button already did inline."""
    balance = await asyncio.to_thread(db.get_balance, table.guild_id, hand.member.id)
    if balance < hand.bet:
        return False, "insufficient_balance"
    await asyncio.to_thread(db.update_balance, table.guild_id, hand.member.id, -hand.bet)
    hand.bet *= 2
    hand.cards.append(table.draw())
    hand.busted = hand_value(hand.cards) > 21
    return hand.busted, None


class BlackjackTurnView(discord.ui.View):
    def __init__(self, table: BlackjackTable, hand: BlackjackHand, dealer: list):
        super().__init__(timeout=ACTION_SECONDS)
        self.table = table
        self.hand = hand
        self.dealer = dealer
        self.done = False
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.hand.member.id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
            return False
        return True

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    def build_display(self, result_text: str | None = None) -> tuple[list[discord.Embed], list[discord.File]]:
        files = []
        currency = db.get_currency_name(self.table.guild_id)

        dealer_buf = cards_render.render_hand(self.dealer, hide_first=True)
        files.append(discord.File(dealer_buf, filename="dealer.png"))
        dealer_embed = discord.Embed(title="🃏 Blackjack — Dealer", color=discord.Color.gold())
        dealer_embed.description = "Value: ?"
        dealer_embed.set_image(url="attachment://dealer.png")

        player_buf = cards_render.render_hand(self.hand.cards)
        files.append(discord.File(player_buf, filename="player.png"))
        player_embed = discord.Embed(title=self.hand.member.display_name, color=discord.Color.gold())
        player_embed.description = f"Value: {hand_value(self.hand.cards)}"
        player_embed.set_image(url="attachment://player.png")
        player_embed.add_field(name="Bet", value=f"{self.hand.bet} {currency}", inline=True)
        if result_text:
            player_embed.add_field(name="Result", value=result_text, inline=False)

        return [dealer_embed, player_embed], files

    async def _finish(self, result_text: str, interaction: discord.Interaction | None = None):
        """Ends this turn: disables the buttons, renders the final state, and stop()s the view
        so play_round's `await view.wait()` unblocks. `interaction` is only present for a
        Discord-button-triggered finish (edits via interaction.response, the required way to
        answer that specific click); a timeout has no interaction to answer, so it edits
        self.message directly instead."""
        self.done = True
        self._disable_all()
        embeds, files = self.build_display(result_text=result_text)
        if interaction is not None:
            await interaction.response.edit_message(embeds=embeds, attachments=files, view=self)
        elif self.message is not None:
            try:
                await self.message.edit(embeds=embeds, attachments=files, view=self)
            except discord.HTTPException:
                pass
        self.stop()

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, row=0)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.table.round.turn_lock:
            if self.done:
                await interaction.response.defer()
                return
            busted = apply_hit(self.table, self.hand)
            if len(self.hand.cards) > 2:
                self.double_down.disabled = True
            if busted:
                await self._finish("💥 Bust!", interaction=interaction)
                return
            embeds, files = self.build_display()
            await interaction.response.edit_message(embeds=embeds, attachments=files, view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, row=0)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.table.round.turn_lock:
            if self.done:
                await interaction.response.defer()
                return
            await self._finish("✋ Stand", interaction=interaction)

    @discord.ui.button(label="Double Down", style=discord.ButtonStyle.danger, row=0)
    async def double_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.table.round.turn_lock:
            if self.done:
                await interaction.response.defer()
                return
            busted, error = await apply_double_down(self.table, self.hand)
            if error:
                currency = db.get_currency_name(self.table.guild_id)
                await interaction.response.send_message(f"You don't have enough {currency} to double down.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
                return
            await self._finish("💥 Bust!" if busted else "✋ Doubled down", interaction=interaction)

    async def on_timeout(self):
        async with self.table.round.turn_lock:
            if self.done:
                return
            await self._finish("⌛ Timed out — standing")


async def settle_round(ctx, table: BlackjackTable, hands: list[BlackjackHand], dealer: list, dealer_natural: bool):
    if table.round is not None:
        table.round.phase = "settled"
    currency = db.get_currency_name(table.guild_id)

    dealer_buf = cards_render.render_hand(dealer)
    embeds = [discord.Embed(title="🃏 Blackjack — Dealer", description=f"Value: {hand_value(dealer)}", color=discord.Color.gold())]
    embeds[0].set_image(url="attachment://dealer.png")
    files = [discord.File(dealer_buf, filename="dealer.png")]

    achievement_hands = []
    for i, hand in enumerate(hands):
        outcome = outcome_for(hand, dealer, dealer_natural)
        payout = int(hand.bet * OUTCOME_PAYOUT_MULTIPLIERS[outcome])
        if payout:
            balance = await asyncio.to_thread(db.update_balance, table.guild_id, hand.member.id, payout)
        else:
            balance = await asyncio.to_thread(db.get_balance, table.guild_id, hand.member.id)
        net = payout - hand.bet
        await asyncio.to_thread(db.log_bet, table.guild_id, hand.member.id, "blackjack", hand.bet, net)
        kinds = achievements.kinds_for_bet("blackjack", net)
        kinds += await achievements.record_and_check(table.guild_id, hand.member.id, "blackjack", net)
        if kinds:
            achievement_hands.append((hand.member, kinds))

        buf = cards_render.render_hand(hand.cards)
        fname = f"hand{i}.png"
        files.append(discord.File(buf, filename=fname))
        color = discord.Color.green() if net > 0 else (discord.Color.red() if net < 0 else discord.Color.greyple())
        player_embed = discord.Embed(title=hand.member.display_name, description=f"Value: {hand_value(hand.cards)}", color=color)
        player_embed.set_image(url=f"attachment://{fname}")
        player_embed.add_field(
            name="Result", value=f"{OUTCOME_LABELS[outcome]} ({'+' if net >= 0 else ''}{net} {currency})", inline=False
        )
        player_embed.set_footer(text=f"Balance: {balance} {currency}")
        embeds.append(player_embed)

    # Capped to 9 (not 10) so run_between_hands has room to append its status embed to these
    # same result embeds afterward, rather than needing to replace/clear them.
    table.last_result_embeds = embeds[:9]
    await _send_or_edit_round(table, ctx, embeds=table.last_result_embeds, files=files, view=None)
    for member, kinds in achievement_hands:
        await achievements.try_award_many(ctx.send, table.guild_id, member.id, member.display_name, kinds)


class BetweenHandsBetModal(discord.ui.Modal):
    def __init__(self, view: "BetweenHandsView"):
        super().__init__(title="Change Next Bet")
        self.view = view
        self.amount_input = discord.ui.TextInput(label="Bet each round", placeholder="e.g. 50")
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message("Enter a whole number.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
            return
        if bet <= 0:
            await interaction.response.send_message("Bet must be positive.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
            return

        self.view.table.set_bet(interaction.user.id, bet)
        await interaction.response.send_message(f"Bet updated to {bet} for the next round.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
        await self.view.mark_decided(interaction.user.id)


class BetweenHandsView(discord.ui.View):
    """Shown after a round settles, listing everyone who just played. The table won't deal
    the next round until every one of them has confirmed -- keep their bet, change it, or
    leave -- rather than letting changes get silently queued for whenever."""

    def __init__(self, table: BlackjackTable, pending_ids: set[int]):
        super().__init__(timeout=None)  # run_table owns the actual wait/timeout, via decided_event
        self.table = table
        self.pending = set(pending_ids)
        self.message: discord.Message | None = None
        self.decided_event = asyncio.Event()
        if not self.pending:
            self.decided_event.set()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.pending:
            await interaction.response.send_message(
                "You're not part of this decision (already decided, or weren't in that round).",
                ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER,
            )
            return False
        return True

    def build_embeds(self, timed_out: bool = False) -> list[discord.Embed]:
        """The last round's result embeds, plus a status embed appended after them -- so the
        results stay visible the whole time everyone's deciding, instead of getting replaced by
        this prompt the instant it appears."""
        status = discord.Embed(title="🃏 Between Hands", color=discord.Color.blurple())
        if self.pending:
            names = ", ".join(f"<@{uid}>" for uid in self.pending)
            if timed_out:
                status.description = f"Still waiting on: {names} — standing up before the next round."
            else:
                status.description = f"Waiting on: {names}\nKeep your bet, change it, or quit before the next round deals."
        else:
            status.description = "Everyone's decided — dealing the next round..."
        return [*self.table.last_result_embeds, status]

    async def mark_decided(self, user_id: int):
        self.pending.discard(user_id)
        if not self.pending:
            self.decided_event.set()
            self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(embeds=self.build_embeds(), view=self)
            except discord.HTTPException:
                pass

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Keep Bet", style=discord.ButtonStyle.secondary)
    async def keep_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Keeping your current bet.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
        await self.mark_decided(interaction.user.id)

    @discord.ui.button(label="Change Bet", style=discord.ButtonStyle.success)
    async def change_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BetweenHandsBetModal(self))

    @discord.ui.button(label="Quit", style=discord.ButtonStyle.danger)
    async def quit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.table.quit_seat(interaction.user.id)
        await interaction.response.send_message("Noted — you'll stand up before the next round.", ephemeral=True, delete_after=EPHEMERAL_DELETE_AFTER)
        await self.mark_decided(interaction.user.id)


async def play_round(ctx, table: BlackjackTable, seats: list[BlackjackSeat]) -> set[int]:
    hands = [BlackjackHand(s.member, s.bet) for s in seats]
    busy_players.update(h.member.id for h in hands)
    try:
        for hand in hands:
            await asyncio.to_thread(db.update_balance, table.guild_id, hand.member.id, -hand.bet)  # escrow the bet

        dealer = [table.draw(), table.draw()]
        for hand in hands:
            hand.cards = [table.draw(), table.draw()]

        dealer_natural = is_blackjack(dealer)
        # Populated here (not earlier) since this is the first point all three pieces exist --
        # from here on, table.round reflects this round's live state for any reader, Discord's
        # own rendering or otherwise.
        table.round = RoundState(hands, dealer, dealer_natural)

        for hand in hands:
            if is_blackjack(hand.cards) or dealer_natural:
                continue  # natural blackjack, or the dealer already has one -- no turn to take
            table.round.active_hand_index = hands.index(hand)
            view = BlackjackTurnView(table, hand, dealer)
            table.round.active_view = view
            embeds, files = view.build_display()
            view.message = await _send_or_edit_round(table, ctx, embeds=embeds, files=files, view=view)
            # Editing round_message doesn't notify anyone -- Discord only pings on new messages --
            # so a small, separate ping message is still needed. It's deleted the moment this
            # turn resolves, so at most one exists at a time instead of piling up like the old
            # per-turn messages did.
            ping = await ctx.send(f"{hand.member.mention} — your turn! {view.message.jump_url}")
            try:
                await view.wait()
                async with table.round.turn_lock:
                    pass  # drain any in-flight callback (e.g. a slow double_down) before reading hand state
            finally:
                table.round.active_view = None
                try:
                    await ping.delete()
                except discord.HTTPException:
                    pass
        table.round.active_hand_index = None

        needs_dealer_play = not dealer_natural and any(
            not h.busted and not is_blackjack(h.cards) for h in hands
        )
        table.round.phase = "dealer_turn"
        if needs_dealer_play:
            moon_effect = moon.effect_for("blackjack")
            while hand_value(dealer) < 17:
                dealer.append(draw_biased(table, moon_effect))

        await settle_round(ctx, table, hands, dealer, dealer_natural)
        return {h.member.id for h in hands}
    finally:
        busy_players.difference_update(h.member.id for h in hands)


async def run_between_hands(ctx, table: BlackjackTable, played_ids: set[int]):
    """Waits for everyone who just played (and is still seated) to confirm keep/change/quit
    before the next round deals -- up to BETWEEN_HANDS_SECONDS, after which anyone who hasn't
    responded just keeps their current bet."""
    pending_ids = {uid for uid in played_ids if table.seat_for(uid) is not None}
    view = BetweenHandsView(table, pending_ids)
    if table.round is not None:
        table.round.between_hands_view = view
    # files omitted (not []) so the result images from settle_round stay attached and visible.
    view.message = await _send_or_edit_round(table, ctx, embeds=view.build_embeds(), view=view)

    # Same reasoning as the per-turn ping in play_round: editing round_message doesn't notify
    # anyone, so without a separate ping this checkpoint can look identical to the table having
    # gone idle or everyone having left -- especially confusing when you're the only one seated,
    # since there's no other player activity to reassure you the table's still alive and waiting
    # on you specifically.
    ping = None
    if pending_ids:
        mentions = " ".join(f"<@{uid}>" for uid in pending_ids)
        ping = await ctx.send(f"{mentions} — waiting on you: keep your bet, change it, or quit! {view.message.jump_url}")

    try:
        await asyncio.wait_for(view.decided_event.wait(), timeout=BETWEEN_HANDS_SECONDS)
    except asyncio.TimeoutError:
        pass
    finally:
        if ping is not None:
            try:
                await ping.delete()
            except discord.HTTPException:
                pass

    if view.pending:
        # Defaulting a non-response to standing up (rather than the old behavior of quietly
        # re-dealing them in with their current bet) means an AFK player's balance stops being
        # put at risk just because they stepped away -- same reasoning as Quit already being the
        # explicit-click default action, just applied to silence too.
        for user_id in view.pending:
            seat = table.seat_for(user_id)
            if seat is not None:
                seat.standing = True
        view._disable_all()
        try:
            await view.message.edit(embeds=view.build_embeds(timed_out=True), view=view)
        except discord.HTTPException:
            pass

    if table.round is not None:
        table.round.between_hands_view = None


async def _close_round_message(table: BlackjackTable, ctx, text: str):
    embed = discord.Embed(description=text, color=discord.Color.dark_grey())
    await _send_or_edit_round(table, ctx, embeds=[embed], files=[], view=None)


async def run_table(ctx, table: BlackjackTable):
    try:
        while True:
            active_seats = [s for s in table.seats if not s.standing]
            playable = []
            for s in active_seats:
                if s.member.id in busy_players:
                    continue
                balance = await asyncio.to_thread(db.get_balance, table.guild_id, s.member.id)
                if balance < s.bet:
                    continue
                playable.append(s)

            if not playable:
                await _close_round_message(
                    table, ctx, "No funded players left to deal a round — table closed. "
                    "Run `!blackjack <bet>` to start a new one."
                )
                break

            played_ids = await play_round(ctx, table, playable)

            table.seats = [s for s in table.seats if not s.standing]
            await update_control_message(table)

            if not table.seats:
                await _close_round_message(table, ctx, "Table closed — everyone's stood up.")
                break

            await run_between_hands(ctx, table, played_ids)

            table.seats = [s for s in table.seats if not s.standing]  # someone may have quit during the wait
            await update_control_message(table)

            if not table.seats:
                await _close_round_message(table, ctx, "Table closed — everyone's stood up.")
                break
    finally:
        active_tables.pop(table.channel_id, None)
        if table.control_message is not None:
            try:
                await table.control_message.edit(view=None)
            except discord.HTTPException:
                pass
        if table.round_message is not None:
            try:
                await table.round_message.edit(view=None)
            except discord.HTTPException:
                pass


def create_table(channel: discord.abc.Messageable, channel_id: int, guild_id: int, author: discord.abc.User, bet: int) -> BlackjackTable:
    """Creates and registers a new table with its opener already seated. Entirely synchronous and
    side-effect-free beyond the active_tables registration, so a caller can rely on the table
    existing in active_tables the instant this returns, with no await-related race to worry about."""
    table = BlackjackTable(channel, channel_id, guild_id)
    table.seats.append(BlackjackSeat(author, bet))
    active_tables[channel_id] = table
    return table


async def run_new_table(ctx, table: BlackjackTable):
    """Posts the lobby message, waits out the join window (or an early Start Game click), then
    runs the table until it closes."""
    view = TableControlView(table)
    message = await ctx.send(
        content=f"🃏 {table.seats[0].member.mention} opened a blackjack table! Dealing the first "
        f"round in {JOIN_SECONDS}s, or click **Start Game** to deal now.",
        embed=build_control_embed(table),
        view=view,
    )
    table.control_message = message

    try:
        await asyncio.wait_for(table.start_event.wait(), timeout=JOIN_SECONDS)
    except asyncio.TimeoutError:
        pass

    # Start Game only makes sense during this initial join window -- remove it before the table
    # starts looping through rounds.
    view.remove_item(view.start_button)
    try:
        await table.control_message.edit(embed=build_control_embed(table), view=view)
    except discord.HTTPException:
        pass

    await run_table(ctx, table)


async def start_blackjack_table(ctx, bet: int):
    table = create_table(ctx.channel, ctx.channel.id, ctx.guild.id, ctx.author, bet)
    await run_new_table(ctx, table)
