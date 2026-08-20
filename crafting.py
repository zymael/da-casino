"""Crafting orchestration: turns a dungeon.RECIPES entry into the material/currency consumption
(db.craft_item, atomic) plus the resulting item grant (equip-if-upgrade or store, same as ordinary
loot, or add to inventory for a consumable). Mirrors quests.py's turn_in -- composes several
separately-atomic db calls rather than one all-encompassing transaction, since the consumption
step and the grant step touch different tables (inventory vs. character_equipment/
equipment_inventory) that can't safely share one open connection/transaction.
"""

import asyncio

import db
import dungeon


async def craft(guild_id: int, user_id: int, recipe_id: str) -> dict:
    """Attempts to craft `recipe_id` for this player. Returns {"success", "status", "balance",
    "output_item", "output_kind", "equipped"}. On failure (status "insufficient_materials" or
    "broke"), success is False and output_item is None -- nothing was consumed or granted."""
    recipe = dungeon.RECIPES[recipe_id]
    status, balance = await asyncio.to_thread(
        db.craft_item, guild_id, user_id, recipe["materials"], recipe.get("currency_cost", 0)
    )
    if status != "ok":
        return {
            "success": False, "status": status, "balance": balance,
            "output_item": None, "output_kind": None, "equipped": False,
        }

    if recipe["output_kind"] == "equipment":
        item = dungeon.EQUIPMENT[recipe["output_id"]]
        equipped_items = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
        slot = item["slot"]
        if dungeon.is_upgrade(equipped_items.get(slot), item):
            await asyncio.to_thread(db.equip_item_smart, guild_id, user_id, slot, item["id"])
            equipped = True
        else:
            await asyncio.to_thread(db.store_equipment_item, guild_id, user_id, item["id"])
            equipped = False
    else:
        item = dungeon.CONSUMABLES[recipe["output_id"]]
        await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, item["id"], 1)
        equipped = False

    return {
        "success": True, "status": "ok", "balance": balance,
        "output_item": item, "output_kind": recipe["output_kind"], "equipped": equipped,
    }
