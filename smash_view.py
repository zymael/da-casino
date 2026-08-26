"""The smash popup -- one Select listing every item this player currently owns that smash.py
recognizes as destroyable (same kind set sell_view.py already lists as sellable, minus any price).
Not NPC-scoped: unlike sell_view.py (opened via npc_view.SellButton for a specific NPC), !smash is
reachable directly as its own command today (see bot.py) since the NPC/room meant to eventually
wrap it don't exist yet -- per CLAUDE.md's "rooms are thin command wrappers" rule, adding that room
button later needs no changes here. Same ephemeral-popup-that-redraws-itself shape as
sell_view.py/shop_view.py; smashing something re-renders the same popup in place so a player can
destroy more than one thing without reopening it. A protected item (has "unsmashable_message") is
listed with no special marker -- selecting it and getting told off is the point.
"""

import asyncio

import discord

import db
import dungeon
import sell
import smash

_KIND_EMOJI = {
    "equipment": "⚔️", "material": "⛏️", "consumable": "🧪", "horse_clothes": "👒", "housing_item": "🛋️",
}
MAX_SELECT_OPTIONS = 25


def _item_blurb(item: dict) -> str:
    """Same field-name reconciliation as sell_view._item_blurb -- most content uses "flavor",
    housing items use "description"."""
    return item.get("flavor") or item.get("description") or ""


async def build_smash_display(guild_id: int, user_id: int) -> tuple[discord.Embed, "SmashView"]:
    held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
    stored_equipment = await asyncio.to_thread(db.get_equipment_inventory, guild_id, user_id)
    horse_clothes_in_use = await asyncio.to_thread(sell.horse_clothes_in_use, guild_id, user_id)
    holdings = smash.smashable_holdings(held, stored_equipment, horse_clothes_in_use)

    embed = discord.Embed(title="🔨 Smash an item", color=discord.Color.dark_red())
    if holdings:
        lines = []
        for kind, item_id, qty in holdings:
            item = smash.SMASHABLE_REGISTRIES[kind]()[item_id]
            qty_suffix = f" x{qty}" if qty > 1 else ""
            lines.append(f"{_KIND_EMOJI[kind]} **{item['name']}**{qty_suffix}")
        embed.description = "\n".join(lines)
    else:
        embed.description = "You don't have anything to smash right now."
    embed.set_footer(text="Destroyed items are gone for good -- but the wreckage is worth something.")

    view = SmashView(guild_id, user_id, holdings)
    return embed, view


class SmashView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, holdings: list[tuple[str, str, int]]):
        super().__init__(timeout=300)
        if holdings:  # a Select needs at least one option -- nothing to add if they own nothing smashable
            self.add_item(SmashSelect(guild_id, user_id, holdings))


class SmashSelect(discord.ui.Select):
    def __init__(self, guild_id: int, user_id: int, holdings: list[tuple[str, str, int]]):
        self.guild_id = guild_id
        self.user_id = user_id
        self.holdings = holdings
        options = []
        for i, (kind, item_id, qty) in enumerate(holdings[:MAX_SELECT_OPTIONS]):
            item = smash.SMASHABLE_REGISTRIES[kind]()[item_id]
            rarity_emoji = dungeon.RARITY_EMOJI[item["rarity"]] if kind == "equipment" else None
            qty_suffix = f" x{qty}" if qty > 1 else ""
            options.append(discord.SelectOption(
                label=f"{item['name']}{qty_suffix}"[:100],
                value=str(i),
                description=_item_blurb(item)[:100] or None,
                emoji=rarity_emoji,
            ))
        super().__init__(placeholder="Smash something...", options=options)

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        kind, item_id, _ = self.holdings[index]
        result = await smash.smash(self.guild_id, self.user_id, kind, item_id)
        item = result["item"]

        if result["protected"]:
            # Falls back to a generic line for a garbage item with no unsmashable_message of its
            # own -- see smash.smash's docstring for why one could reach here at all (stale picker).
            result_text = item.get("unsmashable_message") or "It's already garbage. There's nothing left to smash."
        elif not result["success"]:
            result_text = "You don't have that anymore."
        elif result["byproduct"]:
            junk = result["byproduct"]
            result_text = f"💥 Destroyed **{item['name']}**... and salvaged **{junk['name']}** from the wreckage!"
        else:
            result_text = f"💥 Destroyed **{item['name']}**. Nothing worth keeping survived."

        embed, view = await build_smash_display(self.guild_id, self.user_id)
        embed.add_field(name="Result", value=result_text, inline=False)
        await interaction.response.edit_message(embed=embed, view=view)
