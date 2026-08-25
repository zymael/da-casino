"""Housing: a player's 3x3 grid of placed "housing items," each granting one passive effect.

Ownership of a housing item lives in the same generic `inventory` table (db.py) that quest items,
materials, and consumables already share by item_id -- see the collision check below. Placement
(which item sits in which of the 9 grid slots) is its own table, db.house_placements, following
character_equipment's "one row per filled slot, absence = empty" shape but with an integer slot
instead of a named one, since all 9 positions are mechanically identical.

Housing effects are deliberately a separate, much smaller vocabulary from dungeon.EFFECT_PARAM_SCHEMAS
-- that one is entirely combat-shaped (trigger/chance/target/aoe machinery for damage/heal/buff/debuff
during a fight), which doesn't fit a static item that just sits in a house. HOUSING_EFFECT_TYPES
below is the housing analogue: no trigger, no chance, just a value (and, for stat_bonus only, which
stat).
"""
import json
import os

import db
import dungeon
import quests

_HOUSING_ITEMS_PATH = os.path.join(os.path.dirname(__file__), "housing_items.json")
# "base_value" -- not "value" -- for the sell.py economy price this item is worth (half of it, on
# sale): "value" already means this item's own effect magnitude (see HOUSING_EFFECT_TYPES below),
# a completely different number that would collide with an economy-value field of the same name.
_REQUIRED_ITEM_FIELDS = {"id", "name", "emoji", "description", "effect_type", "value", "base_value"}

# effect_type -> {value_kind, requires_stat}. value_kind is documentation only (nothing enforces it
# at load time beyond "value is a number") -- "percent" types are read as +N% by their hook site,
# "flat" types are added directly. requires_stat gates whether an item's "stat" field is
# required/forbidden -- see _load_housing_items.
HOUSING_EFFECT_TYPES = {
    "dungeon_loot_bonus": {"value_kind": "percent", "requires_stat": False},
    "dungeon_xp_bonus": {"value_kind": "percent", "requires_stat": False},
    "ranch_training_bonus": {"value_kind": "percent", "requires_stat": False},
    "stat_bonus": {"value_kind": "flat", "requires_stat": True},
    "rest_energy_bonus": {"value_kind": "flat", "requires_stat": False},
    "rest_gold_bonus": {"value_kind": "flat", "requires_stat": False},
}

# Matches dungeon.compute_effective_stats' stat keys -- a housing stat_bonus item stacks alongside
# equipment's own constant stat bonuses via that same aggregation, so the vocabulary has to match.
HOUSING_STATS = ("hp", "atk", "def", "spatk", "spdef", "speed")


def _load_housing_items(path: str = _HOUSING_ITEMS_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    items: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_ITEM_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"housing_items.json: item {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in items:
            raise ValueError(f"housing_items.json: duplicate item id {entry_id!r}")
        effect_type = entry["effect_type"]
        if effect_type not in HOUSING_EFFECT_TYPES:
            raise ValueError(f"housing_items.json: item {entry_id!r} has unknown effect_type {effect_type!r}")
        requires_stat = HOUSING_EFFECT_TYPES[effect_type]["requires_stat"]
        stat = entry.get("stat")
        if requires_stat and stat not in HOUSING_STATS:
            raise ValueError(
                f"housing_items.json: item {entry_id!r} (effect_type {effect_type!r}) needs a "
                f"'stat' field set to one of {HOUSING_STATS}"
            )
        if not requires_stat and stat:
            raise ValueError(
                f"housing_items.json: item {entry_id!r} sets 'stat' but effect_type {effect_type!r} "
                f"doesn't use one"
            )
        if entry["base_value"] < 0:
            raise ValueError(f"housing_items.json: item {entry_id!r} base_value must be >= 0")
        items[entry_id] = entry
    return items


HOUSING_ITEMS = _load_housing_items()

# Housing items key the same generic `inventory` table by item_id as quest items, materials, and
# consumables (see quests.py's own identical check for materials/consumables) -- a collision here
# is a genuine, easy-to-hit content bug (two hand-edited JSON files independently picking the same
# id), caught loudly at import time rather than silently letting one kind of item shadow another.
_item_id_collisions = HOUSING_ITEMS.keys() & (
    dungeon.MATERIALS.keys() | dungeon.CONSUMABLES.keys() | quests.QUEST_ITEMS.keys()
)
if _item_id_collisions:
    raise ValueError(f"housing_items.json ids collide with existing inventory items: {sorted(_item_id_collisions)}")

# quests.py can't import this module back to register a "housing_item" reward kind (this module
# already imports quests.py, above, for the collision check -- the reverse would be circular). This
# module is the one that ends up able to see both, so -- same "module that can see both does the
# deferred wiring" shape as room_commands.COMMANDS (populated by bot.py once every @bot.command
# exists, not by rooms.py itself) -- it's the one that registers itself here, once both are loaded.
# quests.validate_reward_item_kinds() (run by bot.py/admin_server.py once every content module,
# including this one, is loaded) is what actually catches a bad reward_item_kind/reward_item
# pairing -- quests.py's own loader can't, since this line hasn't run yet at that point.
#
# A zero-arg getter, not HOUSING_ITEMS itself -- see npcs.SHOP_KINDS' own comment for why a
# captured dict reference would silently go stale the moment a housing_items.json edit landed
# through the admin panel with no restart (admin_server.py's hot-reload rebinds this module's own
# HOUSING_ITEMS attribute via setattr, which a captured reference here would never see).
quests.REWARD_REGISTRIES["housing_item"] = lambda: HOUSING_ITEMS


def get_house_bonuses(guild_id: int, user_id: int) -> dict:
    """Aggregates the passive effects of every item currently placed in this player's house grid.
    Returns a dict keyed by effect_type -> summed value, except "stat_bonus" which maps to a nested
    {stat: summed value} dict (mirrors dungeon.constant_stat_bonuses' own per-stat shape). A caller
    wanting one scalar type does `.get(effect_type, 0)`; a caller wanting stats does
    `.get("stat_bonus", {})` and hands that straight to dungeon.compute_effective_stats."""
    bonuses: dict = {}
    for item_id in db.get_house_placements(guild_id, user_id).values():
        item = HOUSING_ITEMS.get(item_id)
        if item is None:
            continue
        effect_type = item["effect_type"]
        value = item["value"]
        if effect_type == "stat_bonus":
            stat_bonuses = bonuses.setdefault("stat_bonus", {})
            stat_bonuses[item["stat"]] = stat_bonuses.get(item["stat"], 0) + value
        else:
            bonuses[effect_type] = bonuses.get(effect_type, 0) + value
    return bonuses
