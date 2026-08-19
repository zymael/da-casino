import asyncio

import discord

import achievements
import db
import roulette
import roulette_render
from holdem_view import busy_players as holdem_busy_players

ROUND_SECONDS = 45

# channel_id -> RouletteView, so only one open table per channel
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
        super().__init__(timeout=ROUND_SECONDS)
        self.starter = starter
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.bets: list[dict] = []
        self.message: discord.Message | None = None
        self.resolved = False
        self.add_item(TowerShapeSelect())

    def build_display(
        self, footer: str | None = None, winning_number: int | None = None
    ) -> tuple[discord.Embed, discord.File]:
        embed = discord.Embed(
            title="🎡 Roulette — Place Your Bets!" if winning_number is None else "🎡 Roulette",
            description=f"Click a bet type below to join this spin. Betting closes in {ROUND_SECONDS}s."
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
            await interaction.response.send_message("Finish your poker hand first!", ephemeral=True)
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
            await interaction.response.send_message("Finish your poker hand first!", ephemeral=True)
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
            await interaction.response.send_message("Finish your poker hand first!", ephemeral=True)
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
        if self.message is not None:
            try:
                embed, file = self.build_display()
                await self.message.edit(embed=embed, attachments=[file])
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

    async def resolve(self):
        if self.resolved:
            return
        self.resolved = True
        self._disable_all()
        self.stop()
        active_rounds.pop(self.channel_id, None)

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
        for bet in self.bets:
            multiplier = roulette.payout_multiplier(bet["kind"], bet["value"], result)
            payout = bet["amount"] * multiplier
            if payout:
                balance = await asyncio.to_thread(db.update_balance, self.guild_id, bet["user_id"], payout)
            else:
                balance = await asyncio.to_thread(db.get_balance, self.guild_id, bet["user_id"])
            net = payout - bet["amount"]
            await asyncio.to_thread(db.log_bet, self.guild_id, bet["user_id"], "roulette", bet["amount"], net)
            kinds = achievements.kinds_for_bet("roulette", net)
            if kinds:
                achievement_bets.append((bet, kinds))
            outcome = "🎉 WIN" if payout else "❌ LOSE"
            lines.append(
                f"**{bet['display_name']}** — {roulette.describe_bet(bet['kind'], bet['value'])} "
                f"({bet['amount']}) — {outcome} ({'+' if net >= 0 else ''}{net}) — Balance: {balance}"
            )

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

    async def on_timeout(self):
        await self.resolve()
