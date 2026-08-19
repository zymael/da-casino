import asyncio

import discord

import achievements
import cards_render
import db
import video_poker
from game import Deck
from holdem_view import busy_players as holdem_busy_players


class HoldButton(discord.ui.Button):
    def __init__(self, index: int, view: "VideoPokerView"):
        self.index = index
        super().__init__(
            label=self._label(view),
            style=discord.ButtonStyle.success if view.held[index] else discord.ButtonStyle.secondary,
            row=0,
        )

    def _label(self, view: "VideoPokerView") -> str:
        held = view.held[self.index]
        return f"{'✓ Held' if held else 'Hold'}: {view.hand[self.index]}"

    async def callback(self, interaction: discord.Interaction):
        view: VideoPokerView = self.view
        view.held[self.index] = not view.held[self.index]
        self.label = self._label(view)
        self.style = discord.ButtonStyle.success if view.held[self.index] else discord.ButtonStyle.secondary
        await interaction.response.edit_message(embed=view.build_deal_embed(), view=view)


class DrawButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🎴 Draw", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: VideoPokerView = self.view
        if view.author.id in holdem_busy_players:
            await interaction.response.send_message("Finish your poker hand first!", ephemeral=True)
            return

        view.hand = video_poker.draw_replacements(view.deck, view.hand, view.held)
        label, multiplier = video_poker.evaluate(view.hand, view.variant)
        payout = view.bet * multiplier
        net = payout - view.bet
        if payout:
            balance = await asyncio.to_thread(db.update_balance, view.guild_id, view.author.id, payout)
        else:
            balance = await asyncio.to_thread(db.get_balance, view.guild_id, view.author.id)
        await asyncio.to_thread(db.log_bet, view.guild_id, view.author.id, view.variant, view.bet, net)

        view.balance = balance
        view.result_label = label
        view.multiplier = multiplier
        view.net = net
        view.resolved = True
        view.rebuild_items()

        embed = view.build_result_embed()
        file = view.build_hand_file()
        await interaction.response.edit_message(embed=embed, view=view, attachments=[file])

        kinds = achievements.kinds_for_bet(view.variant, net)
        kinds += await achievements.record_and_check(view.guild_id, view.author.id, view.variant, net)
        if kinds:
            await achievements.try_award_many(
                interaction.followup.send, view.guild_id, view.author.id, view.author.display_name, kinds
            )


class PlayAgainButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔁 Play Again", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: VideoPokerView = self.view
        if view.author.id in holdem_busy_players:
            await interaction.response.send_message("Finish your poker hand first!", ephemeral=True)
            return
        if view.bet > view.balance:
            currency = db.get_currency_name(view.guild_id)
            await interaction.response.send_message(
                f"You only have **{view.balance}** {currency} — not enough for a **{view.bet}**-{currency} bet.",
                ephemeral=True,
            )
            return

        balance = await asyncio.to_thread(db.update_balance, view.guild_id, view.author.id, -view.bet)
        view.reset_round(balance)

        embed = view.build_deal_embed()
        file = view.build_hand_file()
        await interaction.response.edit_message(embed=embed, view=view, attachments=[file])


class VideoPokerView(discord.ui.View):
    def __init__(
        self,
        author: discord.abc.User,
        guild_id: int,
        bet: int,
        balance: int,
        variant: str = video_poker.JACKS_OR_BETTER,
    ):
        super().__init__(timeout=90)
        self.author = author
        self.guild_id = guild_id
        self.bet = bet
        self.variant = variant
        self.title = f"🃏 {video_poker.GAME_TITLES[variant]}"
        self.message: discord.Message | None = None

        self.deck: Deck | None = None
        self.hand = []
        self.held = []
        self.resolved = False
        self.result_label = ""
        self.multiplier = 0
        self.net = 0

        self.reset_round(balance)

    def reset_round(self, balance: int):
        self.balance = balance
        self.deck = Deck()
        self.hand = video_poker.deal(self.deck)
        self.held = [False] * 5
        self.resolved = False
        self.result_label = ""
        self.multiplier = 0
        self.net = 0
        self.rebuild_items()

    def rebuild_items(self):
        self.clear_items()
        if self.resolved:
            self.add_item(PlayAgainButton())
        else:
            for i in range(5):
                self.add_item(HoldButton(i, self))
            self.add_item(DrawButton())

    def build_hand_file(self) -> discord.File:
        return discord.File(cards_render.render_hand(self.hand), filename="video_poker.png")

    def build_deal_embed(self) -> discord.Embed:
        currency = db.get_currency_name(self.guild_id)
        held_count = sum(self.held)
        embed = discord.Embed(
            title=self.title,
            description="Tap cards to **hold** them, then hit **Draw** to replace the rest.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Bet", value=f"{self.bet} {currency}", inline=True)
        embed.add_field(name="Held", value=f"{held_count} / 5", inline=True)
        paytable_text = " · ".join(f"{label} {mult}x" for label, mult in video_poker.PAYTABLES[self.variant])
        embed.add_field(name="Paytable", value=paytable_text, inline=False)
        embed.set_image(url="attachment://video_poker.png")
        embed.set_footer(text=f"Balance: {self.balance} {currency}")
        return embed

    def build_result_embed(self) -> discord.Embed:
        currency = db.get_currency_name(self.guild_id)
        payout = self.bet * self.multiplier
        if self.multiplier == 0:
            outcome, color = f"💥 {self.result_label} — you lose {self.bet} {currency}", discord.Color.red()
        elif self.net == 0:
            outcome, color = f"🤝 {self.result_label} — bet returned", discord.Color.greyple()
        else:
            outcome, color = f"🎉 {self.result_label}! (+{self.net} {currency})", discord.Color.green()

        embed = discord.Embed(title=self.title, color=color)
        embed.set_image(url="attachment://video_poker.png")
        embed.add_field(name="Bet", value=f"{self.bet} {currency}", inline=True)
        embed.add_field(name="Payout", value=f"{payout} {currency} ({self.multiplier}x)", inline=True)
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
