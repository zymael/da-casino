"""Selling orchestration: the reverse of shop.py's buy() -- turns one owned item into currency at
half its base_value (rounded down), at any NPC with npcs.json's "buys_items" checked. Every kind
shop.py/dreams.py grant except quest_item is sellable (quest items are narrative-bound, not
economy items -- selling one could silently break whatever quest is waiting on it, so they're left
out of SELLABLE_REGISTRIES entirely rather than gated some other way).

SELLABLE_REGISTRIES, sellable_holdings, horse_clothes_in_use, and consume_free_horse_clothes are
all public (not sell.py-private) because smash.py (destroying an item for no payout) needs the
exact same "what does this player actually own, free and clear" logic -- only what happens after
ownership is confirmed differs between the two, which each module's own top-level function handles
on its own.

Nothing currently in active use can be sold, but "in use" means something different per kind, since
ownership is tracked differently per kind:
  - equipment: only ever sold from equipment_inventory (db.sell_equipment_item never touches
    character_equipment) -- something worn simply isn't in that table at all, so it's automatically
    excluded, no extra bookkeeping needed. Sell out of equipment_inventory's spares; !unequip first
    to sell something worn.
  - housing_item: db.place_house_item already removes a placed item from the generic `inventory`
    table entirely (see its own docstring) -- same "not in the table, automatically excluded" shape
    as equipment, no extra bookkeeping needed here either.
  - horse_clothes: the one real exception. It's a *reusable wardrobe* (db.equip_horse_clothes never
    consumes inventory -- see horse_clothes_view.py's own module docstring, which already flagged
    "a future feature [that] trades/removes an owned item out from under something still wearing
    it" as a foreseeable risk this exact feature is that future feature). An owned copy stays in
    the generic `inventory` table at full qty even while equipped on one or more horses, so
    horse_clothes needs its own explicit "how many of my own horses currently have this exact item
    equipped" count (see horse_clothes_in_use), subtracted from what's offered for sale, with the
    same count re-checked at actual sale/smash time in case a horse got dressed in the gap between
    opening the popup and clicking an option.
"""

import asyncio

import db
import dungeon
import horse_clothes
import housing

# Values are zero-arg getters, not the registries themselves -- see npcs.SHOP_KINDS' own comment
# for why a captured `dungeon.MATERIALS` etc would silently go stale the moment a content edit
# landed through the admin panel with no restart.
SELLABLE_REGISTRIES = {
    "equipment": lambda: dungeon.EQUIPMENT,
    "material": lambda: dungeon.MATERIALS,
    "consumable": lambda: dungeon.CONSUMABLES,
    "horse_clothes": lambda: horse_clothes.HORSE_CLOTHES,
    "housing_item": lambda: housing.HOUSING_ITEMS,
}


def sell_price(item: dict) -> int:
    return item["base_value"] // 2


def horse_clothes_in_use(guild_id: int, user_id: int) -> dict[str, int]:
    """{item_id: count} for how many of this player's own horses currently have that exact
    cosmetic equipped, across both slots -- see the module docstring for why horse_clothes needs
    this and nothing else does. Public (not sell-specific): smash.py needs the exact same
    "how much is actually free to dispose of" check smash.py's own module docstring describes."""
    horses = db.get_ranch_horses(guild_id, user_id)
    equipped_by_horse = db.get_guild_horse_clothes(guild_id)
    counts: dict[str, int] = {}
    for horse in horses:
        for item_id in equipped_by_horse.get(horse["horse_index"], {}).values():
            counts[item_id] = counts.get(item_id, 0) + 1
    return counts


def sellable_holdings(
    held: dict[str, int], stored_equipment: dict[str, int], horse_clothes_in_use: dict[str, int] | None = None,
) -> list[tuple[str, str, int]]:
    """[(kind, item_id, qty), ...] for every item the player currently owns that some
    SELLABLE_REGISTRIES kind recognizes -- `held` is db.get_inventory's shape (material/
    consumable/horse_clothes/housing_item all share that one generic table), `stored_equipment` is
    db.get_equipment_inventory's shape (equipment only, never anything currently worn),
    `horse_clothes_in_use` is horse_clothes_in_use's shape (defaults to none currently in use).
    A quest item or anything else unrecognized is simply not sellable, not an error here -- unlike
    inventory_view._inventory_sections, this isn't the place that catches a genuine content-id
    collision bug; that's already caught elsewhere, every time !inventory renders."""
    horse_clothes_in_use = horse_clothes_in_use or {}
    result = []
    for item_id, qty in stored_equipment.items():
        if item_id in SELLABLE_REGISTRIES["equipment"]():
            result.append(("equipment", item_id, qty))
    for item_id, qty in held.items():
        for kind in ("material", "consumable", "horse_clothes", "housing_item"):
            if item_id in SELLABLE_REGISTRIES[kind]():
                sellable_qty = qty - horse_clothes_in_use.get(item_id, 0) if kind == "horse_clothes" else qty
                if sellable_qty > 0:
                    result.append((kind, item_id, sellable_qty))
                break
    return result


async def sell(guild_id: int, user_id: int, kind: str, item_id: str) -> dict:
    """Attempts to sell one copy of item_id (of the given kind) for this player. Returns
    {"success", "item", "kind", "price", "balance"}. On failure (they don't actually hold a free
    copy -- e.g. a stale picker from before they already sold/used their only one, or for
    horse_clothes, from before a horse got dressed in it since the picker opened), success is
    False and nothing was touched."""
    item = SELLABLE_REGISTRIES[kind]()[item_id]
    price = sell_price(item)
    if kind == "equipment":
        removed = await asyncio.to_thread(db.sell_equipment_item, guild_id, user_id, item_id, 1)
    elif kind == "horse_clothes":
        removed = await asyncio.to_thread(consume_free_horse_clothes, guild_id, user_id, item_id)
    else:
        removed = await asyncio.to_thread(db.consume_inventory_item, guild_id, user_id, item_id, 1)
    if not removed:
        return {"success": False, "item": item, "kind": kind, "price": price, "balance": None}

    new_balance = await asyncio.to_thread(db.update_balance, guild_id, user_id, price)
    return {"success": True, "item": item, "kind": kind, "price": price, "balance": new_balance}


def consume_free_horse_clothes(guild_id: int, user_id: int, item_id: str) -> bool:
    """Re-checks horse_clothes_in_use right before consuming, not just at picker-build time, so a
    horse dressed in this exact item in the gap between opening the sell/smash popup and picking
    an option can't have it removed out from under it -- consume_inventory_item alone has no way
    to know a copy is "reserved" this way, since the wardrobe model never marks it unavailable in
    `inventory` the way an equipped weapon leaves equipment_inventory entirely. Public: shared by
    both sell.sell (payout) and smash.smash (no payout) -- same ownership check either way, only
    what happens after consuming differs, which lives in each caller, not here."""
    held = db.get_inventory(guild_id, user_id)
    in_use = horse_clothes_in_use(guild_id, user_id).get(item_id, 0)
    if held.get(item_id, 0) - in_use < 1:
        return False
    return db.consume_inventory_item(guild_id, user_id, item_id, 1)
