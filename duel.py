"""Duel (1v1 PvP) rules -- pure data and math, no Discord, the duel sibling of all_in.py.

PvP runs on the same combat engine as PvE but is tuned separately, so a class can keep its flavor
while playing differently against a player than against monsters. Everything duel-specific that is
a *number* lives in dungeon_duel.json (one "global" entry, admin-editable like All In); everything
duel-specific that is a *skill* lives on the skill itself (dungeon_skills.json's optional
duel_effects / duel_chip_cost, see duel_skill). dungeon_view.py's Dueling section applies both.

- **Stats are pulled toward the field average.** Each duelist's HP/ATK/DEF/Magic/Speed is moved
  `stat_pull_<stat>` of the way toward the average of every class/subclass build at the duel's
  level (0 = the class's own stat, 1 = everyone identical). Class order survives any pull below 1:
  fighters stay the tankiest and rogues the fastest, just closer together.
- **Damage and crits** are scaled by `damage_multiplier` and `crit_chance`.
- **Skills can have a duel version.** A skill with `duel_effects` (or `duel_effect_groups`, for a
  coin-flip skill) uses that in a duel instead of its own effects/effect_groups; `duel_chip_cost`
  replaces its Chip cost. All optional. Use this to retune an outlier for PvP, or to give a
  party-shaped skill a 1v1 job.
"""

import json
import os

import dungeon

_DUEL_PATH = os.path.join(os.path.dirname(__file__), "dungeon_duel.json")

# stat -> the PartyMember attribute holding it (dungeon.compute_stats keys -> combatant attrs)
DUEL_STATS = {"hp": "max_hp", "atk": "atk", "def": "def_", "spatk": "spatk", "speed": "speed"}
_FRACTION_FIELDS = {"crit_chance"} | {f"stat_pull_{s}" for s in DUEL_STATS}
_REQUIRED_FIELDS = {"id", "damage_multiplier"} | _FRACTION_FIELDS


def _load_duel(path: str = _DUEL_PATH) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    settings: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"dungeon_duel.json: entry {entry_id!r} missing field(s): {sorted(missing)}")
        for field in _FRACTION_FIELDS:
            if not isinstance(entry[field], (int, float)) or not 0 <= entry[field] <= 1:
                raise ValueError(f"dungeon_duel.json: {field} must be a number from 0 to 1")
        if not isinstance(entry["damage_multiplier"], (int, float)) or entry["damage_multiplier"] <= 0:
            raise ValueError("dungeon_duel.json: damage_multiplier must be a number > 0")
        settings[entry_id] = entry
    if "global" not in settings:
        raise ValueError('dungeon_duel.json: must contain an entry with id "global"')
    return settings


SETTINGS = _load_duel()


def settings() -> dict:
    return SETTINGS["global"]


def crit_chance() -> float:
    return settings()["crit_chance"]


def damage_multiplier() -> float:
    return settings()["damage_multiplier"]


def field_average(level: int) -> dict[str, float]:
    """Each duel stat's average over every class/subclass build at `level`."""
    rows = [dungeon.compute_stats_at_level(mc, sc, level) for mc in dungeon.CLASSES for sc in dungeon.SUBCLASSES]
    return {stat: sum(r[stat] for r in rows) / len(rows) for stat in DUEL_STATS}


def pulled_stats(stats: dict[str, int], level: int) -> dict[str, int]:
    """`stats` ({hp, atk, def, spatk, speed}) with each one moved its stat_pull toward the field
    average at `level`. Never below 1."""
    avg = field_average(level)
    s = settings()
    return {
        stat: max(1, round(stats[stat] + (avg[stat] - stats[stat]) * s[f"stat_pull_{stat}"]))
        for stat in DUEL_STATS
    }


_DUEL_FIELDS = ("duel_effects", "duel_effect_groups", "duel_chip_cost")


def duel_skill(skill: dict) -> dict:
    """`skill` as it plays in a duel: its duel_effects or duel_effect_groups and its duel_chip_cost
    swapped in when it has them, otherwise the skill itself, unchanged."""
    if not any(f in skill for f in _DUEL_FIELDS):
        return skill
    out = dict(skill)
    if "duel_effects" in skill:
        out.pop("effect_groups", None)
        out["effects"] = skill["duel_effects"]
    elif "duel_effect_groups" in skill:
        out.pop("effects", None)
        out["effect_groups"] = skill["duel_effect_groups"]
    if "duel_chip_cost" in skill:
        out["chip_cost"] = skill["duel_chip_cost"]
    return out
