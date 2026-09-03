"""Generic NPC interaction UI -- shared by every room (room_view.RoomView adds these for whichever
NPCs quests.npcs_present_in_room reports for that room, no per-room NPC code at all).

TalkToNpcButton opens a small nested conversation flow, all sharing the room's one ephemeral
message via in-place edits: conversation home (NpcConversationView -- the NPC's static
greet_message, always shown, plus one button per current "topic" -- see quests.npc_greet) -> topic
detail (NpcTopicView -- that quest's current prompt/complete_message, plus a "Turn this in" button
only if its trigger is already satisfied) -> back to conversation home, or all the way back to the
plain room. This declutters the room itself down to just one Talk button per NPC (plus
Shop/Sell, unrelated) instead of a room-level button per turn-in-ready quest.

ShopButton (added only for an NPC with a non-empty npcs.json "shop" list -- opens shop_view's
ephemeral purchase popup) and SellButton (added only for an NPC with npcs.json's "buys_items"
checked -- opens sell_view's ephemeral popup, the reverse direction) are untouched by any of this,
following the same pattern as hub_ui.InventoryButton/EquipmentButton rather than anything built
into the room's own view; see CLAUDE.md's "Rooms/NPCs" section for why a room never builds bespoke
picker UI itself.

Two different kinds of "go back," since a room's own rebuild (recomputing every present NPC's
state, exits, specialization fields) is only actually needed once, on the way all the way back to
the room -- every other screen-to-screen transition just edits the message's dialogue image/view in
place (_edit_dialogue) without touching the room. `back_to_room` is the one callback threaded in
from room_view.RoomView (replaces the old `rebuild` param) -- everything else here is self-
contained, never needing to ask the room for anything.
"""

import asyncio

import discord

import achievements
import npc_render
import npcs
import quests
import sell_view
import shop_view


async def _edit_dialogue(
    interaction: discord.Interaction, banner_path: str, text: str, sprite_path: str | None,
    filename: str, view: discord.ui.View,
) -> None:
    """Redraws the dialogue image and swaps in `view`, reusing the message's own existing embed
    (title/description never change on a conversation-screen transition -- only the image and the
    buttons do) rather than re-fetching the whole room the way a full room rebuild would."""
    buf = await asyncio.to_thread(npc_render.render_npc_dialogue, banner_path, text, sprite_path)
    file = discord.File(buf, filename=filename)
    embed = interaction.message.embeds[0]
    embed.set_image(url=f"attachment://{filename}")
    await interaction.response.edit_message(embed=embed, attachments=[file], view=view)


def _turn_in_label(npc_name: str, item: dict | None, label: str | None) -> str:
    return label or (f"🎁 Give {npc_name} the {item['name']}" if item else f"✅ Turn in to {npc_name}")


async def _render_conversation_home(
    interaction: discord.Interaction, npc_id: str, banner_path: str, back_to_room, *,
    text_override: str | None = None,
) -> None:
    """The one place a Talk click, a topic detail's Back, and a post-turn-in refresh all funnel
    through -- so achievement-awarding and the "new quest" toast only need one implementation.
    Re-running npc_greet's try-start step and re-firing that toast/award on every return to this
    screen (not just the very first Talk click) is deliberate: both are already idempotent/safe,
    and it means a turn-in that unlocks a new quest surfaces the toast immediately instead of
    waiting for another Talk click."""
    guild_id, user_id = interaction.guild.id, interaction.user.id
    npc = npcs.NPCS[npc_id]
    result = await quests.npc_greet(guild_id, user_id, npc_id)
    text = text_override if text_override is not None else npc["greet_message"]
    view = NpcConversationView(npc_id, banner_path, result["topics"], back_to_room)
    await _edit_dialogue(interaction, banner_path, text, npc.get("sprite_path"), f"{npc_id}_dialogue.png", view)
    if npc.get("greet_achievement"):
        await achievements.try_award_many(
            interaction.channel.send, guild_id, user_id, interaction.user.display_name,
            [npc["greet_achievement"]],
        )
    if result["just_started"]:
        await interaction.followup.send("🗺️ New quest! Check `!journal`.", ephemeral=True)


class TalkToNpcButton(discord.ui.Button):
    """Opens the conversation home screen -- see module docstring for the full flow."""

    def __init__(self, npc_id: str, banner_path: str, back_to_room, *, row: int, label: str | None = None):
        npc = npcs.NPCS[npc_id]
        super().__init__(label=label or f"👋 Talk to {npc['name']}", style=discord.ButtonStyle.secondary, row=row)
        self.npc_id = npc_id
        self.banner_path = banner_path
        self.back_to_room = back_to_room

    async def callback(self, interaction: discord.Interaction):
        await _render_conversation_home(interaction, self.npc_id, self.banner_path, self.back_to_room)


class ShopButton(discord.ui.Button):
    """Shown for any NPC with a non-empty npcs.json "shop" list -- opens shop_view's ephemeral
    purchase popup rather than redrawing the room banner (buying happens entirely inside the
    popup, which re-renders itself in place; see shop_view.ShopSelect.callback)."""

    def __init__(self, npc_id: str, *, row: int):
        npc = npcs.NPCS[npc_id]
        super().__init__(label=f"🛒 Shop ({npc['name']})", style=discord.ButtonStyle.secondary, row=row)
        self.npc_id = npc_id

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        embed, view = await shop_view.build_shop_display(guild_id, user_id, self.npc_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class SellButton(discord.ui.Button):
    """Shown for any NPC with npcs.json's "buys_items" checked -- opens sell_view's ephemeral
    popup, same shape as ShopButton but listing whatever the player owns (sell.sellable_holdings)
    rather than a fixed catalog."""

    def __init__(self, npc_id: str, *, row: int):
        npc = npcs.NPCS[npc_id]
        super().__init__(label=f"💰 Sell ({npc['name']})", style=discord.ButtonStyle.secondary, row=row)
        self.npc_id = npc_id

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        embed, view = await sell_view.build_sell_display(guild_id, user_id, self.npc_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class _TopicButton(discord.ui.Button):
    """One per topic quests.npc_greet reports for this NPC. Re-resolves the quest's current state
    at click time (quests.quest_topic_state) rather than trusting the state the conversation home
    screen was built with, since time may have passed since that render."""

    def __init__(self, npc_id: str, banner_path: str, back_to_room, topic: dict, *, row: int):
        super().__init__(label=topic["label"][:80], style=discord.ButtonStyle.secondary, row=row)
        self.npc_id = npc_id
        self.banner_path = banner_path
        self.back_to_room = back_to_room
        self.quest_id = topic["quest_id"]

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        npc = npcs.NPCS[self.npc_id]
        state = await quests.quest_topic_state(guild_id, user_id, self.quest_id)
        if state is None:
            # Lost a race (e.g. the quest's state vanished between screens) -- fall back to a
            # fresh conversation home instead of crashing on a stale topic.
            await _render_conversation_home(interaction, self.npc_id, self.banner_path, self.back_to_room)
            return
        view = NpcTopicView(self.npc_id, self.banner_path, state, self.back_to_room)
        await _edit_dialogue(
            interaction, self.banner_path, state["prompt"], npc.get("sprite_path"),
            f"{self.quest_id}_topic.png", view,
        )


class NpcTurnInButton(discord.ui.Button):
    """The topic detail screen's confirm step -- only ever added when quest_topic_state reported
    can_turn_in True. Calls quests.turn_in and, on success, returns to the conversation home screen
    with the path's on_complete_message as the shown text (see _render_conversation_home's
    text_override) plus an ephemeral reward/progress summary, same wording the old room-level
    TurnInButton used."""

    def __init__(
        self, npc_id: str, banner_path: str, back_to_room, quest_id: str, *, row: int,
        item: dict | None = None, label: str | None = None,
    ):
        npc = npcs.NPCS[npc_id]
        super().__init__(label=_turn_in_label(npc["name"], item, label), style=discord.ButtonStyle.success, row=row)
        self.npc_id = npc_id
        self.banner_path = banner_path
        self.back_to_room = back_to_room
        self.quest_id = quest_id
        self.npc_name = npc["name"]

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        result = await quests.turn_in(guild_id, user_id, self.quest_id)
        if not result["success"]:
            await interaction.response.send_message(
                f"You don't have anything to give {self.npc_name} right now.", ephemeral=True,
            )
            return

        await _render_conversation_home(
            interaction, self.npc_id, self.banner_path, self.back_to_room, text_override=result["message"],
        )

        lines = []
        reward_item = result["reward_item"]
        if reward_item:
            if result["reward_item_kind"] == "equipment":
                lines.append(f"⚔️ Received **{reward_item['name']}** — stored in `!equipment`.")
            else:
                lines.append(f"🎁 Received **{reward_item['name']}**! Check `!inventory`.")
        lines.append("🗺️ Quest complete! Check `!journal`." if result["quest_complete"] else "🗺️ Quest updated! Check `!journal`.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)


class _BackToHomeButton(discord.ui.Button):
    """Returns from a topic detail screen to the conversation home screen (not straight to the
    room) -- a fresh npc_greet call, same as any other return to home."""

    def __init__(self, npc_id: str, banner_path: str, back_to_room, *, row: int):
        super().__init__(label="⬅️ Back", style=discord.ButtonStyle.secondary, row=row)
        self.npc_id = npc_id
        self.banner_path = banner_path
        self.back_to_room = back_to_room

    async def callback(self, interaction: discord.Interaction):
        await _render_conversation_home(interaction, self.npc_id, self.banner_path, self.back_to_room)


class _BackToRoomButton(discord.ui.Button):
    """The conversation home screen's own Back -- the one transition that needs a full room
    rebuild, since leaving the conversation means every other present NPC/exit/command needs to be
    fresh again."""

    def __init__(self, back_to_room, *, row: int):
        super().__init__(label="🚪 Back to room", style=discord.ButtonStyle.secondary, row=row)
        self.back_to_room = back_to_room

    async def callback(self, interaction: discord.Interaction):
        await self.back_to_room(interaction)


class NpcTopicView(discord.ui.View):
    """That quest's current dialogue plus (only if satisfiable) a confirm-turn-in button, plus
    Back to conversation home."""

    def __init__(self, npc_id: str, banner_path: str, state: dict, back_to_room):
        super().__init__(timeout=300)
        row = 0
        if state["can_turn_in"]:
            self.add_item(NpcTurnInButton(
                npc_id, banner_path, back_to_room, state["quest_id"], row=row,
                item=state["item"], label=state["turn_in_label"],
            ))
            row += 1
        self.add_item(_BackToHomeButton(npc_id, banner_path, back_to_room, row=row))


class NpcConversationView(discord.ui.View):
    """The NPC's greeting plus one _TopicButton per topic quests.npc_greet reported, plus Back to
    room. Flows into rows of 5 (Discord's per-row limit), same packing room_view.RoomView's own
    _add does -- a fresh row starts once the current one is full, and Back to room joins whatever
    row still has space rather than always getting its own."""

    def __init__(self, npc_id: str, banner_path: str, topics: list[dict], back_to_room):
        super().__init__(timeout=300)
        self._row = 0
        self._row_count = 0
        for topic in topics:
            self._add(_TopicButton(npc_id, banner_path, back_to_room, topic, row=0))
        self._add(_BackToRoomButton(back_to_room, row=0))

    def _add(self, item: discord.ui.Button):
        """Row-packing identical to room_view.RoomView's own _add -- the `row` an item was
        constructed with is a placeholder (discord.ui.Item requires one), overwritten here with
        whichever row actually has space."""
        if self._row_count >= 5:
            self._row_count = 0
            self._row += 1
        item.row = self._row
        self.add_item(item)
        self._row_count += 1
