"""Shop purchase orchestration: turns one of an NPC's npcs.json "shop" entries into a currency
deduction (db.spend_currency, atomic) plus the resulting item grant -- equip-if-upgrade or store
for equipment (same is_upgrade rule as ordinary loot/quest rewards), add_inventory_item for
material/consumable/quest_item. Mirrors crafting.craft and quests.turn_in's shape: compose
separately-atomic db calls rather than one all-encompassing transaction, since the currency
deduction and the item grant touch different tables.
"""

import asyncio

import db
import dungeon
import horse_clothes
import housing
import npcs
import quests

REGISTRIES = {
    "equipment": dungeon.EQUIPMENT,
    "material": dungeon.MATERIALS,
    "consumable": dungeon.CONSUMABLES,
    "quest_item": quests.QUEST_ITEMS,
    "horse_clothes": horse_clothes.HORSE_CLOTHES,
    "housing_item": housing.HOUSING_ITEMS,
}


async def buy(guild_id: int, user_id: int, npc_id: str, index: int) -> dict:
    """Attempts to buy the shop entry at `index` in npcs.NPCS[npc_id]["shop"]. Returns
    {"success", "status", "balance", "item", "kind", "equipped"}. On failure (status "broke"),
    success is False and nothing was touched."""
    entry = npcs.NPCS[npc_id]["shop"][index]
    status, balance = await asyncio.to_thread(db.spend_currency, guild_id, user_id, entry["price"])
    if status != "ok":
        return {
            "success": False, "status": status, "balance": balance,
            "item": None, "kind": entry["kind"], "equipped": False,
        }

    kind = entry["kind"]
    item = REGISTRIES[kind][entry["item_id"]]
    equipped = False
    if kind == "equipment":
        equipped_items = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
        slot = item["slot"]
        if dungeon.is_upgrade(equipped_items.get(slot), item):
            await asyncio.to_thread(db.equip_item_smart, guild_id, user_id, slot, item["id"])
            equipped = True
        else:
            await asyncio.to_thread(db.store_equipment_item, guild_id, user_id, item["id"])
    else:
        await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, item["id"], 1)

    return {"success": True, "status": "ok", "balance": balance, "item": item, "kind": kind, "equipped": equipped}
