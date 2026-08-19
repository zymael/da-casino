"""!casino hub: one-click buttons for every game/economy command, so players don't have to
memorize the ! commands (which still work standalone, unchanged).

Buttons don't duplicate any game logic -- they invoke the *exact same* command callback bot.py
already registers via @bot.command, by wrapping the click/modal-submit interaction in a minimal
ctx-alike (every one of those callbacks only ever touches ctx.guild/.channel/.author/.send).
Command callbacks are passed in from bot.py at hub-construction time (not imported here), since
bot.py is what imports this module -- importing back would be circular.
"""

import asyncio

import discord

import casino_render
import hub_ui

CASINO_BANNER_PATH = "assets/casino_banner.png"


class CasinoView(discord.ui.View):
    """Persistent (no timeout) -- unlike !ranch's per-invocation dashboard, this hub is meant to
    sit in the channel indefinitely as a standing shortcut panel, the same way blackjack's table
    control message persists for the table's lifetime."""

    def __init__(self, commands: dict):
        super().__init__(timeout=None)
        c = commands

        # Row 0-1: games. Bet/buy-in games open a modal; the rest just run.
        self.add_item(hub_ui.AmountButton("🃏 Blackjack", discord.ButtonStyle.primary, 0, c["blackjack"], "Blackjack Bet", "Bet amount"))
        self.add_item(hub_ui.NoArgButton("🎰 Slots", discord.ButtonStyle.primary, 0, c["slots"]))
        self.add_item(hub_ui.NoArgButton("🎡 Roulette", discord.ButtonStyle.primary, 0, c["roulette"]))
        self.add_item(hub_ui.AmountButton("♠️ Hold'em", discord.ButtonStyle.primary, 0, c["holdem"], "Hold'em Buy-in", "Buy-in (blank = default)", required=False))
        self.add_item(hub_ui.AmountButton("🎴 Video Poker", discord.ButtonStyle.primary, 0, c["videopoker"], "Video Poker Bet", "Bet amount"))
        self.add_item(hub_ui.AmountButton("🎴 Deuces Wild", discord.ButtonStyle.primary, 1, c["deuceswild"], "Deuces Wild Bet", "Bet amount"))
        self.add_item(hub_ui.NoArgButton("🐎 Horse Race", discord.ButtonStyle.primary, 1, c["horserace"]))

        # Row 2: economy quick actions.
        self.add_item(hub_ui.NoArgButton("💰 Balance", discord.ButtonStyle.secondary, 2, c["balance"]))
        self.add_item(hub_ui.NoArgButton("🎁 Daily", discord.ButtonStyle.secondary, 2, c["daily"]))
        self.add_item(hub_ui.NoArgButton("⛏️ Mine", discord.ButtonStyle.secondary, 2, c["mine"]))
        self.add_item(hub_ui.NoArgButton("🍕 Pizza", discord.ButtonStyle.secondary, 2, c["pizza"]))
        self.add_item(hub_ui.NoArgButton("🏆 Leaderboard", discord.ButtonStyle.secondary, 2, c["leaderboard"]))

        # Row 3: other hubs / info.
        self.add_item(hub_ui.NoArgButton("🐴 Ranch", discord.ButtonStyle.secondary, 3, c["ranch"]))
        self.add_item(hub_ui.NoArgButton("🗡️ Dungeon", discord.ButtonStyle.secondary, 3, c["dungeon"]))
        self.add_item(hub_ui.NoArgButton("📊 Stats", discord.ButtonStyle.secondary, 3, c["stats"]))
        self.add_item(hub_ui.NoArgButton("🏅 Achievements", discord.ButtonStyle.secondary, 3, c["achievements"]))

        # Row 4: flavor.
        self.add_item(RoyButton())


class RoyButton(discord.ui.Button):
    """Deliberately not a text command -- the only way to meet Roy is to click this. Unlike
    Kel's ranch-hub equivalent, this one has no achievement tied to it (not asked for) -- it just
    swaps the hub's banner for Roy's "LET IT RIDE" greeting, replaying every time it's clicked."""

    def __init__(self):
        super().__init__(label="👹 Meet Roy", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction):
        buf = await asyncio.to_thread(casino_render.render_roy_greeting)
        file = discord.File(buf, filename="roy_greeting.png")
        embed = interaction.message.embeds[0]
        embed.set_image(url="attachment://roy_greeting.png")
        await interaction.response.edit_message(embed=embed, attachments=[file], view=self.view)
