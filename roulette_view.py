import asyncio

import discord

import db
import roulette
import roulette_render
from holdem_view import busy_players as holdem_busy_players

ROUND_SECONDS = 45

# channel_id -> RouletteView, so only one open table per channel
active_rounds: dict[int, "RouletteView"] = {}


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


class RouletteView(discord.ui.View):
    def __init__(self, starter: discord.abc.User, channel_id: int):
        super().__init__(timeout=ROUND_SECONDS)
        self.starter = starter
        self.channel_id = channel_id
        self.bets: list[dict] = []
        self.message: discord.Message | None = None
        self.resolved = False

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
            lines = [
                f"**{bet['display_name']}** — {roulette.describe_bet(bet['kind'], bet['value'])} — {bet['amount']} credits"
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

    async def place_bet(self, interaction: discord.Interaction, kind: str, value: int | None, raw_amount: str):
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

        balance = await asyncio.to_thread(db.get_balance, interaction.user.id)
        if amount > balance:
            await interaction.response.send_message(f"You only have **{balance}** credits.", ephemeral=True)
            return

        await asyncio.to_thread(db.update_balance, interaction.user.id, -amount)
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
            f"✅ Bet placed: {roulette.describe_bet(kind, value)} for **{amount}** credits.", ephemeral=True
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

    @discord.ui.button(label="Spin Now", style=discord.ButtonStyle.gray, row=2)
    async def spin_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.starter.id:
            await interaction.response.send_message(
                "Only the person who started this round can spin early.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.resolve()

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

        result = roulette.spin()
        lines = []
        for bet in self.bets:
            multiplier = roulette.payout_multiplier(bet["kind"], bet["value"], result)
            payout = bet["amount"] * multiplier
            if payout:
                balance = await asyncio.to_thread(db.update_balance, bet["user_id"], payout)
            else:
                balance = await asyncio.to_thread(db.get_balance, bet["user_id"])
            net = payout - bet["amount"]
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

    async def on_timeout(self):
        await self.resolve()
