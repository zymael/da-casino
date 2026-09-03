"""!journal (bot.py's journal_cmd) support -- shows every started quest using its journal_text (an
objective-style summary, distinct from the NPC's own dialogue prompt -- see quests.quest_log's own
docstring) and, for whichever quests are ready, a JournalTurnInButton so a stage can be completed
without visiting the giving NPC. Talking to the NPC (npc_view.py) still works exactly as before --
this is an additional route to quests.turn_in/quests.check_new_quests, not a replacement, per
CLAUDE.md's "Rooms/NPCs" section: bot.py never builds discord.ui components directly, so this
module (requested by journal_cmd, not pre-built into any room) is where that UI lives, same shape
as ranch_view.build_train_horse_picker.
"""

import discord

import npcs
import quests


class JournalTurnInButton(discord.ui.Button):
    """One per quest quest_log reports can_turn_in -- calls quests.turn_in directly (it only ever
    needed a quest_id, never an npc_id) and reports the result as a followup, same wording as
    npc_view.TurnInButton's, just without that button's room-banner redraw since !journal isn't a
    room."""

    def __init__(self, entry: dict, row: int):
        npc = npcs.NPCS[quests.QUESTS_BY_ID[entry["quest_id"]]["npc"]]
        label = entry["turn_in_label"] or (
            f"🎁 Give {npc['name']} the {entry['item']['name']}" if entry["item"] else f"✅ Turn in: {entry['name']}"
        )
        super().__init__(label=label[:80], style=discord.ButtonStyle.success, row=row)
        self.quest_id = entry["quest_id"]

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        result = await quests.turn_in(guild_id, user_id, self.quest_id)
        if not result["success"]:
            await interaction.response.send_message(
                "That's not ready to turn in anymore -- check `!journal` again.", ephemeral=True,
            )
            return

        lines = [result["message"]] if result["message"] else []
        reward_item = result["reward_item"]
        if reward_item:
            if result["reward_item_kind"] == "equipment":
                lines.append(f"⚔️ Received **{reward_item['name']}**, stored in `!equipment`.")
            else:
                lines.append(f"🎁 Received **{reward_item['name']}**! Check `!inventory`.")
        lines.append("🗺️ Quest complete! Check `!journal`." if result["quest_complete"] else "🗺️ Quest updated! Check `!journal`.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class JournalView(discord.ui.View):
    """One JournalTurnInButton per turn-in-ready entry in `log` -- empty (no buttons at all) when
    nothing's ready, which bot.py's journal_cmd treats the same as no view."""

    def __init__(self, log: list[dict]):
        super().__init__(timeout=300)
        row = 0
        for entry in log:
            if entry["can_turn_in"]:
                self.add_item(JournalTurnInButton(entry, row))
                row = (row + 1) % 5


async def build_journal_display(
    guild_id: int, user_id: int, display_name: str,
) -> tuple[discord.Embed, discord.ui.View | None, list[str]]:
    """Starts any newly-eligible quest first (quests.check_new_quests, unscoped to any one NPC --
    the whole point of !journal is picking up and advancing quests without visiting anyone), then
    renders the log. Returns (embed, view-or-None, newly_started_quest_ids) so journal_cmd can
    announce whatever just started before showing the log itself."""
    newly_started = await quests.check_new_quests(guild_id, user_id)
    log = await quests.quest_log(guild_id, user_id)

    embed = discord.Embed(title=f"📔 {display_name}'s Journal", color=discord.Color.blurple())
    if not log:
        embed.description = "No quests started yet."
    else:
        # In Progress before Complete -- sorted() is stable, so quests.json order is preserved
        # within each group.
        for entry in sorted(log, key=lambda e: e["complete"]):
            status = "✅ Complete" if entry["complete"] else f"Stage {entry['stage_index'] + 1}/{entry['total_stages']}"
            npc_name = npcs.NPCS[entry["npc"]]["name"]
            value = f"*Giver: {npc_name}*\n{entry['journal_text']}"
            if entry["can_turn_in"]:
                value += "\n*Ready to turn in below.*"
            embed.add_field(name=f"{entry['name']} ({status})", value=value, inline=False)

    view = JournalView(log)
    if not view.children:
        view = None
    return embed, view, newly_started
