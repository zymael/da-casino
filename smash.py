"""Destroying orchestration: the one-way opposite of sell.py's sell() -- removes one owned item
from inventory permanently, in exchange for a random scrap of junk rather than currency. Reuses
sell.py's SELLABLE_REGISTRIES/sellable_holdings directly rather than re-deriving them -- "what does
this player actually own, free and clear" is identical logic whether the item is about to be sold
or smashed, only what happens after ownership is confirmed differs (a payout vs. a garbage
material). Quest items are excluded for the same reason sell.py excludes them: narrative-bound,
not disposable economy items.

An item can opt out of being destroyed entirely via its own content's optional
"unsmashable_message" field -- checked generically here by field presence, never by item id, so
any future item can decline being smashed with its own flavor text and no code change (see
admin_schemas.py's "equipment"/"materials"/"consumables"/"horse_clothes"/"housing_items" field
lists).

Every successful smash always grants exactly one random material flagged "garbage": true in
dungeon_materials.json -- freeform junk for now (a bent fork, some scrap metal, whatever), with no
recipe yet actually consuming it. What these turn into is a later problem for dungeon_recipes.json,
not this module's."""

import asyncio
import random

import db
import dungeon
import sell

SMASHABLE_REGISTRIES = sell.SELLABLE_REGISTRIES
smashable_holdings = sell.sellable_holdings


def _random_garbage_material() -> dict | None:
    """One random pick among every material flagged "garbage": true -- None if none exist yet (a
    smash still destroys the item, just yields nothing, rather than erroring)."""
    garbage = [item for item in dungeon.MATERIALS.values() if item.get("garbage")]
    return random.choice(garbage) if garbage else None


async def smash(guild_id: int, user_id: int, kind: str, item_id: str) -> dict:
    """Attempts to destroy one copy of item_id (of the given kind) for this player. Returns
    {"success", "item", "kind", "protected", "byproduct"}. "protected" is True (and "success"
    False, nothing touched) when the item's content declined via "unsmashable_message" -- checked
    before any inventory access. Otherwise "success" is False the same way sell.sell's is: a stale
    picker from before the last copy was already used/sold/smashed. "byproduct" is the garbage
    material dict granted on a successful smash (None if no garbage material exists yet, or the
    smash wasn't successful)."""
    item = SMASHABLE_REGISTRIES[kind]()[item_id]
    if item.get("unsmashable_message"):
        return {"success": False, "item": item, "kind": kind, "protected": True, "byproduct": None}

    if kind == "equipment":
        removed = await asyncio.to_thread(db.sell_equipment_item, guild_id, user_id, item_id, 1)
    elif kind == "horse_clothes":
        removed = await asyncio.to_thread(sell.consume_free_horse_clothes, guild_id, user_id, item_id)
    else:
        removed = await asyncio.to_thread(db.consume_inventory_item, guild_id, user_id, item_id, 1)

    byproduct = _random_garbage_material() if removed else None
    if byproduct:
        await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, byproduct["id"], 1)
    return {"success": removed, "item": item, "kind": kind, "protected": False, "byproduct": byproduct}
