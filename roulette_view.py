import asyncio
import random

import discord

import achievements
import db
import roulette
import roulette_render
from holdem_view import busy_players as holdem_busy_players

BET_TIMEOUT_SECONDS = 30  # a round spins this long after its last bet (or after opening, if none yet)
STEAL_CHANCE = 0.01  # 1-in-100 chance a winning payout gets swiped instead of paid out
STEAL_NAMES = ["Lady of the evening", "Classy Escort"]

# Same gif bot.py's !roy posts -- duplicated here rather than imported since roulette_view.py is
# itself imported by bot.py (bot.run() is unguarded at module level there, so nothing may import it
# back). LET IT RIDE fires this alongside its own bet as a bonus, not a real invocation of !roy.
ROY_GIF = "https://media.giphy.com/media/ywGp4PMJdeLyuRq7vJ/giphy.gif"

# The Sickly Victorian Daughters gag -- purely cosmetic, no game-state hook, same running joke
# target as bot.py's RUB_LUCKY_TARGET_ID. Rolled once per round (not per losing/winning bet)
# against this one user's aggregate net for the round, not any single bet -- a loss and a win each
# have their own independent chance/flavor text, sharing the same image and dismiss button.
DAUGHTERS_TARGET_ID = 272816170749526027
DAUGHTERS_LOSS_CHANCE = 0.05  # rare enough to stay funny instead of predictable
DAUGHTERS_WIN_CHANCE = 0.05
DAUGHTERS_IMAGE_PATH = "assets/sickly victorian daughters.png"
DAUGHTERS_LOSS_MESSAGE = (
    "Your three waifish, malnutritioned daughters approach the table and ask if you have lost "
    "all the money they had for food."
)
DAUGHTERS_WIN_MESSAGE = (
    "Your starving, forlorn daughters approach and ask you if you can spare flakes for Quimbo's "
    "Eurasian Goiter medication."
)
# Bumped only when DAUGHTERS_TARGET_ID is the one who actually clicks the dismiss button (see
# SicklyVictorianDaughtersView.push_away) -- someone else clicking to clear the channel doesn't
# count as *him* neglecting anyone. Shown on his own !stats only (bot.py's stats_cmd), floored at 1
# there rather than seeded in the DB, so it reads as "at least once" from the start with no
# per-guild migration needed.
DAUGHTERS_NEGLECT_FLAG = "daughters_neglected"

# The Tax Man gag -- another running joke against this same user (see DAUGHTERS_TARGET_ID above),
# same popup-in-channel shape as SicklyVictorianDaughtersView below. Rolled independently against
# each one of his bets as it settles in resolve() (so a round where he placed several bets gets
# several independent chances, not one per round), demanding TAXMAN_CUT of his biggest single win
# to date (db.get_user_bet_summary's best_win), fixed at popup-creation time so the amount shown
# and the amount actually charged always agree even if his biggest win changes before he responds.
# Paying it off is the only way to clear TAXMAN_WANTED_FLAG -- failing to cover it *or* refusing to
# pay both break his legs (TAXMAN_LEGS_BROKEN_FLAG bumped, shown on his own !stats) and leave the
# flag set, so he stays a live target for a future roll to shake down again.
TAXMAN_TARGET_ID = 272816170749526027
TAXMAN_CUT = 0.30
TAXMAN_TRIGGER_CHANCE = 0.05
TAXMAN_LEGS_BROKEN_FLAG = "legs_broken"
TAXMAN_WANTED_FLAG = "taxman_wanted"
TAXMAN_IMAGE_PATH = "assets/hatman.jpg"

# (guild_id) -> True while a popup is up and unresolved for TAXMAN_TARGET_ID, so a second roll
# can't stack a duplicate demand on top of one he hasn't answered yet. Guild-keyed only (there's
# just the one target) -- purely an anti-spam guard, not persisted state, same "in-memory, not a
# DB row" treatment as active_rounds/last_bets above.
taxman_open_popups: set[int] = set()


class TaxManView(discord.ui.View):
    """Pay or Push Away, same shape as SicklyVictorianDaughtersView's single dismiss button but
    with a real consequence either way. Only TAXMAN_TARGET_ID may click either button -- unlike the
    Daughters popup, this moves real currency and can break his legs, so it's not anyone's call but
    his own."""

    def __init__(self, guild_id: int, demand: int, currency: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.demand = demand
        self.currency = currency

    async def on_timeout(self):
        taxman_open_popups.discard(self.guild_id)

    async def _reject_if_not_target(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != TAXMAN_TARGET_ID:
            await interaction.response.send_message("This isn't your problem.", ephemeral=True)
            return True
        return False

    async def _close_popup(self, interaction: discord.Interaction):
        taxman_open_popups.discard(self.guild_id)
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

    async def _break_legs(self, channel, flavor: str):
        legs_broken = await asyncio.to_thread(
            db.increment_flag, self.guild_id, TAXMAN_TARGET_ID, TAXMAN_LEGS_BROKEN_FLAG
        )
        await asyncio.to_thread(db.set_flag, self.guild_id, TAXMAN_TARGET_ID, TAXMAN_WANTED_FLAG, 1)
        await channel.send(f"{flavor} **CRACK.** 🦵💥 (Legs Broken: {legs_broken})")

    @discord.ui.button(label="Pay", style=discord.ButtonStyle.success)
    async def pay(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._reject_if_not_target(interaction):
            return
        await interaction.response.defer()
        channel = interaction.channel
        await self._close_popup(interaction)
        balance = await asyncio.to_thread(db.get_balance, self.guild_id, TAXMAN_TARGET_ID)
        if balance >= self.demand:
            await asyncio.to_thread(db.update_balance, self.guild_id, TAXMAN_TARGET_ID, -self.demand)
            await asyncio.to_thread(db.set_flag, self.guild_id, TAXMAN_TARGET_ID, TAXMAN_WANTED_FLAG, 0)
            await channel.send(
                f"💸 <@{TAXMAN_TARGET_ID}> pays the **{self.demand} {self.currency}**. "
                "The Tax Man tips his hat and disappears."
            )
        else:
            await self._break_legs(channel, f"<@{TAXMAN_TARGET_ID}> doesn't have the {self.demand} {self.currency}.")

    @discord.ui.button(label="Push him away", style=discord.ButtonStyle.danger)
    async def push_away(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._reject_if_not_target(interaction):
            return
        await interaction.response.defer()
        channel = interaction.channel
        await self._close_popup(interaction)
        await self._break_legs(channel, f"<@{TAXMAN_TARGET_ID}> shoves him away and refuses to pay.")


async def _maybe_send_taxman_popup(guild_id: int, channel) -> None:
    if guild_id in taxman_open_popups or random.random() >= TAXMAN_TRIGGER_CHANCE:
        return
    _, _, _, best_win, _ = await asyncio.to_thread(db.get_user_bet_summary, guild_id, TAXMAN_TARGET_ID)
    demand = int((best_win or 0) * TAXMAN_CUT)
    if demand <= 0:
        return  # never won anything yet -- nothing worth shaking down
    currency = db.get_currency_name(guild_id)
    taxman_open_popups.add(guild_id)
    embed = discord.Embed(
        title="The Tax Man",
        description=(
            f"A man in a cheap suit steps out of the shadows. **\"Tax time.\"**\n"
            f"He wants **{demand} {currency}**, 30% of your biggest win."
        ),
        color=discord.Color.dark_gray(),
    )
    file = discord.File(TAXMAN_IMAGE_PATH, filename="hatman.jpg")
    embed.set_image(url="attachment://hatman.jpg")
    await channel.send(
        f"<@{TAXMAN_TARGET_ID}>", embed=embed, file=file, view=TaxManView(guild_id, demand, currency)
    )


class SicklyVictorianDaughtersView(discord.ui.View):
    """One-button dismissal for the Sickly Victorian Daughters popup (see RouletteView.resolve) --
    purely cosmetic, no game state involved; the button just deletes the popup."""

    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Push your daughters away", style=discord.ButtonStyle.danger)
    async def push_away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if interaction.user.id == DAUGHTERS_TARGET_ID:
            await asyncio.to_thread(db.increment_flag, interaction.guild_id, interaction.user.id, DAUGHTERS_NEGLECT_FLAG)
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass


async def _maybe_send_daughters_popup(channel, net_by_user: dict[int, int]) -> None:
    """Rolls both the loss and win variants of the gag (see the module comment above) against
    DAUGHTERS_TARGET_ID's aggregate net for this round -- net is never both negative and positive
    at once, so at most one of these fires."""
    net = net_by_user.get(DAUGHTERS_TARGET_ID, 0)
    if net < 0 and random.random() < DAUGHTERS_LOSS_CHANCE:
        message = DAUGHTERS_LOSS_MESSAGE
    elif net > 0 and random.random() < DAUGHTERS_WIN_CHANCE:
        message = DAUGHTERS_WIN_MESSAGE
    else:
        return
    embed = discord.Embed(title="Sickly Victorian Daughters", description=message, color=discord.Color.dark_gray())
    file = discord.File(DAUGHTERS_IMAGE_PATH, filename="daughters.png")
    embed.set_image(url="attachment://daughters.png")
    await channel.send(embed=embed, file=file, view=SicklyVictorianDaughtersView())


# channel_id -> the currently-open round's RouletteView, so only one open table per channel.
# Registered/popped for the table's whole persistent lifetime by run_roulette_table -- not by
# RouletteView itself -- since a table now spins round after round rather than closing after one.
active_rounds: dict[int, "RouletteView"] = {}

# (guild_id, user_id) -> that user's bets from their last resolved round, for "Repeat Bets"
last_bets: dict[tuple[int, int], list[dict]] = {}


class BetAmountModal(discord.ui.Modal):
    def __init__(self, round_view: "RouletteView", kind: str):
        super().__init__(title=f"Bet on {roulette.describe_bet(kind, None)}")
        self.round_view = round_view
        self.kind = kind
        self.amount_input = discord.ui.TextInput(label="Bet amount", placeholder="e.g. 50")
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.round_view.place_bet(interaction, self.kind, None, self.amount_input.value)


class NumberBetModal(discord.ui.Modal):
    def __init__(self, round_view: "RouletteView"):
        super().__init__(title="Bet on a Number")
        self.round_view = round_view
        self.number_input = discord.ui.TextInput(label="Number (0-36)", placeholder="e.g. 17")
        self.amount_input = discord.ui.TextInput(label="Bet amount", placeholder="e.g. 50")
        self.add_item(self.number_input)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            number = int(self.number_input.value)
        except ValueError:
            await interaction.response.send_message("Number must be a whole number between 0 and 36.", ephemeral=True)
            return
        if not 0 <= number <= 36:
            await interaction.response.send_message("Number must be between 0 and 36.", ephemeral=True)
            return
        await self.round_view.place_bet(interaction, "number", number, self.amount_input.value)


def _parse_numbers(raw: str) -> list[int]:
    """Parses a comma-separated list of numbers/ranges like "1,5,9,12-15" into a deduped list.

    Raises ValueError with a user-facing message on any bad token or out-of-range number.
    """
    numbers: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token[1:]:
            start_str, end_str = token[1:].split("-", 1)
            start_str = token[0] + start_str
            try:
                start, end = int(start_str), int(end_str)
            except ValueError:
                raise ValueError(f"'{token}' isn't a valid range.")
            if start > end:
                start, end = end, start
            candidates = range(start, end + 1)
        else:
            try:
                candidates = [int(token)]
            except ValueError:
                raise ValueError(f"'{token}' isn't a valid number.")
        for n in candidates:
            if not 0 <= n <= 36:
                raise ValueError(f"{n} is out of range — numbers must be 0-36.")
            if n not in seen:
                seen.add(n)
                numbers.append(n)
    if not numbers:
        raise ValueError("Enter at least one number.")
    return numbers


class MultiNumberBetModal(discord.ui.Modal):
    def __init__(self, round_view: "RouletteView"):
        super().__init__(title="Bet on Multiple Numbers")
        self.round_view = round_view
        self.numbers_input = discord.ui.TextInput(label="Numbers (e.g. 1,5,9,12-15)", placeholder="1,5,9,12-15")
        self.amount_input = discord.ui.TextInput(label="Amount per number", placeholder="e.g. 10")
        self.add_item(self.numbers_input)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            numbers = _parse_numbers(self.numbers_input.value)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await self.round_view.place_number_bets(interaction, numbers, self.amount_input.value)


class ColumnBetModal(discord.ui.Modal):
    def __init__(self, round_view: "RouletteView"):
        super().__init__(title="Bet on a Column")
        self.round_view = round_view
        self.column_input = discord.ui.TextInput(label="Column (1, 2, or 3)", placeholder="e.g. 1")
        self.amount_input = discord.ui.TextInput(label="Bet amount", placeholder="e.g. 50")
        self.add_item(self.column_input)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            column = int(self.column_input.value)
        except ValueError:
            await interaction.response.send_message("Column must be 1, 2, or 3.", ephemeral=True)
            return
        if column not in (1, 2, 3):
            await interaction.response.send_message("Column must be 1, 2, or 3.", ephemeral=True)
            return
        await self.round_view.place_bet(interaction, "column", column, self.amount_input.value)


class DozenBetModal(discord.ui.Modal):
    def __init__(self, round_view: "RouletteView"):
        super().__init__(title="Bet on a Dozen")
        self.round_view = round_view
        self.dozen_input = discord.ui.TextInput(label="Dozen (1=1-12, 2=13-24, 3=25-36)", placeholder="e.g. 1")
        self.amount_input = discord.ui.TextInput(label="Bet amount", placeholder="e.g. 50")
        self.add_item(self.dozen_input)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            dozen = int(self.dozen_input.value)
        except ValueError:
            await interaction.response.send_message("Dozen must be 1, 2, or 3.", ephemeral=True)
            return
        if dozen not in (1, 2, 3):
            await interaction.response.send_message("Dozen must be 1, 2, or 3.", ephemeral=True)
            return
        await self.round_view.place_bet(interaction, "dozen", dozen, self.amount_input.value)


class SplitBetModal(discord.ui.Modal):
    def __init__(self, round_view: "RouletteView"):
        super().__init__(title="Split Bet (2 numbers)")
        self.round_view = round_view
        self.a_input = discord.ui.TextInput(label="Number A (0-36)", placeholder="e.g. 1")
        self.b_input = discord.ui.TextInput(label="Number B (0-36)", placeholder="e.g. 2")
        self.amount_input = discord.ui.TextInput(label="Bet amount", placeholder="e.g. 50")
        self.add_item(self.a_input)
        self.add_item(self.b_input)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            a, b = int(self.a_input.value), int(self.b_input.value)
        except ValueError:
            await interaction.response.send_message("Both numbers must be whole numbers between 0 and 36.", ephemeral=True)
            return
        if not (0 <= a <= 36 and 0 <= b <= 36):
            await interaction.response.send_message("Both numbers must be between 0 and 36.", ephemeral=True)
            return
        if not roulette.are_split_adjacent(a, b):
            await interaction.response.send_message(
                f"{a} and {b} aren't adjacent on the table — a split bet needs two neighboring numbers.",
                ephemeral=True,
            )
            return
        await self.round_view.place_bet(interaction, "combo", tuple(sorted((a, b))), self.amount_input.value)


ANCHOR_COMBO_LABELS = {
    "street": "Any number in the row (1-36)",
    "corner": "Bottom-left number of the block (1-36)",
    "sixline": "Lowest number of the six (1, 4, 7, ... 31)",
}

ANCHOR_COMBO_FUNCS = {
    "street": roulette.street_numbers,
    "corner": roulette.corner_numbers,
    "sixline": roulette.sixline_numbers,
}

ANCHOR_COMBO_TITLES = {
    "street": "Street Bet (3 numbers)",
    "corner": "Corner Bet (4 numbers)",
    "sixline": "Six-Line Bet (6 numbers)",
}


class AnchorComboModal(discord.ui.Modal):
    def __init__(self, round_view: "RouletteView", shape: str):
        super().__init__(title=ANCHOR_COMBO_TITLES[shape])
        self.round_view = round_view
        self.shape = shape
        self.anchor_input = discord.ui.TextInput(label=ANCHOR_COMBO_LABELS[shape], placeholder="e.g. 1")
        self.amount_input = discord.ui.TextInput(label="Bet amount", placeholder="e.g. 50")
        self.add_item(self.anchor_input)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            anchor = int(self.anchor_input.value)
        except ValueError:
            await interaction.response.send_message("Anchor must be a whole number.", ephemeral=True)
            return
        if not 1 <= anchor <= 36:
            await interaction.response.send_message("Anchor must be between 1 and 36.", ephemeral=True)
            return
        numbers = ANCHOR_COMBO_FUNCS[self.shape](anchor)
        if numbers is None:
            await interaction.response.send_message(
                f"{anchor} isn't a valid anchor for that shape — try the label's hint.",
                ephemeral=True,
            )
            return
        await self.round_view.place_bet(interaction, "combo", tuple(sorted(numbers)), self.amount_input.value)


TOWER_SHAPE_OPTIONS = [
    ("split", "Split (2 numbers)"),
    ("street", "Street (3 numbers)"),
    ("corner", "Corner (4 numbers)"),
    ("sixline", "Six-Line (6 numbers)"),
]


class TowerShapeSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=label, value=shape) for shape, label in TOWER_SHAPE_OPTIONS]
        super().__init__(placeholder="Tower Bet: pick a shape...", options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: "RouletteView" = self.view
        if view.resolved:
            await interaction.response.send_message("This round already closed.", ephemeral=True)
            return
        shape = self.values[0]
        if shape == "split":
            await interaction.response.send_modal(SplitBetModal(view))
        else:
            await interaction.response.send_modal(AnchorComboModal(view, shape))


class RouletteView(discord.ui.View):
    def __init__(self, starter: discord.abc.User, channel_id: int, guild_id: int):
        super().__init__(timeout=BET_TIMEOUT_SECONDS)
        self.starter = starter
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.bets: list[dict] = []
        self.message: discord.Message | None = None
        self.resolved = False
        # Set at the very end of resolve(), after all its settlement/payout awaits finish --
        # unlike view.wait() (which unblocks the instant stop() is called, well before that work
        # is done), this is what run_roulette_table actually awaits between rounds so it doesn't
        # start editing the message for the next round while this one's still being settled.
        self.resolved_event = asyncio.Event()
        self.add_item(TowerShapeSelect())

    def _reset_timer(self):
        """Bumps the timeout back out to BET_TIMEOUT_SECONDS from now -- called after every
        successful bet so the round spins 30s after the *last* bet, not a fixed time after it
        opened. (discord.py already does this automatically for a plain button click, since
        clicking Red/Black/etc to open its amount modal is itself a view-item callback -- this
        covers the actual bet-placing methods, which run from a Modal's on_submit instead.)"""
        self.timeout = BET_TIMEOUT_SECONDS

    def build_display(
        self, footer: str | None = None, winning_number: int | None = None
    ) -> tuple[discord.Embed, discord.File]:
        embed = discord.Embed(
            title="🎡 Roulette — Place Your Bets!" if winning_number is None else "🎡 Roulette",
            description=f"Click a bet type below to join this spin. Betting closes {BET_TIMEOUT_SECONDS}s after the last bet."
            if winning_number is None
            else None,
            color=discord.Color.dark_green(),
        )
        if self.bets:
            currency = db.get_currency_name(self.guild_id)
            lines = [
                f"**{bet['display_name']}** — {roulette.describe_bet(bet['kind'], bet['value'])} — {bet['amount']} {currency}"
                for bet in self.bets
            ]
            embed.add_field(name="Current Bets", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Current Bets", value="No bets yet — be the first!", inline=False)
        embed.set_footer(text=footer or f"Started by {self.starter.display_name}")

        buf = roulette_render.render_table(self.bets, winning_number=winning_number)
        file = discord.File(buf, filename="table.png")
        embed.set_image(url="attachment://table.png")
        return embed, file

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    async def place_bet(self, interaction: discord.Interaction, kind: str, value, raw_amount: str):
        if self.resolved:
            await interaction.response.send_message("This round already closed.", ephemeral=True)
            return
        if interaction.user.id in holdem_busy_players:
            await interaction.response.send_message("Finish up whatever you're already doing first.", ephemeral=True)
            return
        try:
            amount = int(raw_amount)
        except ValueError:
            await interaction.response.send_message("Bet amount must be a whole number.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("Bet amount must be positive.", ephemeral=True)
            return

        balance = await asyncio.to_thread(db.get_balance, self.guild_id, interaction.user.id)
        currency = db.get_currency_name(self.guild_id)
        if amount > balance:
            await interaction.response.send_message(f"You only have **{balance}** {currency}.", ephemeral=True)
            return

        await asyncio.to_thread(db.update_balance, self.guild_id, interaction.user.id, -amount)
        self.bets.append(
            {
                "user_id": interaction.user.id,
                "display_name": interaction.user.display_name,
                "kind": kind,
                "value": value,
                "amount": amount,
            }
        )
        await interaction.response.send_message(
            f"✅ Bet placed: {roulette.describe_bet(kind, value)} for **{amount}** {currency}.", ephemeral=True
        )
        self._reset_timer()
        if self.message is not None:
            try:
                embed, file = self.build_display()
                await self.message.edit(embed=embed, attachments=[file])
            except discord.HTTPException:
                pass

    async def place_number_bets(self, interaction: discord.Interaction, numbers: list[int], raw_amount: str):
        if self.resolved:
            await interaction.response.send_message("This round already closed.", ephemeral=True)
            return
        if interaction.user.id in holdem_busy_players:
            await interaction.response.send_message("Finish up whatever you're already doing first.", ephemeral=True)
            return
        try:
            amount = int(raw_amount)
        except ValueError:
            await interaction.response.send_message("Bet amount must be a whole number.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("Bet amount must be positive.", ephemeral=True)
            return

        total = amount * len(numbers)
        balance = await asyncio.to_thread(db.get_balance, self.guild_id, interaction.user.id)
        currency = db.get_currency_name(self.guild_id)
        if total > balance:
            await interaction.response.send_message(
                f"That's {total} {currency} total across {len(numbers)} numbers, but you only have **{balance}** {currency}.",
                ephemeral=True,
            )
            return

        await asyncio.to_thread(db.update_balance, self.guild_id, interaction.user.id, -total)
        for n in numbers:
            self.bets.append(
                {
                    "user_id": interaction.user.id,
                    "display_name": interaction.user.display_name,
                    "kind": "number",
                    "value": n,
                    "amount": amount,
                }
            )
        numbers_str = ", ".join(str(n) for n in numbers)
        await interaction.response.send_message(
            f"✅ Bets placed on {numbers_str} — **{amount}** {currency} each (**{total}** {currency} total).",
            ephemeral=True,
        )
        self._reset_timer()
        if self.message is not None:
            try:
                embed, file = self.build_display()
                await self.message.edit(embed=embed, attachments=[file])
            except discord.HTTPException:
                pass

    async def repeat_bets(self, interaction: discord.Interaction):
        if self.resolved:
            await interaction.response.send_message("This round already closed.", ephemeral=True)
            return
        if interaction.user.id in holdem_busy_players:
            await interaction.response.send_message("Finish up whatever you're already doing first.", ephemeral=True)
            return
        saved = last_bets.get((self.guild_id, interaction.user.id))
        if not saved:
            await interaction.response.send_message("You don't have any previous bets to repeat.", ephemeral=True)
            return

        total = sum(b["amount"] for b in saved)
        balance = await asyncio.to_thread(db.get_balance, self.guild_id, interaction.user.id)
        currency = db.get_currency_name(self.guild_id)
        if total > balance:
            await interaction.response.send_message(
                f"Repeating your last bets costs **{total}** {currency} total, but you only have **{balance}** {currency}.",
                ephemeral=True,
            )
            return

        await asyncio.to_thread(db.update_balance, self.guild_id, interaction.user.id, -total)
        for b in saved:
            self.bets.append(
                {
                    "user_id": interaction.user.id,
                    "display_name": interaction.user.display_name,
                    "kind": b["kind"],
                    "value": b["value"],
                    "amount": b["amount"],
                }
            )
        desc = "; ".join(f"{roulette.describe_bet(b['kind'], b['value'])} ({b['amount']})" for b in saved)
        await interaction.response.send_message(
            f"✅ Repeated {len(saved)} bet(s): {desc} — **{total}** {currency} total.", ephemeral=True
        )
        self._reset_timer()
        if self.message is not None:
            try:
                embed, file = self.build_display()
                await self.message.edit(embed=embed, attachments=[file])
            except discord.HTTPException:
                pass

    async def all_in(self, interaction: discord.Interaction):
        """Same saved bets as Repeat Bets, but scaled up (or down) to spend the player's entire
        current balance rather than replaying the original amounts -- proportional across however
        many bets were saved (usually just one), any rounding remainder from the floor division
        landing on the last one so the total always lands on exactly `balance`, not a few flakes
        under it."""
        if self.resolved:
            await interaction.response.send_message("This round already closed.", ephemeral=True)
            return
        if interaction.user.id in holdem_busy_players:
            await interaction.response.send_message("Finish up whatever you're already doing first.", ephemeral=True)
            return
        saved = last_bets.get((self.guild_id, interaction.user.id))
        if not saved:
            await interaction.response.send_message("You don't have any previous bets to repeat.", ephemeral=True)
            return

        balance = await asyncio.to_thread(db.get_balance, self.guild_id, interaction.user.id)
        currency = db.get_currency_name(self.guild_id)
        if balance <= 0:
            await interaction.response.send_message(f"You don't have any {currency} to go all in with.", ephemeral=True)
            return

        total_prev = sum(b["amount"] for b in saved)
        scaled = []
        running = 0
        for i, b in enumerate(saved):
            amount = balance - running if i == len(saved) - 1 else (b["amount"] * balance) // total_prev
            running += amount
            if amount > 0:
                scaled.append({**b, "amount": amount})
        if not scaled:
            await interaction.response.send_message(
                f"You don't have enough {currency} to place any of those bets.", ephemeral=True
            )
            return

        total = sum(b["amount"] for b in scaled)
        await asyncio.to_thread(db.update_balance, self.guild_id, interaction.user.id, -total)
        for b in scaled:
            self.bets.append(
                {
                    "user_id": interaction.user.id,
                    "display_name": interaction.user.display_name,
                    "kind": b["kind"],
                    "value": b["value"],
                    "amount": b["amount"],
                }
            )
        desc = "; ".join(f"{roulette.describe_bet(b['kind'], b['value'])} ({b['amount']})" for b in scaled)
        await interaction.response.send_message(
            f"😈 LET IT RIDE! Repeated {len(scaled)} bet(s): {desc}, **{total}** {currency} total.", ephemeral=True
        )
        self._reset_timer()
        if self.message is not None:
            try:
                embed, file = self.build_display()
                await self.message.edit(embed=embed, attachments=[file])
            except discord.HTTPException:
                pass
            try:
                roy_embed = discord.Embed(color=discord.Color.gold())
                roy_embed.set_image(url=ROY_GIF)
                await self.message.channel.send(embed=roy_embed)
            except discord.HTTPException:
                pass

    async def _open_amount_modal(self, interaction: discord.Interaction, kind: str):
        if self.resolved:
            await interaction.response.send_message("This round already closed.", ephemeral=True)
            return
        await interaction.response.send_modal(BetAmountModal(self, kind))

    @discord.ui.button(label="Red", style=discord.ButtonStyle.danger, row=0)
    async def bet_red(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_amount_modal(interaction, "red")

    @discord.ui.button(label="Black", style=discord.ButtonStyle.secondary, row=0)
    async def bet_black(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_amount_modal(interaction, "black")

    @discord.ui.button(label="Odd", style=discord.ButtonStyle.primary, row=0)
    async def bet_odd(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_amount_modal(interaction, "odd")

    @discord.ui.button(label="Even", style=discord.ButtonStyle.primary, row=0)
    async def bet_even(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_amount_modal(interaction, "even")

    @discord.ui.button(label="1-18", style=discord.ButtonStyle.blurple, row=1)
    async def bet_low(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_amount_modal(interaction, "low")

    @discord.ui.button(label="19-36", style=discord.ButtonStyle.blurple, row=1)
    async def bet_high(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_amount_modal(interaction, "high")

    @discord.ui.button(label="Number...", style=discord.ButtonStyle.success, row=1)
    async def bet_number(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("This round already closed.", ephemeral=True)
            return
        await interaction.response.send_modal(NumberBetModal(self))

    @discord.ui.button(label="Column...", style=discord.ButtonStyle.blurple, row=1)
    async def bet_column(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("This round already closed.", ephemeral=True)
            return
        await interaction.response.send_modal(ColumnBetModal(self))

    @discord.ui.button(label="Dozen...", style=discord.ButtonStyle.blurple, row=1)
    async def bet_dozen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("This round already closed.", ephemeral=True)
            return
        await interaction.response.send_modal(DozenBetModal(self))

    @discord.ui.button(label="Multiple Numbers...", style=discord.ButtonStyle.success, row=3)
    async def bet_multi(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("This round already closed.", ephemeral=True)
            return
        await interaction.response.send_modal(MultiNumberBetModal(self))

    @discord.ui.button(label="Repeat Bets", style=discord.ButtonStyle.secondary, row=3)
    async def bet_repeat(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.repeat_bets(interaction)

    @discord.ui.button(label="😈 LET IT RIDE", style=discord.ButtonStyle.danger, row=3)
    async def bet_all_in(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.all_in(interaction)

    async def resolve(self):
        """Settles this round's bets (or closes out an empty one). Table lifetime/active_rounds
        registration belongs to run_roulette_table, not here -- this only ever handles one round.
        Always sets resolved_event at the end (even on the empty-round path) so that loop's
        `await view.resolved_event.wait()` doesn't unblock until this round's message has actually
        been updated -- unlike view.wait(), which discord.py unblocks the instant stop() runs
        above, well before any of this method's awaited settlement work finishes."""
        if self.resolved:
            return
        self.resolved = True
        self._disable_all()
        self.stop()
        try:
            if not self.bets:
                embed, file = self.build_display(footer="No bets were placed — table closed.")
                if self.message is not None:
                    try:
                        await self.message.edit(embed=embed, attachments=[file], view=self)
                    except discord.HTTPException:
                        pass
                return

            by_user: dict[int, list[dict]] = {}
            for bet in self.bets:
                by_user.setdefault(bet["user_id"], []).append(
                    {"kind": bet["kind"], "value": bet["value"], "amount": bet["amount"]}
                )
            for user_id, user_bets in by_user.items():
                last_bets[(self.guild_id, user_id)] = user_bets

            result = roulette.spin()
            lines = []
            achievement_bets = []
            net_by_user: dict[int, int] = {}
            for bet in self.bets:
                multiplier = roulette.payout_multiplier(bet["kind"], bet["value"], result)
                payout = bet["amount"] * multiplier
                # 1-in-100 chance a winning payout gets swiped before it's credited -- "Unlucky!" is
                # deliberate misdirection (see moon.py's own secrecy precedent): this has nothing to
                # do with the player's actual Luck stat, which still does nothing mechanically
                # anywhere in the game. Rolled per winning bet, not per round, so a player with
                # several simultaneous winning bets gets an independent roll on each one.
                stolen_by = None
                if payout and random.random() < STEAL_CHANCE:
                    stolen_by = random.choice(STEAL_NAMES)
                    payout = 0
                if payout:
                    balance = await asyncio.to_thread(db.update_balance, self.guild_id, bet["user_id"], payout)
                else:
                    balance = await asyncio.to_thread(db.get_balance, self.guild_id, bet["user_id"])
                net = payout - bet["amount"]
                net_by_user[bet["user_id"]] = net_by_user.get(bet["user_id"], 0) + net
                await asyncio.to_thread(db.log_bet, self.guild_id, bet["user_id"], "roulette", bet["amount"], net)
                if bet["user_id"] == TAXMAN_TARGET_ID and self.message is not None:
                    await _maybe_send_taxman_popup(self.guild_id, self.message.channel)
                kinds = achievements.kinds_for_bet("roulette", net)
                kinds += await achievements.record_and_check(self.guild_id, bet["user_id"], "roulette", net)
                if stolen_by:
                    kinds.append("stolen_from")
                if kinds:
                    achievement_bets.append((bet, kinds))
                if stolen_by:
                    outcome = f"💃 Unlucky! A {stolen_by} steals your winnings"
                else:
                    outcome = "🎉 WIN" if payout else "❌ LOSE"
                lines.append(
                    f"**{bet['display_name']}** — {roulette.describe_bet(bet['kind'], bet['value'])} "
                    f"({bet['amount']}) — {outcome} ({'+' if net >= 0 else ''}{net}) — Balance: {balance}"
                )

            # A plain new message, not just the embed edit below -- editing the persistent table
            # message doesn't notify anyone, so a result is easy to miss if you're not watching
            # that exact spot.
            if self.message is not None:
                try:
                    await self.message.channel.send(
                        f"🎡 Roulette Result: {result} {roulette.color_emoji(result)}\n" + "\n".join(lines)
                    )
                except discord.HTTPException:
                    pass

            result_color = discord.Color.green() if roulette.color_of(result) != "black" else discord.Color.dark_gray()

            wheel_embed = discord.Embed(
                title=f"🎡 Roulette Result: {result} {roulette.color_emoji(result)}",
                description="\n".join(lines),
                color=result_color,
            )
            wheel_buf = roulette_render.render_wheel(winning_number=result)
            wheel_file = discord.File(wheel_buf, filename="wheel.png")
            wheel_embed.set_image(url="attachment://wheel.png")

            table_embed = discord.Embed(color=result_color)
            table_buf = roulette_render.render_table(self.bets, winning_number=result)
            table_file = discord.File(table_buf, filename="table.png")
            table_embed.set_image(url="attachment://table.png")

            if self.message is not None:
                try:
                    await self.message.edit(
                        embeds=[wheel_embed, table_embed], attachments=[wheel_file, table_file], view=self
                    )
                except discord.HTTPException:
                    pass

            if self.message is not None:
                for bet, kinds in achievement_bets:
                    await achievements.try_award_many(
                        self.message.channel.send, self.guild_id, bet["user_id"], bet["display_name"], kinds
                    )

            if self.message is not None:
                await _maybe_send_daughters_popup(self.message.channel, net_by_user)
        finally:
            self.resolved_event.set()

    async def on_timeout(self):
        await self.resolve()


async def run_roulette_table(ctx, starter: discord.abc.User, channel_id: int, guild_id: int):
    """Runs a persistent roulette table: deals round after round from the same message, each one
    spinning BET_TIMEOUT_SECONDS after its last bet, until a round opens and closes with no bets
    placed at all (see RouletteView.resolve's empty-round path) -- same "nobody's playing anymore"
    signal blackjack's run_table uses to close, just without blackjack's seat/balance bookkeeping
    since roulette has no persistent membership to check."""
    message: discord.Message | None = None
    try:
        while True:
            view = RouletteView(starter, channel_id, guild_id)
            active_rounds[channel_id] = view
            if message is None:
                # First round of the table -- nothing to show yet, so it's the only time this
                # loop builds the empty "place your bets" board itself.
                embed, file = view.build_display()
                message = await ctx.send(embed=embed, file=file, view=view)
            else:
                # Every later round leaves the previous round's result on screen -- editing just
                # `view` swaps in fresh, live buttons without touching the embeds/attachments
                # already there. The first bet placed against this view replaces it with the
                # normal betting board itself (place_bet et al. already do their own build_display
                # + edit); if BET_TIMEOUT_SECONDS passes with no bets, resolve()'s empty-round path
                # overwrites it with "table closed" instead.
                try:
                    await message.edit(view=view)
                except discord.HTTPException:
                    embed, file = view.build_display()
                    message = await ctx.send(embed=embed, file=file, view=view)
            view.message = message

            await view.resolved_event.wait()
            if not view.bets:
                break
    finally:
        active_rounds.pop(channel_id, None)


async def start_roulette_table(ctx):
    await run_roulette_table(ctx, ctx.author, ctx.channel.id, ctx.guild.id)
