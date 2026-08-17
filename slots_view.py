import asyncio

import discord

import db
import slots
from holdem_view import busy_players as holdem_busy_players


async def play_spin(user_id: int, bet: int) -> tuple[list[str], int, int]:
    """Escrows the bet, spins, and pays out. Returns (reels, payout, new_balance)."""
    await asyncio.to_thread(db.update_balance, user_id, -bet)
    reels = slots.spin()
    payout = int(bet * slots.payout_multiplier(reels))
    if payout:
        balance = await asyncio.to_thread(db.update_balance, user_id, payout)
    else:
        balance = await asyncio.to_thread(db.get_balance, user_id)
    return reels, payout, balance


class SlotsView(discord.ui.View):
    def __init__(self, author: discord.abc.User, bet: int):
        super().__init__(timeout=60)
        self.author = author
        self.bet = bet
        self.message: discord.Message | None = None

    def build_embed(self, reels: list[str], payout: int, balance: int) -> discord.Embed:
        net = payout - self.bet
        if payout == 0:
            result, color = f"💥 No match — you lose {self.bet} credits", discord.Color.red()
        elif payout == self.bet:
            result, color = "🤝 Push — bet returned", discord.Color.greyple()
        else:
            result, color = f"🎉 You win! (+{net} credits)", discord.Color.green()

        embed = discord.Embed(title="🎰 Slots", color=color)
        embed.add_field(name="Reels", value=" | ".join(reels), inline=False)
        embed.add_field(name="Bet", value=f"{self.bet} credits", inline=True)
        embed.add_field(name="Result", value=result, inline=False)
        embed.set_footer(text=f"Balance: {balance} credits")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🎰 Spin Again", style=discord.ButtonStyle.primary)
    async def spin_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.author.id in holdem_busy_players:
            await interaction.response.send_message("Finish your poker hand first!", ephemeral=True)
            return
        balance = await asyncio.to_thread(db.get_balance, self.author.id)
        if balance < self.bet:
            await interaction.response.send_message(
                f"You only have **{balance}** credits — not enough to spin again.", ephemeral=True
            )
            return

        reels, payout, new_balance = await play_spin(self.author.id, self.bet)
        embed = self.build_embed(reels, payout, new_balance)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
