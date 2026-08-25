"""Selling orchestration: the reverse of shop.py's buy() -- turns one owned item into currency at
half its base_value (rounded down), at any NPC with npcs.json's "buys_items" checked. Every kind
shop.py/dreams.py grant except quest_item is sellable (quest items are narrative-bound, not
economy items -- selling one could silently break whatever quest is waiting on it, so they're left
out of SELLABLE_REGISTRIES entirely rather than gated some other way). Equipment is only ever sold
from equipment_inventory (db.sell_equipment_item never touches character_equipment), so something
currently worn can't be sold out from under the player by accident -- they'd have to !unequip it
first, same "manual, deliberate" spirit as !equipment's own picker.
"""

import asyncio

import db
import dungeon
import horse_clothes
import housing

SELLABLE_REGISTRIES = {
    "equipment": dungeon.EQUIPMENT,
    "material": dungeon.MATERIALS,
    "consumable": dungeon.CONSUMABLES,
    "horse_clothes": horse_clothes.HORSE_CLOTHES,
    "housing_item": housing.HOUSING_ITEMS,
}


def sell_price(item: dict) -> int:
    return item["base_value"] // 2


def sellable_holdings(held: dict[str, int], stored_equipment: dict[str, int]) -> list[tuple[str, str, int]]:
    """[(kind, item_id, qty), ...] for every item the player currently owns that some
    SELLABLE_REGISTRIES kind recognizes -- `held` is db.get_inventory's shape (material/
    consumable/horse_clothes/housing_item all share that one generic table), `stored_equipment` is
    db.get_equipment_inventory's shape (equipment only, never anything currently worn). A quest
    item or anything else unrecognized is simply not sellable, not an error here -- unlike
    inventory_view._inventory_sections, this isn't the place that catches a genuine content-id
    collision bug; that's already caught elsewhere, every time !inventory renders."""
    result = []
    for item_id, qty in stored_equipment.items():
        if item_id in SELLABLE_REGISTRIES["equipment"]:
            result.append(("equipment", item_id, qty))
    for item_id, qty in held.items():
        for kind in ("material", "consumable", "horse_clothes", "housing_item"):
            if item_id in SELLABLE_REGISTRIES[kind]:
                result.append((kind, item_id, qty))
                break
    return result


async def sell(guild_id: int, user_id: int, kind: str, item_id: str) -> dict:
    """Attempts to sell one copy of item_id (of the given kind) for this player. Returns
    {"success", "item", "kind", "price", "balance"}. On failure (they don't actually hold one --
    e.g. a stale picker from before they already sold/used their only copy), success is False and
    nothing was touched."""
    item = SELLABLE_REGISTRIES[kind][item_id]
    price = sell_price(item)
    if kind == "equipment":
        removed = await asyncio.to_thread(db.sell_equipment_item, guild_id, user_id, item_id, 1)
    else:
        removed = await asyncio.to_thread(db.consume_inventory_item, guild_id, user_id, item_id, 1)
    if not removed:
        return {"success": False, "item": item, "kind": kind, "price": price, "balance": None}

    new_balance = await asyncio.to_thread(db.update_balance, guild_id, user_id, price)
    return {"success": True, "item": item, "kind": kind, "price": price, "balance": new_balance}
