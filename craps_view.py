"""A persistent craps table, same shape as roulette_view.py: one shared board per channel, each
round rolls BET_TIMEOUT_SECONDS after its last bet, then the table reposts itself at the bottom of
the channel for the next round.

The one real difference from roulette is carry-over. Pass Line / Don't Pass / Place / Hardway
bets ride across rolls until craps.settle() decides them, so each round's view is seeded with the
previous round's still-riding bets and point. A round with riding bets rolls even if nobody adds
anything new (those bets need a decision); the table only closes once a round times out with
nothing on the felt at all.
"""

import asyncio
import io

import discord

import achievements
import craps
import craps_render
import db
import necro_view
from holdem_view import busy_players as holdem_busy_players
from roulette_view import ROY_GIF

BET_TIMEOUT_SECONDS = 30

# channel_id -> the currently-open round's CrapsView, so only one table per channel.
active_rounds: dict[int, "CrapsView"] = {}

# (guild_id, user_id) -> the bets that user placed in their last round with any new bets, for
# Repeat Bets / LET IT RIDE. Riding bets carried over from earlier rounds aren't re-saved.
last_bets: dict[tuple[int, int], list[dict]] = {}


class BetAmountModal(discord.ui.Modal):
    def __init__(self, round_view: "CrapsView", kind: str, value: int | None):
        super().__init__(title=f"Bet on {craps.describe_bet(kind, value)}")
        self.round_view = round_view
        self.kind = kind
        self.value = value
        self.amount_input = discord.ui.TextInput(label="Bet amount", placeholder="e.g. 50")
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message("Bet amount must be a whole number.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("Bet amount must be positive.", ephemeral=True)
            return
        await self.round_view.place_bets(interaction, [{"kind": self.kind, "value": self.value, "amount": amount}])


class NumberBetSelect(discord.ui.Select):
    """Place and Hardway bets, the craps counterpart of roulette's Tower Bet shape picker."""

    def __init__(self):
        options = [
            discord.SelectOption(
                label=f"Place {n}", value=f"place:{n}",
                description=f"Wins if {n} rolls before a 7. Pays {craps.PLACE_PAYOUT[n][0]} to {craps.PLACE_PAYOUT[n][1]}.",
            )
            for n in craps.POINT_NUMBERS
        ] + [
            discord.SelectOption(
                label=f"Hard {n} ({n // 2}-{n // 2})", value=f"hard:{n}",
                description=f"Wins on {n // 2}-{n // 2} before a 7 or an easy {n}. Pays {craps.HARD_PAYOUT[n][0]} to 1.",
            )
            for n in craps.HARD_NUMBERS
        ]
        super().__init__(placeholder="Place / Hardway bet: pick a number...", options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: "CrapsView" = self.view
        kind, raw = self.values[0].split(":")
        await view.open_amount_modal(interaction, kind, int(raw))


class CrapsView(discord.ui.View):
    def __init__(
        self, starter: discord.abc.User, channel_id: int, guild_id: int,
        point: int | None = None, riding: list[dict] | None = None,
    ):
        super().__init__(timeout=BET_TIMEOUT_SECONDS)
        self.starter = starter
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.point = point
        # Everything on the felt this roll: carried-over riding bets first, then new ones.
        self.bets: list[dict] = list(riding or [])
        self.new_bets: list[dict] = []
        self.message: discord.Message | None = None
        self.resolved = False
        # See roulette_view.RouletteView: run_craps_table waits on this, not view.wait(), so it
        # doesn't repost until settlement has actually finished.
        self.resolved_event = asyncio.Event()
        self.result_embeds: list[discord.Embed] = []
        self.result_images: dict[str, bytes] = {}
        self.add_item(NumberBetSelect())
        if point is not None:
            self.bet_pass.disabled = True
            self.bet_dontpass.disabled = True

    def build_display(self, footer: str | None = None) -> tuple[discord.Embed, discord.File]:
        title = "🎲 Craps: Come-out roll!" if self.point is None else f"🎲 Craps: The point is {self.point}"
        embed = discord.Embed(
            title=title,
            description=f"Click a bet below to get in on this roll. The dice fly {BET_TIMEOUT_SECONDS}s after the last bet.",
            color=discord.Color.dark_green(),
        )
        if self.bets:
            currency = db.get_currency_name(self.guild_id)
            lines = [
                f"**{b['display_name']}**: {craps.describe_bet(b['kind'], b['value'])}, {b['amount']} {currency}"
                for b in self.bets
            ]
            embed.add_field(name="On the Table", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="On the Table", value="No bets yet. Be the first!", inline=False)
        embed.set_footer(text=footer or f"Started by {self.starter.display_name}")
        buf = craps_render.render_table(self.bets, self.point)
        file = discord.File(buf, filename="table.png")
        embed.set_image(url="attachment://table.png")
        return embed, file

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    async def _redraw(self):
        if self.message is None:
            return
        try:
            embed, file = self.build_display()
            await self.message.edit(embed=embed, attachments=[file])
        except discord.HTTPException:
            pass

    async def _precheck(self, interaction: discord.Interaction) -> bool:
        if self.resolved:
            await interaction.response.send_message("This roll already happened.", ephemeral=True)
            return False
        if interaction.user.id in holdem_busy_players:
            await interaction.response.send_message("Finish up whatever you're already doing first.", ephemeral=True)
            return False
        return True

    async def open_amount_modal(self, interaction: discord.Interaction, kind: str, value: int | None):
        if self.resolved:
            await interaction.response.send_message("This roll already happened.", ephemeral=True)
            return
        reason = craps.validate(kind, value, self.point)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        await interaction.response.send_modal(BetAmountModal(self, kind, value))

    async def place_bets(self, interaction: discord.Interaction, wanted: list[dict], *, prefix: str = "✅") -> bool:
        """Charges and places every bet in `wanted` ({kind, value, amount}), all or nothing.
        Bets that aren't legal right now (a Pass Line once the point is on) are dropped with a note
        rather than failing the whole batch, so Repeat Bets still works mid-hand."""
        if not await self._precheck(interaction):
            return False
        skipped = [b for b in wanted if craps.validate(b["kind"], b["value"], self.point)]
        bets = [b for b in wanted if b not in skipped and b["amount"] > 0]
        if not bets:
            reason = craps.validate(skipped[0]["kind"], skipped[0]["value"], self.point) if skipped else "Nothing to bet."
            await interaction.response.send_message(reason, ephemeral=True)
            return False

        total = sum(b["amount"] for b in bets)
        balance = await asyncio.to_thread(db.get_balance, self.guild_id, interaction.user.id)
        currency = db.get_currency_name(self.guild_id)
        if total > balance:
            await interaction.response.send_message(
                f"That's **{total}** {currency}, but you only have **{balance}** {currency}.", ephemeral=True
            )
            return False

        await asyncio.to_thread(db.update_balance, self.guild_id, interaction.user.id, -total)
        for b in bets:
            placed = {
                "user_id": interaction.user.id,
                "display_name": interaction.user.display_name,
                "kind": b["kind"],
                "value": b["value"],
                "amount": b["amount"],
            }
            self.bets.append(placed)
            self.new_bets.append(placed)

        desc = "; ".join(f"{craps.describe_bet(b['kind'], b['value'])} ({b['amount']})" for b in bets)
        msg = f"{prefix} Bet placed: {desc}, **{total}** {currency} total."
        if skipped:
            msg += f"\nSkipped (come-out only): {', '.join(craps.describe_bet(b['kind'], b['value']) for b in skipped)}."
        await interaction.response.send_message(msg, ephemeral=True)
        self.timeout = BET_TIMEOUT_SECONDS
        await self._redraw()
        return True

    async def repeat_bets(self, interaction: discord.Interaction):
        saved = last_bets.get((self.guild_id, interaction.user.id))
        if not saved:
            await interaction.response.send_message("You don't have any previous bets to repeat.", ephemeral=True)
            return
        await self.place_bets(interaction, [dict(b) for b in saved])

    async def all_in(self, interaction: discord.Interaction):
        """Repeat Bets scaled to the player's whole balance, same as roulette's LET IT RIDE. Bets
        that aren't legal right now are dropped before scaling so the full balance still goes in."""
        saved = last_bets.get((self.guild_id, interaction.user.id))
        if not saved:
            await interaction.response.send_message("You don't have any previous bets to repeat.", ephemeral=True)
            return
        legal = [b for b in saved if not craps.validate(b["kind"], b["value"], self.point)]
        if not legal:
            await interaction.response.send_message(
                craps.validate(saved[0]["kind"], saved[0]["value"], self.point), ephemeral=True
            )
            return
        balance = await asyncio.to_thread(db.get_balance, self.guild_id, interaction.user.id)
        if balance <= 0:
            currency = db.get_currency_name(self.guild_id)
            await interaction.response.send_message(f"You don't have any {currency} to go all in with.", ephemeral=True)
            return
        total_prev = sum(b["amount"] for b in legal)
        scaled, running = [], 0
        for i, b in enumerate(legal):
            amount = balance - running if i == len(legal) - 1 else (b["amount"] * balance) // total_prev
            running += amount
            scaled.append({**b, "amount": amount})
        if await self.place_bets(interaction, scaled, prefix="😈 LET IT RIDE!") and self.message is not None:
            try:
                roy_embed = discord.Embed(color=discord.Color.gold())
                roy_embed.set_image(url=ROY_GIF)
                await self.message.channel.send(embed=roy_embed)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Pass Line", style=discord.ButtonStyle.success, row=0)
    async def bet_pass(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_amount_modal(interaction, "pass", None)

    @discord.ui.button(label="Don't Pass", style=discord.ButtonStyle.danger, row=0)
    async def bet_dontpass(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_amount_modal(interaction, "dontpass", None)

    @discord.ui.button(label="Field", style=discord.ButtonStyle.primary, row=0)
    async def bet_field(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_amount_modal(interaction, "field", None)

    @discord.ui.button(label="Any 7", style=discord.ButtonStyle.danger, row=0)
    async def bet_any7(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_amount_modal(interaction, "any7", None)

    @discord.ui.button(label="Any Craps", style=discord.ButtonStyle.secondary, row=1)
    async def bet_anycraps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_amount_modal(interaction, "anycraps", None)

    @discord.ui.button(label="Yo (11)", style=discord.ButtonStyle.primary, row=1)
    async def bet_yo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_amount_modal(interaction, "yo", None)

    @discord.ui.button(label="Snake Eyes (2)", style=discord.ButtonStyle.secondary, row=1)
    async def bet_snake(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_amount_modal(interaction, "snake", None)

    @discord.ui.button(label="Boxcars (12)", style=discord.ButtonStyle.secondary, row=1)
    async def bet_boxcars(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_amount_modal(interaction, "boxcars", None)

    @discord.ui.button(label="Repeat Bets", style=discord.ButtonStyle.secondary, row=3)
    async def bet_repeat(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.repeat_bets(interaction)

    @discord.ui.button(label="😈 LET IT RIDE", style=discord.ButtonStyle.danger, row=3)
    async def bet_all_in(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.all_in(interaction)

    async def resolve(self):
        """Rolls the dice and settles whatever this roll decides. Undecided multi-roll bets are
        left in self.bets (with the new self.point) for run_craps_table to carry into the next
        round. Always sets resolved_event, even on the empty-table path."""
        if self.resolved:
            return
        self.resolved = True
        self._disable_all()
        self.stop()
        try:
            if not self.bets:
                embed, file = self.build_display(footer="No bets on the felt. Table closed.")
                if self.message is not None:
                    try:
                        await self.message.edit(embed=embed, attachments=[file], view=self)
                    except discord.HTTPException:
                        pass
                return

            by_user: dict[int, list[dict]] = {}
            for bet in self.new_bets:
                by_user.setdefault(bet["user_id"], []).append(
                    {"kind": bet["kind"], "value": bet["value"], "amount": bet["amount"]}
                )
            for user_id, user_bets in by_user.items():
                last_bets[(self.guild_id, user_id)] = user_bets

            d1, d2 = craps.roll()
            point_before = self.point
            all_bets = self.bets
            riding: list[dict] = []
            lines = []
            achievement_bets = []
            net_by_user: dict[int, int] = {}
            for bet in all_bets:
                payout = craps.settle(bet["kind"], bet["value"], bet["amount"], d1, d2, point_before)
                if payout is None:
                    riding.append(bet)
                    continue
                if payout:
                    balance = await asyncio.to_thread(db.update_balance, self.guild_id, bet["user_id"], payout)
                else:
                    balance = await asyncio.to_thread(db.get_balance, self.guild_id, bet["user_id"])
                net = payout - bet["amount"]
                net_by_user[bet["user_id"]] = net_by_user.get(bet["user_id"], 0) + net
                await asyncio.to_thread(db.log_bet, self.guild_id, bet["user_id"], "craps", bet["amount"], net)
                kinds = achievements.kinds_for_bet("craps", net)
                kinds += await achievements.record_and_check(self.guild_id, bet["user_id"], "craps", net)
                if kinds:
                    achievement_bets.append((bet, kinds))
                outcome = "🎉 WIN" if net > 0 else ("➖ PUSH" if net == 0 else "❌ LOSE")
                lines.append(
                    f"**{bet['display_name']}**: {craps.describe_bet(bet['kind'], bet['value'])} "
                    f"({bet['amount']}), {outcome} ({'+' if net >= 0 else ''}{net}), Balance: {balance}"
                )

            self.point = craps.next_point(point_before, d1, d2)
            self.bets = riding
            if riding:
                lines.append("")
                lines.append(
                    "🔁 Still riding: "
                    + ", ".join(f"{b['display_name']} {craps.describe_bet(b['kind'], b['value'])}" for b in riding)
                )

            headline = f"🎲 Craps Roll: {craps.dice_text(d1, d2)}. {craps.roll_headline(point_before, d1, d2)}"
            body = "\n".join(lines) if lines else "Nothing decided this roll."
            if self.message is not None:
                try:
                    await self.message.channel.send(f"{headline}\n{body}"[:2000])
                except discord.HTTPException:
                    pass

            total = d1 + d2
            result_color = discord.Color.red() if (point_before is not None and total == 7) else discord.Color.green()
            dice_embed = discord.Embed(title=headline, description=body[:4096], color=result_color)
            dice_buf = craps_render.render_dice(d1, d2)
            dice_file = discord.File(dice_buf, filename="dice.png")
            dice_embed.set_image(url="attachment://dice.png")

            table_embed = discord.Embed(color=result_color)
            table_buf = craps_render.render_table(all_bets, self.point, dice=(d1, d2), point_before=point_before)
            table_file = discord.File(table_buf, filename="table.png")
            table_embed.set_image(url="attachment://table.png")
            self.result_embeds = [dice_embed, table_embed]
            self.result_images = {"dice.png": dice_buf.getvalue(), "table.png": table_buf.getvalue()}

            if self.message is not None:
                try:
                    await self.message.edit(
                        embeds=[dice_embed, table_embed], attachments=[dice_file, table_file], view=self
                    )
                except discord.HTTPException:
                    pass
                for bet, kinds in achievement_bets:
                    await achievements.try_award_many(
                        self.message.channel.send, self.guild_id, bet["user_id"], bet["display_name"], kinds
                    )
                await necro_view.maybe_haunt_winners(self.message.channel.send, self.guild_id, net_by_user)
        finally:
            self.resolved_event.set()

    async def on_timeout(self):
        await self.resolve()


async def run_craps_table(ctx, starter: discord.abc.User, channel_id: int, guild_id: int):
    """Round after round on a freshly posted message (see roulette_view.run_roulette_table), each
    seeded with the last round's point and riding bets. Closes when a round ends with nothing on
    the felt."""
    message: discord.Message | None = None
    previous: CrapsView | None = None
    try:
        while True:
            view = CrapsView(
                starter, channel_id, guild_id,
                point=previous.point if previous else None,
                riding=previous.bets if previous else None,
            )
            active_rounds[channel_id] = view
            if message is not None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            if previous is not None and previous.result_embeds:
                files = [discord.File(io.BytesIO(data), filename=name) for name, data in previous.result_images.items()]
                message = await ctx.send(embeds=previous.result_embeds, files=files, view=view)
            else:
                embed, file = view.build_display()
                message = await ctx.send(embed=embed, file=file, view=view)
            view.message = message
            previous = view

            await view.resolved_event.wait()
            if not view.result_embeds:
                break  # the empty-table path: nothing rolled
    finally:
        active_rounds.pop(channel_id, None)


async def start_craps_table(ctx):
    await run_craps_table(ctx, ctx.author, ctx.channel.id, ctx.guild.id)
