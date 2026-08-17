import asyncio

import discord

import cards_render
import db
from game import Deck, hand_value, is_blackjack
from holdem_view import busy_players as holdem_busy_players

OUTCOME_LABELS = {
    "blackjack": "🂡 Blackjack! You win",
    "win": "🎉 You win!",
    "push": "🤝 Push — bet returned",
    "lose": "💥 You lose",
}
OUTCOME_PAYOUT_MULTIPLIERS = {"blackjack": 2.5, "win": 2, "push": 1, "lose": 0}


class BlackjackView(discord.ui.View):
    def __init__(self, author: discord.abc.User, bet: int, guild_id: int):
        super().__init__(timeout=120)
        self.author = author
        self.bet = bet
        self.guild_id = guild_id
        self.deck = Deck()
        self.player = [self.deck.draw(), self.deck.draw()]
        self.dealer = [self.deck.draw(), self.deck.draw()]
        self.message: discord.Message | None = None
        self.resolved = False

    def build_display(
        self, reveal_dealer: bool = False, result_text: str | None = None, balance: int | None = None
    ) -> tuple[list[discord.Embed], list[discord.File]]:
        files = []

        dealer_buf = cards_render.render_hand(self.dealer, hide_first=not reveal_dealer)
        files.append(discord.File(dealer_buf, filename="dealer.png"))
        dealer_embed = discord.Embed(title="🃏 Blackjack — Dealer", color=discord.Color.gold())
        dealer_embed.description = f"Value: {hand_value(self.dealer)}" if reveal_dealer else "Value: ?"
        dealer_embed.set_image(url="attachment://dealer.png")

        player_buf = cards_render.render_hand(self.player)
        files.append(discord.File(player_buf, filename="player.png"))
        player_embed = discord.Embed(title=self.author.display_name, color=discord.Color.gold())
        player_embed.description = f"Value: {hand_value(self.player)}"
        player_embed.set_image(url="attachment://player.png")
        player_embed.add_field(name="Bet", value=f"{self.bet} credits", inline=True)
        if result_text:
            player_embed.add_field(name="Result", value=result_text, inline=False)
            player_embed.color = dealer_embed.color = discord.Color.green() if "win" in result_text.lower() else (
                discord.Color.red() if "lose" in result_text.lower() else discord.Color.greyple()
            )
        if balance is not None:
            player_embed.set_footer(text=f"Balance: {balance} credits")

        return [dealer_embed, player_embed], files

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return False
        return True

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    async def _settle(self, outcome: str) -> tuple[list[discord.Embed], list[discord.File]]:
        self.resolved = True
        self._disable_all()
        self.stop()

        payout = int(self.bet * OUTCOME_PAYOUT_MULTIPLIERS[outcome])
        if payout:
            balance = await asyncio.to_thread(db.update_balance, self.guild_id, self.author.id, payout)
        else:
            balance = await asyncio.to_thread(db.get_balance, self.guild_id, self.author.id)

        net = payout - self.bet
        text = f"{OUTCOME_LABELS[outcome]} ({'+' if net >= 0 else ''}{net} credits)"
        return self.build_display(reveal_dealer=True, result_text=text, balance=balance)

    async def _play_dealer_and_settle(self) -> tuple[list[discord.Embed], list[discord.File]]:
        while hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.draw())
        player_total, dealer_total = hand_value(self.player), hand_value(self.dealer)
        if dealer_total > 21 or player_total > dealer_total:
            return await self._settle("win")
        if player_total < dealer_total:
            return await self._settle("lose")
        return await self._settle("push")

    async def resolve_naturals(self) -> tuple[list[discord.Embed], list[discord.File]] | None:
        """Immediately settles the hand if either side was dealt a natural blackjack."""
        player_bj, dealer_bj = is_blackjack(self.player), is_blackjack(self.dealer)
        if not player_bj and not dealer_bj:
            return None
        outcome = "push" if player_bj and dealer_bj else ("blackjack" if player_bj else "lose")
        return await self._settle(outcome)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.append(self.deck.draw())
        if len(self.player) > 2:
            self.double_down.disabled = True
        if hand_value(self.player) > 21:
            embeds, files = await self._settle("lose")
        else:
            embeds, files = self.build_display()
        await interaction.response.edit_message(embeds=embeds, attachments=files, view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        embeds, files = await self._play_dealer_and_settle()
        await interaction.response.edit_message(embeds=embeds, attachments=files, view=self)

    @discord.ui.button(label="Double Down", style=discord.ButtonStyle.danger)
    async def double_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.author.id in holdem_busy_players:
            await interaction.response.send_message("Finish your poker hand first!", ephemeral=True)
            return
        balance = await asyncio.to_thread(db.get_balance, self.guild_id, self.author.id)
        if balance < self.bet:
            await interaction.response.send_message("You don't have enough credits to double down.", ephemeral=True)
            return

        await asyncio.to_thread(db.update_balance, self.guild_id, self.author.id, -self.bet)
        self.bet *= 2
        self.player.append(self.deck.draw())
        if hand_value(self.player) > 21:
            embeds, files = await self._settle("lose")
        else:
            embeds, files = await self._play_dealer_and_settle()
        await interaction.response.edit_message(embeds=embeds, attachments=files, view=self)

    async def on_timeout(self):
        if self.resolved:
            return
        self._disable_all()
        await asyncio.to_thread(db.update_balance, self.guild_id, self.author.id, self.bet)  # refund escrowed bet
        if self.message is not None:
            embeds, files = self.build_display(reveal_dealer=True, result_text="⌛ Game timed out — bet refunded")
            try:
                await self.message.edit(embeds=embeds, attachments=files, view=self)
            except discord.HTTPException:
                pass
