"""Registry of NPCs -- id, display name, which room they're in, optional portrait sprite, static
greeting, and an optional achievement granted (idempotently) the first time they're talked to.
Mirrors quests.py's own loader shape (achievements.py's ACHIEVEMENTS, dungeon.py's EQUIPMENT
registry).

This module deliberately knows nothing about quests -- an NPC is just "someone with a face, a
room, and a default thing to say." Whatever quests.py quest is currently active with a given NPC
(if any) is what actually decides what dialogue shows on any given click (see
npc_view.TalkToNpcButton); greet_message is only ever the fallback for "no quest is active with
them right now."

"room" is what makes NPC placement data instead of a per-hub Python decision -- every hub's view
loops over quests.npcs_present_in_room(room_id) uniformly rather than hardcoding which NPC ids it
adds a button for. An NPC's optional "visible_trigger" (same condition vocabulary as a quest's own
triggers -- quests.TRIGGER_SCHEMAS) additionally gates whether they're currently present at all
(e.g. the Greasy Princess only after mondor_goblin_chieftain is complete) -- absent means always
present in their room. This module can't validate visible_trigger's contents itself (that needs
quests.TRIGGER_SCHEMAS/_validate_trigger, and quests.py already imports this module -- the reverse
would be circular), so quests.py cross-validates every NPC's visible_trigger right after loading
both registries; see the bottom of quests.py.

An NPC's optional "shop" is a non-empty list of {kind, item_id, price} -- any item any registry
recognizes (dungeon.EQUIPMENT/MATERIALS/CONSUMABLES, or a quests.QUEST_ITEMS one) can be sold, told
apart by "kind" rather than inferred from item_id the way inventory_view.py has to for a held
item, since a shop entry needs to be unambiguous before the item is ever owned. Presence of a
non-empty list is what makes npc_view.ShopButton show a store icon for this NPC -- there's no
separate on/off flag. This module can validate the dungeon-backed kinds itself (dungeon.py doesn't
import this module, so that direction isn't circular) but not "quest_item" (same circularity as
visible_trigger above -- quests.py cross-validates those entries too, right alongside
visible_trigger at the bottom of quests.py).
"""

import json
import os

import achievements
import dungeon

_NPCS_PATH = os.path.join(os.path.dirname(__file__), "npcs.json")
_REQUIRED_NPC_FIELDS = {"id", "name", "room", "greet_message"}
_REQUIRED_SHOP_ENTRY_FIELDS = {"kind", "item_id", "price"}
# "quest_item" maps to None -- its item_id can't be checked here (quests.py isn't importable from
# this module; see module docstring), so it's cross-validated in quests.py instead.
SHOP_KINDS = {
    "equipment": dungeon.EQUIPMENT,
    "material": dungeon.MATERIALS,
    "consumable": dungeon.CONSUMABLES,
    "quest_item": None,
}


def _load_npcs(path: str = _NPCS_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    npcs_by_id: dict[str, dict] = {}
    achievement_kinds = {a["kind"] for a in achievements.ACHIEVEMENTS}
    for entry in raw:
        npc_id = entry.get("id", "?")
        missing = _REQUIRED_NPC_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"npcs.json: npc {npc_id!r} missing field(s): {sorted(missing)}")
        if npc_id in npcs_by_id:
            raise ValueError(f"npcs.json: duplicate npc id {npc_id!r}")
        greet_achievement = entry.get("greet_achievement")
        if greet_achievement and greet_achievement not in achievement_kinds:
            raise ValueError(f"npcs.json: npc {npc_id!r} has unknown greet_achievement {greet_achievement!r}")
        visible_trigger = entry.get("visible_trigger")
        if visible_trigger is not None and not (isinstance(visible_trigger, dict) and "type" in visible_trigger):
            raise ValueError(f"npcs.json: npc {npc_id!r} has a malformed visible_trigger")
        shop = entry.get("shop")
        if shop is not None:
            if not isinstance(shop, list) or not shop:
                raise ValueError(f"npcs.json: npc {npc_id!r} has an empty or malformed shop")
            for i, shop_entry in enumerate(shop):
                if not isinstance(shop_entry, dict) or _REQUIRED_SHOP_ENTRY_FIELDS - shop_entry.keys():
                    raise ValueError(f"npcs.json: npc {npc_id!r} shop entry {i} missing field(s)")
                kind = shop_entry["kind"]
                if kind not in SHOP_KINDS:
                    raise ValueError(f"npcs.json: npc {npc_id!r} shop entry {i} has unknown kind {kind!r}")
                if not isinstance(shop_entry["price"], int) or shop_entry["price"] <= 0:
                    raise ValueError(f"npcs.json: npc {npc_id!r} shop entry {i} price must be a positive integer")
                registry = SHOP_KINDS[kind]
                if registry is not None and shop_entry["item_id"] not in registry:
                    raise ValueError(
                        f"npcs.json: npc {npc_id!r} shop entry {i} item_id {shop_entry['item_id']!r} "
                        f"not in dungeon's {kind!r} registry"
                    )
        npcs_by_id[npc_id] = entry
    return npcs_by_id


NPCS = _load_npcs()
