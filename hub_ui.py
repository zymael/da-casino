"""Shared discord.ui building blocks for hub-style views (!casino, !dungeon, ...) that invoke an
existing @bot.command's callback from a button click or modal submit, instead of a typed command.
Command callbacks are passed in from bot.py at hub-construction time (not imported here), since
bot.py is what imports these hub modules -- importing back would be circular.
"""

import discord


class InteractionContext:
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
        await self.command_callback(InteractionContext(interaction), amount)


class NoArgButton(discord.ui.Button):
    """A button for a command that takes no arguments -- just runs it."""

    def __init__(self, label: str, style: discord.ButtonStyle, row: int, command_callback):
        super().__init__(label=label, style=style, row=row)
        self.command_callback = command_callback

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.command_callback(InteractionContext(interaction))


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
