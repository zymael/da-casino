"""Shared discord.ui building blocks for room views (room_view.py's generic RoomView, and every
specialization's extra_items) that invoke an existing @bot.command's callback from a button click
or modal submit, instead of a typed command. Command callbacks are passed in from bot.py/
room_commands.py at room-construction time (not imported here), since bot.py is what imports these
view modules -- importing back would be circular.
"""

import asyncio

import discord

import inventory_view


class HubSession:
    """Tracks the single ephemeral /play message's idle-deletion timer, threaded through every
    view reachable from it (the explorer landing page and all three hubs) so a click *anywhere*
    resets the countdown -- an active session never expires mid-use, only after idle_seconds of
    no interaction at all.

    Deletion always goes through whichever interaction touched it most recently (not the
    original /play interaction) specifically because Discord interaction tokens are only valid
    ~15 minutes from creation; anchoring to the latest click's interaction instead means a long
    but active session keeps getting a fresh token rather than the delete silently starting to
    fail past the 15-minute mark from the original invocation."""

    def __init__(self, interaction: discord.Interaction, idle_seconds: float = 300):
        self.idle_seconds = idle_seconds
        self.latest_interaction = interaction
        self._task: asyncio.Task | None = None
        self.touch(interaction)

    def touch(self, interaction: discord.Interaction):
        self.latest_interaction = interaction
        if self._task is not None:
            self._task.cancel()
        self._task = asyncio.create_task(self._expire())

    async def _expire(self):
        try:
            await asyncio.sleep(self.idle_seconds)
        except asyncio.CancelledError:
            return
        try:
            await self.latest_interaction.delete_original_response()
        except discord.HTTPException:
            pass


class InteractionContext:
    """Just enough of discord.ext.commands.Context for a command callback written for normal
    prefix invocation to run unmodified from a button click or modal submission instead."""

    def __init__(self, interaction: discord.Interaction):
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.author = interaction.user
        self.send = interaction.channel.send


class AmountModal(discord.ui.Modal):
    """Collects a single integer (a bet, a buy-in, a horse number, ...) and hands it to
    `command_callback` exactly as if it had been typed as a command argument. `const_args`, if
    given, are extra arguments appended *after* the collected amount -- fixed at button-build time
    rather than typed by the player (e.g. a room's "Boost SPD" button pre-binding the `stat`
    argument of `!boost <number> <stat>`, so the modal only ever has to ask for the number)."""

    def __init__(
        self, title: str, command_callback, input_label: str, required: bool, closes_hub: bool,
        const_args: tuple = (),
    ):
        super().__init__(title=title)
        self.command_callback = command_callback
        self.closes_hub = closes_hub
        self.const_args = const_args
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
        # A modal submitted from a component defers a DEFERRED_MESSAGE_UPDATE on the message that
        # component lived on (the hub) -- ephemeral= would only apply to a fresh application-command
        # response, so it's omitted here rather than left in as a no-op. See closes_hub below.
        await interaction.response.defer()
        await self.command_callback(InteractionContext(interaction), amount, *self.const_args)
        if self.closes_hub:
            await close_hub(interaction)


class NoArgButton(discord.ui.Button):
    """A button for a command that takes no *player-typed* arguments -- just runs it.
    `const_args`, if given, are still passed through (fixed at button-build time, e.g. a room's
    "Upgrade Facility" button pre-binding the `action` argument of `!facility <action>` to
    `"buy"`) -- "no arg" means nothing needs collecting from the player, not that the underlying
    command itself is literally zero-arg."""

    def __init__(
        self, label: str, style: discord.ButtonStyle, row: int, command_callback, closes_hub: bool = False,
        const_args: tuple = (),
    ):
        super().__init__(label=label, style=style, row=row)
        self.command_callback = command_callback
        self.closes_hub = closes_hub
        self.const_args = const_args

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.command_callback(InteractionContext(interaction), *self.const_args)
        if self.closes_hub:
            await close_hub(interaction)


class AmountButton(discord.ui.Button):
    """A button for a command that takes one player-typed integer argument -- opens AmountModal
    to collect it first, matching this codebase's existing bet-collection convention (e.g.
    blackjack's Join/Set Bet modal) rather than needing that amount typed on the command line.
    `const_args` -- see AmountModal."""

    def __init__(
        self, label: str, style: discord.ButtonStyle, row: int, command_callback,
        modal_title: str, input_label: str, required: bool = True, closes_hub: bool = False,
        const_args: tuple = (),
    ):
        super().__init__(label=label, style=style, row=row)
        self.command_callback = command_callback
        self.modal_title = modal_title
        self.input_label = input_label
        self.required = required
        self.closes_hub = closes_hub
        self.const_args = const_args

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            AmountModal(
                self.modal_title, self.command_callback, self.input_label, self.required, self.closes_hub,
                self.const_args,
            )
        )


async def close_hub(interaction: discord.Interaction):
    """Makes the hub message disappear once a game/delve it launched is actually underway --
    the DEFERRED_MESSAGE_UPDATE from defer() above targets that same hub message, so its
    "original response" (for *this* interaction) correctly refers to it."""
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


class InventoryButton(discord.ui.Button):
    """Shared read-only inventory popup -- present on every hub. Doesn't touch the hub message at
    all (a fresh ephemeral response, not an edit), so browsing it never closes or resets what
    you're doing on the hub."""

    def __init__(self, row: int):
        super().__init__(label="🎒 Inventory", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        embed, view = await inventory_view.build_inventory_display(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class EquipmentButton(discord.ui.Button):
    """Shared equipment-management popup -- present on every hub. Same "fresh ephemeral response,
    hub untouched" idea as InventoryButton."""

    def __init__(self, row: int):
        super().__init__(label="⚔️ Equipment", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        embed, view = await inventory_view.build_equipment_display(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
