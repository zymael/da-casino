"""All In mode: any delve replayed with its monsters scaled to the level cap and harder per tier,
paying more gold and dropping item-level-stamped gear with random bonus rolls.

Nothing here is baked into delve/monster/equipment JSON. Every All In number is derived at the
moment it's needed from three live inputs: the base content, the tier, and the current level cap
(all_in_level(), i.e. dungeon_leveling.json's subclass_max_level). Raising the cap through the
admin panel's Leveling page therefore rescales every All In delve immediately, no code change.

Tuning lives in dungeon_all_in.json (one "global" entry, admin-editable like Leveling).

**Progress** is one flag per (player, delve): `allin:<delve_id>` (db.py's generic flags table) =
highest tier cleared + 1, with a normal clear counting as tier 0. So 0 = never cleared, 1 = cleared
normally (All In 1 unlocked), 2 = cleared All In 1 (All In 2 unlocked), ... A player can enter tiers
1..min(flag, max_tier), and only while at the level cap. Storing "cleared + 1" rather than "highest
unlocked" keeps a max-tier clear recorded even before max_tier is raised.

**Gear** never gets a DB row of its own. An All In drop's id encodes everything about it --
`<base_id>@ai<tier>.L<item level>[.<stat><value>-<stat><value>...]`, e.g.
`deep_dish@ai3.L40.atk8-def9-hp14` -- and is stored in the existing equipment tables like any other
item id. dungeon.EQUIPMENT resolves such ids on lookup (dungeon.EquipmentRegistry -> derive_item),
so every existing `EQUIPMENT[item_id]` call site works unchanged. The item level and the rolled
bonuses are fixed in the id, so the item never changes when the cap or the roll tuning does; only
the base item's own authored stats (scaled from its intended level up to the stamped item level)
are read live from dungeon_equipment.json.

**Speed** is handled conservatively everywhere because it compounds (turn frequency is linear in
speed): monster speed rises by a small flat amount instead of a multiplier, never per tier, and is
ceilinged at monster_speed_max; a base item's own speed_buff is never scaled up; a speed bonus roll
is low-weight, capped at speed_affix_max, and at most one per item.
"""

import json
import math
import os
import random
import re

import dungeon

_ALL_IN_PATH = os.path.join(os.path.dirname(__file__), "dungeon_all_in.json")

ITEM_ID_MARKER = "@ai"
_ITEM_ID_RE = re.compile(r"^(?P<base>[^@]+)@ai(?P<tier>\d+)\.L(?P<level>\d+)(?:\.(?P<affixes>[a-z]+\d+(?:-[a-z]+\d+)*))?$")
_AFFIX_RE = re.compile(r"([a-z]+)(\d+)")

AFFIX_STATS = ("hp", "atk", "def", "spatk", "speed")
_STAT_TO_EFFECT_TYPE = {
    "hp": "hp_buff", "atk": "atk_buff", "def": "def_buff", "spatk": "spatk_buff", "speed": "speed_buff",
}
# Same budget-units-per-point convention as dungeon.generate_item_constant_effects.
_STAT_DIVISORS = {"hp": 1, "atk": 2, "def": 1, "spatk": 2, "speed": 2}
# A base item's constant stat effects that scale up to the stamped item level. speed_buff is
# deliberately absent -- see the module docstring on speed.
_SCALED_ITEM_EFFECT_TYPES = {"hp_buff", "atk_buff", "def_buff", "spatk_buff"}
# Monster skill effects carrying a flat stat-point value that should grow with the monster.
# speed_debuff/speed_buff deliberately excluded (speed), as are multipliers/fractions/durations,
# which are already level-independent.
_SCALED_MONSTER_EFFECT_TYPES = {"def_shred", "atk_debuff", "spatk_debuff", "atk_buff", "def_buff", "spatk_buff", "dot"}

_INT_FIELDS = {"max_tier", "monster_speed_max", "affixes_per_tier", "speed_affix_max"}
_FLOAT_FIELDS = {
    "monster_stat_per_tier", "monster_scaling_exponent", "gold_per_tier", "gold_scaling_exponent", "gear_value_per_tier", "affix_budget_fraction",
    "affix_roll_min", "affix_roll_max",
    "affix_weight_hp", "affix_weight_atk", "affix_weight_def", "affix_weight_spatk", "affix_weight_speed",
}
_REQUIRED_FIELDS = {"id"} | _INT_FIELDS | _FLOAT_FIELDS


def _load_all_in(path: str = _ALL_IN_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    settings: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"dungeon_all_in.json: entry {entry_id!r} missing field(s): {sorted(missing)}")
        for field in _INT_FIELDS:
            if not isinstance(entry[field], int) or entry[field] < 0:
                raise ValueError(f"dungeon_all_in.json: {field} must be an int >= 0")
        for field in _FLOAT_FIELDS:
            if not isinstance(entry[field], (int, float)) or entry[field] < 0:
                raise ValueError(f"dungeon_all_in.json: {field} must be a number >= 0")
        if entry["max_tier"] < 1:
            raise ValueError("dungeon_all_in.json: max_tier must be >= 1")
        if entry["affix_roll_min"] > entry["affix_roll_max"]:
            raise ValueError("dungeon_all_in.json: affix_roll_min must be <= affix_roll_max")
        if not any(entry[f"affix_weight_{s}"] for s in AFFIX_STATS):
            raise ValueError("dungeon_all_in.json: at least one affix_weight_* must be > 0")
        settings[entry_id] = entry
    if "global" not in settings:
        raise ValueError('dungeon_all_in.json: must contain an entry with id "global"')
    return settings


SETTINGS = _load_all_in()


def settings() -> dict:
    return SETTINGS["global"]


def max_tier() -> int:
    return settings()["max_tier"]


def all_in_level() -> int:
    """The level All In scales to: the game's real level cap (a subclassed character's cap), read
    live so raising it in the admin panel's Leveling page moves All In with it."""
    return dungeon.LEVELING["global"]["subclass_max_level"]


# --- Progress ---------------------------------------------------------------------------------

def progress_flag_key(delve_id: str) -> str:
    return f"allin:{delve_id}"


def available_tier(character_level: int, progress: int) -> int:
    """Highest tier this player may enter for a delve whose progress flag is `progress` -- 0 means
    no All In at all (not at the cap yet, or never cleared the delve)."""
    if character_level < all_in_level():
        return 0
    return min(progress, max_tier())


def progress_after_clear(progress: int, tier: int) -> int:
    """The progress flag's new value after clearing `tier` (0 = a normal clear). Only advances if
    the player had that tier unlocked themselves -- a party carry hands out gear and gold, not
    keys -- so keys are always earned by clearing your own previous tier."""
    if tier == 0 or min(progress, max_tier()) >= tier:
        return max(progress, tier + 1)
    return progress


def unlock_line(name: str, old: int, new: int, character_level: int) -> str | None:
    """The log line announcing what a clear just unlocked, or None if nothing new."""
    if new <= old:
        return None
    if new > max_tier():
        return f"♠️ {name} has conquered **All In {max_tier()}**, the highest tier there is."
    cap = all_in_level()
    if character_level < cap:
        return f"♠️ {name} unlocked **All In {new}** for this delve (enter once you reach level {cap})."
    return f"♠️ {name} unlocked **All In {new}** for this delve."


def tier_summary(tier: int) -> str:
    s = settings()
    level = all_in_level()
    rolls = s["affixes_per_tier"] * tier
    return (
        f"♠️ **All In {tier}**: monsters scaled to level {level}, "
        f"+{round(s['monster_stat_per_tier'] * tier * 100)}% monster stats, "
        f"+{round(s['gold_per_tier'] * tier * 100)}% gold. Gear drops at item level {level} "
        f"with {rolls} random bonus roll{'s' if rolls != 1 else ''}."
    )


# --- Monsters ---------------------------------------------------------------------------------

def scale_monster(monster: dict, tier: int) -> dict:
    """A scaled copy of `monster` for All In `tier` -- the shared content dict is never mutated.
    Stats are first lifted from the monster's own level to the cap along the same budget curves the
    admin panel's "Generate stats for level" uses (so a goblin keeps its goblin proportions, just
    at level 40), then the per-tier multiplier goes on top. A monster already above the cap is never
    scaled down."""
    if tier <= 0:
        return monster
    s = settings()
    base_level = monster.get("intended_level") or dungeon.estimate_monster_level(monster)
    target = max(all_in_level(), base_level)
    # Raw budget-curve lifts, then damped by the scaling exponents: early content is authored
    # stronger for its level than late content (a level 1 goblin sits higher on the curve than the
    # level 40 Old God), so a straight lift would make an early delve's trash hit like the final
    # boss. power ** exponent keeps late monsters near-untouched while reining early ones in.
    raw_power = dungeon._monster_power_budget(target) / dungeon._monster_power_budget(base_level)
    raw_special = dungeon._monster_special_budget(target) / dungeon._monster_special_budget(base_level)
    power = raw_power ** s["monster_scaling_exponent"]
    special = raw_special ** s["monster_scaling_exponent"]
    tier_mult = 1 + s["monster_stat_per_tier"] * tier
    gold_mult = raw_power ** s["gold_scaling_exponent"] * (1 + s["gold_per_tier"] * tier)

    # Speed: flat additive lift (never a multiplier, never per tier), ceilinged -- but a monster
    # that was already faster than the ceiling keeps its own authored speed rather than slowing.
    lifted = monster["spd"] + (dungeon._monster_speed_budget(target) - dungeon._monster_speed_budget(base_level))
    speed = max(monster["spd"], min(round(lifted), s["monster_speed_max"]))

    # Flat skill values grow by the square root of the stat lift: authored skill numbers don't
    # track the budget curve linearly (a level 5 shred of 2 vs a level 30 shred of 5), so the full
    # lift would overshoot badly on low-level monsters.
    skill_mult = math.sqrt(raw_power)

    def _scale_effect(effect: dict) -> dict:
        if effect.get("type") in _SCALED_MONSTER_EFFECT_TYPES and isinstance(effect.get("value"), (int, float)):
            return {**effect, "value": max(1, round(effect["value"] * skill_mult))}
        return effect

    skills = []
    for skill in monster.get("skills", []):
        skill = dict(skill)
        if "effects" in skill:
            skill["effects"] = [_scale_effect(e) for e in skill["effects"]]
        if "effect_groups" in skill:
            skill["effect_groups"] = [
                {**g, "effects": [_scale_effect(e) for e in g["effects"]]} for g in skill["effect_groups"]
            ]
        skills.append(skill)

    return {
        **monster,
        "hp": max(1, round(monster["hp"] * power * tier_mult)),
        "atk": max(1, round(monster["atk"] * power * tier_mult)),
        "def": max(1, round(monster["def"] * power * tier_mult)),
        "spatk": max(1, round(monster["spatk"] * special * tier_mult)),
        "spd": speed,
        "loot_min": round(monster["loot_min"] * gold_mult),
        "loot_max": round(monster["loot_max"] * gold_mult),
        "intended_level": round(target),
        "skills": skills,
    }


# --- Gear -------------------------------------------------------------------------------------

def is_all_in_item_id(item_id) -> bool:
    return isinstance(item_id, str) and ITEM_ID_MARKER in item_id


def parse_item_id(item_id: str) -> tuple[str, int, int, list[tuple[str, int]]] | None:
    """(base_id, tier, item_level, [(stat, value), ...]) for an All In item id, or None if
    `item_id` isn't a well-formed one."""
    match = _ITEM_ID_RE.match(item_id)
    if not match:
        return None
    tier, level = int(match["tier"]), int(match["level"])
    if tier < 1 or level < 1:
        return None
    affixes = []
    if match["affixes"]:
        for part in match["affixes"].split("-"):
            stat, value = _AFFIX_RE.fullmatch(part).groups()
            if stat not in AFFIX_STATS:
                return None
            affixes.append((stat, int(value)))
    return match["base"], tier, level, affixes


def roll_affixes(base_item: dict, tier: int, level: int, rng: random.Random | None = None) -> dict[str, int]:
    """`affixes_per_tier * tier` random stat bonuses, merged per stat. Each roll's size is a
    fraction of the item-level budget (scaled by rarity, same as authored gear), between
    affix_roll_min and affix_roll_max of that fraction. Speed: at most one roll, capped."""
    rng = rng or random
    s = settings()
    count = s["affixes_per_tier"] * tier
    budget = (
        dungeon._item_power_budget(level) * dungeon.RARITY_STAT_MULTIPLIERS.get(base_item.get("rarity"), 1.0)
        * s["affix_budget_fraction"]
    )
    weights = {stat: s[f"affix_weight_{stat}"] for stat in AFFIX_STATS}
    rolled: dict[str, int] = {}
    for _ in range(count):
        pool = [stat for stat, w in weights.items() if w > 0]
        if not pool:
            break
        stat = rng.choices(pool, weights=[weights[p] for p in pool])[0]
        value = max(1, round(budget * rng.uniform(s["affix_roll_min"], s["affix_roll_max"]) / _STAT_DIVISORS[stat]))
        if stat == "speed":
            value = min(value, s["speed_affix_max"])
            weights["speed"] = 0  # one speed roll per item, ever
        if value > 0:
            rolled[stat] = rolled.get(stat, 0) + value
    return rolled


def make_item_id(base_id: str, tier: int, level: int, affixes: dict[str, int]) -> str:
    item_id = f"{base_id}{ITEM_ID_MARKER}{tier}.L{level}"
    ordered = [(stat, affixes[stat]) for stat in AFFIX_STATS if affixes.get(stat)]
    if ordered:
        item_id += "." + "-".join(f"{stat}{value}" for stat, value in ordered)
    return item_id


def roll_item_id(base_id: str, tier: int) -> str:
    """A freshly rolled All In version of `base_id`, stamped at the current cap."""
    level = all_in_level()
    return make_item_id(base_id, tier, level, roll_affixes(dungeon.EQUIPMENT[base_id], tier, level))


def derive_item(item_id: str, registry: dict) -> dict | None:
    """The full equipment dict for an All In id, built from its base item in `registry` (the plain
    entries of dungeon.EQUIPMENT), or None if the id is malformed or its base no longer exists."""
    parsed = parse_item_id(item_id)
    if parsed is None:
        return None
    base_id, tier, level, affixes = parsed
    base = dict.get(registry, base_id)
    if base is None:
        return None
    base_level = base.get("intended_level") or dungeon.estimate_item_level(base)
    ratio = max(1.0, dungeon._item_power_budget(level) / dungeon._item_power_budget(base_level))
    effects = []
    for effect in base["effects"]:
        if effect.get("trigger") == "constant" and effect["type"] in _SCALED_ITEM_EFFECT_TYPES:
            effect = {**effect, "value": max(1, round(effect["value"] * ratio))}
        effects.append(effect)
    for stat, value in affixes:
        effects.append({"type": _STAT_TO_EFFECT_TYPE[stat], "trigger": "constant", "value": value})
    return {
        **base,
        "id": item_id,
        "name": f"{base['name']} ♠{tier}",
        "intended_level": level,
        "base_value": round(base["base_value"] * ratio * (1 + settings()["gear_value_per_tier"] * tier)),
        "effects": effects,
        "all_in": {"base_id": base_id, "tier": tier, "level": level, "affixes": affixes},
    }


def item_tag_line(item: dict) -> str | None:
    """'♠️ All In 3 · Item Level 40 · Rolled: +8 ATK, +14 HP' for an All In item, else None."""
    info = item.get("all_in")
    if not info:
        return None
    rolled = ", ".join(f"+{value} {dungeon.stat_label(stat)}" for stat, value in info["affixes"])
    line = f"♠️ All In {info['tier']} · Item Level {info['level']}"
    return f"{line} · Rolled: {rolled}" if rolled else line
