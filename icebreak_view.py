import asyncio

import discord

import achievements
import db
import hub_ui
import icebreak
import icebreak_render
from holdem_view import busy_players

CHALLENGE_TIMEOUT = 120  # accept/decline window, same as mancala_view/connect4_view's own
TURN_TIMEOUT = 300  # same leash as mancala_view/connect4_view (see IceBreakBoardView.on_timeout)

# user_id -> whatever icebreak-related thing currently has that player reserved, mirroring
# mancala_view.active_mancala / connect4_view.active_connect4.
active_icebreak: dict[int, "IceBreakChallenge | IceBreakSession"] = {}

COL_LETTERS = icebreak_render.COL_LETTERS


def _cleanup(entity) -> None:
    for uid in entity.all_user_ids():
        active_icebreak.pop(uid, None)
        busy_players.discard(uid)


class IceBreakChallenge:
    """Pending state between !icebreak and the target accepting/declining -- kept separate from
    IceBreakSession so a real game (with its own board) never exists half-initialized during the
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


class IceBreakSession:
    def __init__(
        self, guild_id: int, challenger_id: int, challenger_name: str, opponent_id: int, opponent_name: str, wager: int,
    ):
        self.guild_id = guild_id
        self.challenger_id = challenger_id
        self.challenger_name = challenger_name
        self.opponent_id = opponent_id
        self.opponent_name = opponent_name
        self.wager = wager
        self.board = icebreak.new_board()
        self.challenger_turn = True
        self.message: discord.Message | None = None
        self.current_view: discord.ui.View | None = None

    def all_user_ids(self) -> list[int]:
        return [self.challenger_id, self.opponent_id]

    def current_user_id(self) -> int:
        return self.challenger_id if self.challenger_turn else self.opponent_id

    def current_name(self) -> str:
        return self.challenger_name if self.challenger_turn else self.opponent_name


def build_icebreak_challenge_embed(challenge: IceBreakChallenge) -> discord.Embed:
    wager_line = f"\n💰 Wager: **{challenge.wager}** each" if challenge.wager else ""
    return discord.Embed(
        title="🧊 Don't Break the Ice Challenge",
        description=f"**{challenge.challenger_name}** has challenged **{challenge.target_name}** to Don't Break the Ice!{wager_line}",
        color=discord.Color.gold(),
    )


def _icebreak_embed(session: IceBreakSession, log_text: str) -> tuple[discord.Embed, discord.File]:
    embed = discord.Embed(title="🧊 Don't Break the Ice", description=log_text, color=discord.Color.teal())
    if session.wager:
        embed.add_field(name="💰 Wager", value=f"{session.wager} each ({session.wager * 2} to the winner)", inline=False)
    embed.add_field(name="Turn", value=f"➡️ **{session.current_name()}**", inline=False)
    buf = icebreak_render.render_board(
        session.board, session.challenger_name, session.opponent_name, session.challenger_turn,
    )
    file = discord.File(buf, filename="icebreak.png")
    embed.set_image(url="attachment://icebreak.png")
    return embed, file


async def _send_icebreak_update(
    interaction: discord.Interaction | None, session: IceBreakSession, embed: discord.Embed,
    file: discord.File | None, view: discord.ui.View | None,
) -> None:
    """IceBreak sibling of mancala_view._send_mancala_update / connect4_view._send_connect4_update
    -- a move can come from a live interaction (the actor's own click) or from nothing at all (an
    AFK timeout ending the game)."""
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


async def _end_icebreak(
    interaction: discord.Interaction | None, session: IceBreakSession, loser_is_challenger: bool,
) -> None:
    """Ends the game, pays out the wager (if any), and logs both players' outcomes as bets
    (db.log_bet, game="icebreak") -- same tiered win/loss achievement tracking every wagered game
    already gets via achievements.GAMES' "icebreak" bucket. Unlike Mancala/Connect4 there's no
    draw case here -- the sheet always ends up either wholly intact or fractured by exactly one
    player's move, so a winner and a loser always exist by the time this is called."""
    currency = db.get_currency_name(session.guild_id)
    loser_id = session.challenger_id if loser_is_challenger else session.opponent_id
    loser_name = session.challenger_name if loser_is_challenger else session.opponent_name
    winner_id = session.opponent_id if loser_is_challenger else session.challenger_id
    winner_name = session.opponent_name if loser_is_challenger else session.challenger_name

    payout_line = ""
    if session.wager:
        pot = session.wager * 2
        await asyncio.to_thread(db.update_balance, session.guild_id, winner_id, pot)
        payout_line = f"\n\n💰 **{winner_name}** wins **{pot}** {currency}!"
    await asyncio.to_thread(db.log_bet, session.guild_id, winner_id, "icebreak", session.wager, session.wager)
    await asyncio.to_thread(db.log_bet, session.guild_id, loser_id, "icebreak", session.wager, -session.wager)
    win_kinds = achievements.kinds_for_bet("icebreak", session.wager, is_win=True)
    win_kinds += await achievements.record_and_check(session.guild_id, winner_id, "icebreak", session.wager, is_win=True)
    loss_kinds = achievements.kinds_for_bet("icebreak", -session.wager, is_win=False)
    loss_kinds += await achievements.record_and_check(session.guild_id, loser_id, "icebreak", -session.wager, is_win=False)

    title = f"🧊 {winner_name} wins!"
    description = f"**{loser_name}** breaks the ice and falls through!{payout_line}"
    embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
    buf = icebreak_render.render_board(
        session.board, session.challenger_name, session.opponent_name, session.challenger_turn, game_over=True,
    )
    file = discord.File(buf, filename="icebreak.png")
    embed.set_image(url="attachment://icebreak.png")
    _cleanup(session)
    await _send_icebreak_update(interaction, session, embed, file, None)

    send = interaction.followup.send if interaction is not None else session.message.channel.send
    if win_kinds:
        await achievements.try_award_many(send, session.guild_id, winner_id, winner_name, win_kinds)
    if loss_kinds:
        await achievements.try_award_many(send, session.guild_id, loser_id, loser_name, loss_kinds)


class IceCellButton(discord.ui.Button):
    def __init__(self, row: int, col: int, broken: bool):
        label = f"{COL_LETTERS[row]}{col + 1}"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=broken, row=row)
        self.cell_row = row
        self.cell_col = col

    async def callback(self, interaction: discord.Interaction):
        session: IceBreakSession = self.view.session
        actor_name = session.current_name()
        is_challenger = session.challenger_turn
        result = icebreak.apply_move(session.board, self.cell_row, self.cell_col)

        if result.collapsed:
            await _end_icebreak(interaction, session, loser_is_challenger=is_challenger)
            return

        session.challenger_turn = not session.challenger_turn
        extra_falls = len(result.fallen) - 1
        cascade_note = f" {extra_falls} more cube{'s' if extra_falls != 1 else ''} lose their footing and go under!" if extra_falls else ""
        log_text = f"**{actor_name}** breaks **{self.label}**.{cascade_note} The ice holds... for now."
        embed, file = _icebreak_embed(session, log_text)
        view = IceBreakBoardView(session)
        await _send_icebreak_update(interaction, session, embed, file, view)


class IceBreakBoardView(discord.ui.View):
    def __init__(self, session: IceBreakSession):
        super().__init__(timeout=TURN_TIMEOUT)
        self.session = session
        for row in range(icebreak.ROWS):
            for col in range(icebreak.COLS):
                self.add_item(IceCellButton(row, col, broken=session.board[row][col] == 0))
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
        # same call as mancala_view/connect4_view's own on_timeout.
        await _end_icebreak(None, session, loser_is_challenger=session.challenger_turn)


async def start_icebreak(
    guild_id: int, challenger_id: int, challenger_name: str, target_id: int, target_name: str, wager: int,
) -> IceBreakChallenge:
    """Registers both players (active_icebreak/busy_players, mirroring mancala_view.start_mancala)
    and returns the pending IceBreakChallenge -- bot.py's !icebreak command builds the challenge
    embed + IceBreakChallengeView around this and sends it."""
    challenge = IceBreakChallenge(guild_id, challenger_id, challenger_name, target_id, target_name, wager)
    for uid in challenge.all_user_ids():
        active_icebreak[uid] = challenge
        busy_players.add(uid)
    return challenge


class IceBreakChallengeView(discord.ui.View):
    def __init__(self, challenge: IceBreakChallenge):
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
            title="🧊 Challenge Expired",
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
                    title="🧊 Challenge Cancelled",
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
                    title="🧊 Challenge Cancelled",
                    description=f"**{challenge.target_name}** can't afford the **{wager}** {currency} wager.",
                    color=discord.Color.dark_grey(),
                )
                await interaction.response.edit_message(embed=embed, view=None)
                self.stop()
                return

        session = IceBreakSession(
            guild_id, challenge.challenger_id, challenge.challenger_name,
            challenge.target_id, interaction.user.display_name, wager,
        )
        for uid in session.all_user_ids():
            active_icebreak[uid] = session  # swap IceBreakChallenge -> IceBreakSession in place, ids stay reserved throughout

        embed, file = _icebreak_embed(session, f"**{session.challenger_name}** and **{session.opponent_name}** grab their hammers!")
        view = IceBreakBoardView(session)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
        session.message = await interaction.original_response()
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = self.challenge
        _cleanup(challenge)
        embed = discord.Embed(
            title="🧊 Challenge Declined", description=f"**{challenge.target_name}** declined the game.",
            color=discord.Color.dark_grey(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


class IceBreakWagerModal(discord.ui.Modal):
    """Collects the (optional) wager once a target's already picked (IceBreakTargetSelect) -- same
    two-step story as mancala_view.MancalaWagerModal/connect4_view.Connect4WagerModal."""

    def __init__(self, on_pick, target: discord.Member):
        super().__init__(title=f"Ice Break vs {target.display_name}"[:45])  # Discord's modal title cap
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


class IceBreakTargetSelect(discord.ui.UserSelect):
    """Presented by !icebreak's own response when called with no target -- same shape as
    mancala_view.MancalaTargetSelect/connect4_view.Connect4TargetSelect."""

    def __init__(self, on_pick):
        super().__init__(placeholder="Choose who to play Don't Break the Ice with...")
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(IceBreakWagerModal(self.on_pick, self.values[0]))


def build_icebreak_target_picker(on_pick) -> discord.ui.View:
    view = discord.ui.View(timeout=120)
    view.add_item(IceBreakTargetSelect(on_pick))
    return view
