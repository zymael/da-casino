import asyncio

import discord

import cards_render
import db
from game import Deck, hand_value, is_blackjack
from holdem_view import busy_players

JOIN_SECONDS = 45
ACTION_SECONDS = 45
BETWEEN_ROUNDS_SECONDS = 5

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
        # The shoe persists across rounds and is only reshuffled once it runs out, so
        # the running count carries between hands -- that's what makes counting possible.
        self.shoe = Deck()

    def seat_for(self, user_id: int) -> BlackjackSeat | None:
        return next((s for s in self.seats if s.member.id == user_id), None)

    def draw(self):
        if not self.shoe.cards:
            self.shoe = Deck()
        return self.shoe.draw()


class BlackjackHand:
    def __init__(self, member: discord.abc.User, bet: int):
        self.member = member
        self.bet = bet
        self.cards: list = []
        self.busted = False


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
        "Join / Set Bet to sit down or change your wager. Stand to leave whenever you like."
    )
    return embed


async def update_control_message(table: BlackjackTable):
    if table.control_message is None:
        return
    try:
        await table.control_message.edit(embed=build_control_embed(table))
    except discord.HTTPException:
        pass


class BetModal(discord.ui.Modal):
    def __init__(self, table: BlackjackTable):
        super().__init__(title="Join / Set Bet")
        self.table = table
        self.amount_input = discord.ui.TextInput(label="Bet each round", placeholder="e.g. 50")
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message("Enter a whole number.", ephemeral=True)
            return
        if bet <= 0:
            await interaction.response.send_message("Bet must be positive.", ephemeral=True)
            return

        seat = self.table.seat_for(interaction.user.id)
        if seat is not None:
            seat.bet = bet
            seat.standing = False
            await interaction.response.send_message("Bet updated for your next round.", ephemeral=True)
        else:
            self.table.seats.append(BlackjackSeat(interaction.user, bet))
            await interaction.response.send_message("You're seated! You'll be dealt into the next round.", ephemeral=True)

        await update_control_message(self.table)


class TableControlView(discord.ui.View):
    def __init__(self, table: BlackjackTable):
        super().__init__(timeout=None)
        self.table = table

    @discord.ui.button(label="Join / Set Bet", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot:
            await interaction.response.send_message("Bots can't play.", ephemeral=True)
            return
        await interaction.response.send_modal(BetModal(self.table))

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.danger)
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        seat = self.table.seat_for(interaction.user.id)
        if seat is None:
            await interaction.response.send_message("You're not seated at this table.", ephemeral=True)
            return
        seat.standing = True
        await interaction.response.send_message("Noted — you'll stand up after the current round.", ephemeral=True)
        await update_control_message(self.table)


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
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
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

    async def _finish(self, interaction: discord.Interaction, result_text: str):
        self.done = True
        self._disable_all()
        embeds, files = self.build_display(result_text=result_text)
        await interaction.response.edit_message(embeds=embeds, attachments=files, view=self)
        self.stop()

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.hand.cards.append(self.table.draw())
        if len(self.hand.cards) > 2:
            self.double_down.disabled = True
        if hand_value(self.hand.cards) > 21:
            self.hand.busted = True
            await self._finish(interaction, "💥 Bust!")
            return
        embeds, files = self.build_display()
        await interaction.response.edit_message(embeds=embeds, attachments=files, view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, "✋ Stand")

    @discord.ui.button(label="Double Down", style=discord.ButtonStyle.danger)
    async def double_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        currency = db.get_currency_name(self.table.guild_id)
        balance = await asyncio.to_thread(db.get_balance, self.table.guild_id, self.hand.member.id)
        if balance < self.hand.bet:
            await interaction.response.send_message(f"You don't have enough {currency} to double down.", ephemeral=True)
            return

        await asyncio.to_thread(db.update_balance, self.table.guild_id, self.hand.member.id, -self.hand.bet)
        self.hand.bet *= 2
        self.hand.cards.append(self.table.draw())
        if hand_value(self.hand.cards) > 21:
            self.hand.busted = True
        await self._finish(interaction, "💥 Bust!" if self.hand.busted else "✋ Doubled down")

    async def on_timeout(self):
        if self.done:
            return
        self.done = True
        self._disable_all()
        if self.message is not None:
            embeds, files = self.build_display(result_text="⌛ Timed out — standing")
            try:
                await self.message.edit(embeds=embeds, attachments=files, view=self)
            except discord.HTTPException:
                pass


async def settle_round(ctx, table: BlackjackTable, hands: list[BlackjackHand], dealer: list, dealer_natural: bool):
    currency = db.get_currency_name(table.guild_id)

    dealer_buf = cards_render.render_hand(dealer)
    embeds = [discord.Embed(title="🃏 Blackjack — Dealer", description=f"Value: {hand_value(dealer)}", color=discord.Color.gold())]
    embeds[0].set_image(url="attachment://dealer.png")
    files = [discord.File(dealer_buf, filename="dealer.png")]

    for i, hand in enumerate(hands):
        outcome = outcome_for(hand, dealer, dealer_natural)
        payout = int(hand.bet * OUTCOME_PAYOUT_MULTIPLIERS[outcome])
        if payout:
            balance = await asyncio.to_thread(db.update_balance, table.guild_id, hand.member.id, payout)
        else:
            balance = await asyncio.to_thread(db.get_balance, table.guild_id, hand.member.id)
        net = payout - hand.bet
        await asyncio.to_thread(db.log_bet, table.guild_id, hand.member.id, "blackjack", hand.bet, net)

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

    await ctx.send(embeds=embeds[:10], files=files)


async def play_round(ctx, table: BlackjackTable, seats: list[BlackjackSeat]):
    hands = [BlackjackHand(s.member, s.bet) for s in seats]
    busy_players.update(h.member.id for h in hands)
    try:
        for hand in hands:
            await asyncio.to_thread(db.update_balance, table.guild_id, hand.member.id, -hand.bet)  # escrow the bet

        dealer = [table.draw(), table.draw()]
        for hand in hands:
            hand.cards = [table.draw(), table.draw()]

        dealer_natural = is_blackjack(dealer)

        for hand in hands:
            if is_blackjack(hand.cards) or dealer_natural:
                continue  # natural blackjack, or the dealer already has one -- no turn to take
            view = BlackjackTurnView(table, hand, dealer)
            embeds, files = view.build_display()
            message = await ctx.send(content=f"{hand.member.mention} — your turn", embeds=embeds, files=files, view=view)
            view.message = message
            await view.wait()

        needs_dealer_play = not dealer_natural and any(
            not h.busted and not is_blackjack(h.cards) for h in hands
        )
        if needs_dealer_play:
            while hand_value(dealer) < 17:
                dealer.append(table.draw())

        await settle_round(ctx, table, hands, dealer, dealer_natural)
    finally:
        busy_players.difference_update(h.member.id for h in hands)


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
                await ctx.send("No funded players left to deal a round — table closed. Run `!blackjack <bet>` to start a new one.")
                break

            await play_round(ctx, table, playable)

            table.seats = [s for s in table.seats if not s.standing]
            await update_control_message(table)

            if not table.seats:
                await ctx.send("Table closed — everyone's stood up.")
                break

            await asyncio.sleep(BETWEEN_ROUNDS_SECONDS)
    finally:
        active_tables.pop(table.channel_id, None)
        if table.control_message is not None:
            try:
                await table.control_message.edit(view=None)
            except discord.HTTPException:
                pass


async def start_blackjack_table(ctx, bet: int):
    table = BlackjackTable(ctx.channel, ctx.channel.id, ctx.guild.id)
    table.seats.append(BlackjackSeat(ctx.author, bet))
    active_tables[ctx.channel.id] = table

    view = TableControlView(table)
    message = await ctx.send(
        content=f"🃏 {ctx.author.mention} opened a blackjack table! Dealing the first round in {JOIN_SECONDS}s.",
        embed=build_control_embed(table),
        view=view,
    )
    table.control_message = message

    await asyncio.sleep(JOIN_SECONDS)
    await run_table(ctx, table)
