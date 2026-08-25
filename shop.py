"""Shop purchase orchestration: turns one of an NPC's npcs.json "shop" entries into a currency
deduction (db.spend_currency, atomic) plus the resulting item grant -- always stored, never
auto-equipped, for equipment (same as ordinary loot/quest rewards -- the player equips manually
via !equipment), add_inventory_item for material/consumable/quest_item/horse_clothes/housing_item.
Mirrors crafting.craft and quests.turn_in's shape: compose separately-atomic db calls rather than
one all-encompassing transaction, since the currency deduction and the item grant touch different
tables.
"""

import asyncio

import db
import dungeon
import horse_clothes
import housing
import npcs
import quests

# Values are zero-arg getters, not the registries themselves -- see npcs.SHOP_KINDS' own comment
# for why a captured `dungeon.MATERIALS` etc would silently go stale the moment a content edit
# landed through the admin panel with no restart.
REGISTRIES = {
    "equipment": lambda: dungeon.EQUIPMENT,
    "material": lambda: dungeon.MATERIALS,
    "consumable": lambda: dungeon.CONSUMABLES,
    "quest_item": lambda: quests.QUEST_ITEMS,
    "horse_clothes": lambda: horse_clothes.HORSE_CLOTHES,
    "housing_item": lambda: housing.HOUSING_ITEMS,
}


async def buy(guild_id: int, user_id: int, npc_id: str, index: int) -> dict:
    """Attempts to buy the shop entry at `index` in npcs.NPCS[npc_id]["shop"]. Returns
    {"success", "status", "balance", "item", "kind"}. On failure (status "broke"), success is
    False and nothing was touched."""
    entry = npcs.NPCS[npc_id]["shop"][index]
    status, balance = await asyncio.to_thread(db.spend_currency, guild_id, user_id, entry["price"])
    if status != "ok":
        return {
            "success": False, "status": status, "balance": balance,
            "item": None, "kind": entry["kind"],
        }

    kind = entry["kind"]
    item = REGISTRIES[kind]()[entry["item_id"]]
    if kind == "equipment":
        await asyncio.to_thread(db.store_equipment_item, guild_id, user_id, item["id"])
    else:
        await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, item["id"], 1)

    return {"success": True, "status": "ok", "balance": balance, "item": item, "kind": kind}
