import asyncio

import discord

import db
import slots
import slots_render
from holdem_view import busy_players as holdem_busy_players

BASE_LINE_BET = 1  # credits staked per active line at 1x multiplier
MULTIPLIERS = [1, 2, 5, 10, 20]


async def play_spin(user_id: int, lines: int, multiplier: int) -> tuple[list[list[str]], list, int, int]:
    """Escrows the total bet, spins, and pays out.

    Returns (grid, winning_lines, total_payout, new_balance), where winning_lines is a
    list of (line_index, symbols, payout) for each active line that hit.
    """
    bet_per_line = multiplier * BASE_LINE_BET
    bet = lines * bet_per_line
    await asyncio.to_thread(db.update_balance, user_id, -bet)

    grid = slots.spin_grid()
    winning_lines = []
    total_payout = 0
    for i in range(lines):
        symbols = slots.line_symbols(grid, slots.PAYLINES[i])
        payout = int(bet_per_line * slots.payout_multiplier(symbols))
        if payout:
            winning_lines.append((i, symbols, payout))
            total_payout += payout

    if total_payout:
        balance = await asyncio.to_thread(db.update_balance, user_id, total_payout)
    else:
        balance = await asyncio.to_thread(db.get_balance, user_id)
    return grid, winning_lines, total_payout, balance


class LineSelect(discord.ui.Select):
    def __init__(self, current: int):
        options = [
            discord.SelectOption(label=f"{n} Payline{'s' if n > 1 else ''}", value=str(n), default=(n == current))
            for n in range(1, slots.MAX_LINES + 1)
        ]
        super().__init__(placeholder="Paylines", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: SlotsView = self.view
        view.lines = int(self.values[0])
        await interaction.response.edit_message(embed=view.build_bet_embed(), view=view)


class MultiplierSelect(discord.ui.Select):
    def __init__(self, current: int):
        options = [
            discord.SelectOption(label=f"{m}x Multiplier", value=str(m), default=(m == current))
            for m in MULTIPLIERS
        ]
        super().__init__(placeholder="Multiplier", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: SlotsView = self.view
        view.multiplier = int(self.values[0])
        await interaction.response.edit_message(embed=view.build_bet_embed(), view=view)


class SpinButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🎰 Spin", style=discord.ButtonStyle.primary, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: SlotsView = self.view
        if view.author.id in holdem_busy_players:
            await interaction.response.send_message("Finish your poker hand first!", ephemeral=True)
            return
        if view.total_bet > view.balance:
            await interaction.response.send_message(
                f"You only have **{view.balance}** credits — not enough for a **{view.total_bet}**-credit bet.",
                ephemeral=True,
            )
            return

        grid, winning_lines, total_payout, balance = await play_spin(view.author.id, view.lines, view.multiplier)
        view.balance = balance
        embed = view.build_result_embed(winning_lines, total_payout)
        file = discord.File(slots_render.render_reels(grid, winning_lines), filename="slots.png")
        await interaction.response.edit_message(embed=embed, view=view, attachments=[file])


class SlotsView(discord.ui.View):
    def __init__(self, author: discord.abc.User, balance: int):
        super().__init__(timeout=90)
        self.author = author
        self.balance = balance
        self.lines = 1
        self.multiplier = 1
        self.message: discord.Message | None = None

        self.add_item(LineSelect(self.lines))
        self.add_item(MultiplierSelect(self.multiplier))
        self.add_item(SpinButton())

    @property
    def total_bet(self) -> int:
        return self.lines * self.multiplier * BASE_LINE_BET

    def build_bet_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎰 Slots",
            description="Choose your paylines and multiplier, then hit **Spin**.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Paylines", value=f"{self.lines} / {slots.MAX_LINES}", inline=True)
        embed.add_field(name="Multiplier", value=f"{self.multiplier}x", inline=True)
        embed.add_field(name="Total Bet", value=f"{self.total_bet} credits", inline=True)
        embed.set_image(url="attachment://slots.png")
        embed.set_footer(text=f"Balance: {self.balance} credits")
        return embed

    def build_initial_file(self) -> discord.File:
        return discord.File(slots_render.render_reels(None), filename="slots.png")

    def build_result_embed(self, winning_lines: list, total_payout: int) -> discord.Embed:
        bet = self.total_bet
        net = total_payout - bet
        if not winning_lines:
            outcome, color = f"💥 No winning lines — you lose {bet} credits", discord.Color.red()
        elif net == 0:
            outcome, color = "🤝 Push — bet returned", discord.Color.greyple()
        elif net > 0:
            outcome, color = f"🎉 You win! (+{net} credits)", discord.Color.green()
        else:
            outcome, color = f"😬 Partial win, net -{abs(net)} credits", discord.Color.orange()

        embed = discord.Embed(title="🎰 Slots", color=color)
        embed.set_image(url="attachment://slots.png")
        if winning_lines:
            lines_text = "\n".join(
                f"Line {i + 1} ({' '.join(symbols)}): +{payout}" for i, symbols, payout in winning_lines
            )
            embed.add_field(name="Winning Lines", value=lines_text, inline=False)
        embed.add_field(name="Paylines", value=str(self.lines), inline=True)
        embed.add_field(name="Multiplier", value=f"{self.multiplier}x", inline=True)
        embed.add_field(name="Total Bet", value=f"{bet} credits", inline=True)
        embed.add_field(name="Result", value=outcome, inline=False)
        embed.set_footer(text=f"Balance: {self.balance} credits")
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
