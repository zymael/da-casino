"""The shop purchase popup -- one Select listing an NPC's npcs.json "shop" entries (25-max per
Discord Select, same limit inventory_view.EquipmentSlotSelect already works around), opened via
npc_view.ShopButton as an ephemeral popup (same shape as hub_ui.InventoryButton/EquipmentButton),
not part of the room's own persistent view. Buying re-renders the same popup in place so a player
can buy more than one thing without reopening it.
"""

import asyncio

import discord

import db
import dungeon
import inventory_view
import npcs
import shop

_KIND_EMOJI = {"equipment": "⚔️", "material": "⛏️", "consumable": "🧪", "quest_item": "🎒", "horse_clothes": "👒"}
MAX_SELECT_OPTIONS = 25


def _item_blurb(kind: str, item: dict) -> str:
    """quest items describe themselves with "description", everything else with "flavor" --
    the one place those two field names need reconciling for shop rendering."""
    return item.get("flavor") or item.get("description") or ""


def _entry_line(entry: dict) -> str:
    item = shop.REGISTRIES[entry["kind"]][entry["item_id"]]
    emoji = _KIND_EMOJI[entry["kind"]]
    blurb = _item_blurb(entry["kind"], item)
    lines = []
    if entry["kind"] == "equipment":
        # Same detail a player would see in !inventory/!equipment before buying -- rarity dot,
        # stat bonuses, and any on_use/on_hit effect, not just a bare name and price.
        emoji = f"{emoji}{dungeon.RARITY_EMOJI[item['rarity']]}"
        stat_text = inventory_view.stat_bonus_text(item)
        stat_suffix = f" ({stat_text})" if stat_text else ""
        lines.append(f"{emoji} **{item['name']}**{stat_suffix} — {entry['price']} gold")
        lines.extend(f"> {effect_line}" for effect_line in inventory_view.dynamic_effect_lines(item))
    else:
        lines.append(f"{emoji} **{item['name']}** — {entry['price']} gold")
    if blurb:
        lines.append(f"> {blurb}")
    return "\n".join(lines)


async def build_shop_display(guild_id: int, user_id: int, npc_id: str) -> tuple[discord.Embed, "ShopView"]:
    npc = npcs.NPCS[npc_id]
    balance = await asyncio.to_thread(db.get_balance, guild_id, user_id)
    currency = db.get_currency_name(guild_id)

    embed = discord.Embed(title=f"🛒 {npc['name']}'s Shop", color=discord.Color.gold())
    embed.description = "\n\n".join(_entry_line(entry) for entry in npc["shop"])
    embed.set_footer(text=f"You have {balance} {currency}.")

    view = ShopView(guild_id, user_id, npc_id, npc["shop"], currency)
    return embed, view


class ShopView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, npc_id: str, entries: list[dict], currency: str):
        super().__init__(timeout=300)
        self.add_item(ShopSelect(guild_id, user_id, npc_id, entries, currency))


class ShopSelect(discord.ui.Select):
    def __init__(self, guild_id: int, user_id: int, npc_id: str, entries: list[dict], currency: str):
        self.guild_id = guild_id
        self.user_id = user_id
        self.npc_id = npc_id
        self.currency = currency
        options = []
        for i, entry in enumerate(entries[:MAX_SELECT_OPTIONS]):
            item = shop.REGISTRIES[entry["kind"]][entry["item_id"]]
            rarity_emoji = dungeon.RARITY_EMOJI[item["rarity"]] if entry["kind"] == "equipment" else None
            options.append(discord.SelectOption(
                label=f"{item['name']} — {entry['price']} {currency}",
                value=str(i),
                description=_item_blurb(entry["kind"], item)[:100] or None,
                emoji=rarity_emoji,
            ))
        super().__init__(placeholder="Buy something...", options=options)

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        result = await shop.buy(self.guild_id, self.user_id, self.npc_id, index)
        if not result["success"]:
            await interaction.response.send_message(
                f"You can't afford that -- you have {result['balance']} {self.currency}.", ephemeral=True,
            )
            return

        item = result["item"]
        if result["kind"] == "equipment":
            status = "equipped!" if result["equipped"] else "stored in `!equipment` (your current gear is better)"
            purchase_text = f"⚔️ Bought **{item['name']}** — {status}"
        else:
            purchase_text = f"🎉 Bought **{item['name']}**! Check `!inventory`."

        embed, view = await build_shop_display(self.guild_id, self.user_id, self.npc_id)
        embed.add_field(name="Purchased", value=purchase_text, inline=False)
        await interaction.response.edit_message(embed=embed, view=view)
