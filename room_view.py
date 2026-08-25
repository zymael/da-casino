"""The generic room renderer -- replaces explorer_view.py (Town Square was the only hand-written
"hub of hubs"; now it's just another rooms.json entry with no commands) and the build_X_display/
XView pair each of casino_view/ranch_view/dungeon_view used to own. One `build_room_display` and
one `RoomView` work for every room in rooms.json, because by this point everything a room needs is
either generic data (background, exits, commands -- rooms.py) or already-generic per-NPC state
(npcs.json + quests.npcs_present_in_room/talk_to_npc, unchanged since Phase B).

The one documented exception is `_SPECIALIZATIONS`: a small, fixed, hardcoded dict (never dynamic)
from a room's optional "specialization" key to whichever of casino_view/ranch_view/dungeon_view
knows how to contribute two optional things a generic room embed/view can't derive on its own --
`extra_items(commands) -> list[discord.ui.Item]` (Casino's GameSelect -- one Select standing in for
7 different commands, a shape rooms.json's commands[] schema can't express) and
`async extra_embed_fields(guild_id, user_id) -> list[(name, value, inline)]` (Ranch's facility/
horse-roster fields, Dungeon's energy field -- live per-player DB reads a generic room has no way
to know about). Both are genuinely optional; a room with no specialization gets neither, and each
existing specialization only ever defines the one of the two it actually needs. Neither hook ever
builds a bespoke argument-collection UI -- see CLAUDE.md's "Rooms/NPCs: the ethos that matters
most" before adding a third.
"""

import asyncio
import os

import discord

import casino_view
import dungeon_view
import hub_ui
import npc_render
import npc_view
import npcs
import quests
import ranch_view
import room_commands
import rooms

_SPECIALIZATIONS = {
    "casino": casino_view,
    "ranch": ranch_view,
    "dungeon": dungeon_view,
}
rooms.validate_specializations(_SPECIALIZATIONS.keys())

_MAX_ROW_ITEMS = 5


async def build_room_display(
    guild_id: int, user_id: int, room_id: str, session: hub_ui.HubSession,
) -> tuple[discord.Embed, "RoomView", discord.File]:
    """Builds the (embed, view, file) triple for any room -- reused by /play's initial navigation
    and every button/exit click afterward, so it always reflects fresh DB state (same idea every
    build_X_display used to follow individually)."""
    room = rooms.ROOMS[room_id]
    present_npcs = await quests.npcs_present_in_room(guild_id, user_id, room_id)
    npc_states = {npc_id: await quests.talk_to_npc(guild_id, user_id, npc_id) for npc_id in present_npcs}
    npc_talk_labels = {npc_id: await quests.npc_talk_label(guild_id, user_id, npc_id) for npc_id in present_npcs}

    filename = os.path.basename(room["background_path"])
    sprite_paths = [
        npcs.NPCS[npc_id]["sprite_path"] for npc_id in present_npcs if npcs.NPCS[npc_id].get("sprite_path")
    ]
    buf = await asyncio.to_thread(npc_render.render_room_banner, room["background_path"], sprite_paths)
    file = discord.File(buf, filename=filename)

    embed = discord.Embed(title=room["name"], color=discord.Color.blurple())
    if room.get("description"):
        embed.description = room["description"]
    embed.set_image(url=f"attachment://{filename}")

    specialization = _SPECIALIZATIONS.get(room.get("specialization"))
    if specialization is not None and hasattr(specialization, "extra_embed_fields"):
        for name, value, inline in await specialization.extra_embed_fields(guild_id, user_id):
            embed.add_field(name=name, value=value, inline=inline)

    view = RoomView(guild_id, user_id, room_id, present_npcs, npc_states, npc_talk_labels, session)
    return embed, view, file


class RoomExitButton(discord.ui.Button):
    """Navigates to another room in place -- replaces both hub_ui.TownSquareButton and every hub's
    bespoke _go_X method with one generic class, since any room can now reach any other room the
    same way. No more go_home-closure-threading through every view's constructor."""

    def __init__(self, guild_id: int, user_id: int, target_room_id: str, label: str, session: hub_ui.HubSession, *, row: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.guild_id = guild_id
        self.user_id = user_id
        self.target_room_id = target_room_id
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        embed, view, file = await build_room_display(self.guild_id, self.user_id, self.target_room_id, self.session)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)


class RoomView(discord.ui.View):
    """Launcher for whatever a room actually contains: the specialization's extra_items (if any),
    one NoArgButton/AmountButton per rooms.json commands[] entry, one TalkToNpcButton per NPC
    currently present (npcs.json data -- see quests.npcs_present_in_room; its label is
    quests.npc_talk_label's per-quest-stage override if one's set, else the button's own generic
    "Talk to X" default) plus one ShopButton/SellButton per NPC with a non-empty "shop"/"buys_items"
    checked and one TurnInButton per turn-in-able quest any of them has with this player, the
    shared Inventory/Equipment buttons, and one RoomExitButton per exit. No manual row numbers
    anywhere -- `_add` auto-flows every item into groups of 5, Discord's per-row limit, so
    authoring a room's command order never has to think about Discord UI row math."""

    def __init__(
        self, guild_id: int, user_id: int, room_id: str, present_npcs: list[str],
        npc_states: dict[str, list[dict]], npc_talk_labels: dict[str, str | None], session: hub_ui.HubSession,
    ):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        self.room_id = room_id
        self.session = session
        self._row = 0
        self._row_count = 0

        room = rooms.ROOMS[room_id]
        specialization = _SPECIALIZATIONS.get(room.get("specialization"))
        if specialization is not None and hasattr(specialization, "extra_items"):
            for item in specialization.extra_items(room_commands.COMMANDS):
                self._add(item)

        for command in room["commands"]:
            command_callback = room_commands.COMMANDS[command["key"]]
            const_args = tuple(command.get("const_args", ()))
            closes_hub = command.get("closes_hub", False)
            if command["kind"] == "amount":
                button = hub_ui.AmountButton(
                    command["label"], discord.ButtonStyle.primary, 0, command_callback,
                    command["modal_title"], command["input_label"],
                    closes_hub=closes_hub, const_args=const_args,
                )
            else:
                button = hub_ui.NoArgButton(
                    command["label"], discord.ButtonStyle.primary, 0, command_callback,
                    closes_hub=closes_hub, const_args=const_args,
                )
            self._add(button)

        for npc_id in present_npcs:
            self._add(npc_view.TalkToNpcButton(
                npc_id, room["background_path"], self._rebuild, row=0, label=npc_talk_labels.get(npc_id),
            ))
            if npcs.NPCS[npc_id].get("shop"):
                self._add(npc_view.ShopButton(npc_id, row=0))
            if npcs.NPCS[npc_id].get("buys_items"):
                self._add(npc_view.SellButton(npc_id, row=0))
            for state in npc_states[npc_id]:
                if state["can_turn_in"]:
                    self._add(npc_view.TurnInButton(
                        state["quest_id"], room["background_path"], self._rebuild, row=0, item=state["item"],
                        label=state["turn_in_label"],
                    ))

        self._add(hub_ui.InventoryButton(row=0))
        self._add(hub_ui.EquipmentButton(row=0))

        for exit_entry in room["exits"]:
            self._add(RoomExitButton(guild_id, user_id, exit_entry["room_id"], exit_entry["label"], session, row=0))

    def _add(self, item: discord.ui.Item):
        """A Select consumes an entire row (width 5) in Discord's layout, unlike a Button (width
        1) -- if one doesn't fit what's left of the current row, it starts a fresh one rather than
        packing alongside other items."""
        width = 5 if isinstance(item, discord.ui.Select) else 1
        if self._row_count + width > _MAX_ROW_ITEMS:
            self._row_count = 0
            self._row += 1
        item.row = self._row
        self.add_item(item)
        self._row_count += width
        if self._row_count >= _MAX_ROW_ITEMS:
            self._row_count = 0
            self._row += 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        self.session.touch(interaction)
        return True

    async def _rebuild(self, interaction: discord.Interaction, buf, filename: str):
        """The `rebuild` callable threaded into every npc_view button on this room -- redraws the
        room fresh and applies the dialogue image that button just rendered."""
        embed, view, _file = await build_room_display(self.guild_id, self.user_id, self.room_id, self.session)
        file = discord.File(buf, filename=filename)
        embed.set_image(url=f"attachment://{filename}")
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
