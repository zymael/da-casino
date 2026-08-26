import asyncio

import discord

import achievements
import connect4
import connect4_render
import db
import hub_ui
from holdem_view import busy_players

CHALLENGE_TIMEOUT = 120  # accept/decline window, same as mancala_view's own
TURN_TIMEOUT = 300  # same leash as mancala_view -- no fixed combat tempo, but still finite so an
# AFK opponent can't stall the game forever (see Connect4BoardView.on_timeout)

# user_id -> whatever connect4-related thing currently has that player reserved, mirroring
# mancala_view.active_mancala.
active_connect4: dict[int, "Connect4Challenge | Connect4Session"] = {}


def _cleanup(entity) -> None:
    for uid in entity.all_user_ids():
        active_connect4.pop(uid, None)
        busy_players.discard(uid)


class Connect4Challenge:
    """Pending state between !connect4 and the target accepting/declining -- kept separate from
    Connect4Session so a real game (with its own board) never exists half-initialized during the
    accept window, same reasoning as mancala_view.MancalaChallenge/MancalaSession."""

    def __init__(
        self, guild_id: int, challenger_id: int, challenger_name: str, target_id: int, target_name: str, wager: int,
    ):
        self.guild_id = guild_id
        self.challenger_id = challenger_id
        self.challenger_name = challenger_name
        self.target_id = target_id
        self.target_name = target_name
        self.wager = wager
        self.message: discord.Message | None = None
        self.current_view: discord.ui.View | None = None

    def all_user_ids(self) -> list[int]:
        return [self.challenger_id, self.target_id]


class Connect4Session:
    def __init__(
        self, guild_id: int, challenger_id: int, challenger_name: str, opponent_id: int, opponent_name: str, wager: int,
    ):
        self.guild_id = guild_id
        self.challenger_id = challenger_id
        self.challenger_name = challenger_name
        self.opponent_id = opponent_id
        self.opponent_name = opponent_name
        self.wager = wager
        self.board = connect4.new_board()
        self.challenger_turn = True
        self.message: discord.Message | None = None
        self.current_view: discord.ui.View | None = None

    def all_user_ids(self) -> list[int]:
        return [self.challenger_id, self.opponent_id]

    def current_user_id(self) -> int:
        return self.challenger_id if self.challenger_turn else self.opponent_id

    def current_name(self) -> str:
        return self.challenger_name if self.challenger_turn else self.opponent_name


def build_connect4_challenge_embed(challenge: Connect4Challenge) -> discord.Embed:
    wager_line = f"\n💰 Wager: **{challenge.wager}** each" if challenge.wager else ""
    return discord.Embed(
        title="🔴🟡 Connect 4 Challenge",
        description=f"**{challenge.challenger_name}** has challenged **{challenge.target_name}** to Connect 4!{wager_line}",
        color=discord.Color.gold(),
    )


def _connect4_embed(session: Connect4Session, log_text: str, win_line=None) -> tuple[discord.Embed, discord.File]:
    embed = discord.Embed(title="🔴🟡 Connect 4", description=log_text, color=discord.Color.dark_blue())
    if session.wager:
        embed.add_field(name="💰 Wager", value=f"{session.wager} each ({session.wager * 2} to the winner)", inline=False)
    embed.add_field(name="Turn", value=f"➡️ **{session.current_name()}**", inline=False)
    buf = connect4_render.render_board(
        session.board, session.challenger_name, session.opponent_name, session.challenger_turn, win_line=win_line,
    )
    file = discord.File(buf, filename="connect4.png")
    embed.set_image(url="attachment://connect4.png")
    return embed, file


async def _send_connect4_update(
    interaction: discord.Interaction | None, session: Connect4Session, embed: discord.Embed,
    file: discord.File | None, view: discord.ui.View | None,
) -> None:
    """Connect4 sibling of mancala_view._send_mancala_update -- a move can come from a live
    interaction (the actor's own click) or from nothing at all (an AFK timeout ending the game)."""
    attachments = [file] if file else []
    if interaction is not None:
        await interaction.response.edit_message(embed=embed, attachments=attachments, view=view)
        return
    if session.message is None:
        return
    try:
        await session.message.edit(embed=embed, attachments=attachments, view=view)
    except discord.HTTPException:
        pass


async def _end_connect4(
    interaction: discord.Interaction | None, session: Connect4Session, winner_is_challenger: bool | None, win_line=None,
) -> None:
    """Ends the game, pays out the wager (if any), and logs both players' outcomes as bets
    (db.log_bet, game="connect4") -- same tiered win/loss achievement tracking every wagered game
    already gets via achievements.GAMES' "connect4" bucket. `winner_is_challenger=None` is a draw
    (board fills with no four-in-a-row) -- wagers refund and neither side earns a win/loss
    achievement kind, same as mancala_view._end_mancala's tie case."""
    currency = db.get_currency_name(session.guild_id)
    win_kinds: list[str] = []
    loss_kinds: list[str] = []
    winner_id = winner_name = loser_id = loser_name = None

    if winner_is_challenger is None:
        if session.wager:
            await asyncio.to_thread(db.update_balance, session.guild_id, session.challenger_id, session.wager)
            await asyncio.to_thread(db.update_balance, session.guild_id, session.opponent_id, session.wager)
        await asyncio.to_thread(db.log_bet, session.guild_id, session.challenger_id, "connect4", session.wager, 0)
        await asyncio.to_thread(db.log_bet, session.guild_id, session.opponent_id, "connect4", session.wager, 0)
        title = "🔴🟡 It's a draw!"
        refund_note = " Wagers refunded." if session.wager else ""
        description = f"The board fills up with no four in a row.{refund_note}"
    else:
        winner_id = session.challenger_id if winner_is_challenger else session.opponent_id
        winner_name = session.challenger_name if winner_is_challenger else session.opponent_name
        loser_id = session.opponent_id if winner_is_challenger else session.challenger_id
        loser_name = session.opponent_name if winner_is_challenger else session.challenger_name
        payout_line = ""
        if session.wager:
            pot = session.wager * 2
            await asyncio.to_thread(db.update_balance, session.guild_id, winner_id, pot)
            payout_line = f"\n\n💰 **{winner_name}** wins **{pot}** {currency}!"
        await asyncio.to_thread(db.log_bet, session.guild_id, winner_id, "connect4", session.wager, session.wager)
        await asyncio.to_thread(db.log_bet, session.guild_id, loser_id, "connect4", session.wager, -session.wager)
        # A finished game always has a winner/loser, wagered or not -- pass is_win explicitly so
        # win/loss counting and the win_connect4/tiered achievements fire even at net == 0 (the
        # default, wagerless case), same reasoning as mancala_view._end_mancala.
        win_kinds = achievements.kinds_for_bet("connect4", session.wager, is_win=True)
        win_kinds += await achievements.record_and_check(session.guild_id, winner_id, "connect4", session.wager, is_win=True)
        loss_kinds = achievements.kinds_for_bet("connect4", -session.wager, is_win=False)
        loss_kinds += await achievements.record_and_check(session.guild_id, loser_id, "connect4", -session.wager, is_win=False)
        title = f"🔴🟡 {winner_name} wins!"
        description = f"**{winner_name}** connects four in a row!{payout_line}"

    embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
    buf = connect4_render.render_board(
        session.board, session.challenger_name, session.opponent_name, session.challenger_turn, win_line=win_line,
    )
    file = discord.File(buf, filename="connect4.png")
    embed.set_image(url="attachment://connect4.png")
    _cleanup(session)
    await _send_connect4_update(interaction, session, embed, file, None)

    send = interaction.followup.send if interaction is not None else session.message.channel.send
    if win_kinds:
        await achievements.try_award_many(send, session.guild_id, winner_id, winner_name, win_kinds)
    if loss_kinds:
        await achievements.try_award_many(send, session.guild_id, loser_id, loser_name, loss_kinds)


class Connect4ColumnButton(discord.ui.Button):
    def __init__(self, col: int, full: bool, row: int):
        super().__init__(label=f"⬇️ {col + 1}", style=discord.ButtonStyle.secondary, disabled=full, row=row)
        self.col = col

    async def callback(self, interaction: discord.Interaction):
        session: Connect4Session = self.view.session
        actor_name = session.current_name()
        is_challenger = session.challenger_turn
        result = connect4.apply_move(session.board, self.col, is_challenger)

        if result.win_line:
            await _end_connect4(interaction, session, is_challenger, win_line=result.win_line)
            return
        if result.draw:
            await _end_connect4(interaction, session, None)
            return

        session.challenger_turn = not session.challenger_turn
        log_text = f"**{actor_name}** drops a disc into column **{self.col + 1}**."
        embed, file = _connect4_embed(session, log_text)
        view = Connect4BoardView(session)
        await _send_connect4_update(interaction, session, embed, file, view)


class Connect4BoardView(discord.ui.View):
    def __init__(self, session: Connect4Session):
        super().__init__(timeout=TURN_TIMEOUT)
        self.session = session
        legal = set(connect4.legal_moves(session.board))
        for col in range(connect4.COLS):
            self.add_item(Connect4ColumnButton(col, full=col not in legal, row=col // 4))
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.current_user_id():
            await interaction.response.send_message(f"It's {self.session.current_name()}'s turn.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return  # superseded -- the turn already resolved through some other path
        # No neutral "pass" -- a stalled turn just ends the game as a loss for whoever stalled,
        # same call as mancala_view.MancalaBoardView.on_timeout.
        await _end_connect4(None, session, winner_is_challenger=not session.challenger_turn)


async def start_connect4(
    guild_id: int, challenger_id: int, challenger_name: str, target_id: int, target_name: str, wager: int,
) -> Connect4Challenge:
    """Registers both players (active_connect4/busy_players, mirroring mancala_view.start_mancala)
    and returns the pending Connect4Challenge -- bot.py's !connect4 command builds the challenge
    embed + Connect4ChallengeView around this and sends it."""
    challenge = Connect4Challenge(guild_id, challenger_id, challenger_name, target_id, target_name, wager)
    for uid in challenge.all_user_ids():
        active_connect4[uid] = challenge
        busy_players.add(uid)
    return challenge


class Connect4ChallengeView(discord.ui.View):
    def __init__(self, challenge: Connect4Challenge):
        super().__init__(timeout=CHALLENGE_TIMEOUT)
        self.challenge = challenge
        challenge.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.challenge.target_id:
            await interaction.response.send_message("This challenge isn't addressed to you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        challenge = self.challenge
        if challenge.current_view is not self:
            return
        _cleanup(challenge)
        if challenge.message is None:
            return
        embed = discord.Embed(
            title="🔴🟡 Connect 4 Challenge Expired",
            description=f"**{challenge.target_name}** didn't respond in time.",
            color=discord.Color.dark_grey(),
        )
        try:
            await challenge.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = self.challenge
        guild_id = challenge.guild_id
        wager = challenge.wager

        if wager:
            currency = db.get_currency_name(guild_id)
            status, _ = await asyncio.to_thread(db.spend_currency, guild_id, challenge.challenger_id, wager)
            if status != "ok":
                _cleanup(challenge)
                embed = discord.Embed(
                    title="🔴🟡 Connect 4 Cancelled",
                    description=f"**{challenge.challenger_name}** can no longer afford the **{wager}** {currency} wager.",
                    color=discord.Color.dark_grey(),
                )
                await interaction.response.edit_message(embed=embed, view=None)
                self.stop()
                return
            status, _ = await asyncio.to_thread(db.spend_currency, guild_id, challenge.target_id, wager)
            if status != "ok":
                await asyncio.to_thread(db.update_balance, guild_id, challenge.challenger_id, wager)  # refund
                _cleanup(challenge)
                embed = discord.Embed(
                    title="🔴🟡 Connect 4 Cancelled",
                    description=f"**{challenge.target_name}** can't afford the **{wager}** {currency} wager.",
                    color=discord.Color.dark_grey(),
                )
                await interaction.response.edit_message(embed=embed, view=None)
                self.stop()
                return

        session = Connect4Session(
            guild_id, challenge.challenger_id, challenge.challenger_name,
            challenge.target_id, interaction.user.display_name, wager,
        )
        for uid in session.all_user_ids():
            active_connect4[uid] = session  # swap Connect4Challenge -> Connect4Session in place, ids stay reserved throughout

        embed, file = _connect4_embed(session, f"**{session.challenger_name}** and **{session.opponent_name}** drop in!")
        view = Connect4BoardView(session)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
        session.message = await interaction.original_response()
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = self.challenge
        _cleanup(challenge)
        embed = discord.Embed(
            title="🔴🟡 Connect 4 Declined", description=f"**{challenge.target_name}** declined the game.",
            color=discord.Color.dark_grey(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


class Connect4WagerModal(discord.ui.Modal):
    """Collects the (optional) wager once a target's already picked (Connect4TargetSelect) -- same
    two-step story as mancala_view.MancalaWagerModal."""

    def __init__(self, on_pick, target: discord.Member):
        super().__init__(title=f"Connect 4 vs {target.display_name}"[:45])  # Discord's modal title cap
        self.on_pick = on_pick
        self.target = target
        self.wager_input = discord.ui.TextInput(
            label="Wager (optional)", placeholder="e.g. 100 — leave blank for none", required=False,
        )
        self.add_item(self.wager_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.wager_input.value.strip()
        if not raw:
            wager = 0
        else:
            try:
                wager = int(raw)
            except ValueError:
                await interaction.response.send_message("Enter a whole number.", ephemeral=True)
                return
        await interaction.response.defer()
        await self.on_pick(hub_ui.InteractionContext(interaction), self.target, wager)


class Connect4TargetSelect(discord.ui.UserSelect):
    """Presented by !connect4's own response when called with no target -- same shape as
    mancala_view.MancalaTargetSelect."""

    def __init__(self, on_pick):
        super().__init__(placeholder="Choose who to play Connect 4 with...")
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(Connect4WagerModal(self.on_pick, self.values[0]))


def build_connect4_target_picker(on_pick) -> discord.ui.View:
    view = discord.ui.View(timeout=120)
    view.add_item(Connect4TargetSelect(on_pick))
    return view
