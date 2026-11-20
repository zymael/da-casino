import asyncio
import random
import time

import discord

import cards_render
import db
import npcs
import poker
import poker_ai
import quests
from game import Deck

JOIN_SECONDS = 45
ACTION_SECONDS = 45
BETWEEN_HANDS_SECONDS = 5
SMALL_BLIND = 5
BIG_BLIND = 10
MAX_SEATS = 9
# NPC bots (npcs.json poker_bot) play from a house-funded stack: each hand, as deep as the deepest
# human at the table, but never more than this.
BOT_STACK_CAP = 5000
BOT_THINK_SECONDS = 1.5
DEFAULT_CHATTER = 0.6
# Random bots seated automatically when a table opens (fewer if not enough are available).
STARTING_BOTS = 4

# channel_id -> HoldemTable, so only one table can be running per channel
active_tables: dict[int, "HoldemTable"] = {}

# user_ids currently dealt into an in-progress hand — locked out of other
# balance-affecting commands until the hand resolves, so their tracked stack can't
# desync from their real balance.
busy_players: set[int] = set()


class NpcMember:
    """Stands in for a discord.Member at the table for an NPC bot. Its id is negative, so it can
    never collide with a real user, and every db write is skipped for it (`is_npc`)."""

    is_npc = True

    def __init__(self, npc_id: str, bot_id: int):
        self.npc_id = npc_id
        self.id = bot_id

    @property
    def npc(self) -> dict:
        return npcs.NPCS[self.npc_id]

    @property
    def display_name(self) -> str:
        return self.npc["name"]

    @property
    def mention(self) -> str:
        return f"**{self.display_name}**"


def isolate(name: str) -> str:
    """Wraps a display name in Unicode first-strong-isolate marks, so a right-to-left name (Arabic,
    Persian, Hebrew) can't flip the order of the stack numbers and badges around it."""
    return f"⁨{name}⁩"


def is_npc(member) -> bool:
    return getattr(member, "is_npc", False)


def npc_line(npc: dict, kind: str, always: bool = False) -> str | None:
    """A random one of this NPC's lines for `kind`, or None (no lines, or it stayed quiet)."""
    lines = npcs.poker_lines(npc, kind)
    if not lines or (not always and random.random() >= npc.get("poker_chatter", DEFAULT_CHATTER)):
        return None
    return random.choice(lines)


class Seat:
    def __init__(self, member, buy_in: int | None, seat_no: int = 0):
        self.member = member
        self.buy_in = buy_in  # None = bring full balance to every hand (ignored for bots)
        self.standing = False  # leave once the current/next hand wraps up
        # Ever-increasing per table, so the dealer button can move "one seat left" past players
        # who've since left (see next_hand_order).
        self.seat_no = seat_no


class HoldemTable:
    def __init__(self, channel: discord.abc.Messageable, channel_id: int, guild_id: int, host_id: int):
        self.channel = channel
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.host_id = host_id  # whose state an NPC's poker_trigger is checked against
        self.seats: list[Seat] = []
        self.control_message: discord.Message | None = None
        self.control_view: "TableControlView | None" = None
        self.current_game: "HoldemGame | None" = None
        self.current_players_by_id: dict[int, "Player"] = {}
        self.current_action_view: "ActionView | None" = None
        self._next_bot_id = -1
        self.closed = False
        self.started = False  # first hand dealt; random starting bots are picked at this moment
        self.pending_bots = 0  # mystery bots still to be picked at the deal
        self._next_seat_no = 0
        self.last_dealer_seat_no: int | None = None

    def add_seat(self, member, buy_in: int | None) -> Seat:
        seat = Seat(member, buy_in, self._next_seat_no)
        self._next_seat_no += 1
        self.seats.append(seat)
        return seat

    def seat_for(self, user_id: int) -> Seat | None:
        return next((s for s in self.seats if s.member.id == user_id), None)

    def bot_seat_for(self, npc_id: str) -> Seat | None:
        return next((s for s in self.seats if is_npc(s.member) and s.member.npc_id == npc_id), None)

    def new_bot_member(self, npc_id: str) -> NpcMember:
        member = NpcMember(npc_id, self._next_bot_id)
        self._next_bot_id -= 1
        return member

    def seated_humans(self) -> list[Seat]:
        return [s for s in self.seats if not s.standing and not is_npc(s.member)]

    def seated(self) -> list[Seat]:
        return [s for s in self.seats if not s.standing]


async def bot_available(table: HoldemTable, npc: dict) -> bool:
    trigger = npc.get("poker_trigger")
    return trigger is None or await quests.trigger_satisfied(table.guild_id, table.host_id, trigger)


class Player:
    def __init__(self, member, starting_stack: int):
        self.member = member
        self.hole: list = []
        self.folded = False
        self.all_in = False
        self.contributed = 0
        self.street_contributed = 0
        self.remaining = starting_stack
        self.acted = False
        self.raises_this_street = 0


class HoldemGame:
    def __init__(self, players: list[Player], guild_id: int):
        self.players = players
        self.guild_id = guild_id
        self.deck = Deck()
        self.community: list = []
        self.current_bet = 0

    def active(self) -> list[Player]:
        return [p for p in self.players if not p.folded]


def blind_seats(n: int) -> tuple[int, int, int]:
    """Returns (dealer_idx, small_blind_idx, big_blind_idx). Heads-up is special-cased:
    the dealer posts the small blind and acts first preflop, last postflop."""
    if n == 2:
        return 0, 0, 1
    return 0, 1 % n, 2 % n


def _order_from(start: int, players: list[Player]) -> list[Player]:
    n = len(players)
    return [players[(start + i) % n] for i in range(n)]


def preflop_order(players: list[Player]) -> list[Player]:
    n = len(players)
    dealer, _sb, bb = blind_seats(n)
    start = dealer if n == 2 else (bb + 1) % n
    return _order_from(start, players)


def postflop_order(players: list[Player]) -> list[Player]:
    n = len(players)
    dealer, sb, bb = blind_seats(n)
    start = bb if n == 2 else sb
    return _order_from(start, players)


async def _commit(game: HoldemGame, player: Player, amount: int):
    """Escrows `amount` from the player's balance into the pot."""
    if amount <= 0:
        return
    if not is_npc(player.member):
        await asyncio.to_thread(db.update_balance, game.guild_id, player.member.id, -amount)
    player.remaining -= amount
    player.contributed += amount
    player.street_contributed += amount
    if player.remaining <= 0:
        player.all_in = True


async def apply_action(game: HoldemGame, player: Player, action: tuple):
    kind = action[0]
    if kind == "fold":
        player.folded = True
    elif kind == "check":
        pass
    elif kind == "call":
        to_call = game.current_bet - player.street_contributed
        await _commit(game, player, min(to_call, player.remaining))
    elif kind == "raise":
        raise_to = action[1]
        await _commit(game, player, raise_to - player.street_contributed)
        game.current_bet = player.street_contributed
        player.raises_this_street += 1
        for p in game.active():
            if p is not player:
                p.acted = False
    elif kind == "allin":
        await _commit(game, player, player.remaining)
        if player.street_contributed > game.current_bet:
            game.current_bet = player.street_contributed
            player.raises_this_street += 1
            for p in game.active():
                if p is not player:
                    p.acted = False
    player.acted = True


class RaiseModal(discord.ui.Modal):
    def __init__(self, action_view: "ActionView"):
        super().__init__(title="Raise")
        self.action_view = action_view
        min_raise = action_view.game.current_bet + 1
        max_raise = action_view.player.street_contributed + action_view.player.remaining
        self.amount_input = discord.ui.TextInput(
            label=f"Raise to (total bet), {min_raise}-{max_raise}",
            placeholder=str(min_raise),
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message("Enter a whole number.", ephemeral=True)
            return

        player = self.action_view.player
        game = self.action_view.game
        max_raise = player.street_contributed + player.remaining
        if amount <= game.current_bet:
            await interaction.response.send_message(
                f"Raise must be more than the current bet ({game.current_bet}).", ephemeral=True
            )
            return
        if amount > max_raise:
            await interaction.response.send_message(
                f"You can only raise up to {max_raise} — use All-In for that.", ephemeral=True
            )
            return

        self.action_view.result = ("raise", amount)
        await interaction.response.defer()
        self.action_view.stop()


class ActionView(discord.ui.View):
    def __init__(self, table: HoldemTable, game: HoldemGame, player: Player, to_call: int):
        super().__init__(timeout=ACTION_SECONDS)
        self.table = table
        self.game = game
        self.player = player
        self.to_call = to_call
        self.result: tuple | None = None
        self.call_button.label = "Check" if to_call == 0 else f"Call {to_call}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (interaction.data or {}).get("custom_id") == HAND_BUTTON_ID:
            return True  # anyone in the hand may peek at their own cards from the turn prompt
        if interaction.user.id != self.player.member.id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Fold", style=discord.ButtonStyle.danger)
    async def fold_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = ("fold",)
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Check", style=discord.ButtonStyle.secondary)
    async def call_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = ("check",) if self.to_call == 0 else ("call",)
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Raise", style=discord.ButtonStyle.primary)
    async def raise_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RaiseModal(self))

    @discord.ui.button(label="All-In", style=discord.ButtonStyle.danger)
    async def allin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = ("allin",)
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        seat = self.table.seat_for(self.player.member.id)
        if seat is not None:
            seat.standing = True
        self.result = ("fold",)
        await interaction.response.defer()
        self.stop()
        await update_control_message(self.table)

    @discord.ui.button(label="👀 My Hand", style=discord.ButtonStyle.secondary, row=1, custom_id="holdem_hand")
    async def hand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_hand(interaction, self.table)


HAND_BUTTON_ID = "holdem_hand"


async def send_hand(interaction: discord.Interaction, table: HoldemTable):
    """Privately shows whoever clicked their hole cards (and the board so far, when there is one)."""
    player = table.current_players_by_id.get(interaction.user.id)
    if player is None:
        await interaction.response.send_message("You're not in this hand.", ephemeral=True)
        return
    buf = cards_render.render_hand(player.hole)
    file = discord.File(buf, filename="hole.png")
    embed = discord.Embed(title="🂠 Your hand", color=discord.Color.blurple())
    embed.set_image(url="attachment://hole.png")
    game = table.current_game
    if game is not None and game.community:
        score, _best = poker.best_hand(player.hole + game.community)
        embed.description = f"Board: {' '.join(str(c) for c in game.community)}\nYou have: **{poker.describe_score(score)}**"
    await interaction.response.send_message(embed=embed, file=file, ephemeral=True)


class RevealHandView(discord.ui.View):
    """Sits on the deal message and every street message, so your cards are always a click away
    however far bot chatter has scrolled the deal message up."""

    def __init__(self, table: HoldemTable):
        super().__init__(timeout=None)
        self.table = table

    @discord.ui.button(label="👀 Show My Hand", style=discord.ButtonStyle.secondary)
    async def reveal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_hand(interaction, self.table)


def build_control_embed(table: HoldemTable) -> discord.Embed:
    embed = discord.Embed(title="🃏 Texas Hold'em Table", color=discord.Color.dark_green())
    if not table.seats:
        embed.description = "No one's seated yet."
    else:
        lines = []
        for s in table.seats:
            if is_npc(s.member):
                buyin_str = "🤖 bot, house stack"
            else:
                buyin_str = f"buy-in {s.buy_in}" if s.buy_in is not None else "full balance"
            tag = " — *standing up after this hand*" if s.standing else ""
            lines.append(f"**{isolate(s.member.display_name)}** — {buyin_str}{tag}")
        if table.pending_bots and not table.started:
            lines.append(f"🤖 **+{table.pending_bots} mystery bots** — revealed on the deal")
        embed.description = "\n".join(lines)
    embed.set_footer(text="Buy In to sit down or update your buy-in. Stand to leave whenever you like.")
    return embed


class BuyInModal(discord.ui.Modal):
    def __init__(self, table: HoldemTable):
        super().__init__(title="Buy In")
        self.table = table
        self.amount_input = discord.ui.TextInput(
            label="Buy-in amount (blank = full balance)",
            required=False,
            placeholder="e.g. 200",
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount_input.value.strip()
        buy_in = None
        if raw:
            try:
                buy_in = int(raw)
            except ValueError:
                await interaction.response.send_message("Enter a whole number, or leave it blank.", ephemeral=True)
                return
            if buy_in <= 0:
                await interaction.response.send_message("Buy-in must be positive.", ephemeral=True)
                return

        seat = self.table.seat_for(interaction.user.id)
        if seat is not None:
            seat.buy_in = buy_in
            seat.standing = False
            await interaction.response.send_message("Buy-in updated for your next hand.", ephemeral=True)
        else:
            if len(self.table.seated()) >= MAX_SEATS:
                await interaction.response.send_message(f"The table's full ({MAX_SEATS} seats).", ephemeral=True)
                return
            self.table.add_seat(interaction.user, buy_in)
            await interaction.response.send_message("You're seated! You'll be dealt into the next hand.", ephemeral=True)

        await update_control_message(self.table)


def sit_text(npc: dict) -> str:
    text = f"🤖 **{npc['name']}** sits down at the table."
    line = npc_line(npc, "sit", always=True)
    if line:
        text += f"\n> {line}"
    return text


async def seat_random_bots(table: HoldemTable, count: int) -> list[dict]:
    """Seats up to `count` random bots from whoever's available (poker_trigger) and not already
    seated, capped by free seats. Players never pick which bots they get: choosing the opponent's
    AI would make it trivial to farm the loosest personality."""
    pool = [
        npc for npc in npcs.poker_bots()
        if table.bot_seat_for(npc["id"]) is None and await bot_available(table, npc)
    ]
    count = max(0, min(count, len(pool), MAX_SEATS - len(table.seated())))
    chosen = random.sample(pool, count)
    for npc in chosen:
        table.add_seat(table.new_bot_member(npc["id"]), None)
    return chosen


class NoBotsButton(discord.ui.Button):
    """Lets the host send the random starting bots home, before the first deal only. One-way on
    purpose: a toggle back on would let the host retry for a line-up. The bots haven't been picked
    yet at this point (see start_holdem_table), so this just cancels them."""

    def __init__(self, table: HoldemTable):
        super().__init__(label="No Bots", emoji="🚫", style=discord.ButtonStyle.secondary)
        self.table = table

    async def callback(self, interaction: discord.Interaction):
        table = self.table
        if interaction.user.id != table.host_id:
            await interaction.response.send_message("Only whoever opened the table can do that.", ephemeral=True)
            return
        if table.started:
            await interaction.response.send_message("Too late, the cards are already out.", ephemeral=True)
            return
        table.pending_bots = 0
        await interaction.response.send_message("🚫 No bots tonight. Humans only at this table.")
        await update_control_message(table)


class TableControlView(discord.ui.View):
    def __init__(self, table: HoldemTable):
        super().__init__(timeout=None)
        self.table = table
        if not table.started and table.pending_bots:
            self.add_item(NoBotsButton(table))

    @discord.ui.button(label="Buy In", style=discord.ButtonStyle.success)
    async def buy_in_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot:
            await interaction.response.send_message("Bots can't play.", ephemeral=True)
            return
        await interaction.response.send_modal(BuyInModal(self.table))

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.danger)
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        table = self.table
        seat = table.seat_for(interaction.user.id)
        if seat is None:
            await interaction.response.send_message("You're not seated at this table.", ephemeral=True)
            return

        seat.standing = True
        player = table.current_players_by_id.get(interaction.user.id)
        if player is not None and not player.folded and not player.all_in:
            player.folded = True
            active_view = table.current_action_view
            if active_view is not None and active_view.player is player and not active_view.is_finished():
                active_view.result = ("fold",)
                active_view.stop()

        await interaction.response.send_message("Noted — you'll stand up after the current hand.", ephemeral=True)
        await update_control_message(table)


def build_control_view(table: HoldemTable) -> "TableControlView":
    if table.control_view is not None:
        table.control_view.stop()
    table.control_view = TableControlView(table)
    return table.control_view


async def update_control_message(table: HoldemTable):
    if table.control_message is None or table.closed:
        return
    try:
        await table.control_message.edit(embed=build_control_embed(table), view=build_control_view(table))
    except discord.HTTPException:
        pass


def build_table_embed(
    game: HoldemGame, street_name: str, prompt_player: Player | None = None
) -> tuple[discord.Embed, discord.File | None]:
    embed = discord.Embed(title=f"🃏 Texas Hold'em — {street_name}", color=discord.Color.dark_green())

    file = None
    if game.community:
        buf = cards_render.render_hand(game.community)
        file = discord.File(buf, filename="community.png")
        embed.set_image(url="attachment://community.png")
    else:
        embed.add_field(name="Community", value="—", inline=False)

    pot_total = sum(p.contributed for p in game.players)
    currency = db.get_currency_name(game.guild_id)
    embed.add_field(name="Pot", value=f"{pot_total} {currency}", inline=True)
    embed.add_field(name="Current bet", value=f"{game.current_bet} {currency}", inline=True)

    lines = []
    dealer_idx, sb_idx, bb_idx = blind_seats(len(game.players))
    for i, p in enumerate(game.players):
        badges = [b for b, idx in (("🔘D", dealer_idx), ("SB", sb_idx), ("BB", bb_idx)) if idx == i]
        badge = f"`{' '.join(badges)}` " if badges else ""
        if p.folded:
            tag = "❌ folded"
        elif p.all_in:
            tag = "🔥 all-in"
        elif prompt_player is p:
            tag = "🎯 to act"
        else:
            tag = ""
        lines.append(f"{badge}**{isolate(p.member.display_name)}** — {p.street_contributed} in (stack {p.remaining}) {tag}")
    embed.add_field(name="Players", value="\n".join(lines), inline=False)
    return embed, file


async def _bot_turn(ctx, game: HoldemGame, player: Player, to_call: int):
    """An NPC bot thinks (Monte Carlo equity in a thread, padded to a readable pause), acts through
    the same apply_action a human's buttons use, and announces it, maybe with one of its lines."""
    npc = player.member.npc
    started = time.monotonic()
    opponents = len(game.active()) - 1
    equity = await asyncio.to_thread(poker_ai.estimate_equity, player.hole, list(game.community), opponents)
    situation = poker_ai.Situation(
        hole=player.hole, community=list(game.community), n_opponents=opponents, to_call=to_call,
        pot=sum(p.contributed for p in game.players), current_bet=game.current_bet,
        street_contributed=player.street_contributed, remaining=player.remaining, big_blind=BIG_BLIND,
        raises_this_street=player.raises_this_street,
    )
    action = poker_ai.decide(situation, poker_ai.Personality.from_npc(npc), equity)
    if action[0] == "check" and to_call > 0:
        action = ("call",)
    await asyncio.sleep(max(0.0, BOT_THINK_SECONDS - (time.monotonic() - started)))

    call_amount = min(to_call, player.remaining)
    opening_bet = game.current_bet == 0
    await apply_action(game, player, action)
    kind = action[0]
    if kind == "fold":
        verb = "folds"
    elif kind == "check":
        verb = "checks"
    elif kind == "call":
        verb = f"calls {call_amount}" + (", all-in" if player.all_in else "")
    elif kind == "raise":
        verb = f"bets {game.current_bet}" if opening_bet else f"raises to {game.current_bet}"
    else:
        kind = "allin"
        verb = f"goes **all-in** ({player.street_contributed} this street)"
    text = f"🤖 **{player.member.display_name}** {verb}."
    line = npc_line(npc, kind)
    if line:
        text += f"\n> {line}"
    await ctx.send(text)


async def run_betting_round(ctx, table: HoldemTable, game: HoldemGame, street_name: str, reset_street: bool):
    active = [p for p in game.players if not p.folded]
    if len(active) <= 1:
        return
    if len([p for p in active if not p.all_in]) <= 1:
        return  # nobody left who can voluntarily act — no more betting this hand

    if reset_street:
        for p in game.players:
            p.street_contributed = 0
        game.current_bet = 0
    for p in game.players:
        p.acted = False
        p.raises_this_street = 0

    order = preflop_order(game.players) if street_name == "Pre-Flop" else postflop_order(game.players)

    while True:
        if len([p for p in game.players if not p.folded]) <= 1:
            return
        pending = [
            p for p in order
            if not p.folded and not p.all_in and (not p.acted or p.street_contributed < game.current_bet)
        ]
        if not pending:
            return

        player = pending[0]
        to_call = game.current_bet - player.street_contributed
        if is_npc(player.member):
            await _bot_turn(ctx, game, player, to_call)
            continue
        view = ActionView(table, game, player, to_call)
        table.current_action_view = view
        embed, file = build_table_embed(game, street_name, prompt_player=player)
        send_kwargs = {"content": f"{player.member.mention} — your turn ({street_name})", "embed": embed, "view": view}
        if file is not None:
            send_kwargs["file"] = file
        turn_message = await ctx.send(**send_kwargs)

        timed_out = await view.wait()
        action = view.result
        if timed_out or action is None:
            action = ("fold",) if to_call > 0 else ("check",)
        await apply_action(game, player, action)
        table.current_action_view = None

        try:
            await turn_message.edit(view=None)
        except discord.HTTPException:
            pass


async def settle_hand(ctx, game: HoldemGame):
    active = game.active()
    contributions = {p.member.id: p.contributed for p in game.players}
    folded_ids = {p.member.id for p in game.players if p.folded}

    if len(active) == 1:
        winner = active[0]
        amount = sum(contributions.values())
        currency = db.get_currency_name(game.guild_id)
        if is_npc(winner.member):
            balance_str = ""
        else:
            new_balance = await asyncio.to_thread(db.update_balance, game.guild_id, winner.member.id, amount)
            balance_str = f" (Balance: {new_balance})"
        embed = discord.Embed(
            title="🃏 Texas Hold'em — Result",
            description=(
                f"Everyone else folded — **{winner.member.display_name}** takes the pot of "
                f"**{amount}** {currency}!{balance_str}"
            ),
            color=discord.Color.gold(),
        )
        if is_npc(winner.member):
            line = npc_line(winner.member.npc, "win")
            if line:
                embed.add_field(name=f"{winner.member.display_name} says", value=line, inline=False)
        send_kwargs = {"embed": embed}
        if game.community:
            buf = cards_render.render_hand(game.community)
            send_kwargs["file"] = discord.File(buf, filename="community.png")
            embed.set_image(url="attachment://community.png")
        else:
            embed.add_field(name="Community", value="—", inline=False)
        await ctx.send(**send_kwargs)
        return

    pots = poker.build_pots(contributions, folded_ids)
    id_to_player = {p.member.id: p for p in game.players}
    payouts: dict[int, int] = {}
    hand_desc: dict[int, str] = {}

    for amount, eligible_ids in pots:
        eligible_players = [id_to_player[uid] for uid in eligible_ids]
        scored = []
        for p in eligible_players:
            score, _best5 = poker.best_hand(p.hole + game.community)
            hand_desc[p.member.id] = poker.describe_score(score)
            scored.append((score, p))
        best_score = max(s for s, _ in scored)
        winners = [p for s, p in scored if s == best_score]
        share, remainder = divmod(amount, len(winners))
        for i, w in enumerate(winners):
            payouts[w.member.id] = payouts.get(w.member.id, 0) + share + (1 if i < remainder else 0)

    for uid, amount in payouts.items():
        if not is_npc(id_to_player[uid].member):
            await asyncio.to_thread(db.update_balance, game.guild_id, uid, amount)

    community_buf = cards_render.render_hand(game.community)
    files = [discord.File(community_buf, filename="community.png")]
    community_embed = discord.Embed(title="🃏 Texas Hold'em — Showdown", color=discord.Color.gold())
    community_embed.set_image(url="attachment://community.png")
    embeds = [community_embed]

    currency = db.get_currency_name(game.guild_id)
    for i, p in enumerate(active):
        won = payouts.get(p.member.id, 0)
        hand_buf = cards_render.render_hand(p.hole)
        fname = f"hand{i}.png"
        files.append(discord.File(hand_buf, filename=fname))
        title = f"{p.member.display_name} — won {won} {currency}" if won else p.member.display_name
        description = hand_desc.get(p.member.id, "—")
        if is_npc(p.member):
            line = npc_line(p.member.npc, "win" if won else "lose")
            if line:
                description += f"\n> {line}"
        player_embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.green() if won else discord.Color.greyple(),
        )
        player_embed.set_image(url=f"attachment://{fname}")
        embeds.append(player_embed)

    await ctx.send(embeds=embeds, files=files)


async def play_hand(ctx, table: HoldemTable, members: list, stacks: dict[int, int]):
    players = [Player(m, stacks[m.id]) for m in members]
    game = HoldemGame(players, table.guild_id)
    table.current_game = game
    table.current_players_by_id = {p.member.id: p for p in players}
    humans = [m for m in members if not is_npc(m)]
    busy_players.update(m.id for m in humans)

    try:
        dealer, sb_idx, bb_idx = blind_seats(len(players))
        sb_player, bb_player = players[sb_idx], players[bb_idx]
        await _commit(game, sb_player, min(SMALL_BLIND, sb_player.remaining))
        await _commit(game, bb_player, min(BIG_BLIND, bb_player.remaining))
        game.current_bet = bb_player.street_contributed

        for p in players:
            p.hole = [game.deck.draw(), game.deck.draw()]

        preflop_embed, preflop_file = build_table_embed(game, "Pre-Flop")
        preflop_kwargs = {
            "content": "Cards are dealt — click below to see your hand (only you can see it).",
            "embed": preflop_embed,
            "view": RevealHandView(table),
        }
        if preflop_file is not None:
            preflop_kwargs["file"] = preflop_file
        await ctx.send(**preflop_kwargs)
        await run_betting_round(ctx, table, game, "Pre-Flop", reset_street=False)

        for street_name, n_cards in (("Flop", 3), ("Turn", 1), ("River", 1)):
            if len(game.active()) <= 1:
                break
            game.deck.draw()  # burn card
            for _ in range(n_cards):
                game.community.append(game.deck.draw())

            can_act = [p for p in game.active() if not p.all_in]
            street_embed, street_file = build_table_embed(game, street_name)
            street_kwargs = {"embed": street_embed, "view": RevealHandView(table)}
            if street_file is not None:
                street_kwargs["file"] = street_file
            await ctx.send(**street_kwargs)
            if len(can_act) > 1:
                await run_betting_round(ctx, table, game, street_name, reset_street=True)
            else:
                await asyncio.sleep(1.5)  # let everyone see the card land before dealing the next

        await settle_hand(ctx, game)
    finally:
        busy_players.difference_update(m.id for m in humans)
        table.current_game = None
        table.current_players_by_id = {}
        table.current_action_view = None


def next_hand_order(table: HoldemTable, members: list) -> list:
    """Seats this hand's players in table order with the dealer first (play_hand/blind_seats treat
    index 0 as the button). The button moves one seat on from last hand's dealer, skipping anyone
    sitting out, and the blinds follow it."""
    seat_nos = {s.member.id: s.seat_no for s in table.seats}
    ordered = sorted(members, key=lambda m: seat_nos[m.id])
    last = table.last_dealer_seat_no
    start = 0
    if last is not None:
        start = next((i for i, m in enumerate(ordered) if seat_nos[m.id] > last), 0)
    ordered = ordered[start:] + ordered[:start]
    table.last_dealer_seat_no = seat_nos[ordered[0].id]
    return ordered


async def run_table(ctx, table: HoldemTable):
    try:
        while True:
            # A bot whose poker_trigger stopped holding (Kel when the full moon rises) gets up.
            for s in table.seated():
                if is_npc(s.member) and not await bot_available(table, s.member.npc):
                    s.standing = True
                    await ctx.send(f"🤖 **{s.member.display_name}** has somewhere to be and leaves the table.")
            table.seats = [s for s in table.seats if not s.standing]

            members: list = []
            stacks: dict[int, int] = {}
            bot_seats = []
            for s in table.seated():
                if is_npc(s.member):
                    bot_seats.append(s)
                    continue
                if s.member.id in busy_players:
                    continue
                balance = await asyncio.to_thread(db.get_balance, table.guild_id, s.member.id)
                if balance <= 0:
                    continue
                members.append(s.member)
                stacks[s.member.id] = min(balance, s.buy_in) if s.buy_in is not None else balance
            if members:
                bot_stack = min(BOT_STACK_CAP, max(stacks.values()))
                for s in bot_seats:
                    members.append(s.member)
                    stacks[s.member.id] = bot_stack
            if not any(not is_npc(m) for m in members) or len(members) < 2:
                await ctx.send("Not enough funded players to deal a hand — table closed. Run `!holdem` to start a new one.")
                break

            members = next_hand_order(table, members)

            await play_hand(ctx, table, members, stacks)

            table.seats = [s for s in table.seats if not s.standing]
            await update_control_message(table)

            if not table.seated_humans() or len(table.seated()) < 2:
                await ctx.send("Table closed — not enough players left.")
                break

            await asyncio.sleep(BETWEEN_HANDS_SECONDS)
    finally:
        table.closed = True
        active_tables.pop(table.channel_id, None)
        if table.control_view is not None:
            table.control_view.stop()
        if table.control_message is not None:
            try:
                await table.control_message.edit(view=None)
            except discord.HTTPException:
                pass


async def start_holdem_table(ctx, buy_in: int | None):
    table = HoldemTable(ctx.channel, ctx.channel.id, ctx.guild.id, ctx.author.id)
    table.add_seat(ctx.author, buy_in)
    active_tables[ctx.channel.id] = table
    # Who the bots are stays secret until the deal: shown at open, a host could close and reopen
    # until they got a line-up they liked.
    table.pending_bots = STARTING_BOTS

    content = (
        f"🃏 {ctx.author.mention} opened a Hold'em table! Dealing the first hand in {JOIN_SECONDS}s.\n"
        f"🤖 {STARTING_BOTS} mystery bots will sit down when the cards are dealt.\n"
        f"-# {ctx.author.display_name} can send them home with No Bots before then."
    )
    message = await ctx.send(
        content=content,
        embed=build_control_embed(table),
        view=build_control_view(table),
    )
    table.control_message = message

    await asyncio.sleep(JOIN_SECONDS)
    table.started = True
    if table.pending_bots:
        seated = await seat_random_bots(table, table.pending_bots)
        table.pending_bots = 0
        if seated:
            await ctx.send("\n".join(sit_text(npc) for npc in seated))
    await update_control_message(table)  # shows the bots, drops the No Bots button
    await run_table(ctx, table)
