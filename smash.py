"""Destroying orchestration: the one-way opposite of sell.py's sell() -- removes one owned item
from inventory permanently, for no reward. Reuses sell.py's SELLABLE_REGISTRIES/sellable_holdings
directly rather than re-deriving them -- "what does this player actually own, free and clear" is
identical logic whether the item is about to be sold or smashed, only what happens after ownership
is confirmed differs (a payout vs. nothing). Quest items are excluded for the same reason sell.py
excludes them: narrative-bound, not disposable economy items.

An item can opt out of being destroyed entirely via its own content's optional
"unsmashable_message" field -- checked generically here by field presence, never by item id, so
any future item can decline being smashed with its own flavor text and no code change (see
admin_schemas.py's "equipment"/"materials"/"consumables"/"horse_clothes"/"housing_items" field
lists)."""

import asyncio

import db
import sell

SMASHABLE_REGISTRIES = sell.SELLABLE_REGISTRIES
smashable_holdings = sell.sellable_holdings


async def smash(guild_id: int, user_id: int, kind: str, item_id: str) -> dict:
    """Attempts to destroy one copy of item_id (of the given kind) for this player. Returns
    {"success", "item", "kind", "protected"}. "protected" is True (and "success" False, nothing
    touched) when the item's content declined via "unsmashable_message" -- checked before any
    inventory access. Otherwise "success" is False the same way sell.sell's is: a stale picker
    from before the last copy was already used/sold/smashed."""
    item = SMASHABLE_REGISTRIES[kind]()[item_id]
    if item.get("unsmashable_message"):
        return {"success": False, "item": item, "kind": kind, "protected": True}

    if kind == "equipment":
        removed = await asyncio.to_thread(db.sell_equipment_item, guild_id, user_id, item_id, 1)
    elif kind == "horse_clothes":
        removed = await asyncio.to_thread(sell.consume_free_horse_clothes, guild_id, user_id, item_id)
    else:
        removed = await asyncio.to_thread(db.consume_inventory_item, guild_id, user_id, item_id, 1)
    return {"success": removed, "item": item, "kind": kind, "protected": False}
