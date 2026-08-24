import asyncio

import discord

import achievements
import db
import jackpot
import slots
import slots_render
from holdem_view import busy_players as holdem_busy_players

BASE_LINE_BET = 1  # credits staked per active line at 1x multiplier
MULTIPLIERS = [1, 2, 5, 10, 20]
JACKPOT_GAME = "slots"


async def play_spin(guild_id: int, user_id: int, lines: int, multiplier: int) -> tuple[list[list[str]], list, int, int, int, int, int]:
    """Escrows the total bet, spins, and pays out.

    Returns (grid, winning_lines, total_payout, new_balance, net, jackpot_won, jackpot_pot),
    where winning_lines is a list of (line_index, symbols, payout) for each active line that
    hit, jackpot_won is >0 if a natural triple-7 claimed the progressive jackpot this spin (this
    on top of that line's normal payout, and it's already folded into total_payout), and
    jackpot_pot is the pot's size after this spin (its new, post-payout size if claimed).
    """
    bet_per_line = multiplier * BASE_LINE_BET
    bet = lines * bet_per_line
    await asyncio.to_thread(db.update_balance, guild_id, user_id, -bet)
    jackpot_pot = await asyncio.to_thread(jackpot.contribute, guild_id, JACKPOT_GAME, bet)

    grid = slots.spin_grid()
    winning_lines = []
    total_payout = 0
    hit_jackpot = False
    for i in range(lines):
        symbols = slots.line_symbols(grid, slots.PAYLINES[i])
        payout = int(bet_per_line * slots.payout_multiplier(symbols))
        if payout:
            winning_lines.append((i, symbols, payout))
            total_payout += payout
        if symbols == [slots.JACKPOT_SYMBOL] * 3:
            hit_jackpot = True

    jackpot_won = 0
    if hit_jackpot:
        jackpot_won = await asyncio.to_thread(jackpot.claim, guild_id, JACKPOT_GAME)
        jackpot_pot = jackpot.SEED
        total_payout += jackpot_won

    if total_payout:
        balance = await asyncio.to_thread(db.update_balance, guild_id, user_id, total_payout)
    else:
        balance = await asyncio.to_thread(db.get_balance, guild_id, user_id)
    net = total_payout - bet
    await asyncio.to_thread(db.log_bet, guild_id, user_id, "slots", bet, net)
    return grid, winning_lines, total_payout, balance, net, jackpot_won, jackpot_pot


class LineSelect(discord.ui.Select):
    def __init__(self, current: int):
        options = [
            discord.SelectOption(label=f"{n} Payline{'s' if n > 1 else ''}", value=str(n), default=(n == current))
            for n in range(1, slots.MAX_LINES + 1)
        ]
        super().__init__(placeholder="Paylines", options=options, row=0)

    def sync_default(self, lines: int):
        for option in self.options:
            option.default = int(option.value) == lines

    async def callback(self, interaction: discord.Interaction):
        view: SlotsView = self.view
        view.lines = int(self.values[0])
        view.sync_option_defaults()
        await interaction.response.edit_message(embed=view.build_bet_embed(), view=view)


class MultiplierSelect(discord.ui.Select):
    def __init__(self, current: int):
        options = [
            discord.SelectOption(label=f"{m}x Multiplier", value=str(m), default=(m == current))
            for m in MULTIPLIERS
        ]
        super().__init__(placeholder="Multiplier", options=options, row=1)

    def sync_default(self, multiplier: int):
        for option in self.options:
            option.default = int(option.value) == multiplier

    async def callback(self, interaction: discord.Interaction):
        view: SlotsView = self.view
        view.multiplier = int(self.values[0])
        view.sync_option_defaults()
        await interaction.response.edit_message(embed=view.build_bet_embed(), view=view)


class SpinButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🎰 Spin", style=discord.ButtonStyle.primary, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: SlotsView = self.view
        if view.author.id in holdem_busy_players:
            await interaction.response.send_message("Finish up whatever you're already doing first.", ephemeral=True)
            return
        if view.total_bet > view.balance:
            currency = db.get_currency_name(view.guild_id)
            await interaction.response.send_message(
                f"You only have **{view.balance}** {currency} — not enough for a **{view.total_bet}**-{currency} bet.",
                ephemeral=True,
            )
            return

        grid, winning_lines, total_payout, balance, net, jackpot_won, jackpot_pot = await play_spin(
            view.guild_id, view.author.id, view.lines, view.multiplier
        )
        view.balance = balance
        view.jackpot_pot = jackpot_pot
        view.sync_option_defaults()
        embed = view.build_result_embed(winning_lines, total_payout, jackpot_won)
        file = discord.File(slots_render.render_reels(grid, winning_lines), filename="slots.png")
        await interaction.response.edit_message(embed=embed, view=view, attachments=[file])
        if jackpot_won:
            currency = db.get_currency_name(view.guild_id)
            await interaction.followup.send(
                f"🎰💰 **JACKPOT!!!** {view.author.display_name} hit three {slots.JACKPOT_SYMBOL} and won the "
                f"progressive jackpot of **{jackpot_won}** {currency}!"
            )

        kinds = achievements.kinds_for_bet("slots", net)
        kinds += await achievements.record_and_check(view.guild_id, view.author.id, "slots", net)
        if jackpot_won:
            kinds.append("hit_jackpot")
        if kinds:
            await achievements.try_award_many(
                interaction.followup.send, view.guild_id, view.author.id, view.author.display_name, kinds
            )


class SlotsView(discord.ui.View):
    def __init__(self, author: discord.abc.User, balance: int, guild_id: int, jackpot_pot: int):
        super().__init__(timeout=90)
        self.author = author
        self.balance = balance
        self.guild_id = guild_id
        self.jackpot_pot = jackpot_pot
        self.lines = 1
        self.multiplier = 1
        self.message: discord.Message | None = None

        self.line_select = LineSelect(self.lines)
        self.multiplier_select = MultiplierSelect(self.multiplier)
        self.add_item(self.line_select)
        self.add_item(self.multiplier_select)
        self.add_item(SpinButton())

    def sync_option_defaults(self):
        """Keeps each dropdown's 'currently selected' flag in step with view state.

        Discord re-derives the visible selection from each option's `default` flag on
        every render, so without this the dropdown snaps back to its original choice
        (and can appear to reject re-selecting it) as soon as any other edit happens.
        """
        self.line_select.sync_default(self.lines)
        self.multiplier_select.sync_default(self.multiplier)

    @property
    def total_bet(self) -> int:
        return self.lines * self.multiplier * BASE_LINE_BET

    def build_bet_embed(self) -> discord.Embed:
        currency = db.get_currency_name(self.guild_id)
        embed = discord.Embed(
            title="🎰 Slots",
            description="Choose your paylines and multiplier, then hit **Spin**.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Paylines", value=f"{self.lines} / {slots.MAX_LINES}", inline=True)
        embed.add_field(name="Multiplier", value=f"{self.multiplier}x", inline=True)
        embed.add_field(name="Total Bet", value=f"{self.total_bet} {currency}", inline=True)
        embed.add_field(
            name="💰 Progressive Jackpot",
            value=f"{self.jackpot_pot} {currency} — three {slots.JACKPOT_SYMBOL} on any line wins it!",
            inline=False,
        )
        embed.set_image(url="attachment://slots.png")
        embed.set_footer(text=f"Balance: {self.balance} {currency}")
        return embed

    def build_initial_file(self) -> discord.File:
        return discord.File(slots_render.render_reels(None), filename="slots.png")

    def build_result_embed(self, winning_lines: list, total_payout: int, jackpot_won: int = 0) -> discord.Embed:
        currency = db.get_currency_name(self.guild_id)
        bet = self.total_bet
        net = total_payout - bet
        if jackpot_won:
            outcome, color = f"🎰💰 JACKPOT! +{net} {currency}", discord.Color.gold()
        elif not winning_lines:
            outcome, color = f"💥 No winning lines — you lose {bet} {currency}", discord.Color.red()
        elif net == 0:
            outcome, color = "🤝 Push — bet returned", discord.Color.greyple()
        elif net > 0:
            outcome, color = f"🎉 You win! (+{net} {currency})", discord.Color.green()
        else:
            outcome, color = f"😬 Partial win, net -{abs(net)} {currency}", discord.Color.orange()

        embed = discord.Embed(title="🎰 Slots", color=color)
        embed.set_image(url="attachment://slots.png")
        if winning_lines:
            lines_text = "\n".join(
                f"Line {i + 1} ({' '.join(symbols)}): +{payout}" for i, symbols, payout in winning_lines
            )
            embed.add_field(name="Winning Lines", value=lines_text, inline=False)
        embed.add_field(name="Paylines", value=str(self.lines), inline=True)
        embed.add_field(name="Multiplier", value=f"{self.multiplier}x", inline=True)
        embed.add_field(name="Total Bet", value=f"{bet} {currency}", inline=True)
        if jackpot_won:
            embed.add_field(name="💰 Jackpot Won", value=f"{jackpot_won} {currency}", inline=True)
        embed.add_field(name="💰 Progressive Jackpot", value=f"{self.jackpot_pot} {currency}", inline=True)
        embed.add_field(name="Result", value=outcome, inline=False)
        embed.set_footer(text=f"Balance: {self.balance} {currency}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
