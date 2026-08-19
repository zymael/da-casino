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

CASINO_BANNER_PATH = "assets/casino_banner.png"


class _InteractionContext:
    """Just enough of discord.ext.commands.Context for a command callback written for normal
    prefix invocation to run unmodified from a button click or modal submission instead."""

    def __init__(self, interaction: discord.Interaction):
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.author = interaction.user
        self.send = interaction.channel.send


class AmountModal(discord.ui.Modal):
    """Collects a single integer (a bet or buy-in) and hands it to `command_callback` exactly
    as if it had been typed as a command argument."""

    def __init__(self, title: str, command_callback, input_label: str, required: bool = True):
        super().__init__(title=title)
        self.command_callback = command_callback
        self.amount_input = discord.ui.TextInput(label=input_label, placeholder="e.g. 50", required=required)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount_input.value.strip()
        if not raw:
            amount = None
        else:
            try:
                amount = int(raw)
            except ValueError:
                await interaction.response.send_message("Enter a whole number.", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        await self.command_callback(_InteractionContext(interaction), amount)


class NoArgButton(discord.ui.Button):
    """A button for a command that takes no arguments -- just runs it."""

    def __init__(self, label: str, style: discord.ButtonStyle, row: int, command_callback):
        super().__init__(label=label, style=style, row=row)
        self.command_callback = command_callback

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.command_callback(_InteractionContext(interaction))


class AmountButton(discord.ui.Button):
    """A button for a command that takes one integer argument -- opens AmountModal to collect
    it first, matching this codebase's existing bet-collection convention (e.g. blackjack's
    Join/Set Bet modal) rather than needing that amount typed on the command line."""

    def __init__(
        self, label: str, style: discord.ButtonStyle, row: int, command_callback,
        modal_title: str, input_label: str, required: bool = True,
    ):
        super().__init__(label=label, style=style, row=row)
        self.command_callback = command_callback
        self.modal_title = modal_title
        self.input_label = input_label
        self.required = required

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            AmountModal(self.modal_title, self.command_callback, self.input_label, self.required)
        )


class CasinoView(discord.ui.View):
    """Persistent (no timeout) -- unlike !ranch's per-invocation dashboard, this hub is meant to
    sit in the channel indefinitely as a standing shortcut panel, the same way blackjack's table
    control message persists for the table's lifetime."""

    def __init__(self, commands: dict):
        super().__init__(timeout=None)
        c = commands

        # Row 0-1: games. Bet/buy-in games open a modal; the rest just run.
        self.add_item(AmountButton("🃏 Blackjack", discord.ButtonStyle.primary, 0, c["blackjack"], "Blackjack Bet", "Bet amount"))
        self.add_item(NoArgButton("🎰 Slots", discord.ButtonStyle.primary, 0, c["slots"]))
        self.add_item(NoArgButton("🎡 Roulette", discord.ButtonStyle.primary, 0, c["roulette"]))
        self.add_item(AmountButton("♠️ Hold'em", discord.ButtonStyle.primary, 0, c["holdem"], "Hold'em Buy-in", "Buy-in (blank = default)", required=False))
        self.add_item(AmountButton("🎴 Video Poker", discord.ButtonStyle.primary, 0, c["videopoker"], "Video Poker Bet", "Bet amount"))
        self.add_item(AmountButton("🎴 Deuces Wild", discord.ButtonStyle.primary, 1, c["deuceswild"], "Deuces Wild Bet", "Bet amount"))
        self.add_item(NoArgButton("🐎 Horse Race", discord.ButtonStyle.primary, 1, c["horserace"]))

        # Row 2: economy quick actions.
        self.add_item(NoArgButton("💰 Balance", discord.ButtonStyle.secondary, 2, c["balance"]))
        self.add_item(NoArgButton("🎁 Daily", discord.ButtonStyle.secondary, 2, c["daily"]))
        self.add_item(NoArgButton("⛏️ Mine", discord.ButtonStyle.secondary, 2, c["mine"]))
        self.add_item(NoArgButton("🍕 Pizza", discord.ButtonStyle.secondary, 2, c["pizza"]))
        self.add_item(NoArgButton("🏆 Leaderboard", discord.ButtonStyle.secondary, 2, c["leaderboard"]))

        # Row 3: other hubs / info.
        self.add_item(NoArgButton("🐴 Ranch", discord.ButtonStyle.secondary, 3, c["ranch"]))
        self.add_item(NoArgButton("📊 Stats", discord.ButtonStyle.secondary, 3, c["stats"]))
        self.add_item(NoArgButton("🏅 Achievements", discord.ButtonStyle.secondary, 3, c["achievements"]))

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
