"""Registry of cosmetic horse equipment -- content-as-data like dungeon.py's EQUIPMENT, but purely
visual in races and dungeons: no stat_bonuses there, and never an upgrade/downgrade decision (a
player just owns it or doesn't). The one exception is optional war_speed/war_endurance/war_spirit,
read only by war.py as barding. Same loader/registry shape as dungeon.py's registries (id, name, +
content-specific fields), editable through the admin panel via admin_schemas.py's CONTENT_TYPES
entry rather than requiring a code change to add a new saddle or hat.

image_path is composited directly onto a horse's coat sprite in horserace_render.py's race
photos -- the art (assets/horses/horse_clothes/*.png) is deliberately pre-authored on the exact
same native canvas size as the coat sprites (assets/horses/*.png), so it alpha_composites at
(0, 0) with no per-item positioning/scaling math of its own.
"""

import json
import os

_HORSE_CLOTHES_PATH = os.path.join(os.path.dirname(__file__), "horse_clothes.json")
_REQUIRED_FIELDS = {"id", "name", "slot", "image_path", "flavor", "base_value"}
CLOTHES_SLOTS = ("saddle", "hat")


def _load_horse_clothes(path: str = _HORSE_CLOTHES_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    clothes: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"horse_clothes.json: item {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in clothes:
            raise ValueError(f"horse_clothes.json: duplicate item id {entry_id!r}")
        if entry["slot"] not in CLOTHES_SLOTS:
            raise ValueError(f"horse_clothes.json: item {entry_id!r} has unknown slot {entry['slot']!r}")
        if entry["base_value"] < 0:
            raise ValueError(f"horse_clothes.json: item {entry_id!r} base_value must be >= 0")
        # Optional !war barding (see war.barding_bonuses) -- absent means no war effect.
        for stat in ("war_speed", "war_endurance", "war_spirit"):
            if stat in entry and not isinstance(entry[stat], int):
                raise ValueError(f"horse_clothes.json: item {entry_id!r} {stat} must be an integer")
        clothes[entry_id] = entry
    return clothes


HORSE_CLOTHES = _load_horse_clothes()
