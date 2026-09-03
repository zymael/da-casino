"""!journal (bot.py's journal_cmd) support -- read-only: shows every started quest using its
journal_text (an objective-style summary, distinct from the NPC's own dialogue prompt -- see
quests.quest_log's own docstring) plus a "ready to turn in" hint once a stage is satisfied.
Turning in only ever happens by actually talking to the NPC (npc_view.py's nested conversation
flow) -- deliberately no shortcut here, unlike an earlier version of this module."""

import discord

import npcs
import quests


async def build_journal_display(
    guild_id: int, user_id: int, display_name: str,
) -> tuple[discord.Embed, list[str]]:
    """Starts any newly-eligible quest first (quests.check_new_quests, unscoped to any one NPC --
    the whole point of !journal is picking up quests without visiting anyone), then renders the
    log. Returns (embed, newly_started_quest_ids) so journal_cmd can announce whatever just started
    before showing the log itself."""
    newly_started = await quests.check_new_quests(guild_id, user_id)
    log = await quests.quest_log(guild_id, user_id)

    embed = discord.Embed(title=f"📔 {display_name}'s Journal", color=discord.Color.blurple())
    if not log:
        embed.description = "No quests started yet."
    else:
        # In Progress before Complete -- sorted() is stable, so quests.json order is preserved
        # within each group. header tracks which section header(s) have been added already, so
        # each only shows once, right before its group's first entry.
        header = None
        for entry in sorted(log, key=lambda e: e["complete"]):
            if entry["complete"] and header != "complete":
                embed.add_field(name="​", value="**Complete**", inline=False)
                header = "complete"
            elif not entry["complete"] and header is None:
                embed.add_field(name="​", value="**In Progress**", inline=False)
                header = "in_progress"
            status = "✅ Complete" if entry["complete"] else f"Stage {entry['stage_index'] + 1}/{entry['total_stages']}"
            npc_name = npcs.NPCS[entry["npc"]]["name"]
            value = f"*Giver: {npc_name}*\n{entry['journal_text']}"
            if entry["can_turn_in"]:
                value += f"\n*Ready to turn in -- go talk to {npc_name}.*"
            embed.add_field(name=f"{entry['name']} ({status})", value=value, inline=False)

    return embed, newly_started
