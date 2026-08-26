import asyncio
import random
import traceback
from collections import Counter

import discord

import achievements
import db
import uno
import uno_render
from holdem_view import busy_players

LOBBY_TIMEOUT = 180  # 3 minutes to gather 2-4 players before the table auto-cancels and refunds
TURN_TIMEOUT = 300  # a stalled turn auto-resolves rather than freezing the table forever (see the
# on_timeout handlers below) -- no votekick needed since the auto-resolution itself is low-stakes
MAX_HAND_BUTTONS = 24  # leaves room for the Draw button within Discord's 25-components-per-view cap

COLOR_EMOJI = {"red": "🟥", "yellow": "🟨", "green": "🟩", "blue": "🟦", "wild": "⬛"}
KIND_LABEL = {"skip": "Skip", "reverse": "Reverse", "draw_two": "+2", "wild": "Wild", "wild_draw_four": "Wild +4"}
COLOR_STYLE = {
    "red": discord.ButtonStyle.danger, "green": discord.ButtonStyle.success,
    "blue": discord.ButtonStyle.primary, "yellow": discord.ButtonStyle.secondary, "wild": discord.ButtonStyle.secondary,
}

# channel_id -> UnoTable, mirroring holdem_view.py/blackjack_view.py's own per-game active_tables
active_tables: dict[int, "UnoTable"] = {}


def _card_label(card: "uno.Card") -> str:
    kind_text = card.kind if card.kind.isdigit() else KIND_LABEL[card.kind]
    return f"{COLOR_EMOJI[card.color]} {kind_text}"


class UnoSeat:
    def __init__(self, member: discord.Member, paid: int):
        self.member = member
        self.paid = paid  # escrowed buy-in, refunded if the table cancels before starting


class UnoTable:
    def __init__(self, channel: discord.abc.Messageable, channel_id: int, guild_id: int, host_id: int, buy_in: int):
        self.channel = channel
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.host_id = host_id
        self.buy_in = buy_in
        self.seats: list[UnoSeat] = []
        self.message: discord.Message | None = None
        self.game: uno.UnoGame | None = None
        self.started = False
        self.render_seq = 0  # bumped per public render so each attachment gets a fresh filename
        # -- Discord's CDN can cache an attachment by filename, so reusing "uno.png" on every edit
        # risks a client showing a stale image even though the message genuinely updated.
        self.current_turn_view: discord.ui.View | None = None  # whichever ephemeral view (hand,
        # color picker, drawn-card choice) is the LATEST one for the current turn -- a player can
        # click "Your Hand" more than once (e.g. reopening after losing track of the first one),
        # which creates a separate view with its own independent timeout timer each time; without
        # this, an earlier view's timer expiring would auto-resolve the turn out from under a
        # player actively deciding on a newer one. Same "stale view" guard shape as mancala_view.py
        # /connect4_view.py/icebreak_view.py's own session.current_view.
        self.turns_since_repost = 0  # once this reaches len(self.seats) -- a full lap of the
        # table -- _update_public_table deletes and reposts the table message instead of editing
        # it in place, so it doesn't get buried under a growing wall of other channel chat.

    def seat_for(self, user_id: int) -> UnoSeat | None:
        return next((s for s in self.seats if s.member.id == user_id), None)

    def all_user_ids(self) -> list[int]:
        return [s.member.id for s in self.seats]

    def pot(self) -> int:
        return self.buy_in * len(self.seats)


def _cleanup(table: UnoTable) -> None:
    active_tables.pop(table.channel_id, None)
    for uid in table.all_user_ids():
        busy_players.discard(uid)


async def _join_table(table: UnoTable, member: discord.Member) -> str:
    """Returns "ok" or a reason string the caller turns into a user-facing message."""
    if member.bot:
        return "bot"
    if table.seat_for(member.id) is not None:
        return "already_seated"
    if len(table.seats) >= uno.MAX_PLAYERS:
        return "full"
    if member.id in busy_players:
        return "busy"
    if table.buy_in:
        balance = await asyncio.to_thread(db.get_balance, table.guild_id, member.id)
        if balance < table.buy_in:
            return "cant_afford"
        status, _ = await asyncio.to_thread(db.spend_currency, table.guild_id, member.id, table.buy_in)
        if status != "ok":
            return "cant_afford"
    table.seats.append(UnoSeat(member, table.buy_in))
    busy_players.add(member.id)
    return "ok"


async def _leave_table(table: UnoTable, member: discord.Member) -> None:
    seat = table.seat_for(member.id)
    if seat is None:
        return
    if seat.paid:
        await asyncio.to_thread(db.update_balance, table.guild_id, member.id, seat.paid)
    busy_players.discard(member.id)
    table.seats.remove(seat)
    if member.id == table.host_id and table.seats:
        table.host_id = table.seats[0].member.id  # promote the next-earliest seat rather than getting stuck


def build_lobby_embed(table: UnoTable) -> discord.Embed:
    buyin_line = f"💰 Buy-in: **{table.buy_in}** each (pot: **{table.pot()}**)\n\n" if table.buy_in else ""
    seat_lines = "\n".join(f"• {s.member.display_name}" for s in table.seats) or "*(empty)*"
    host_name = next((s.member.display_name for s in table.seats if s.member.id == table.host_id), "?")
    embed = discord.Embed(
        title="🎴 UNO Table",
        description=f"{buyin_line}Seats ({len(table.seats)}/{uno.MAX_PLAYERS}):\n{seat_lines}",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Host: {host_name} — needs at least {uno.MIN_PLAYERS} players before Start unlocks.")
    return embed


class UnoLobbyView(discord.ui.View):
    def __init__(self, table: UnoTable):
        super().__init__(timeout=LOBBY_TIMEOUT)
        self.table = table

    async def on_timeout(self):
        table = self.table
        if table.channel_id not in active_tables or table.started:
            return  # already started or already torn down through some other path
        for uid in table.all_user_ids():
            member_seat = table.seat_for(uid)
            if member_seat and member_seat.paid:
                await asyncio.to_thread(db.update_balance, table.guild_id, uid, member_seat.paid)
        _cleanup(table)
        if table.message is None:
            return
        embed = discord.Embed(
            title="🎴 UNO Table Closed", description="Not enough players joined in time — buy-ins refunded.",
            color=discord.Color.dark_grey(),
        )
        try:
            await table.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        table = self.table
        if table.started:
            await interaction.response.send_message("This table already started.", ephemeral=True)
            return
        status = await _join_table(table, interaction.user)
        messages = {
            "bot": "Bots can't play UNO.",
            "already_seated": "You're already seated at this table.",
            "full": f"This table is full ({uno.MAX_PLAYERS} players max).",
            "busy": "Finish up whatever you're already doing first.",
            "cant_afford": f"You can't afford the **{table.buy_in}** buy-in.",
        }
        if status != "ok":
            await interaction.response.send_message(messages[status], ephemeral=True)
            return
        await interaction.response.edit_message(embed=build_lobby_embed(table), view=self)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        table = self.table
        if table.seat_for(interaction.user.id) is None:
            await interaction.response.send_message("You're not seated at this table.", ephemeral=True)
            return
        await _leave_table(table, interaction.user)
        if not table.seats:
            _cleanup(table)
            embed = discord.Embed(
                title="🎴 UNO Table Closed", description="Everyone left.", color=discord.Color.dark_grey(),
            )
            await interaction.response.edit_message(embed=embed, view=None)
            self.stop()
            return
        await interaction.response.edit_message(embed=build_lobby_embed(table), view=self)

    @discord.ui.button(label="Start Game", style=discord.ButtonStyle.primary)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        table = self.table
        if interaction.user.id != table.host_id:
            await interaction.response.send_message("Only the host can start the game.", ephemeral=True)
            return
        if len(table.seats) < uno.MIN_PLAYERS:
            await interaction.response.send_message(f"Need at least {uno.MIN_PLAYERS} players to start.", ephemeral=True)
            return
        table.started = True
        self.stop()
        game = uno.UnoGame([(s.member.id, s.member.display_name) for s in table.seats])
        uno.deal_initial_hands(game)
        table.game = game
        embed, file = build_table_display(table)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=UnoTableView(table))
        table.message = await interaction.original_response()


def build_table_display(table: UnoTable, log_text: str | None = None) -> tuple[discord.Embed, discord.File]:
    game = table.game
    description = log_text or f"**{game.current_seat().name}**'s turn — click 🎴 Your Hand below."
    embed = discord.Embed(title="🎴 UNO", description=description, color=discord.Color.teal())
    buf = uno_render.render_table(game, pot=table.pot())
    table.render_seq += 1
    filename = f"uno_{table.render_seq}.png"  # unique per render -- see UnoTable.render_seq's own comment
    file = discord.File(buf, filename=filename)
    embed.set_image(url=f"attachment://{filename}")
    return embed, file


async def _update_public_table(table: UnoTable, log_text: str) -> None:
    try:
        embed, file = build_table_display(table, log_text)
        table.turns_since_repost += 1
        if table.turns_since_repost >= len(table.seats):
            # A full lap of the table -- repost fresh at the bottom of the channel instead of
            # editing in place, so the table doesn't end up buried above a wall of chat.
            table.turns_since_repost = 0
            old_message = table.message
            table.message = await table.channel.send(embed=embed, file=file, view=UnoTableView(table))
            if old_message is not None:
                try:
                    await old_message.delete()
                except discord.HTTPException:
                    pass
        else:
            await table.message.edit(embed=embed, attachments=[file])
    except Exception:
        print(f"[uno] failed to update public table in channel {table.channel_id}:")
        traceback.print_exc()


async def _end_uno_round(table: UnoTable, winner_seat_idx: int) -> None:
    """Pays out the pot (if any) to the winner, logs every seat's outcome as a bet (db.log_bet,
    game="uno") for the usual tiered win/loss achievement tracking (achievements.GAMES' "uno"
    bucket), and closes the table. UNO always ends with exactly one seat at 0 cards -- no draw
    case, same shape as icebreak_view._end_icebreak."""
    game = table.game
    winner = game.seats[winner_seat_idx]
    currency = db.get_currency_name(table.guild_id)
    pot = table.pot()
    payout_line = ""
    if pot:
        await asyncio.to_thread(db.update_balance, table.guild_id, winner.user_id, pot)
        payout_line = f"\n\n💰 **{winner.name}** wins the **{pot}** {currency} pot!"
    await asyncio.to_thread(db.log_bet, table.guild_id, winner.user_id, "uno", table.buy_in, pot - table.buy_in)
    win_kinds = achievements.kinds_for_bet("uno", table.buy_in, is_win=True)
    win_kinds += await achievements.record_and_check(table.guild_id, winner.user_id, "uno", table.buy_in, is_win=True)

    loss_kinds_by_seat: dict[int, list[str]] = {}
    for seat in game.seats:
        if seat.user_id == winner.user_id:
            continue
        await asyncio.to_thread(db.log_bet, table.guild_id, seat.user_id, "uno", table.buy_in, -table.buy_in)
        lk = achievements.kinds_for_bet("uno", -table.buy_in, is_win=False)
        lk += await achievements.record_and_check(table.guild_id, seat.user_id, "uno", -table.buy_in, is_win=False)
        loss_kinds_by_seat[seat.user_id] = lk

    embed = discord.Embed(
        title=f"🎉 {winner.name} wins UNO!",
        description=f"**{winner.name}** empties their hand first!{payout_line}",
        color=discord.Color.gold(),
    )
    buf = uno_render.render_table(game, pot=0)
    table.render_seq += 1
    filename = f"uno_{table.render_seq}.png"
    file = discord.File(buf, filename=filename)
    embed.set_image(url=f"attachment://{filename}")
    _cleanup(table)
    try:
        await table.message.edit(embed=embed, attachments=[file], view=None)
    except discord.HTTPException:
        pass

    send = table.channel.send
    if win_kinds:
        await achievements.try_award_many(send, table.guild_id, winner.user_id, winner.name, win_kinds)
    for seat in game.seats:
        lk = loss_kinds_by_seat.get(seat.user_id)
        if lk:
            await achievements.try_award_many(send, table.guild_id, seat.user_id, seat.name, lk)


async def _resolve_play(table: UnoTable, seat_idx: int, card: "uno.Card", chosen_color: str | None, prefix: str = "") -> None:
    """Applies a play and broadcasts the result to the public table (or ends the round on a win)
    -- shared by the interactive path (_finalize_play) and the color-picker's on_timeout, which
    has no interaction to clean up but otherwise resolves exactly the same way."""
    game = table.game
    actor_name = game.seats[seat_idx].name
    result = uno.apply_play(game, seat_idx, card, chosen_color)
    if result.winner:
        await _end_uno_round(table, seat_idx)
        return
    log = f"{prefix}**{actor_name}** plays {_card_label(card)}."
    if result.announce_uno:
        log += f" 🔔 **{actor_name}** has UNO!"
    await _update_public_table(table, log)


async def _finalize_play(
    interaction: discord.Interaction, table: UnoTable, seat_idx: int, card: "uno.Card", chosen_color: str | None,
) -> None:
    try:
        await interaction.response.defer()
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass
    await _resolve_play(table, seat_idx, card, chosen_color)


def _default_color(hand: list["uno.Card"]) -> str:
    """A reasonable auto-pick for a stalled wild-color choice -- whichever color the player is
    holding the most of, falling back to random if their hand is nothing but wilds."""
    colors = [c.color for c in hand if c.color != "wild"]
    if not colors:
        return random.choice(uno.COLORS)
    return Counter(colors).most_common(1)[0][0]


class UnoColorButton(discord.ui.Button):
    def __init__(self, color: str):
        super().__init__(label=color.capitalize(), style=COLOR_STYLE[color], emoji=COLOR_EMOJI[color])
        self.color = color

    async def callback(self, interaction: discord.Interaction):
        view: "UnoColorPickerView" = self.view
        await _finalize_play(interaction, view.table, view.seat_idx, view.card, chosen_color=self.color)


class UnoColorPickerView(discord.ui.View):
    def __init__(self, table: UnoTable, seat_idx: int, card: "uno.Card"):
        super().__init__(timeout=TURN_TIMEOUT)
        self.table = table
        self.seat_idx = seat_idx
        self.card = card
        for color in uno.COLORS:
            self.add_item(UnoColorButton(color))
        table.current_turn_view = self

    async def on_timeout(self):
        table = self.table
        game = table.game
        # table.channel_id leaving active_tables is the authoritative "round already over" signal
        # (see _end_uno_round/_cleanup) -- current_index alone isn't enough, since a *winning*
        # play never advances it, so a stale timeout on the exact card that just won could
        # otherwise try to replay a card no longer in anyone's hand. current_turn_view catches the
        # other stale case: this exact seat reopened "Your Hand" (or redrew) since this view was
        # created, so a newer view is what they're actually looking at now.
        if (
            table.channel_id not in active_tables
            or self.seat_idx != game.current_index
            or table.current_turn_view is not self
        ):
            return
        color = _default_color(game.seats[self.seat_idx].hand)
        await _resolve_play(table, self.seat_idx, self.card, color, prefix="⌛ ")


class UnoDrawnCardChoiceView(discord.ui.View):
    def __init__(self, table: UnoTable, seat_idx: int, card: "uno.Card"):
        super().__init__(timeout=TURN_TIMEOUT)
        self.table = table
        self.seat_idx = seat_idx
        self.card = card
        table.current_turn_view = self

    @discord.ui.button(label="Play it", style=discord.ButtonStyle.success)
    async def play_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.card.is_wild():
            view = UnoColorPickerView(self.table, self.seat_idx, self.card)
            await interaction.response.edit_message(content="Choose a color:", embed=None, attachments=[], view=view)
            return
        await _finalize_play(interaction, self.table, self.seat_idx, self.card, chosen_color=None)

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary)
    async def keep_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        actor_name = self.table.game.seats[self.seat_idx].name
        uno.pass_turn(self.table.game)
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        await _update_public_table(self.table, f"**{actor_name}** draws a card and keeps it — turn passes.")

    async def on_timeout(self):
        table = self.table
        game = table.game
        if (
            table.channel_id not in active_tables
            or self.seat_idx != game.current_index
            or table.current_turn_view is not self
        ):
            return
        actor_name = game.seats[self.seat_idx].name
        uno.pass_turn(game)
        await _update_public_table(table, f"⌛ **{actor_name}** took too long — keeps the drawn card, turn passes.")


class UnoDrawButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🎴 Draw", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: "UnoHandView" = self.view
        table, seat_idx = view.table, view.seat_idx
        game = table.game
        if seat_idx != game.current_index:
            await interaction.response.send_message("It's not your turn anymore.", ephemeral=True)
            return
        seat = game.seats[seat_idx]
        top = game.top_card()
        if uno.legal_plays(seat.hand, top, game.current_color):
            await interaction.response.send_message("You have a legal card to play — you can't draw instead.", ephemeral=True)
            return
        actor_name = seat.name
        drawn = uno.draw_card(game, seat_idx)
        if uno.card_matches(drawn, top, game.current_color):
            buf = uno_render.render_hand([drawn])
            file = discord.File(buf, filename="drawn.png")
            embed = discord.Embed(title="🎴 You drew a card!", description="Play it now, or keep it for later?")
            embed.set_image(url="attachment://drawn.png")
            await interaction.response.edit_message(
                embed=embed, attachments=[file], view=UnoDrawnCardChoiceView(table, seat_idx, drawn),
            )
            return
        uno.pass_turn(game)
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        await _update_public_table(table, f"**{actor_name}** draws a card and can't play it — turn passes.")


class UnoCardButton(discord.ui.Button):
    def __init__(self, card: "uno.Card", playable: bool, row: int | None = None):
        super().__init__(label=_card_label(card), style=COLOR_STYLE[card.color], disabled=not playable, row=row)
        self.card = card

    async def callback(self, interaction: discord.Interaction):
        view: "UnoHandView" = self.view
        table, seat_idx = view.table, view.seat_idx
        game = table.game
        if seat_idx != game.current_index:
            await interaction.response.send_message("It's not your turn anymore.", ephemeral=True)
            return
        seat = game.seats[seat_idx]
        if self.card not in seat.hand:
            await interaction.response.send_message("You don't have that card anymore.", ephemeral=True)
            return
        top = game.top_card()
        if self.card not in uno.legal_plays(seat.hand, top, game.current_color):
            await interaction.response.send_message("That card isn't playable right now.", ephemeral=True)
            return
        if self.card.is_wild():
            view_ = UnoColorPickerView(table, seat_idx, self.card)
            await interaction.response.edit_message(content="Choose a color:", embed=None, attachments=[], view=view_)
            return
        await _finalize_play(interaction, table, seat_idx, self.card, chosen_color=None)


class UnoHandView(discord.ui.View):
    def __init__(self, table: UnoTable, seat_idx: int):
        super().__init__(timeout=TURN_TIMEOUT)
        self.table = table
        self.seat_idx = seat_idx
        game = table.game
        seat = game.seats[seat_idx]
        hand = uno.sorted_hand(seat.hand)
        top = game.top_card()
        legal = uno.legal_plays(seat.hand, top, game.current_color)
        # No explicit row -- auto-packs in insertion order, same reasoning as dungeon_view's
        # DuelAttackButton: up to MAX_HAND_BUTTONS cards always leaves the Draw button room in
        # Discord's 5-rows-of-5 layout.
        for card in hand[:MAX_HAND_BUTTONS]:
            self.add_item(UnoCardButton(card, playable=card in legal))
        self.add_item(UnoDrawButton())
        table.current_turn_view = self

    async def on_timeout(self):
        table = self.table
        game = table.game
        if (
            table.channel_id not in active_tables
            or self.seat_idx != game.current_index
            or table.current_turn_view is not self
        ):
            return
        actor_name = game.seats[self.seat_idx].name
        uno.draw_card(game, self.seat_idx)
        uno.pass_turn(game)
        await _update_public_table(table, f"⌛ **{actor_name}** took too long — auto-draws a card and passes.")


def build_hand_embed(table: UnoTable, seat_idx: int) -> tuple[discord.Embed, discord.File]:
    game = table.game
    hand = uno.sorted_hand(game.seats[seat_idx].hand)
    top = game.top_card()
    buf = uno_render.render_hand(hand)
    file = discord.File(buf, filename="hand.png")
    embed = discord.Embed(
        title="🎴 Your Hand",
        description=f"Top card: {_card_label(top)} — color in play: **{game.current_color}**",
        color=discord.Color.dark_teal(),
    )
    embed.set_image(url="attachment://hand.png")
    return embed, file


class UnoTableView(discord.ui.View):
    """The only view attached to the live public table message -- persistent (timeout=None) for
    the whole round. Unlike Hold'em's per-turn ActionView churn, one button suffices for every
    seat and every turn: it looks up who's asking and whether it's their turn fresh on every
    click, same lookup-by-interaction.user.id shape as holdem_view.RevealHandView, but the
    response here is the actual turn UI, not a read-only reveal."""

    def __init__(self, table: UnoTable):
        super().__init__(timeout=None)
        self.table = table

    @discord.ui.button(label="🎴 Your Hand", style=discord.ButtonStyle.primary)
    async def your_hand(self, interaction: discord.Interaction, button: discord.ui.Button):
        table = self.table
        game = table.game
        seat_idx = next((i for i, s in enumerate(game.seats) if s.user_id == interaction.user.id), None)
        if seat_idx is None:
            await interaction.response.send_message("You're not seated at this table.", ephemeral=True)
            return
        if seat_idx != game.current_index:
            await interaction.response.send_message(f"It's {game.current_seat().name}'s turn.", ephemeral=True)
            return
        embed, file = build_hand_embed(table, seat_idx)
        await interaction.response.send_message(
            embed=embed, file=file, view=UnoHandView(table, seat_idx), ephemeral=True,
        )


async def start_uno_table(ctx, buy_in: int) -> None:
    """Opens a new UNO table lobby in ctx.channel, auto-seating the host, and posts the
    persistent UnoLobbyView (Join/Leave/Start) -- bot.py's !uno command calls this after its own
    busy/buy-in/one-table-per-channel gates pass."""
    table = UnoTable(ctx.channel, ctx.channel.id, ctx.guild.id, ctx.author.id, buy_in)
    active_tables[ctx.channel.id] = table
    status = await _join_table(table, ctx.author)
    if status != "ok":
        active_tables.pop(ctx.channel.id, None)
        currency = db.get_currency_name(ctx.guild.id)
        await ctx.send(f"You can't afford the **{buy_in}** {currency} buy-in.")
        return
    view = UnoLobbyView(table)
    table.message = await ctx.send(embed=build_lobby_embed(table), view=view)
