"""The Mane-hay-tan Project: a guild-wide gold sink. Everyone donates to one communal pot for
"Dubious Research"; once the pot reaches the current bomb's goal the bomb is armed, and anyone at
all can !launch it. A detonation starts Fallout (irradiated foals, boosted training, The Front
closed) and the project starts over on the next, pricier bomb.

The bomb ladder is content (manhattan.json, editable through the admin panel via admin_schemas.py's
CONTENT_TYPES entry); past the end of the ladder the last bomb repeats with its goal growing by
GOAL_GROWTH each cycle. Fallout's own tuning stays here in code, the same way war.py keeps its
battle numbers. All state is guild-wide flags at user_id 0 (db.MANHATTAN_* keys) -- same sentinel
jackpot.py uses -- so this needed no new table. No discord code here; see manhattan_view.py.
"""

import json
import os
import time

import db

_MANHATTAN_PATH = os.path.join(os.path.dirname(__file__), "manhattan.json")
_REQUIRED_FIELDS = {
    "id", "order", "name", "goal", "fallout_hours", "log_25", "log_50", "log_75", "armed_text", "detonation_text",
}
LOG_FIELDS = ((25, "log_25"), (50, "log_50"), (75, "log_75"))

GOAL_GROWTH = 1.5  # per cycle, once the ladder runs out
FALLOUT_STAT_CAP = 120  # permanent cap for a foal bought during Fallout (normal: horserace.STAT_CAP)
FALLOUT_TRAINING_MULT = 1.5  # every training gain, on top of facility/housing bonuses


def _load_bombs(path: str = _MANHATTAN_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    bombs: dict[str, dict] = {}
    orders: set[int] = set()
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"manhattan.json: bomb {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in bombs:
            raise ValueError(f"manhattan.json: duplicate bomb id {entry_id!r}")
        if not isinstance(entry["order"], int) or entry["order"] in orders:
            raise ValueError(f"manhattan.json: bomb {entry_id!r} needs a unique integer order")
        if not isinstance(entry["goal"], int) or entry["goal"] < 1:
            raise ValueError(f"manhattan.json: bomb {entry_id!r} goal must be a positive integer")
        if not isinstance(entry["fallout_hours"], int) or entry["fallout_hours"] < 1:
            raise ValueError(f"manhattan.json: bomb {entry_id!r} fallout_hours must be a positive integer")
        orders.add(entry["order"])
        bombs[entry_id] = entry
    if not bombs:
        raise ValueError("manhattan.json: needs at least one bomb")
    return bombs


BOMBS = _load_bombs()


def bomb_for_cycle(cycle: int) -> dict:
    """The bomb being built on `cycle` (0-based). Past the end of the ladder, the last bomb again
    with its goal grown GOAL_GROWTH per extra cycle. Always a copy -- callers may not mutate BOMBS."""
    ladder = sorted(BOMBS.values(), key=lambda b: b["order"])
    if cycle < len(ladder):
        return dict(ladder[cycle])
    bomb = dict(ladder[-1])
    bomb["goal"] = int(bomb["goal"] * GOAL_GROWTH ** (cycle - len(ladder) + 1))
    return bomb


def current_cycle(guild_id: int) -> int:
    return db.get_flag(guild_id, db.GUILD_FLAG_USER, db.MANHATTAN_CYCLE_FLAG)


def current_bomb(guild_id: int) -> dict:
    return bomb_for_cycle(current_cycle(guild_id))


def fallout_until(guild_id: int) -> int:
    return db.get_flag(guild_id, db.GUILD_FLAG_USER, db.FALLOUT_UNTIL_FLAG)


def fallout_active(guild_id: int, now: int | None = None) -> bool:
    return fallout_until(guild_id) > (now if now is not None else int(time.time()))


def is_irradiated(guild_id: int, horse_index: int) -> bool:
    return db.get_flag(guild_id, db.GUILD_FLAG_USER, db.irradiated_flag(horse_index)) > 0


def stat_cap_for(guild_id: int, horse_index: int, normal_cap: float) -> float:
    return FALLOUT_STAT_CAP if is_irradiated(guild_id, horse_index) else normal_cap


def status(guild_id: int) -> dict:
    """Everything !manhattan shows: the bomb, its pot/goal, whether it's armed, Fallout's end
    (0 when not active), lifetime detonations, and this cycle's top 5 donors."""
    flags = db.get_flags(guild_id, db.GUILD_FLAG_USER, [
        db.MANHATTAN_CYCLE_FLAG, db.MANHATTAN_POT_FLAG, db.MANHATTAN_ARMED_FLAG,
        db.FALLOUT_UNTIL_FLAG, db.MANHATTAN_DETONATIONS_FLAG,
    ])
    cycle = flags[db.MANHATTAN_CYCLE_FLAG]
    until = flags[db.FALLOUT_UNTIL_FLAG]
    return {
        "bomb": bomb_for_cycle(cycle),
        "cycle": cycle,
        "pot": flags[db.MANHATTAN_POT_FLAG],
        "armed": flags[db.MANHATTAN_ARMED_FLAG] > 0,
        "fallout_until": until if until > time.time() else 0,
        "detonations": flags[db.MANHATTAN_DETONATIONS_FLAG],
        "top_donors": db.top_flag_holders(guild_id, db.manhattan_donated_flag(cycle), 5),
    }


def crossed_log_lines(bomb: dict, old_pot: int, new_pot: int) -> list[str]:
    """The research-log lines a donation moving the pot old_pot -> new_pot just crossed, in order."""
    lines = []
    for percent, field in LOG_FIELDS:
        threshold = bomb["goal"] * percent / 100
        if old_pot < threshold <= new_pot:
            lines.append(bomb[field])
    return lines
