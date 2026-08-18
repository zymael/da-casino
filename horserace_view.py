import asyncio

import discord

import db
import horserace
import horserace_render
from holdem_view import busy_players as holdem_busy_players

ROUND_SECONDS = 45
LEG_DELAY_SECONDS = 2.5

BET_KIND_LABELS = {
    "win": "Win (1st)",
    "place": "Place (top 2)",
    "show": "Show (top 3)",
    "across": "Across the Board",
}

# channel_id -> HorseRaceView, so only one open race per channel
active_races: dict[int, "HorseRaceView"] = {}


class BetKindSelect(discord.ui.Select):
    def __init__(self, current: str):
        options = [
            discord.SelectOption(label=label, value=kind, default=(kind == current))
            for kind, label in BET_KIND_LABELS.items()
        ]
        super().__init__(placeholder="Bet Type", options=options, row=0)

    def sync_default(self, kind: str):
        for option in self.options:
            option.default = option.value == kind

    async def callback(self, interaction: discord.Interaction):
        view: HorseRaceView = self.view
        view.bet_kind = self.values[0]
        self.sync_default(view.bet_kind)
        view.refresh_horse_button_labels()
        await interaction.response.edit_message(view=view)


class BetAmountModal(discord.ui.Modal):
    def __init__(self, round_view: "HorseRaceView", horse_index: int, kind: str):
        name = round_view.roster[horse_index]["name"]
        if kind == "across":
            # Odds for all three legs won't fit in Discord's 45-char modal title, so they're
            # only shown in the confirmation message after submit.
            title = f"Across the Board: {name}"
            amount_label = "Bet amount EACH WAY (x3 total)"
        else:
            odds = horserace.describe_odds(horse_index, round_view.probabilities[kind])
            title = f"{BET_KIND_LABELS[kind]}: {name} ({odds})"
            amount_label = "Bet amount"
        super().__init__(title=title)
        self.round_view = round_view
        self.horse_index = horse_index
        self.kind = kind
        self.amount_input = discord.ui.TextInput(label=amount_label, placeholder="e.g. 50")
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.round_view.place_bet(interaction, self.horse_index, self.kind, self.amount_input.value)


class HorseButton(discord.ui.Button):
    def __init__(self, horse_index: int, label: str, row: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.horse_index = horse_index

    async def callback(self, interaction: discord.Interaction):
        view: HorseRaceView = self.view
        if view.resolved:
            await interaction.response.send_message("This race already closed.", ephemeral=True)
            return
        await interaction.response.send_modal(BetAmountModal(view, self.horse_index, view.bet_kind))


class HorseRaceView(discord.ui.View):
    def __init__(
        self,
        starter: discord.abc.User,
        channel_id: int,
        guild_id: int,
        roster: dict[int, dict],
        race_field: list[int],
        probabilities: dict[str, dict[int, float]],
    ):
        super().__init__(timeout=ROUND_SECONDS)
        self.starter = starter
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.roster = roster
        self.race_field = race_field
        self.probabilities = probabilities
        self.bet_kind = "win"
        self.bets: list[dict] = []
        self.message: discord.Message | None = None
        self.resolved = False

        self.add_item(BetKindSelect(self.bet_kind))
        for position, horse_index in enumerate(race_field):
            label = self._horse_button_label(horse_index, self.bet_kind)
            self.add_item(HorseButton(horse_index, label, row=1 + position // 4))

    def _horse_button_label(self, horse_index: int, kind: str) -> str:
        name = self.roster[horse_index]["name"]
        if kind == "across":
            return f"{name} ({self._odds_str(horse_index, kind)})"
        return f"{name} ({horserace.describe_odds(horse_index, self.probabilities[kind])})"

    def refresh_horse_button_labels(self):
        for item in self.children:
            if isinstance(item, HorseButton):
                item.label = self._horse_button_label(item.horse_index, self.bet_kind)

    def _names(self) -> list[str]:
        return [self.roster[i]["name"] for i in self.race_field]

    def _colors(self) -> list[tuple[int, int, int, int]]:
        return [horserace.color_for_index(i) for i in self.race_field]

    def _odds_labels(self) -> list[str]:
        return [horserace.describe_odds(i, self.probabilities["win"]) for i in self.race_field]

    def _odds_str(self, horse_index: int, kind: str) -> str:
        if kind == "across":
            legs = ", ".join(
                f"{leg[0].upper()} {horserace.describe_odds(horse_index, self.probabilities[leg])}"
                for leg in horserace.ACROSS_LEGS
            )
            return legs
        return horserace.describe_odds(horse_index, self.probabilities[kind])

    def build_display(self, footer: str | None = None) -> tuple[discord.Embed, discord.File]:
        embed = discord.Embed(
            title="🐎 Horse Racing — Place Your Bets!",
            description=f"Click a horse below to back it. Betting closes in {ROUND_SECONDS}s.",
            color=discord.Color.dark_green(),
        )
        if self.bets:
            currency = db.get_currency_name(self.guild_id)
            lines = [
                f"**{bet['display_name']}** — {self.roster[bet['horse_index']]['name']} "
                f"({BET_KIND_LABELS[bet['kind']]}, {self._odds_str(bet['horse_index'], bet['kind'])}) — "
                f"{bet['amount'] * horserace.STAKE_MULTIPLIER[bet['kind']]} {currency}"
                for bet in self.bets
            ]
            embed.add_field(name="Current Bets", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Current Bets", value="No bets yet — be the first!", inline=False)
        embed.set_footer(text=footer or f"Started by {self.starter.display_name}")

        buf = horserace_render.render_track(self._names(), self._colors(), self._odds_labels())
        file = discord.File(buf, filename="track.png")
        embed.set_image(url="attachment://track.png")
        return embed, file

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    async def place_bet(self, interaction: discord.Interaction, horse_index: int, kind: str, raw_amount: str):
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

        stake = amount * horserace.STAKE_MULTIPLIER[kind]
        balance = await asyncio.to_thread(db.get_balance, self.guild_id, interaction.user.id)
        currency = db.get_currency_name(self.guild_id)
        if stake > balance:
            need = f"**{stake}** {currency} (**{amount}** each way, x3)" if kind == "across" else f"**{stake}** {currency}"
            await interaction.response.send_message(
                f"You only have **{balance}** {currency} — this bet needs {need}.", ephemeral=True
            )
            return

        await asyncio.to_thread(db.update_balance, self.guild_id, interaction.user.id, -stake)
        self.bets.append(
            {
                "user_id": interaction.user.id,
                "display_name": interaction.user.display_name,
                "horse_index": horse_index,
                "kind": kind,
                "amount": amount,
            }
        )
        odds = self._odds_str(horse_index, kind)
        name = self.roster[horse_index]["name"]
        stake_desc = f"**{amount}** {currency} each way (**{stake}** total)" if kind == "across" else f"**{amount}** {currency}"
        await interaction.response.send_message(
            f"✅ Bet placed: {BET_KIND_LABELS[kind]} — {name} ({odds}) for {stake_desc}.",
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

        stat_roster = [
            {
                "speed": self.roster[i]["speed"],
                "endurance": self.roster[i]["endurance"],
                "spirit": self.roster[i]["spirit"],
            }
            for i in self.race_field
        ]
        frames = horserace.simulate_race(stat_roster)
        order = horserace.finish_order_of(frames)
        winner_position = order[0]
        winner = self.race_field[winner_position]
        finish_order = [self.race_field[p] for p in order]
        rank_by_horse = {horse_index: rank for rank, horse_index in enumerate(finish_order, start=1)}
        final_max = max(frames[-1])

        names, colors, odds_labels = self._names(), self._colors(), self._odds_labels()
        if self.message is not None:
            for leg_index, positions in enumerate(frames):
                embed = discord.Embed(
                    title="🐎 They're off!" if leg_index == 0 else f"🐎 Leg {leg_index + 1} of {len(frames)}",
                    color=discord.Color.dark_green(),
                )
                buf = horserace_render.render_track(names, colors, odds_labels, positions, final_max=final_max)
                file = discord.File(buf, filename="track.png")
                embed.set_image(url="attachment://track.png")
                try:
                    await self.message.edit(embed=embed, attachments=[file], view=self)
                except discord.HTTPException:
                    pass
                await asyncio.sleep(LEG_DELAY_SECONDS)

        winner_name = self.roster[winner]["name"]
        lines = []
        pot = 0
        for bet in self.bets:
            kind = bet["kind"]
            stake = bet["amount"] * horserace.STAKE_MULTIPLIER[kind]
            if kind in ("win", "across") and bet["horse_index"] == winner:
                pot += bet["amount"]  # win-leg contribution only, even for an across bet
            rank = rank_by_horse.get(bet["horse_index"])
            if kind == "across":
                multiplier = horserace.payout_multiplier_across(bet["horse_index"], rank, self.probabilities)
            else:
                multiplier = horserace.payout_multiplier(
                    bet["horse_index"], rank, horserace.BET_KIND_THRESHOLDS[kind], self.probabilities[kind]
                )
            payout = int(bet["amount"] * multiplier)
            if payout:
                balance = await asyncio.to_thread(db.update_balance, self.guild_id, bet["user_id"], payout)
            else:
                balance = await asyncio.to_thread(db.get_balance, self.guild_id, bet["user_id"])
            net = payout - stake
            await asyncio.to_thread(db.log_bet, self.guild_id, bet["user_id"], "horserace", stake, net)
            outcome = "🎉 WIN" if payout else "❌ LOSE"
            lines.append(
                f"**{bet['display_name']}** — {self.roster[bet['horse_index']]['name']} "
                f"({BET_KIND_LABELS[kind]}, {stake}) — "
                f"{outcome} ({'+' if net >= 0 else ''}{net}) — Balance: {balance}"
            )

        result_embed = discord.Embed(
            title=f"🏁 Winner: {winner_name} ({horserace.describe_odds(winner, self.probabilities['win'])})",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await asyncio.to_thread(
            db.record_race_result, self.guild_id, finish_order, horserace.RACE_AGE_INTERVAL
        )

        owner_id = self.roster[winner]["owner_id"]
        if owner_id is not None and pot > 0:
            cut = round(pot * horserace.OWNER_CUT_FRACTION)
            if cut > 0:
                await asyncio.to_thread(db.update_balance, self.guild_id, owner_id, cut)
                await asyncio.to_thread(db.log_bet, self.guild_id, owner_id, "horserace_owner", 0, cut)
                currency = db.get_currency_name(self.guild_id)
                result_embed.add_field(
                    name="🐴 Owner Bonus",
                    value=f"<@{owner_id}> owns {winner_name} and earns **+{cut}** {currency} "
                    f"({int(horserace.OWNER_CUT_FRACTION * 100)}% of the {pot}-{currency} pot on this horse).",
                    inline=False,
                )

        buf = horserace_render.render_track(
            names, colors, odds_labels, frames[-1], final_max=final_max, winner=winner_position
        )
        file = discord.File(buf, filename="track.png")
        result_embed.set_image(url="attachment://track.png")

        if self.message is not None:
            try:
                await self.message.edit(embed=result_embed, attachments=[file], view=self)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        await self.resolve()
