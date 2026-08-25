"""Crafting orchestration: turns a dungeon.RECIPES entry into the material/currency consumption
(db.craft_item, atomic) plus the resulting item grant -- equip-if-upgrade or store for equipment
(same as ordinary loot), or add to the generic `inventory` table for every other output_kind
(consumable, quest_item, horse_clothes, housing_item -- see _INVENTORY_REGISTRIES). Mirrors
quests.py's turn_in and shop.py's buy -- composes several separately-atomic db calls rather than
one all-encompassing transaction, since the consumption step and the grant step touch different
tables (inventory vs. character_equipment/equipment_inventory) that can't safely share one open
connection/transaction.
"""

import asyncio

import db
import dungeon
import horse_clothes
import housing
import quests

# Every non-equipment output_kind just adds one to the generic `inventory` table by item_id --
# equipment is the only kind with an equip-or-store decision (see craft() below). Mirrors shop.py's
# own REGISTRIES.
_INVENTORY_REGISTRIES = {
    "consumable": dungeon.CONSUMABLES,
    "quest_item": quests.QUEST_ITEMS,
    "horse_clothes": horse_clothes.HORSE_CLOTHES,
    "housing_item": housing.HOUSING_ITEMS,
}


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
        item = _INVENTORY_REGISTRIES[recipe["output_kind"]][recipe["output_id"]]
        await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, item["id"], 1)
        equipped = False

    await quests.record_progress(guild_id, user_id, "craft_item", recipe_id=recipe_id)

    return {
        "success": True, "status": "ok", "balance": balance,
        "output_item": item, "output_kind": recipe["output_kind"], "equipped": equipped,
    }


async def combine(guild_id: int, user_id: int, material_ids: list[str]) -> dict:
    """Discovery-based crafting: `material_ids` is what the player picked (e.g. ["stick",
    "stick"] for a 2-stick combo) with no recipe chosen up front -- this is what figures out
    whether that combo actually means anything. Delegates the real consumption/grant work to
    craft() once a match is found, so there's exactly one place that logic lives; this only adds
    the material-combo lookup and discovery tracking on top.

    Returns everything craft() does, plus "newly_discovered" (True only the first time this
    player has ever landed on this particular recipe -- lets the UI call out a genuine discovery
    differently from a routine repeat craft). On "no_match", nothing is touched -- a failed guess
    costs nothing, so experimenting isn't punishing."""
    materials: dict[str, int] = {}
    for material_id in material_ids:
        materials[material_id] = materials.get(material_id, 0) + 1

    recipe = dungeon.find_recipe_by_materials(materials)
    if recipe is None:
        return {
            "success": False, "status": "no_match", "balance": None,
            "output_item": None, "output_kind": None, "equipped": False, "newly_discovered": False,
        }

    result = await craft(guild_id, user_id, recipe["id"])
    result["newly_discovered"] = (
        await asyncio.to_thread(db.mark_recipe_discovered, guild_id, recipe["id"], user_id)
        if result["success"] else False
    )
    return result
