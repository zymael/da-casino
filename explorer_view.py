"""The /play entry point: a lightweight landing page with one button per hub (Casino / Ranch /
Dungeon). Picking one swaps the *same* ephemeral message's embed/view in place via
interaction.response.edit_message rather than posting a new message -- see hub_ui.HubSession for
why everything reachable from /play shares one message and one idle-deletion timer.

Every hub (casino_view/ranch_view/dungeon_view) gets a `go_home` callable threaded into its
build_X_display() so its "Town Square" button can return here, without any of those modules
needing to import this one back -- this module is the only one that imports all three.
"""

import discord

import casino_view
import dungeon_view
import hub_ui
import ranch_view

EXPLORER_BANNER_PATH = "assets/town_banner.png"


def make_go_home(guild_id: int, user_id: int, commands: dict, session: hub_ui.HubSession):
    """A zero-arg-per-call async callable that swaps the current message back to the explorer --
    passed down into every hub's build_X_display() so its Town Square button can use it without
    that module needing to import this one."""

    async def go_home(interaction: discord.Interaction):
        embed, view = await build_explorer_display(guild_id, user_id, commands, session)
        file = hub_ui.banner_file(EXPLORER_BANNER_PATH)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)

    return go_home


async def build_explorer_display(
    guild_id: int, user_id: int, commands: dict, session: hub_ui.HubSession
) -> tuple[discord.Embed, "ExplorerView"]:
    embed = discord.Embed(
        title="🏘️ Town Square",
        description="Where to?",
        color=discord.Color.blurple(),
    )
    embed.set_image(url="attachment://town_banner.png")
    view = ExplorerView(guild_id, user_id, commands, session)
    return embed, view


class ExplorerView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, commands: dict, session: hub_ui.HubSession):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        self.commands = commands
        self.session = session
        self.go_home = make_go_home(guild_id, user_id, commands, session)
        self.add_item(_DestinationButton("🎰 Casino", self._go_casino))
        self.add_item(_DestinationButton("🐴 Ranch", self._go_ranch))
        self.add_item(_DestinationButton("🗡️ Dungeon", self._go_dungeon))
        self.add_item(hub_ui.InventoryButton(row=1))
        self.add_item(hub_ui.EquipmentButton(row=1))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        self.session.touch(interaction)
        return True

    async def _go_casino(self, interaction: discord.Interaction):
        embed, view = await casino_view.build_casino_display(self.commands, self.session, self.go_home)
        file = hub_ui.banner_file(casino_view.CASINO_BANNER_PATH)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)

    async def _go_ranch(self, interaction: discord.Interaction):
        embed, view = await ranch_view.build_ranch_display(
            self.guild_id, self.user_id, interaction.user.display_name, None, self.session, self.go_home
        )
        file = hub_ui.banner_file(ranch_view.RANCH_BANNER_PATH)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)

    async def _go_dungeon(self, interaction: discord.Interaction):
        embed, view, file = await dungeon_view.build_dungeon_hub_display(
            self.guild_id, self.user_id, self.commands, self.session, self.go_home
        )
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)


class _DestinationButton(discord.ui.Button):
    def __init__(self, label: str, go):
        super().__init__(label=label, style=discord.ButtonStyle.primary, row=0)
        self.go = go

    async def callback(self, interaction: discord.Interaction):
        await self.go(interaction)
