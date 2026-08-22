"""Dungeon RPG: pure logic and content (no discord import), mirroring horserace.py's split
between game logic here and Discord UI in dungeon_view.py.

Characters are a permanent one-time choice: a main class (face rank) x a subclass (suit) = 16
builds. Combat is deliberately lightweight -- HP/ATK/DEF only, no persistent status effects, one
skill usable once per fight, unlocked automatically as the character levels. Each of the 16
builds has its own skill line (dungeon_skills.json, see SKILLS below) rather than sharing one
ability per class. Monster content lives in dungeon_monsters.json (not here) specifically so new
monsters can be added without touching this file -- see MONSTERS below.
"""

import json
import math
import os
import random

# Base HP/ATK/DEF per class, before subclass modifiers. Kept to three stats total rather than a
# full JRPG sheet. Archetypes: Fighter tanks (high HP/DEF, modest ATK), Mage nukes (high ATK,
# fragile), Rogue is balanced/quick, Healer leans on HP + its Heal ability to outlast fights.
# Healer's ATK was originally 4, which combined with tougher monsters' DEF made roll_damage floor
# at 1 almost every hit -- an unwinnable slog regardless of how tanky Healer otherwise is. Bumped
# to 6 so Healer can still meaningfully damage things; simulated combat confirms this fixed it
# without needing to touch any other class. Each class's signature skill(s) now live in SKILLS
# below, keyed by (main_class, subclass) rather than on this dict.
CLASSES = {
    "fighter": {"rank": "A", "hp": 32, "atk": 6, "def": 6},
    "healer": {"rank": "K", "hp": 26, "atk": 6, "def": 5},
    "mage": {"rank": "Q", "hp": 16, "atk": 10, "def": 2},
    "rogue": {"rank": "J", "hp": 22, "atk": 7, "def": 3},
}
RANK_TO_CLASS = {info["rank"]: name for name, info in CLASSES.items()}

# Subclass (suit) modifiers layered on top of the class base -- the same attitude framework used
# for the 16 display names: clubs (brawler) adds raw power, spades (lethal) trades defense for
# offense, hearts (loyal) adds survivability, diamonds (greedy) trades a little combat edge for
# meaningfully better loot.
SUBCLASSES = {
    "clubs": {"hp": 4, "atk": 2, "def": 0, "loot_mult": 1.0},
    "spades": {"hp": 0, "atk": 3, "def": -1, "loot_mult": 1.0},
    "hearts": {"hp": 4, "atk": 0, "def": 2, "loot_mult": 1.0},
    # A -1 DEF here originally, on top of an already-below-average build, made a couple of
    # specific class+diamonds combos nearly unwinnable in simulation. A small +1 ATK (a
    # mercenary/treasure hunter still fights competently, just prioritizes the score) fixed that
    # without diamonds needing to be a pure stat no-op alongside its loot bonus.
    "diamonds": {"hp": 0, "atk": 1, "def": 0, "loot_mult": 1.25},
}
SUIT_SYMBOLS = {"clubs": "♣", "spades": "♠", "hearts": "♥", "diamonds": "♦"}

# The 16-name grid, worked out with the product owner: (class, subclass) -> display name.
NAMES = {
    ("fighter", "clubs"): "Barbarian", ("fighter", "spades"): "Duelist",
    ("fighter", "hearts"): "Guardian", ("fighter", "diamonds"): "Mercenary",
    ("healer", "clubs"): "Bonesetter", ("healer", "spades"): "Plague Doctor",
    ("healer", "hearts"): "Priest", ("healer", "diamonds"): "Charlatan",
    ("mage", "clubs"): "War Mage", ("mage", "spades"): "Nightweaver",
    ("mage", "hearts"): "Enchanter", ("mage", "diamonds"): "Artificer",
    ("rogue", "clubs"): "Bar Fighter", ("rogue", "spades"): "Assassin",
    ("rogue", "hearts"): "Con Artist", ("rogue", "diamonds"): "Treasure Hunter",
}


def display_name(main_class: str, subclass: str) -> str:
    return NAMES[(main_class, subclass)]


def compute_stats(main_class: str, subclass: str) -> dict:
    """Snapshotted once at character creation (see db.create_character) rather than recomputed
    on every read, so a character's power stays stable even if these base numbers get
    rebalanced later."""
    base = CLASSES[main_class]
    mod = SUBCLASSES[subclass]
    return {
        "hp": base["hp"] + mod["hp"],
        "atk": base["atk"] + mod["atk"],
        "def": base["def"] + mod["def"],
        "loot_mult": mod["loot_mult"],
    }


# --- Monster content -------------------------------------------------------------------------
# Loaded once at import time from dungeon_monsters.json, which is meant to be hand-edited to add
# new monsters -- no code change should ever be required just to add content. Validated loudly
# (raises on any malformed entry) so a bad edit breaks bot startup immediately rather than
# surfacing as a confusing runtime error mid-delve.
#
# `drops` is an optional list of {kind, item_id, chance} -- each monster's own explicit loot
# table, replacing the old tier-wide weighted roll (see roll_drops below). Loaded after
# EQUIPMENT/MATERIALS since a monster's drops get cross-validated against whichever registry its
# `kind` points into.

_MONSTERS_PATH = os.path.join(os.path.dirname(__file__), "dungeon_monsters.json")
_REQUIRED_MONSTER_FIELDS = {
    "id", "name", "tier", "hp", "atk", "def", "shape", "color", "flavor", "loot_min", "loot_max",
}
DROP_KINDS = ("equipment", "material")


def _validate_monster_drops(drops, context: str):
    for drop in drops:
        kind = drop.get("kind")
        if kind not in DROP_KINDS:
            raise ValueError(f"{context} has a drop with unknown kind {kind!r}")
        item_id = drop.get("item_id")
        registry = EQUIPMENT if kind == "equipment" else MATERIALS
        if item_id not in registry:
            raise ValueError(f"{context} has a drop referencing unknown {kind} {item_id!r}")
        if kind == "equipment" and registry[item_id].get("quest_only"):
            raise ValueError(f"{context} has a drop referencing quest_only equipment {item_id!r}")
        chance = drop.get("chance")
        if not isinstance(chance, (int, float)) or not (0 < chance <= 1):
            raise ValueError(f"{context} has a drop for {item_id!r} with chance not in (0, 1]")


def _load_monsters(path: str = _MONSTERS_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    monsters: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_MONSTER_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"dungeon_monsters.json: monster {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in monsters:
            raise ValueError(f"dungeon_monsters.json: duplicate monster id {entry_id!r}")
        for field in ("hp", "atk", "def", "tier", "loot_min", "loot_max"):
            if entry[field] < 0:
                raise ValueError(f"dungeon_monsters.json: monster {entry_id!r} has negative {field}")
        if entry["loot_min"] > entry["loot_max"]:
            raise ValueError(f"dungeon_monsters.json: monster {entry_id!r} has loot_min > loot_max")
        _validate_monster_drops(entry.get("drops", []), f"dungeon_monsters.json: monster {entry_id!r}")
        monsters[entry_id] = entry
    return monsters


def roll_drops(monster: dict, chance_mult: float = 1.0) -> list[dict]:
    """Rolls this monster's own `drops` list (see _validate_monster_drops) -- each entry's chance
    is an independent roll, so a single kill can land any number of its configured drops (zero,
    one, or several), unlike the old shared tier table where equipment and material were exactly
    one roll each. Returns the full item dict for each hit, tagged with its `kind` so the caller
    knows which registry (and which downstream handling -- equip-or-store vs. inventory-add) it
    came from. `chance_mult` scales every configured chance uniformly -- used to halve drop odds
    for a party delve's non-leader members, 1.0 (unchanged) for everyone else."""
    hits = []
    for drop in monster.get("drops", []):
        if random.random() > drop["chance"] * chance_mult:
            continue
        registry = EQUIPMENT if drop["kind"] == "equipment" else MATERIALS
        item = dict(registry[drop["item_id"]])
        item["_drop_kind"] = drop["kind"]
        hits.append(item)
    return hits


# --- Delve content ---------------------------------------------------------------------------
# A delve is a named dungeon layout: a room GRAPH, not a flat sequence. Rooms are addressed by id
# (unique within the delve); each room's own "next" (a combat room) or its actions' own outcomes
# (a choice room) name which room comes after it, so a delve can fork into multiple paths,
# reconverge, or dead-end back on itself -- a flat list-with-implicit-next-index can only ever
# express one successor per position, which can't represent a fork at all. `start_room` says which
# room id a session begins at, since room order is no longer implicit.
#
# Two room types:
#   "combat": {"id", "type": "combat", "monsters": [...], "background_path"?, "next"?, "prompt"?}
#     monsters is the explicit set of ids that can show up there (one picked at random each visit,
#     see monster_for_room); next is another room's id, or absent -- absent means clearing this
#     room's monster wins the delve (same semantics as today's "last room", just explicit now
#     instead of positional). prompt (optional, unlike a choice room's required one) introduces the
#     room itself -- shown once, right when the room is entered, ahead of the monster's own flavor
#     text (see dungeon_view._combat_intro_text) -- purely flavor, never read by combat logic
#     itself.
#   "choice": {"id", "type": "choice", "prompt": str, "actions": [...], "background_path"?}
#     a non-combat room: flavor text plus a menu of player-chosen actions (see _validate_action),
#     each carrying its own optional gate/cost/skill-check and its own next-room outcome -- this is
#     where actual branching happens, not on combat rooms. Each action can also carry its own
#     optional "x"/"y" (numbers) -- where the admin panel's flowchart editor drew that action's own
#     connector box, same presentational-only role as a delve's top-level "layout" for rooms, just
#     carried directly on the action instead of in an external dict (an action has no stable id of
#     its own to key such a dict by -- its position within "actions" can shift as rows are added/
#     removed/reordered, unlike a room id).
# background_path (either room type) optionally overrides the delve's own top-level
# background_path for just that room -- a room without one falls back to the delve's (see
# dungeon_view.py's _room_background_path).
#
# layout (optional, top-level on the delve, not per-room): {room_id: {"x": number, "y": number}},
# written by the admin panel's flowchart editor to remember where each room's box was dragged to on
# the canvas. Purely presentational -- dungeon_view.py's traversal only ever does
# rooms_by_id(delve)[current_room_id] lookups and never reads this, and it has no bearing on the
# edges/reachability graph below. A room with no entry here just hasn't been positioned yet (the
# editor falls back to a default grid slot); layout is never required to cover every room.
#
# active (optional, top-level, bool, default True if absent): whether this delve is offered to
# players at all -- see active_delves() below, the one place that's decided. An inactive delve
# still lives in DELVES and is still structurally validated here (known ids, valid action shapes,
# etc. -- protects against a crash the moment any code touches it), but is exempt from the
# reachability check below, since a delve mid-construction in the admin panel is legitimately
# unreachable-riddled for most of that process and still needs to be saveable. Flip it back to
# active once it's actually finished.
#
# Loaded after MONSTERS/MATERIALS/CONSUMABLES since a delve's rooms get cross-validated against
# them (an unknown monster/material/consumable id would otherwise only surface as a crash mid-
# delve, not at startup where a bad edit belongs) -- see the load order note near CONSUMABLES
# below for why DELVES is actually instantiated after that registry despite this section coming
# first in the file. A choice room's "requires" (any trigger type) and a quest_item cost's item id
# can NOT be validated here at all -- dungeon.py can't import quests.py without a cycle, the same
# constraint npcs.py's own visible_trigger already lives under. See quests.py's own post-load
# cross-validation pass for that half; _load_delves below only checks that "requires"/quest_item
# costs are shaped like plain dicts, nothing about their actual contents.

ROOM_TYPES = ("combat", "choice")
CHECK_STATS = ("atk", "def", "hp")  # which of a character's own stats a skill check can roll against
ACTION_COST_ITEM_KINDS = ("material", "consumable", "quest_item")
_REQUIRED_ROOM_FIELDS_BY_TYPE = {"combat": {"monsters"}, "choice": {"prompt", "actions"}}
_OPTIONAL_ROOM_FIELDS_BY_TYPE = {"combat": {"background_path", "next", "prompt"}, "choice": {"background_path"}}

_DELVES_PATH = os.path.join(os.path.dirname(__file__), "dungeon_delves.json")
_REQUIRED_DELVE_FIELDS = {"id", "name", "flavor", "rooms", "start_room"}
_ACTION_OUTCOME_KEYS = {"next", "hp_delta", "message"}


def _validate_action(action: dict, context: str) -> None:
    """One action within a choice room -- see the module docstring above for the full shape.
    Doesn't touch "requires" contents (opaque here, see module docstring) or whether cost/outcome
    "next" room ids actually exist (checked by the caller, which has the full room-id set)."""
    if not action.get("label"):
        raise ValueError(f"{context}: missing a label")

    # x/y (both optional): where the admin panel's flowchart editor drew this action's own
    # connector box on the canvas -- purely presentational, exactly like a delve's top-level
    # "layout" (see the module docstring), just carried directly on the action instead of in an
    # external dict, since an action has no stable id of its own to key such a dict by (its
    # position within "actions" can shift as rows are added/removed/reordered, unlike a room id).
    for key in ("x", "y"):
        if key in action and not isinstance(action[key], (int, float)):
            raise ValueError(f"{context}: {key} must be a number")
    if action.get("requires") is not None and not isinstance(action["requires"], dict):
        raise ValueError(f"{context}: requires must be an object")

    cost = action.get("cost")
    if cost is not None:
        if not cost.get("currency") and not cost.get("item_id"):
            raise ValueError(f"{context}: cost must set currency and/or item_id")
        if "currency" in cost and cost["currency"] <= 0:
            raise ValueError(f"{context}: cost currency must be positive")
        if cost.get("item_id"):
            item_kind = cost.get("item_kind")
            if item_kind not in ACTION_COST_ITEM_KINDS:
                raise ValueError(f"{context}: cost with an item_id needs item_kind in {ACTION_COST_ITEM_KINDS}")
            if item_kind == "material" and cost["item_id"] not in MATERIALS:
                raise ValueError(f"{context}: cost references unknown material {cost['item_id']!r}")
            if item_kind == "consumable" and cost["item_id"] not in CONSUMABLES:
                raise ValueError(f"{context}: cost references unknown consumable {cost['item_id']!r}")
            # quest_item existence is checked by quests.py's cross-validation pass (see module docstring)
            if cost.get("item_qty", 1) <= 0:
                raise ValueError(f"{context}: cost item_qty must be positive")

    check = action.get("check")
    if check is not None:
        if check.get("stat") not in CHECK_STATS:
            raise ValueError(f"{context}: check stat must be one of {CHECK_STATS}")
        if not isinstance(check.get("dc"), (int, float)) or check["dc"] <= 0:
            raise ValueError(f"{context}: check dc must be a positive number")

    if "on_success" not in action:
        raise ValueError(f"{context}: missing on_success")
    if check is not None and "on_fail" not in action:
        raise ValueError(f"{context}: has a check but no on_fail")
    if check is None and "on_fail" in action:
        raise ValueError(f"{context}: has on_fail but no check to fail against")
    for key in ("on_success", "on_fail"):
        outcome = action.get(key)
        if outcome is None:
            continue
        unknown = outcome.keys() - _ACTION_OUTCOME_KEYS
        if unknown:
            raise ValueError(f"{context}: {key} has unknown field(s): {sorted(unknown)}")
        if "hp_delta" in outcome and not isinstance(outcome["hp_delta"], int):
            raise ValueError(f"{context}: {key}.hp_delta must be an int")


def _load_delves(path: str = _DELVES_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    delves: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_DELVE_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"dungeon_delves.json: delve {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in delves:
            raise ValueError(f"dungeon_delves.json: duplicate delve id {entry_id!r}")
        rooms = entry["rooms"]
        if not rooms:
            raise ValueError(f"dungeon_delves.json: delve {entry_id!r} has empty rooms")

        room_ids: set[str] = set()
        for room in rooms:
            room_id = room.get("id")
            if not room_id:
                raise ValueError(f"dungeon_delves.json: delve {entry_id!r} has a room with no id")
            if room_id in room_ids:
                raise ValueError(f"dungeon_delves.json: delve {entry_id!r} has duplicate room id {room_id!r}")
            room_ids.add(room_id)
            room_type = room.get("type")
            if room_type not in ROOM_TYPES:
                raise ValueError(
                    f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} has unknown type {room_type!r}"
                )
            allowed = _REQUIRED_ROOM_FIELDS_BY_TYPE[room_type] | _OPTIONAL_ROOM_FIELDS_BY_TYPE[room_type] | {"id", "type"}
            missing_room = _REQUIRED_ROOM_FIELDS_BY_TYPE[room_type] - room.keys()
            if missing_room:
                raise ValueError(
                    f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} missing field(s): {sorted(missing_room)}"
                )
            unknown_room = room.keys() - allowed
            if unknown_room:
                raise ValueError(
                    f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} has unknown field(s): {sorted(unknown_room)}"
                )

        if entry["start_room"] not in room_ids:
            raise ValueError(
                f"dungeon_delves.json: delve {entry_id!r} start_room {entry['start_room']!r} is not a room here"
            )

        if "active" in entry and not isinstance(entry["active"], bool):
            raise ValueError(f"dungeon_delves.json: delve {entry_id!r} active must be true/false")

        layout = entry.get("layout")
        if layout is not None:
            if not isinstance(layout, dict):
                raise ValueError(f"dungeon_delves.json: delve {entry_id!r} layout must be an object")
            for room_id, pos in layout.items():
                if room_id not in room_ids:
                    raise ValueError(
                        f"dungeon_delves.json: delve {entry_id!r} layout references unknown room {room_id!r}"
                    )
                if not isinstance(pos, dict) or pos.keys() != {"x", "y"}:
                    raise ValueError(
                        f"dungeon_delves.json: delve {entry_id!r} layout for room {room_id!r} must be an "
                        f"object with exactly x and y"
                    )
                if not all(isinstance(pos[k], (int, float)) for k in ("x", "y")):
                    raise ValueError(
                        f"dungeon_delves.json: delve {entry_id!r} layout for room {room_id!r} has non-numeric x/y"
                    )

        # Every "next"-shaped edge (a combat room's own, or a choice action's on_success/on_fail),
        # collected as we validate each room -- reused below for the reachability pass.
        edges: dict[str, list[str]] = {rid: [] for rid in room_ids}
        for room in rooms:
            room_id = room["id"]
            if room["type"] == "combat":
                monsters = room["monsters"]
                if not monsters:
                    raise ValueError(f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} has no monsters")
                for monster_id in monsters:
                    if monster_id not in MONSTERS:
                        raise ValueError(
                            f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} "
                            f"references unknown monster {monster_id!r}"
                        )
                next_room = room.get("next")
                if next_room is not None:
                    if next_room not in room_ids:
                        raise ValueError(
                            f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} "
                            f"next {next_room!r} is not a room here"
                        )
                    edges[room_id].append(next_room)
            else:  # choice
                if not room.get("prompt"):
                    raise ValueError(f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} has no prompt")
                actions = room.get("actions")
                if not actions:
                    raise ValueError(f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} has no actions")
                for i, action in enumerate(actions):
                    context = f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} action {i}"
                    _validate_action(action, context)
                    for key in ("on_success", "on_fail"):
                        outcome = action.get(key)
                        next_room = outcome.get("next") if outcome else None
                        if next_room is not None:
                            if next_room not in room_ids:
                                raise ValueError(f"{context}: {key}.next {next_room!r} is not a room here")
                            edges[room_id].append(next_room)

        # Every authored room must be reachable from start_room -- an unreachable room is almost
        # certainly a content bug (a dangling fork, a copy-paste leftover), the same strictness
        # this codebase already applies elsewhere (e.g. every build needs a level-1 skill). A room
        # with no outgoing edges at all (a legitimate dead end/final room) is fine either way.
        #
        # Only enforced for an *active* delve (entry.get("active", True) -- absent/missing means
        # active, so every delve authored before this field existed keeps behaving exactly as it
        # always did). A delve being actively built in the admin panel's flowchart editor is
        # legitimately unreachable-riddled for most of that process -- rooms get added before
        # they're wired up, choice actions before their outcomes are connected -- and requiring
        # full reachability on every single save made it impossible to save that work in progress
        # at all. Flip a delve to inactive while building it (see admin_schemas.py's "active"
        # field) and this check no longer blocks the save; flip it back once it's actually done,
        # at which point this same check is what confirms it's really finished. Structural
        # correctness (known room/monster/material ids, valid action shapes, etc.) is never
        # relaxed by this flag -- those protect against a crash the moment any code touches this
        # delve, active or not, e.g. from the admin panel's own list view.
        if entry.get("active", True):
            seen = {entry["start_room"]}
            frontier = [entry["start_room"]]
            while frontier:
                for neighbor in edges[frontier.pop()]:
                    if neighbor not in seen:
                        seen.add(neighbor)
                        frontier.append(neighbor)
            unreachable = room_ids - seen
            if unreachable:
                raise ValueError(
                    f"dungeon_delves.json: delve {entry_id!r} has unreachable room(s): {sorted(unreachable)}"
                )

        delves[entry_id] = entry
    return delves


def active_delves() -> dict[str, dict]:
    """Every delve actually offered to players -- the one place "is this delve playable" is
    decided, so a new delve-selection surface (a command, a picker view, ...) never needs its own
    copy of the "active" check. See _load_delves' module-docstring-adjacent comment on the
    "active" field for why an inactive delve can still be saved (and still lives in DELVES) despite
    never showing up here."""
    return {delve_id: delve for delve_id, delve in DELVES.items() if delve.get("active", True)}


def rooms_by_id(delve: dict) -> dict[str, dict]:
    return {room["id"]: room for room in delve["rooms"]}


def monster_for_room(room: dict) -> dict:
    return MONSTERS[random.choice(room["monsters"])]


# --- Leveling ------------------------------------------------------------------------------
# XP is awarded per monster kill, scaled by room tier (deeper = more XP, same "push deeper pays
# off more" logic already driving loot). Leveling grants automatic flat stat growth -- no player
# choice -- applied by mutating the character's stored stats in place (db.add_xp), the same way
# horse training already grows a horse's stats via db.train_horse. Subclass-specific skills
# unlocked by level are a deferred follow-up; `level` is tracked now specifically so that pass
# won't need a data-model change.
XP_PER_TIER = {1: 10, 2: 20, 3: 40}
LEVEL_HP_GAIN, LEVEL_ATK_GAIN, LEVEL_DEF_GAIN = 2, 1, 1


def xp_for_monster(tier: int) -> int:
    return XP_PER_TIER[tier]


def xp_to_next_level(level: int) -> int:
    """XP required to advance from `level` to `level + 1`."""
    return 50 * level


# --- Skills ----------------------------------------------------------------------------------
# Each of the 16 (main_class, subclass) builds has its own skill line, loaded from
# dungeon_skills.json so new skills are a JSON edit, not a code change -- same registry pattern
# as MONSTERS/EQUIPMENT above. A skill unlocks automatically once the character's level (already
# tracked in the characters table) reaches its unlock_level; there's no player choice and nothing
# extra to persist -- unlocked_skills below derives the answer live from level.
#
# A skill's `effects` is a list of {"type": ..., **params} entries built from a small fixed set
# of reusable primitives (EFFECT_PARAM_SCHEMAS) rather than bespoke per-skill code, so combining
# 1-2 primitives is how skills stay distinct from each other. dungeon_view.py's EFFECT_HANDLERS
# is what actually interprets these during combat; this module only validates their shape.

_SKILLS_PATH = os.path.join(os.path.dirname(__file__), "dungeon_skills.json")
_REQUIRED_SKILL_FIELDS = {"id", "main_class", "subclass", "unlock_level", "name", "flavor", "effects"}

# type -> (required param names, optional param names, fraction param names). Fraction params
# must be in (0, 1] (they scale a max-HP heal, a damage reduction, etc); every other numeric
# param just needs to be > 0 (a raw multiplier, a flat stat delta, ...). Shared with consumables
# (dungeon_consumables.json) via _validate_effects, so there is exactly one definition of what an
# effect is, used by every kind of content that can carry one.
EFFECT_PARAM_SCHEMAS = {
    "damage_multiplier": ({"value"}, set(), set()),
    "heal_fraction": ({"value"}, set(), {"value"}),
    "guard": ({"reduction"}, set(), {"reduction"}),
    "lifesteal_fraction": ({"value"}, set(), {"value"}),
    "def_shred": ({"value"}, set(), set()),
    "extra_attack": (set(), {"multiplier"}, set()),
    "atk_buff": ({"value"}, set(), set()),
    "def_buff": ({"value"}, set(), set()),
}


def _validate_effects(effects, context: str):
    """Shared by skill and consumable loading -- `context` is a f-string-ready label (e.g.
    "dungeon_skills.json: skill 'foo'") prefixed onto every error so a bad JSON edit points
    straight at the offending entry."""
    if not effects:
        raise ValueError(f"{context} has empty effects")
    for effect in effects:
        effect_type = effect.get("type")
        if effect_type not in EFFECT_PARAM_SCHEMAS:
            raise ValueError(f"{context} has unknown effect type {effect_type!r}")
        required, optional, fraction_params = EFFECT_PARAM_SCHEMAS[effect_type]
        params = effect.keys() - {"type"}
        missing = required - params
        if missing:
            raise ValueError(f"{context} effect {effect_type!r} missing param(s): {sorted(missing)}")
        unknown = params - required - optional
        if unknown:
            raise ValueError(f"{context} effect {effect_type!r} has unknown param(s): {sorted(unknown)}")
        for param in params:
            value = effect[param]
            if param in fraction_params and not (0 < value <= 1):
                raise ValueError(f"{context} effect {effect_type!r} param {param!r} must be in (0, 1]")
            elif param not in fraction_params and value <= 0:
                raise ValueError(f"{context} effect {effect_type!r} param {param!r} must be > 0")


def _load_skills(path: str = _SKILLS_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    skills: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_SKILL_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"dungeon_skills.json: skill {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in skills:
            raise ValueError(f"dungeon_skills.json: duplicate skill id {entry_id!r}")
        if entry["main_class"] not in CLASSES:
            raise ValueError(f"dungeon_skills.json: skill {entry_id!r} has unknown main_class {entry['main_class']!r}")
        if entry["subclass"] not in SUBCLASSES:
            raise ValueError(f"dungeon_skills.json: skill {entry_id!r} has unknown subclass {entry['subclass']!r}")
        if entry["unlock_level"] < 1:
            raise ValueError(f"dungeon_skills.json: skill {entry_id!r} has invalid unlock_level")
        _validate_effects(entry["effects"], f"dungeon_skills.json: skill {entry_id!r}")
        skills[entry_id] = entry
    return skills


def _build_skills_by_combo(skills: dict[str, dict]) -> dict[tuple[str, str], list[dict]]:
    by_combo: dict[tuple[str, str], list[dict]] = {}
    for skill in skills.values():
        combo = (skill["main_class"], skill["subclass"])
        by_combo.setdefault(combo, []).append(skill)
    for combo, combo_skills in by_combo.items():
        combo_skills.sort(key=lambda s: s["unlock_level"])
    # Every build must have exactly one level-1 skill -- otherwise a fresh character could get an
    # empty Ability button, which is a strictly worse UX regression than anything a content typo
    # elsewhere in this file would cause, so it's checked here rather than left to be noticed live.
    for main_class in CLASSES:
        for subclass in SUBCLASSES:
            combo = (main_class, subclass)
            level_ones = [s for s in by_combo.get(combo, []) if s["unlock_level"] == 1]
            if len(level_ones) != 1:
                raise ValueError(
                    f"dungeon_skills.json: {main_class}/{subclass} must have exactly one unlock_level=1 "
                    f"skill, found {len(level_ones)}"
                )
    return by_combo


SKILLS = _load_skills()
SKILLS_BY_COMBO = _build_skills_by_combo(SKILLS)


def unlocked_skills(main_class: str, subclass: str, level: int) -> list[dict]:
    """All skills this build has unlocked by `level`, sorted by unlock_level ascending (so
    index 0 is always the level-1 base skill)."""
    return [s for s in SKILLS_BY_COMBO[(main_class, subclass)] if s["unlock_level"] <= level]


# --- Equipment -----------------------------------------------------------------------------
# Same registry pattern as MONSTERS above -- content lives in dungeon_equipment.json so new gear
# is a JSON edit, not a code change. Found as dungeon loot (a monster's own `drops` list -- see
# roll_drops above), never bought. Loaded before MONSTERS since a monster's drops get
# cross-validated against this registry.

_EQUIPMENT_PATH = os.path.join(os.path.dirname(__file__), "dungeon_equipment.json")
_REQUIRED_EQUIPMENT_FIELDS = {"id", "name", "slot", "rarity", "stat_bonuses", "flavor"}
EQUIPMENT_SLOTS = ("weapon", "armor", "trinket")
_STAT_BONUS_KEYS = {"hp", "atk", "def"}


def _load_equipment(path: str = _EQUIPMENT_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    equipment: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_EQUIPMENT_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"dungeon_equipment.json: item {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in equipment:
            raise ValueError(f"dungeon_equipment.json: duplicate item id {entry_id!r}")
        if entry["slot"] not in EQUIPMENT_SLOTS:
            raise ValueError(f"dungeon_equipment.json: item {entry_id!r} has unknown slot {entry['slot']!r}")
        bonuses = entry["stat_bonuses"]
        if not bonuses:
            raise ValueError(f"dungeon_equipment.json: item {entry_id!r} has empty stat_bonuses")
        bad_keys = set(bonuses) - _STAT_BONUS_KEYS
        if bad_keys:
            raise ValueError(f"dungeon_equipment.json: item {entry_id!r} has unknown stat_bonuses key(s): {bad_keys}")
        if any(v < 0 for v in bonuses.values()):
            raise ValueError(f"dungeon_equipment.json: item {entry_id!r} has a negative stat bonus")
        equipment[entry_id] = entry
    return equipment


EQUIPMENT = _load_equipment()


# --- Materials -------------------------------------------------------------------------------
# Crafting inputs -- same registry pattern as MONSTERS/EQUIPMENT/SKILLS. Materials have no combat
# stats; they're only meaningful once dungeon_recipes.json exists to turn them into something.
# Held in the same generic `inventory` table as quest items (db.add_inventory_item etc) rather
# than a dedicated table. Loaded before MONSTERS for the same drops cross-validation reason as
# EQUIPMENT above.

_MATERIALS_PATH = os.path.join(os.path.dirname(__file__), "dungeon_materials.json")
_REQUIRED_MATERIAL_FIELDS = {"id", "name", "rarity", "flavor"}


def _load_materials(path: str = _MATERIALS_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    materials: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_MATERIAL_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"dungeon_materials.json: material {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in materials:
            raise ValueError(f"dungeon_materials.json: duplicate material id {entry_id!r}")
        materials[entry_id] = entry
    return materials


MATERIALS = _load_materials()

# Loaded here, after EQUIPMENT/MATERIALS: a monster's drops list is cross-validated against them.
MONSTERS = _load_monsters()


# --- Consumables -------------------------------------------------------------------------------
# One-time-use crafted items, usable mid-combat (dungeon_view.py). Their own registry rather than
# a flag on EQUIPMENT since they're not gear -- no slot, no character_equipment row, no stats of
# their own beyond an effects list, using the exact same _validate_effects as SKILLS above so
# there's one definition of what an effect is for every kind of content that carries one. Held in
# the same generic `inventory` table as quest items and materials.

_CONSUMABLES_PATH = os.path.join(os.path.dirname(__file__), "dungeon_consumables.json")
_REQUIRED_CONSUMABLE_FIELDS = {"id", "name", "kind", "flavor", "effects"}


def _load_consumables(path: str = _CONSUMABLES_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    consumables: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_CONSUMABLE_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"dungeon_consumables.json: item {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in consumables:
            raise ValueError(f"dungeon_consumables.json: duplicate item id {entry_id!r}")
        if entry["kind"] != "consumable":
            raise ValueError(f"dungeon_consumables.json: item {entry_id!r} has kind {entry['kind']!r}, expected 'consumable'")
        _validate_effects(entry["effects"], f"dungeon_consumables.json: item {entry_id!r}")
        consumables[entry_id] = entry
    return consumables


CONSUMABLES = _load_consumables()

# DELVES is instantiated down here, after MONSTERS/MATERIALS/CONSUMABLES all exist, even though
# _load_delves/_validate_action (the functions) are defined much earlier alongside the rest of the
# "Delve content" section -- a choice room's action costs can reference any of the three, so the
# call has to wait for whichever loads last.
DELVES = _load_delves()


# --- Recipes -------------------------------------------------------------------------------
# Crafting turns materials (+ optional currency) into either an EQUIPMENT item or a CONSUMABLES
# item -- see crafting.py for the orchestration and db.craft_item for the atomic consumption
# transaction. Loaded last among the four registries above since a recipe's output_id is
# cross-validated against whichever of EQUIPMENT/CONSUMABLES it points into.

_RECIPES_PATH = os.path.join(os.path.dirname(__file__), "dungeon_recipes.json")
_REQUIRED_RECIPE_FIELDS = {"id", "name", "output_kind", "output_id", "materials"}
RECIPE_OUTPUT_KINDS = ("equipment", "consumable")


def _load_recipes(path: str = _RECIPES_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    recipes: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_RECIPE_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"dungeon_recipes.json: recipe {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in recipes:
            raise ValueError(f"dungeon_recipes.json: duplicate recipe id {entry_id!r}")
        if entry["output_kind"] not in RECIPE_OUTPUT_KINDS:
            raise ValueError(f"dungeon_recipes.json: recipe {entry_id!r} has unknown output_kind {entry['output_kind']!r}")
        materials = entry["materials"]
        if not materials:
            raise ValueError(f"dungeon_recipes.json: recipe {entry_id!r} has empty materials")
        for material_id, qty in materials.items():
            if material_id not in MATERIALS:
                raise ValueError(f"dungeon_recipes.json: recipe {entry_id!r} references unknown material {material_id!r}")
            if qty <= 0:
                raise ValueError(f"dungeon_recipes.json: recipe {entry_id!r} has non-positive qty for {material_id!r}")
        currency_cost = entry.get("currency_cost", 0)
        if currency_cost < 0:
            raise ValueError(f"dungeon_recipes.json: recipe {entry_id!r} has negative currency_cost")
        registry = EQUIPMENT if entry["output_kind"] == "equipment" else CONSUMABLES
        if entry["output_id"] not in registry:
            raise ValueError(
                f"dungeon_recipes.json: recipe {entry_id!r} output_id {entry['output_id']!r} not found in "
                f"{'EQUIPMENT' if entry['output_kind'] == 'equipment' else 'CONSUMABLES'}"
            )
        recipes[entry_id] = entry
    return recipes


RECIPES = _load_recipes()


def find_recipe_by_materials(materials: dict[str, int]) -> dict | None:
    """The recipe whose `materials` exactly matches `materials` -- same material ids *and*
    quantities, nothing extra, nothing missing. Powers discovery-based crafting (crafting.combine):
    a player picks materials without knowing the recipe first, and this is what tells them
    whether their guess landed on something. Exact-match (not "materials is a superset of the
    recipe's") is what makes two recipes distinguishable purely by quantity, e.g. Smelly Bomb
    (used_thong: 1, droppings: 1) vs. Bulging Tights (used_thong: 1, droppings: 2)."""
    for recipe in RECIPES.values():
        if recipe["materials"] == materials:
            return recipe
    return None


def item_power(item: dict) -> int:
    """Total stat value of an item -- the yardstick used to decide whether a newly found piece
    of gear replaces what's currently equipped in that slot."""
    return sum(item["stat_bonuses"].values())


def is_upgrade(current_item_id: str | None, new_item: dict) -> bool:
    """Whether new_item is worth equipping over whatever (if anything) is currently in its slot --
    the same power comparison used for both ordinary dungeon loot (dungeon_view.py's kill-rewards
    flow) and quest turn-in gear rewards (quests.py's turn_in), so the rule only lives once."""
    current_item = EQUIPMENT.get(current_item_id) if current_item_id else None
    return current_item is None or item_power(new_item) > item_power(current_item)


def compute_effective_stats(character: dict, equipped: dict[str, str]) -> dict:
    """A character's stored hp/atk/def (which already include all permanent level growth) plus
    whatever's currently equipped in each slot. `equipped` is {slot: item_id}, e.g. from
    db.get_equipped_items."""
    hp, atk, def_ = character["hp"], character["atk"], character["def"]
    for item_id in equipped.values():
        item = EQUIPMENT.get(item_id)
        if item is None:
            continue  # defensive: an item removed from the JSON after being equipped
        bonuses = item["stat_bonuses"]
        hp += bonuses.get("hp", 0)
        atk += bonuses.get("atk", 0)
        def_ += bonuses.get("def", 0)
    return {"hp": hp, "atk": atk, "def": def_}


# --- Combat ------------------------------------------------------------------------------------
# Deliberately lightweight: one attacker at a time, no persistent status effects beyond what a
# skill's own effects apply for the rest of the fight (see dungeon_view.py's EFFECT_HANDLERS).

DAMAGE_VARIANCE_LOW, DAMAGE_VARIANCE_HIGH = 0.85, 1.15


def roll_damage(atk: int, defense: int, multiplier: float = 1.0) -> int:
    """Damage is attacker's ATK (times an optional ability multiplier) minus defender's DEF,
    with +-15% variance, floored at 1 so a fight can never stall."""
    raw = (atk * multiplier - defense) * random.uniform(DAMAGE_VARIANCE_LOW, DAMAGE_VARIANCE_HIGH)
    return max(1, round(raw))


def roll_loot(monster: dict, loot_mult: float = 1.0) -> int:
    return round(random.randint(monster["loot_min"], monster["loot_max"]) * loot_mult)


def party_hp_multiplier(living_count: int) -> float:
    """A party delve's monster HP scale, vs. living_count party members -- sqrt rather than
    linear so a full party doesn't face a monster with 4x HP just because there are 4 attackers
    (roughly 1.0/1.41/1.73/2.0 for party sizes 1-4). Recomputed fresh at every new monster off
    however many members are still standing at that moment, not the party's original size."""
    return math.sqrt(max(1, living_count))


def roll_check(stat_value: int, dc: int) -> tuple[bool, int]:
    """A choice room's skill check -- rolls one of a character's own existing stats (see
    CHECK_STATS) against an author-set difficulty, same +-15% variance as roll_damage rather than
    a hand-set probability, so a check's odds naturally improve as a character's stats grow instead
    of needing to be re-tuned separately. `dc` is a plain number in the stat's own units (e.g. a
    DEF-based check calibrated against typical DEF values), not a 0-1 probability -- the same
    "hand-tune against real stat numbers" approach monster ATK/DEF already use. Returns (success,
    the actual rolled value) -- the roll is exposed so callers can show it in combat-log text."""
    rolled = round(stat_value * random.uniform(DAMAGE_VARIANCE_LOW, DAMAGE_VARIANCE_HIGH))
    return rolled >= dc, rolled
