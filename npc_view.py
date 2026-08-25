"""Generic NPC interaction buttons -- shared by every room (room_view.RoomView adds these for
whichever NPCs quests.npcs_present_in_room reports for that room, no per-room NPC code at all).
Three classes cover every NPC: TalkToNpcButton (always shows whatever's currently relevant -- their
static greeting, or every active quest's current prompt with them), TurnInButton (one per quest
that quests.talk_to_npc reports can_turn_in -- an NPC can have more than one eligible quest active
at once, so a room adds zero or more of these, each scoped to its own quest_id rather than the NPC
as a whole; see quests.talk_to_npc's docstring for why that matters), and ShopButton (added only
for an NPC with a non-empty npcs.json "shop" list -- opens shop_view's ephemeral purchase popup,
same pattern as hub_ui.InventoryButton/EquipmentButton, rather than anything built into the room's
own view; see CLAUDE.md's "Rooms/NPCs" section for why a room never builds bespoke picker UI
itself).

TalkToNpcButton/TurnInButton take a `rebuild` callable -- `async def rebuild(interaction, buf,
filename) -> None` -- that redraws the room and applies the freshly-rendered dialogue image;
room_view.RoomView passes its own `_rebuild` in. A present NPC's reveal sprite (e.g. the Greasy
Princess, once mondor_goblin_chieftain is complete) shows on the room's *resting* banner
automatically via npc_render.render_room_banner's sprite-list compositing -- no NPC-specific code
anywhere in this path, room_view.py just asks each present NPC's npcs.json entry for a
sprite_path.
"""

import asyncio

import discord

import achievements
import npc_render
import npcs
import quests
import shop_view


class TalkToNpcButton(discord.ui.Button):
    """Shows whatever quests.talk_to_npc reports is currently relevant with this NPC (every active
    quest's current stage prompt, one per line, if more than one), else their static greet_message
    if none are active -- fully resolved and sent (both this dialogue image *and* self.rebuild's
    own redraw of the room, which independently re-fetches every present NPC's states too) before
    awarding the NPC's greet_achievement (if set), so a quest whose start_trigger is that same
    achievement never starts on the same click that earns it: the first-ever talk always shows
    greet_message (plus the achievement banked quietly right after, once nothing this click still
    needs to read state from the DB), and the quest itself only starts the next time this player
    talks to them -- either by clicking this button again or simply re-opening/refreshing the room
    (build_room_display calls quests.talk_to_npc for every present NPC unconditionally, not just
    the one clicked). Reads better for a quest that's supposed to feel like something offered once
    you're already acquainted, not blurted out as your very first hello -- and awarding stays
    idempotent/safe every click regardless of this ordering, so nothing here changes for a
    greet_achievement with no quest attached to it."""

    def __init__(self, npc_id: str, banner_path: str, rebuild, *, row: int, label: str | None = None):
        npc = npcs.NPCS[npc_id]
        super().__init__(label=label or f"👋 Talk to {npc['name']}", style=discord.ButtonStyle.secondary, row=row)
        self.npc_id = npc_id
        self.banner_path = banner_path
        self.rebuild = rebuild

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        npc = npcs.NPCS[self.npc_id]
        states = await quests.talk_to_npc(guild_id, user_id, self.npc_id)
        text = "\n\n".join(state["prompt"] for state in states) if states else npc["greet_message"]
        buf = await asyncio.to_thread(npc_render.render_npc_dialogue, self.banner_path, text, npc.get("sprite_path"))
        await self.rebuild(interaction, buf, f"{self.npc_id}_dialogue.png")
        if npc.get("greet_achievement"):
            await achievements.try_award_many(
                interaction.channel.send, guild_id, user_id, interaction.user.display_name,
                [npc["greet_achievement"]],
            )


class ShopButton(discord.ui.Button):
    """Shown for any NPC with a non-empty npcs.json "shop" list -- opens shop_view's ephemeral
    purchase popup rather than redrawing the room banner (unlike TalkToNpcButton/TurnInButton,
    this doesn't take a `rebuild` -- buying happens entirely inside the popup, which re-renders
    itself in place; see shop_view.ShopSelect.callback)."""

    def __init__(self, npc_id: str, *, row: int):
        npc = npcs.NPCS[npc_id]
        super().__init__(label=f"🛒 Shop ({npc['name']})", style=discord.ButtonStyle.secondary, row=row)
        self.npc_id = npc_id

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        embed, view = await shop_view.build_shop_display(guild_id, user_id, self.npc_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class TurnInButton(discord.ui.Button):
    """Scoped to one specific quest_id (not an NPC as a whole -- see module docstring). Only ever
    added to a view once quests.talk_to_npc reports can_turn_in for that quest (see each hub's own
    view-building function) -- calls quests.turn_in and shows the resulting on_complete_message. A
    reward_item, if granted, is reported as a separate ephemeral followup (equipped or stored),
    same as the buttons this replaces. `item`, if given, is used to build the default label
    ("Give <npc> the <item>") the same way every turn-in button already read; pass an explicit
    `label` instead for a stage whose trigger has nothing physical to hand over."""

    def __init__(
        self, quest_id: str, banner_path: str, rebuild, *, row: int, item: dict | None = None,
        label: str | None = None,
    ):
        npc = npcs.NPCS[quests.QUESTS_BY_ID[quest_id]["npc"]]
        npc_name = npc["name"]
        if label is None:
            label = f"🎁 Give {npc_name} the {item['name']}" if item else f"✅ Turn in to {npc_name}"
        super().__init__(label=label, style=discord.ButtonStyle.success, row=row)
        self.quest_id = quest_id
        self.npc_name = npc_name
        self.sprite_path = npc.get("sprite_path")
        self.banner_path = banner_path
        self.rebuild = rebuild

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        result = await quests.turn_in(guild_id, user_id, self.quest_id)
        if not result["success"]:
            await interaction.response.send_message(
                f"You don't have anything to give {self.npc_name} right now.", ephemeral=True,
            )
            return

        buf = await asyncio.to_thread(
            npc_render.render_npc_dialogue, self.banner_path, result["message"], self.sprite_path,
        )
        await self.rebuild(interaction, buf, f"{self.quest_id}_turnin.png")

        reward_item = result["reward_item"]
        if reward_item:
            if result["reward_item_kind"] == "equipment":
                status_text = f"⚔️ Received **{reward_item['name']}** — stored in `!equipment`."
            else:
                # Same "just landed in your bag" phrasing as dreams.py's own non-equipment reward
                # notification -- reward_item_kind used to be assumed "equipment" unconditionally
                # here, which showed a nonsense "your current weapon is better" message for e.g. a
                # housing_item reward that was never compared to any weapon at all.
                status_text = f"🎁 Received **{reward_item['name']}**! Check `!inventory`."
            await interaction.followup.send(status_text, ephemeral=True)
