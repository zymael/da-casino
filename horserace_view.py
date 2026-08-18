import asyncio

import discord

import db
import horserace
import horserace_render
from holdem_view import busy_players as holdem_busy_players

ROUND_SECONDS = 45
LEG_DELAY_SECONDS = 2.5

# channel_id -> HorseRaceView, so only one open race per channel
active_races: dict[int, "HorseRaceView"] = {}


class BetAmountModal(discord.ui.Modal):
    def __init__(self, round_view: "HorseRaceView", horse_index: int):
        super().__init__(title=f"Bet on {round_view.names[horse_index]}")
        self.round_view = round_view
        self.horse_index = horse_index
        self.amount_input = discord.ui.TextInput(label="Bet amount", placeholder="e.g. 50")
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.round_view.place_bet(interaction, self.horse_index, self.amount_input.value)


class HorseButton(discord.ui.Button):
    def __init__(self, horse_index: int, name: str, row: int):
        super().__init__(
            label=f"{name} ({horserace.describe_odds(horse_index)})",
            style=discord.ButtonStyle.secondary,
            row=row,
        )
        self.horse_index = horse_index

    async def callback(self, interaction: discord.Interaction):
        view: HorseRaceView = self.view
        if view.resolved:
            await interaction.response.send_message("This race already closed.", ephemeral=True)
            return
        await interaction.response.send_modal(BetAmountModal(view, self.horse_index))


class HorseRaceView(discord.ui.View):
    def __init__(
        self,
        starter: discord.abc.User,
        channel_id: int,
        guild_id: int,
        names: list[str],
        owners: list[int | None],
    ):
        super().__init__(timeout=ROUND_SECONDS)
        self.starter = starter
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.names = names
        self.owners = owners
        self.bets: list[dict] = []
        self.message: discord.Message | None = None
        self.resolved = False

        for i in range(len(horserace.HORSES)):
            self.add_item(HorseButton(i, names[i], row=i // 4))

    def build_display(self, footer: str | None = None) -> tuple[discord.Embed, discord.File]:
        embed = discord.Embed(
            title="🐎 Horse Racing — Place Your Bets!",
            description=f"Click a horse below to back it. Betting closes in {ROUND_SECONDS}s.",
            color=discord.Color.dark_green(),
        )
        if self.bets:
            lines = [
                f"**{bet['display_name']}** — {self.names[bet['horse_index']]} "
                f"({horserace.describe_odds(bet['horse_index'])}) — {bet['amount']} credits"
                for bet in self.bets
            ]
            embed.add_field(name="Current Bets", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Current Bets", value="No bets yet — be the first!", inline=False)
        embed.set_footer(text=footer or f"Started by {self.starter.display_name}")

        buf = horserace_render.render_track(names=self.names)
        file = discord.File(buf, filename="track.png")
        embed.set_image(url="attachment://track.png")
        return embed, file

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    async def place_bet(self, interaction: discord.Interaction, horse_index: int, raw_amount: str):
        if self.resolved:
            await interaction.response.send_message("This race already closed.", ephemeral=True)
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
        if amount > balance:
            await interaction.response.send_message(f"You only have **{balance}** credits.", ephemeral=True)
            return

        await asyncio.to_thread(db.update_balance, self.guild_id, interaction.user.id, -amount)
        self.bets.append(
            {
                "user_id": interaction.user.id,
                "display_name": interaction.user.display_name,
                "horse_index": horse_index,
                "amount": amount,
            }
        )
        await interaction.response.send_message(
            f"✅ Bet placed: {self.names[horse_index]} ({horserace.describe_odds(horse_index)}) "
            f"for **{amount}** credits.",
            ephemeral=True,
        )
        if self.message is not None:
            try:
                embed, file = self.build_display()
                await self.message.edit(embed=embed, attachments=[file])
            except discord.HTTPException:
                pass

    async def resolve(self):
        if self.resolved:
            return
        self.resolved = True
        self._disable_all()
        self.stop()
        active_races.pop(self.channel_id, None)

        if not self.bets:
            embed, file = self.build_display(footer="No bets were placed — race called off.")
            if self.message is not None:
                try:
                    await self.message.edit(embed=embed, attachments=[file], view=self)
                except discord.HTTPException:
                    pass
            return

        winner = horserace.pick_winner()
        frames = horserace.simulate_race(winner)
        final_max = max(frames[-1])

        if self.message is not None:
            for leg_index, positions in enumerate(frames):
                embed = discord.Embed(
                    title="🐎 They're off!" if leg_index == 0 else f"🐎 Leg {leg_index + 1} of {len(frames)}",
                    color=discord.Color.dark_green(),
                )
                buf = horserace_render.render_track(positions, final_max=final_max, names=self.names)
                file = discord.File(buf, filename="track.png")
                embed.set_image(url="attachment://track.png")
                try:
                    await self.message.edit(embed=embed, attachments=[file], view=self)
                except discord.HTTPException:
                    pass
                await asyncio.sleep(LEG_DELAY_SECONDS)

        winner_name = self.names[winner]
        lines = []
        pot = 0
        for bet in self.bets:
            if bet["horse_index"] == winner:
                pot += bet["amount"]
            multiplier = horserace.payout_multiplier(bet["horse_index"], winner)
            payout = int(bet["amount"] * multiplier)
            if payout:
                balance = await asyncio.to_thread(db.update_balance, self.guild_id, bet["user_id"], payout)
            else:
                balance = await asyncio.to_thread(db.get_balance, self.guild_id, bet["user_id"])
            net = payout - bet["amount"]
            await asyncio.to_thread(db.log_bet, self.guild_id, bet["user_id"], "horserace", bet["amount"], net)
            outcome = "🎉 WIN" if payout else "❌ LOSE"
            lines.append(
                f"**{bet['display_name']}** — {self.names[bet['horse_index']]} ({bet['amount']}) — "
                f"{outcome} ({'+' if net >= 0 else ''}{net}) — Balance: {balance}"
            )

        result_embed = discord.Embed(
            title=f"🏁 Winner: {winner_name} ({horserace.describe_odds(winner)})",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )

        owner_id = self.owners[winner]
        if owner_id is not None and pot > 0:
            cut = round(pot * horserace.OWNER_CUT_FRACTION)
            if cut > 0:
                await asyncio.to_thread(db.update_balance, self.guild_id, owner_id, cut)
                await asyncio.to_thread(db.log_bet, self.guild_id, owner_id, "horserace_owner", 0, cut)
                result_embed.add_field(
                    name="🐴 Owner Bonus",
                    value=f"<@{owner_id}> owns {winner_name} and earns **+{cut}** credits "
                    f"({int(horserace.OWNER_CUT_FRACTION * 100)}% of the {pot}-credit pot on this horse).",
                    inline=False,
                )

        buf = horserace_render.render_track(frames[-1], final_max=final_max, winner=winner, names=self.names)
        file = discord.File(buf, filename="track.png")
        result_embed.set_image(url="attachment://track.png")

        if self.message is not None:
            try:
                await self.message.edit(embed=result_embed, attachments=[file], view=self)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        await self.resolve()
