import asyncio

import discord

import achievements
import db
import hub_ui
import mancala
import mancala_render
from holdem_view import busy_players

CHALLENGE_TIMEOUT = 120  # accept/decline window, same as dungeon_view's own DUEL_CHALLENGE_TIMEOUT
TURN_TIMEOUT = 300  # a board game has no fixed combat tempo like a duel's 150s -- longer leash,
# but still finite so an AFK opponent can't stall the game forever (see MancalaBoardView.on_timeout)

# user_id -> whatever mancala-related thing currently has that player reserved, mirroring
# dungeon_view.active_delves -- a MancalaChallenge while the invite is pending, swapped in place
# for a MancalaSession the moment it's accepted (see accept_button below).
active_mancala: dict[int, "MancalaChallenge | MancalaSession"] = {}


def _cleanup(entity) -> None:
    for uid in entity.all_user_ids():
        active_mancala.pop(uid, None)
        busy_players.discard(uid)


class MancalaChallenge:
    """Pending state between !mancala and the target accepting/declining -- kept separate from
    MancalaSession so a real game (with its own board) never exists half-initialized during the
    accept window, same reasoning as dungeon_view.DuelChallenge/DuelSession."""

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


class MancalaSession:
    def __init__(
        self, guild_id: int, challenger_id: int, challenger_name: str, opponent_id: int, opponent_name: str, wager: int,
    ):
        self.guild_id = guild_id
        self.challenger_id = challenger_id
        self.challenger_name = challenger_name
        self.opponent_id = opponent_id
        self.opponent_name = opponent_name
        self.wager = wager
        self.board = mancala.new_board()
        self.challenger_turn = True
        self.message: discord.Message | None = None
        self.current_view: discord.ui.View | None = None

    def all_user_ids(self) -> list[int]:
        return [self.challenger_id, self.opponent_id]

    def current_user_id(self) -> int:
        return self.challenger_id if self.challenger_turn else self.opponent_id

    def current_name(self) -> str:
        return self.challenger_name if self.challenger_turn else self.opponent_name


def build_mancala_challenge_embed(challenge: MancalaChallenge) -> discord.Embed:
    wager_line = f"\n💰 Wager: **{challenge.wager}** each" if challenge.wager else ""
    return discord.Embed(
        title="🟤 Mancala Challenge",
        description=f"**{challenge.challenger_name}** has challenged **{challenge.target_name}** to a game of Mancala!{wager_line}",
        color=discord.Color.gold(),
    )


def _mancala_embed(session: MancalaSession, log_text: str) -> tuple[discord.Embed, discord.File]:
    embed = discord.Embed(title="🟤 Mancala", description=log_text, color=discord.Color.dark_gold())
    if session.wager:
        embed.add_field(name="💰 Wager", value=f"{session.wager} each ({session.wager * 2} to the winner)", inline=False)
    embed.add_field(name="Turn", value=f"➡️ **{session.current_name()}**", inline=False)
    buf = mancala_render.render_board(session.board, session.challenger_name, session.opponent_name, session.challenger_turn)
    file = discord.File(buf, filename="mancala.png")
    embed.set_image(url="attachment://mancala.png")
    return embed, file


async def _send_mancala_update(
    interaction: discord.Interaction | None, session: MancalaSession, embed: discord.Embed,
    file: discord.File | None, view: discord.ui.View | None,
) -> None:
    """Mancala sibling of dungeon_view._send_duel_update -- a move can come from a live
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


async def _end_mancala(interaction: discord.Interaction | None, session: MancalaSession, winner_is_challenger: bool | None) -> None:
    """Ends the game, pays out the wager (if any), and logs both players' outcomes as bets
    (db.log_bet, game="mancala") -- same tiered win/loss achievement tracking every wagered game
    already gets via achievements.GAMES' "mancala" bucket. `winner_is_challenger=None` is a tie
    (stores end up equal) -- wagers refund and neither side earns a win/loss achievement kind,
    same as a duel's own double-KO draw."""
    # A normal in-rules finish already swept both sides' remaining pits into their stores inside
    # apply_move; an AFK-timeout forfeit hasn't, so this call is a no-op there and the one that
    # actually settles the score here -- either way the final score always reflects every stone.
    mancala.sweep(session.board)
    currency = db.get_currency_name(session.guild_id)
    challenger_score = session.board[mancala.CHALLENGER_STORE]
    opponent_score = session.board[mancala.OPPONENT_STORE]
    score_line = f"🟤 Final score — **{session.challenger_name}** {challenger_score} : {opponent_score} **{session.opponent_name}**"
    win_kinds: list[str] = []
    loss_kinds: list[str] = []
    winner_id = winner_name = loser_id = loser_name = None

    if winner_is_challenger is None:
        if session.wager:
            await asyncio.to_thread(db.update_balance, session.guild_id, session.challenger_id, session.wager)
            await asyncio.to_thread(db.update_balance, session.guild_id, session.opponent_id, session.wager)
        await asyncio.to_thread(db.log_bet, session.guild_id, session.challenger_id, "mancala", session.wager, 0)
        await asyncio.to_thread(db.log_bet, session.guild_id, session.opponent_id, "mancala", session.wager, 0)
        title = "🟤 It's a tie!"
        refund_note = " Wagers refunded." if session.wager else ""
        description = f"{score_line}\n\nNeither side comes out ahead.{refund_note}"
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
        await asyncio.to_thread(db.log_bet, session.guild_id, winner_id, "mancala", session.wager, session.wager)
        await asyncio.to_thread(db.log_bet, session.guild_id, loser_id, "mancala", session.wager, -session.wager)
        # A finished game always has a winner/loser, wagered or not -- pass is_win explicitly so
        # win/loss counting and the win_mancala/tiered achievements fire even at net == 0 (the
        # default, wagerless case), same reasoning as dungeon_view._end_duel.
        win_kinds = achievements.kinds_for_bet("mancala", session.wager, is_win=True)
        win_kinds += await achievements.record_and_check(session.guild_id, winner_id, "mancala", session.wager, is_win=True)
        loss_kinds = achievements.kinds_for_bet("mancala", -session.wager, is_win=False)
        loss_kinds += await achievements.record_and_check(session.guild_id, loser_id, "mancala", -session.wager, is_win=False)
        title = f"🟤 {winner_name} wins!"
        description = f"{score_line}{payout_line}"

    embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
    buf = mancala_render.render_board(session.board, session.challenger_name, session.opponent_name, session.challenger_turn)
    file = discord.File(buf, filename="mancala.png")
    embed.set_image(url="attachment://mancala.png")
    _cleanup(session)
    await _send_mancala_update(interaction, session, embed, file, None)

    send = interaction.followup.send if interaction is not None else session.message.channel.send
    if win_kinds:
        await achievements.try_award_many(send, session.guild_id, winner_id, winner_name, win_kinds)
    if loss_kinds:
        await achievements.try_award_many(send, session.guild_id, loser_id, loser_name, loss_kinds)


class MancalaPitButton(discord.ui.Button):
    def __init__(self, pit: int, count: int, row: int):
        # Numbered 1-6 counting away from the clicking player's own store, from their own seated
        # perspective -- true for both sides despite the shared top-down board image rendering the
        # opponent's row mirrored (mancala_render.py's own layout comment explains why), the same
        # way two people at a real board read their own row without confusing each other.
        pit_number = pit % 7 + 1
        super().__init__(label=f"Pit {pit_number} ({count})", style=discord.ButtonStyle.secondary, disabled=(count == 0), row=row)
        self.pit = pit

    async def callback(self, interaction: discord.Interaction):
        session: MancalaSession = self.view.session
        actor_name = session.current_name()
        is_challenger = session.challenger_turn
        result = mancala.apply_move(session.board, self.pit, is_challenger)

        log_parts = [f"**{actor_name}** plays pit **{self.pit % 7 + 1}**."]
        if result.captured:
            log_parts.append(f"💥 Captures **{result.captured}** stones!")

        if result.game_over:
            await _end_mancala(interaction, session, mancala.winner(session.board))
            return

        if result.extra_turn:
            log_parts.append(f"🔁 **{actor_name}** goes again!")
        else:
            session.challenger_turn = not session.challenger_turn

        embed, file = _mancala_embed(session, " ".join(log_parts))
        view = MancalaBoardView(session)
        await _send_mancala_update(interaction, session, embed, file, view)


class MancalaBoardView(discord.ui.View):
    def __init__(self, session: MancalaSession):
        super().__init__(timeout=TURN_TIMEOUT)
        self.session = session
        for i, pit in enumerate(mancala.side_pits(session.challenger_turn)):
            self.add_item(MancalaPitButton(pit, session.board[pit], row=i // 3))
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
        # No neutral "pass" the way a stalled dungeon turn gets -- a player to move always has at
        # least one legal pit (the game already ends the instant either side is empty), so a
        # stalled turn just ends the game as a loss for whoever stalled.
        await _end_mancala(None, session, winner_is_challenger=not session.challenger_turn)


async def start_mancala(
    guild_id: int, challenger_id: int, challenger_name: str, target_id: int, target_name: str, wager: int,
) -> MancalaChallenge:
    """Registers both players (active_mancala/busy_players, mirroring dungeon_view.start_duel) and
    returns the pending MancalaChallenge -- bot.py's !mancala command builds the challenge embed +
    MancalaChallengeView around this and sends it."""
    challenge = MancalaChallenge(guild_id, challenger_id, challenger_name, target_id, target_name, wager)
    for uid in challenge.all_user_ids():
        active_mancala[uid] = challenge
        busy_players.add(uid)
    return challenge


class MancalaChallengeView(discord.ui.View):
    def __init__(self, challenge: MancalaChallenge):
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
            title="🟤 Mancala Challenge Expired",
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
                    title="🟤 Mancala Cancelled",
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
                    title="🟤 Mancala Cancelled",
                    description=f"**{challenge.target_name}** can't afford the **{wager}** {currency} wager.",
                    color=discord.Color.dark_grey(),
                )
                await interaction.response.edit_message(embed=embed, view=None)
                self.stop()
                return

        session = MancalaSession(
            guild_id, challenge.challenger_id, challenge.challenger_name,
            challenge.target_id, interaction.user.display_name, wager,
        )
        for uid in session.all_user_ids():
            active_mancala[uid] = session  # swap MancalaChallenge -> MancalaSession in place, ids stay reserved throughout

        embed, file = _mancala_embed(session, f"**{session.challenger_name}** and **{session.opponent_name}** take their seats!")
        view = MancalaBoardView(session)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
        session.message = await interaction.original_response()
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = self.challenge
        _cleanup(challenge)
        embed = discord.Embed(
            title="🟤 Mancala Declined", description=f"**{challenge.target_name}** declined the game.",
            color=discord.Color.dark_grey(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


class MancalaWagerModal(discord.ui.Modal):
    """Collects the (optional) wager once a target's already picked (MancalaTargetSelect) -- same
    two-step story as dungeon_view.DuelWagerModal, and for the same reason (a Select's callback
    can only hand back the one value it collected itself)."""

    def __init__(self, on_pick, target: discord.Member):
        super().__init__(title=f"Mancala vs {target.display_name}"[:45])  # Discord's modal title cap
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


class MancalaTargetSelect(discord.ui.UserSelect):
    """Presented by !mancala's own response when called with no target -- same shape as
    dungeon_view.DuelTargetSelect, and the same reasoning: the room button that invokes !mancala
    stays a plain zero-arg command wrapper, and !mancala's own response is what supplies the
    richer picker UI, not the room."""

    def __init__(self, on_pick):
        super().__init__(placeholder="Choose who to play Mancala with...")
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(MancalaWagerModal(self.on_pick, self.values[0]))


def build_mancala_target_picker(on_pick) -> discord.ui.View:
    view = discord.ui.View(timeout=120)
    view.add_item(MancalaTargetSelect(on_pick))
    return view
