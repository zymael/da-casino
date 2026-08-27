"""Dungeon RPG: pure logic and content (no discord import), mirroring horserace.py's split
between game logic here and Discord UI in dungeon_view.py.

Characters are a permanent one-time choice: a main class (face rank) x a subclass (suit) = 16
builds. Combat is deliberately lightweight -- HP/ATK/DEF/SpAtk/SpDef/Speed plus a per-delve Chips
pool (the casino's on-theme name for what a JRPG would call mana), no persistent status effects.
SpAtk/SpDef are a second, parallel combat-stat pair (Pokemon-style Physical/Special split) -- a
skill flagged "special" (dungeon_skills.json's optional `special` field, defaulting to
Physical/False) rolls its damage against SpAtk/SpDef instead of ATK/DEF; the plain Attack action is
always Physical. Speed drives turn order (see preview_next_turns below) -- a Final Fantasy X-style
Conditional Turn-Based queue, not a real-time gauge: turn order is a deterministic, precomputable
"next N turns" list derived purely from each combatant's own speed, resolved turn-by-turn between
discrete player actions, never live-filling. Skills cost Chips (dungeon_skills.json's chip_cost)
rather than being limited to one use per fight -- Chips refill to max once at the start of a delve
and are never spent outside combat, so the same pool is spent down across every fight in that delve
(a multi-fight delve rewards pacing Chips, not just burning them turn one) -- tracked only on the
in-memory delve session (DelveSession/PartyMember), never persisted to the DB. Skills unlock automatically as the character
levels. Each of the 16 builds has its own skill line (dungeon_skills.json, see SKILLS below) rather
than sharing one ability per class. Monster content lives in dungeon_monsters.json (not here)
specifically so new monsters can be added without touching this file -- see MONSTERS below.
"""

import json
import math
import os
import random

import horse_clothes

# Base HP/ATK/DEF/SpAtk/SpDef/Chips per class, before subclass modifiers. Archetypes: Fighter
# tanks (high HP/DEF, modest ATK, low Chips -- it leans on raw stats, not repeat skill casts) and
# is purely martial (low SpAtk, but SpDef matches its overall tankiness). Mage nukes physically
# (high ATK, fragile, highest Chips pool) AND is the premier spellcaster -- SpAtk (14) is even
# higher than its own ATK (10), while SpDef stays as fragile as DEF, matching its glass-cannon
# flavor either way. Rogue is balanced/quick with the second-highest Chips pool, mostly physical/
# martial in its skill flavor (Haymaker, Gut Strike, ...) so SpAtk/SpDef stay modest. Healer leans
# on HP + its Heal ability (modest Chips -- enough for a couple of heals) to outlast fights, and is
# the secondary spellcaster (solid SpAtk/SpDef, a support/caster archetype). Healer's ATK was
# originally 4, which combined with tougher monsters' DEF made roll_damage floor at 1 almost every
# hit -- an unwinnable slog regardless of how tanky Healer otherwise is. Bumped to 6 so Healer can
# still meaningfully damage things; simulated combat confirms this fixed it without needing to
# touch any other class. Each class's signature skill(s) now live in SKILLS below, keyed by
# (main_class, subclass) rather than on this dict.
#
# Chips pools (here and in SUBCLASSES below) are set so that even the worst-case build (fighter +
# clubs, whose -5 Chips modifier is the largest penalty) can still afford its own most expensive
# skill (an unlock_level=8 skill, costing 20 Chips per _load_skills' tier formula) at least once a
# fight -- see _load_skills' chip_cost validation, which enforces this for every build/skill pair
# rather than leaving it to be noticed live.
#
# `speed` drives turn order (see preview_next_turns below) -- a first design pass with no prior
# balance testing behind it (unlike the other five stats), so treat these numbers as a starting
# point, not a settled value. Rogue gets the highest base on purpose: its own flavor text already
# calls it "balanced/quick," so speed is the stat that actually cashes that description in rather
# than just being a slogan. Fighter is lowest, matching its heavy-armor tank identity; mage sits
# in the middle rather than at either extreme (a glass cannon isn't necessarily agile); healer is
# a flat average.
CLASSES = {
    "fighter": {"rank": "A", "hp": 32, "atk": 6, "def": 6, "spatk": 3, "spdef": 6, "chips": 25, "speed": 8},
    "healer": {"rank": "K", "hp": 26, "atk": 6, "def": 5, "spatk": 9, "spdef": 7, "chips": 30, "speed": 10},
    "mage": {"rank": "Q", "hp": 16, "atk": 10, "def": 2, "spatk": 14, "spdef": 3, "chips": 45, "speed": 9},
    "rogue": {"rank": "J", "hp": 22, "atk": 7, "def": 3, "spatk": 4, "spdef": 4, "chips": 40, "speed": 14},
}
RANK_TO_CLASS = {info["rank"]: name for name, info in CLASSES.items()}

# Casino-flavored display names for the four main classes themselves (distinct from NAMES' 16
# subclass-combo names below, e.g. "The Muscle" -- this is the class alone, before a subclass is
# even chosen). Single source of truth for dungeon_view.CLASS_OPTIONS (the character-creation
# picker) and admin_schemas.py's "skills" main_class dropdown, so the two surfaces can't drift.
MAIN_CLASS_DISPLAY = {
    "fighter": "The Enforcer (Ace)",
    "healer": "The Pit Boss (King)",
    "mage": "The Oracle (Queen)",
    "rogue": "The Hustler (Jack)",
}

# Subclass (suit) modifiers layered on top of the class base -- the same attitude framework used
# for the 16 display names: clubs (brawler) adds raw power but leans on brute force over finesse
# (lowest Chips, and no SpAtk/SpDef lean either -- pure muscle, no magic flavor, and the slowest
# suit to match), spades (lethal) trades defense for offense both physically and magically (SpAtk
# up, SpDef down, mirroring its ATK/DEF trade) and is the fastest suit -- a striker archetype
# (rogue+spades is literally "Assassin"), hearts (loyal) adds survivability and the most Chips (a
# support-leaning suit that most wants extra casts) plus the best SpDef (protective flavor), speed
# left neutral, diamonds (greedy) trades a little combat edge for meaningfully better loot, stays
# Chips-neutral, gets a small SpAtk bump mirroring its small ATK one, and a small speed bump too
# (an opportunist who gets in and out fast).
# Sentinel for "hasn't picked a subclass yet" -- a real, zero-modifier member of SUBCLASSES rather
# than None/"" threaded through every call site, so every existing SUBCLASSES[subclass]-keyed
# lookup (compute_stats, SKILLS_BY_COMBO, PartyMember/DelveSession's loot_mult/max_chips, the admin
# panel's skill_subclass cascade, quest trigger validation) keeps working unmodified for a
# base-class character. A character is created with this subclass (db.create_character), then
# db.choose_subclass swaps it for a real suit once at SUBCLASS_UNLOCK_LEVEL, applying that suit's
# modifiers onto their already-leveled stats -- see db.choose_subclass's own docstring.
NO_SUBCLASS = "none"
SUBCLASSES = {
    "clubs": {"hp": 4, "atk": 2, "def": 0, "spatk": 0, "spdef": 0, "loot_mult": 1.0, "chips": -5, "speed": -2},
    "spades": {"hp": 0, "atk": 3, "def": -1, "spatk": 2, "spdef": -1, "loot_mult": 1.0, "chips": 5, "speed": 2},
    "hearts": {"hp": 4, "atk": 0, "def": 2, "spatk": 0, "spdef": 2, "loot_mult": 1.0, "chips": 10, "speed": 0},
    # A -1 DEF here originally, on top of an already-below-average build, made a couple of
    # specific class+diamonds combos nearly unwinnable in simulation. A small +1 ATK (a
    # mercenary/treasure hunter still fights competently, just prioritizes the score) fixed that
    # without diamonds needing to be a pure stat no-op alongside its loot bonus.
    "diamonds": {"hp": 0, "atk": 1, "def": 0, "spatk": 1, "spdef": 0, "loot_mult": 1.25, "chips": 0, "speed": 1},
    NO_SUBCLASS: {"hp": 0, "atk": 0, "def": 0, "spatk": 0, "spdef": 0, "loot_mult": 1.0, "chips": 0, "speed": 0},
}
SUIT_SYMBOLS = {"clubs": "♣", "spades": "♠", "hearts": "♥", "diamonds": "♦", NO_SUBCLASS: ""}

# Level a character can first use !class again to pick a subclass -- a level gate for now, meant to
# eventually become a quest requirement instead (see NO_SUBCLASS above).
SUBCLASS_UNLOCK_LEVEL = 5

# The 16-name grid, worked out with the product owner: (class, subclass) -> display name.
NAMES = {
    ("fighter", "clubs"): "The Muscle", ("fighter", "spades"): "The Duelist",
    ("fighter", "hearts"): "The Minder", ("fighter", "diamonds"): "The Mercenary",
    ("healer", "clubs"): "The Cutman", ("healer", "spades"): "The Fixer",
    ("healer", "hearts"): "The Chaplain", ("healer", "diamonds"): "The Charlatan",
    ("mage", "clubs"): "The Wildcard", ("mage", "spades"): "The Jinx",
    ("mage", "hearts"): "The Enchanter", ("mage", "diamonds"): "The Mechanic",
    ("rogue", "clubs"): "The Bar Fighter", ("rogue", "spades"): "The Hitman",
    ("rogue", "hearts"): "The Heartbreaker", ("rogue", "diamonds"): "The Treasure Hunter",
}


def display_name(main_class: str, subclass: str) -> str:
    if subclass == NO_SUBCLASS:
        return MAIN_CLASS_DISPLAY[main_class]
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
        "spatk": base["spatk"] + mod["spatk"],
        "spdef": base["spdef"] + mod["spdef"],
        "chips": base["chips"] + mod["chips"],
        "speed": base["speed"] + mod["speed"],
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
    "id", "name", "hp", "atk", "def", "spatk", "spdef", "spd", "shape", "color", "flavor", "loot_min", "loot_max",
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
        for field in ("hp", "atk", "def", "spatk", "spdef", "spd", "loot_min", "loot_max"):
            if entry[field] < 0:
                raise ValueError(f"dungeon_monsters.json: monster {entry_id!r} has negative {field}")
        if entry["loot_min"] > entry["loot_max"]:
            raise ValueError(f"dungeon_monsters.json: monster {entry_id!r} has loot_min > loot_max")
        if "intended_level" in entry and (not isinstance(entry["intended_level"], int) or entry["intended_level"] < 1):
            raise ValueError(f"dungeon_monsters.json: monster {entry_id!r} intended_level must be a positive int")
        if "attack_chance" in entry and (
            not isinstance(entry["attack_chance"], (int, float)) or entry["attack_chance"] < 0
        ):
            raise ValueError(f"dungeon_monsters.json: monster {entry_id!r} attack_chance must be a number >= 0")
        if entry.get("skills"):
            for skill in entry["skills"]:
                _validate_monster_skill(skill, f"dungeon_monsters.json: monster {entry_id!r}")
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
#   "combat": {"id", "type": "combat", "monster_groups": [{"monsters": [...], "chance"?}, ...],
#              "background_path"?, "next"?, "prompt"?}
#     monster_groups is a list of possible monster groups for this room -- one group ("monsters", a
#     list of monster ids, all of which spawn together) is picked each visit via a weighted random
#     choice, see monsters_for_room. "chance" is the exact same relative-weight convention a
#     monster's own "skills" list already uses (dungeon.pick_monster_action) -- NOT a 0-1
#     probability, optional per group and defaulting to DEFAULT_MONSTER_GROUP_CHANCE (so an
#     untouched group is on equal footing with every other untouched group, same as today's uniform
#     random.choice used to be); lower it on one group to make it a rare encounter relative to the
#     room's other groups. RECONSTRUCTION NOTE: this list-of-groups shape (vs. a single flat list of
#     monster-id alternatives) is inferred from the live dungeon_monsters.json/dungeon_view.py
#     call site after an accidental truncation of this file -- the original wording/comment here
#     was not recovered, only the JSON shape and the one call site. next is another room's id, or
#     absent -- absent means clearing this room's monsters wins the delve (same semantics as
#     today's "last room", just explicit now instead of positional). prompt (optional, unlike a
#     choice room's required one) introduces the room itself -- shown once, right when the room is
#     entered, ahead of the monsters' own flavor text (see dungeon_view._combat_intro_text) --
#     purely flavor, never read by combat logic itself.
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
# which of a character's own stats a skill check can roll against -- every stat compute_effective_stats
# produces, so a check can be calibrated against any of them, not just the original atk/def/hp trio.
CHECK_STATS = ("hp", "atk", "def", "spatk", "spdef", "speed")
ACTION_COST_ITEM_KINDS = ("material", "consumable", "quest_item")
_REQUIRED_ROOM_FIELDS_BY_TYPE = {"combat": {"monster_groups"}, "choice": {"prompt", "actions"}}
_OPTIONAL_ROOM_FIELDS_BY_TYPE = {"combat": {"background_path", "next", "prompt"}, "choice": {"background_path"}}

_DELVES_PATH = os.path.join(os.path.dirname(__file__), "dungeon_delves.json")
_REQUIRED_DELVE_FIELDS = {"id", "name", "flavor", "rooms", "start_room"}
# currency_delta/item_* let an outcome give (positive) or take (negative) currency/an item on top of
# hp_delta -- item_qty's sign is what decides give vs. take, same single-field convention as
# hp_delta/currency_delta rather than separate give/take fields.
_ACTION_OUTCOME_KEYS = {"next", "hp_delta", "message", "currency_delta", "item_kind", "item_id", "item_qty"}


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
        if "currency_delta" in outcome:
            if not isinstance(outcome["currency_delta"], int) or outcome["currency_delta"] == 0:
                raise ValueError(f"{context}: {key}.currency_delta must be a nonzero int")
        if "item_id" in outcome:
            item_kind = outcome.get("item_kind")
            if item_kind not in ACTION_COST_ITEM_KINDS:
                raise ValueError(f"{context}: {key} with an item_id needs item_kind in {ACTION_COST_ITEM_KINDS}")
            if item_kind == "material" and outcome["item_id"] not in MATERIALS:
                raise ValueError(f"{context}: {key} references unknown material {outcome['item_id']!r}")
            if item_kind == "consumable" and outcome["item_id"] not in CONSUMABLES:
                raise ValueError(f"{context}: {key} references unknown consumable {outcome['item_id']!r}")
            # quest_item existence is checked by quests.py's cross-validation pass (see module docstring)
            if not isinstance(outcome.get("item_qty"), int) or outcome["item_qty"] == 0:
                raise ValueError(f"{context}: {key}.item_qty must be a nonzero int")
        elif "item_kind" in outcome or "item_qty" in outcome:
            raise ValueError(f"{context}: {key} has item_kind/item_qty but no item_id")


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
                monster_groups = room["monster_groups"]
                if not monster_groups:
                    raise ValueError(f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} has no monster_groups")
                for group in monster_groups:
                    if not isinstance(group, dict) or "monsters" not in group:
                        raise ValueError(
                            f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} has a monster group "
                            f"that isn't a {{monsters, chance?}} object"
                        )
                    monsters = group["monsters"]
                    if not monsters:
                        raise ValueError(f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} has an empty monster group")
                    for monster_id in monsters:
                        if monster_id not in MONSTERS:
                            raise ValueError(
                                f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} "
                                f"references unknown monster {monster_id!r}"
                            )
                    if "chance" in group:
                        chance = group["chance"]
                        if not isinstance(chance, (int, float)) or chance < 0:
                            raise ValueError(
                                f"dungeon_delves.json: delve {entry_id!r} room {room_id!r} "
                                f"has a monster group chance that must be a number >= 0"
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


def active_delves(include_inactive: bool = False) -> dict[str, dict]:
    """Every delve actually offered to players -- the one place "is this delve playable" is
    decided, so a new delve-selection surface (a command, a picker view, ...) never needs its own
    copy of the "active" check. See _load_delves' module-docstring-adjacent comment on the
    "active" field for why an inactive delve can still be saved (and still lives in DELVES) despite
    never showing up here. `include_inactive` is for a guild with delve_test_mode on (db.py) --
    testers get to see/play a delve before it's flipped active for everyone else."""
    if include_inactive:
        return dict(DELVES)
    return {delve_id: delve for delve_id, delve in DELVES.items() if delve.get("active", True)}


def rooms_by_id(delve: dict) -> dict[str, dict]:
    return {room["id"]: room for room in delve["rooms"]}


DEFAULT_MONSTER_GROUP_CHANCE = 1.0


def monsters_for_room(room: dict) -> list[dict]:
    """Picks one of this room's monster_groups (weighted by each group's own "chance", defaulting
    to DEFAULT_MONSTER_GROUP_CHANCE -- same relative-weight convention as pick_monster_action's own
    weights, so a "rare encounter" group is just a smaller number next to the room's other groups)
    and resolves it to full monster dicts -- the whole group spawns together. RECONSTRUCTION NOTE:
    the underlying list-of-groups shape was rebuilt after an accidental truncation of this file,
    inferred from dungeon_view.py's call site and dungeon_monsters.json's shape; weighting was added
    later, on top of that reconstruction."""
    weights = [g.get("chance", DEFAULT_MONSTER_GROUP_CHANCE) for g in room["monster_groups"]]
    group = random.choices(room["monster_groups"], weights=weights, k=1)[0]
    return [MONSTERS[monster_id] for monster_id in group["monsters"]]


# --- Leveling ------------------------------------------------------------------------------
# XP is awarded per monster kill, scaled by monster difficulty (deeper/tougher = more XP, same
# "push deeper pays off more" logic already driving loot). Leveling grants automatic flat stat
# growth -- no player choice -- applied by mutating the character's stored stats in place
# (db.add_xp), the same way horse training already grows a horse's stats via db.train_horse.
# Subclass-specific skills unlocked by level are a deferred follow-up; `level` is tracked now
# specifically so that pass won't need a data-model change.
LEVEL_HP_GAIN, LEVEL_ATK_GAIN, LEVEL_DEF_GAIN = 2, 1, 1
LEVEL_SPATK_GAIN, LEVEL_SPDEF_GAIN = 1, 1  # matches ATK/DEF's growth rate
LEVEL_SPEED_GAIN = 1  # same growth rate again -- speed grows proportionally, not disproportionately


def xp_for_monster(monster: dict) -> int:
    """RECONSTRUCTION NOTE: this file was accidentally truncated and this function's real body
    (which took a room/monster "tier" 1-3 mapped to 10/20/40 XP) was not recovered. monsters no
    longer carry a tier field at all (see _REQUIRED_MONSTER_FIELDS) -- real content now uses an
    optional intended_level (observed range 1-30 in dungeon_monsters.json) instead, so this is a
    straight-line placeholder (10 XP per intended_level, defaulting to 1) preserving the old
    tier-1 rate as a floor. Needs a real balance pass against actual leveling curve/xp_to_next_level."""
    return 10 * monster.get("intended_level", 1)


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
_REQUIRED_SKILL_FIELDS = {"id", "main_class", "subclass", "unlock_level", "name", "flavor", "chip_cost"}

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
    "spatk_buff": ({"value"}, set(), set()),
    "spdef_buff": ({"value"}, set(), set()),
    # Raises max HP (and current HP by the same amount, a fortify rather than just extra
    # headroom) -- mirrors the other *_buff entries, no equivalent "hp_debuff" (nothing currently
    # authors a max-HP-lowering effect, unlike ATK/DEF/SpAtk/SpDef which already had def_shred).
    "hp_buff": ({"value"}, set(), set()),
    # Raises speed for the rest of the fight -- turn order (preview_next_turns) reads this live at
    # every scheduling point, never cached, so a mid-fight speed_buff changes future turn
    # frequency immediately without needing to reschedule anything already queued.
    "speed_buff": ({"value"}, set(), set()),
    # Debuffs mirroring def_shred (permanent for the fight, no duration) -- there's no separate
    # "def_debuff" type since def_shred already fills that role.
    "atk_debuff": ({"value"}, set(), set()),
    "spatk_debuff": ({"value"}, set(), set()),
    "spdef_debuff": ({"value"}, set(), set()),
    # Lowers speed for the rest of the fight -- same "read live, never cached" story as speed_buff
    # above, just subtracted (DelveSession/PartyMember/MonsterInstance already carry a
    # speed_debuff field and every turn-order/turn-interval call site already reads
    # speed - speed_debuff; this type was the only thing missing to ever actually set it).
    "speed_debuff": ({"value"}, set(), set()),
    # Threat manipulation (see the "Party threat" section below) -- raises/lowers the actor's own
    # standing in a monster's threat table (dungeon_view.MonsterInstance.threat). Enemy-targeted
    # like atk_debuff/def_shred (ENEMY_TARGETED_EFFECT_TYPES below) -- single-target by default
    # (only current_target's table), "aoe": true hits every living monster's table at once.
    # Player-only: dungeon._validate_monster_skill explicitly rejects both on a monster's own skill
    # (players already pick their own attack target directly, so there's no equivalent "who do I
    # attack" ambiguity for a monster to need swaying), and both are excluded from equipment
    # entirely (ON_HIT_EQUIPMENT_EFFECT_TYPES and _validate_equipment_effects' on_use check below)
    # -- skills/consumables only.
    "taunt": ({"value"}, set(), set()),
    "lower_threat": ({"value"}, set(), set()),
    # Genuinely temporary effects (N rounds, ticked by dungeon_view._tick_timed_effects) rather
    # than permanent-for-fight -- "duration" is validated as a positive int by _validate_effects
    # below (turns can't be fractional, unlike every other numeric param here).
    "dodge_buff": ({"value", "duration"}, set(), {"value"}),
    "resist_buff": ({"value", "duration"}, set(), {"value"}),
    "dot": ({"value", "duration"}, set(), set()),
    "hot": ({"value", "duration"}, set(), {"value"}),
    # Crowd control -- also genuinely temporary (ticked by dungeon_view._tick_timed_effects) like
    # the four above, but ENEMY_TARGETED rather than ally-shaped (see that set below): they land on
    # current_target/every monster, not the caster. No "value" param -- there's nothing to scale,
    # just "skip this many of the target's own turns." Sap breaks the instant its target takes ANY
    # damage, including from the very same action that applied it (dungeon_view._break_sap runs at
    # every point damage lands on any entity, with no way to except "this hit doesn't count because
    # it's the one that just inflicted Sap") -- so a skill pairing direct damage with Sap on the
    # same cast will see the Sap broken immediately; author Sap as a pure-utility effect if it's
    # meant to actually hold. Stun has no such condition -- it always lasts its full duration.
    "sap": ({"duration"}, set(), set()),
    "stun": ({"duration"}, set(), set()),
    # Ally-shaped (unlike sap/stun above) -- a cleanse targets the caster, or (per its own "aoe")
    # every living party member, same as heal_fraction/the *_buff family. No params at all -- there's
    # nothing to scale, it's a pure "remove this category of active timed effect" toggle. Two
    # separate types rather than one "cleanse everything bad" effect so a skill/consumable can be
    # authored as DoT-cleanse-only or CC-cleanse-only (e.g. a cheap early-game antidote that can't
    # also break a Stun) instead of always doing both.
    "cleanse_dot": (set(), set(), set()),
    "cleanse_cc": (set(), set(), set()),
}

# Which handler-calling-convention an effect type uses, for dungeon_view.py's dispatch -- three
# buckets, not two. This USED to also be what decided *who* an effect could ever hit (a stun could
# only ever land on an enemy, a heal only ever on an ally) -- that restriction is gone (see "target"
# below, and dungeon_view._resolve_player_action); these sets now only decide *how* an effect is
# invoked and, for MODS_ONLY specifically, that it participates in the damage roll at all:
#   - MODS_ONLY: configures the attack itself (a multiplier, an extra hit, a lifesteal fraction of
#     whatever damage ends up dealt) rather than being applied to an entity directly -- the damage
#     roll it configures now targets whichever entity/entities its own "target" (defaulting to
#     "enemy") resolves to, self and allies fully included.
#   - ENEMY_TARGETED: EFFECT_HANDLERS[type] is called as (actor, entity, ...) -- `entity` is
#     whichever this effect's own "target" (defaulting to "enemy") resolves to. The name is legacy
#     (every type in this set used to be enemy-only); it's really "second-argument-shaped" now.
#   - everything else (not in either set above) is "first-argument-shaped": EFFECT_HANDLERS[type] is
#     called as (entity, None, ...), `entity` again resolved from this effect's own "target"
#     (defaulting to "ally").
# "aoe" (validated below) still means "every living entity in whichever pool 'target' resolved to"
# regardless of bucket. Dodge-gating now follows resolved target, not bucket: anything resolving to
# "enemy" is dodge-gated (matching every enemy-shaped effect's existing behavior); "self"/"ally"
# never are (a self-heal, or now a self-inflicted stun, was never really "the attack" that could be
# dodged) -- see dungeon_view._resolve_player_action.
MODS_ONLY_EFFECT_TYPES = {"damage_multiplier", "extra_attack", "lifesteal_fraction"}
ENEMY_TARGETED_EFFECT_TYPES = {
    "atk_debuff", "spatk_debuff", "spdef_debuff", "speed_debuff", "def_shred",
    "taunt", "lower_threat", "sap", "stun",
}

# Who an effect lands on -- fully decoupled from its type (dungeon_view._resolve_player_action):
# "self" (the caster, always singular, "aoe" is a no-op), "ally" (whichever living ally the caster
# currently has selected, dungeon_view.PartyDelveSession.ally_target_for -- defaults to the caster
# themselves outside a party -- or every living ally with "aoe"), "enemy" (current_target, or every
# living enemy with "aoe"). Every effect type can use every target -- a self-inflicted stun, a heal
# aimed at the enemy, a damage_multiplier that hurts an ally, all equally legal now. Optional per
# effect; absent means default_effect_target's type-based default, which reproduces every existing
# skill/consumable/monster-skill's behavior exactly as authored before "target" existed.
EFFECT_TARGETS = ("self", "ally", "enemy")


def default_effect_target(effect_type: str) -> str:
    """The target an effect resolves to when it doesn't set "target" explicitly -- reproduces
    exactly what every effect type already did before targeting became an explicit per-effect
    choice, so no existing content needs touching. MODS_ONLY/ENEMY_TARGETED types default to
    "enemy" (their old exclusive target); everything else defaults to "ally" (its old exclusive
    target -- "ally" alone, non-aoe, already means "self" via ally_target_for's own default)."""
    if effect_type in MODS_ONLY_EFFECT_TYPES or effect_type in ENEMY_TARGETED_EFFECT_TYPES:
        return "enemy"
    return "ally"


def _validate_effects(effects, context: str):
    """Shared by skill and consumable loading -- `context` is a f-string-ready label (e.g.
    "dungeon_skills.json: skill 'foo'") prefixed onto every error so a bad JSON edit points
    straight at the offending entry.

    "aoe" is a universal optional bool on every effect entry, of every type -- whether THIS effect
    hits its usual single target or every living entity in whichever pool "target" (see below)
    resolved to instead. Validated here, separately from the required/optional/fraction machinery
    below (that's all numeric-param shaped; "aoe" is the first non-numeric param this function ever
    needed), and excluded from `params` before the required/unknown-param checks so it's never
    treated as an unknown param on any type, nor forced into any type's own required/optional list.

    "target" is a universal optional enum ("self"/"ally"/"enemy", EFFECT_TARGETS) picking WHO this
    effect lands on, fully decoupled from its type -- no restriction of any kind on which type can
    use which target, unlike the "self_only" flag this replaced (which only ever worked on an
    ally-shaped effect). Absent means default_effect_target(effect_type)'s type-based default,
    which reproduces exactly what every effect type already did before "target" existed, so no
    existing content needs touching. See dungeon_view._resolve_player_action for how a resolved
    "enemy" target is dodge-gated and "self"/"ally" never are, regardless of the effect's own type.

    "chance" is a universal optional 0-1 probability, independently rolled per effect at cast time
    (dungeon_view.resolve_cast_effects) -- absent means "always fires" (probability 1), same as
    every effect authored before this existed. Two effects on the same skill each with their own
    "chance" are independent Bernoulli trials, not alternatives -- a 50%-stun + 75%-damage skill can
    land both, either, or neither. This is deliberately the same param name equipment's own on_hit
    trigger already uses for an identical concept (an independent per-effect fire probability) --
    equipment strips its own "chance" out of each effect dict before ever reaching this function
    (see _validate_equipment_effects), so the two never collide despite sharing a name. For
    choosing between mutually-exclusive alternatives ("50% this OR 50% that") see effect_groups
    instead (_validate_effect_groups) -- a different mechanism, relative weights instead of
    independent probabilities, same distinction dungeon_view.monsters_for_room's group "chance"
    (relative weight) already draws against this same param name."""
    if not effects:
        raise ValueError(f"{context} has empty effects")
    for effect in effects:
        effect_type = effect.get("type")
        if effect_type not in EFFECT_PARAM_SCHEMAS:
            raise ValueError(f"{context} has unknown effect type {effect_type!r}")
        required, optional, fraction_params = EFFECT_PARAM_SCHEMAS[effect_type]
        if "aoe" in effect and not isinstance(effect["aoe"], bool):
            raise ValueError(f"{context} effect {effect_type!r} param 'aoe' must be a bool")
        if "chance" in effect:
            chance = effect["chance"]
            if not isinstance(chance, (int, float)) or not (0 < chance <= 1):
                raise ValueError(f"{context} effect {effect_type!r} param 'chance' must be in (0, 1]")
        if "target" in effect and effect["target"] not in EFFECT_TARGETS:
            raise ValueError(f"{context} effect {effect_type!r} param 'target' must be one of {EFFECT_TARGETS}")
        params = effect.keys() - {"type", "aoe", "target", "chance"}
        missing = required - params
        if missing:
            raise ValueError(f"{context} effect {effect_type!r} missing param(s): {sorted(missing)}")
        unknown = params - required - optional
        if unknown:
            raise ValueError(f"{context} effect {effect_type!r} has unknown param(s): {sorted(unknown)}")
        for param in params:
            value = effect[param]
            if param == "duration":
                if not isinstance(value, int) or value <= 0:
                    raise ValueError(f"{context} effect {effect_type!r} param 'duration' must be a positive int")
            elif param in fraction_params and not (0 < value <= 1):
                raise ValueError(f"{context} effect {effect_type!r} param {param!r} must be in (0, 1]")
            elif param not in fraction_params and value <= 0:
                raise ValueError(f"{context} effect {effect_type!r} param {param!r} must be > 0")


def _validate_effect_groups(effect_groups, context: str) -> None:
    """The alternative to a flat "effects" list -- a list of {"chance"?, "effects"} groups, exactly
    ONE of which is chosen at cast time via a weighted random pick (dungeon_view.
    resolve_cast_effects) -- the group's own "chance" is a relative WEIGHT against its sibling
    groups, the same convention monsters_for_room's own monster_groups and a monster's own "skills"
    list already use (NOT the independent per-effect probability _validate_effects' "chance"
    means -- see that function's own docstring for the full distinction). This is how a "50% this
    OR 50% that" skill is authored. It is NOT how "50% chance of X, independently also 75% chance
    of Y" is authored -- that's just two effects each carrying their own "chance" inside one group
    (or the plain flat "effects" list, a single implicit group) -- no effect_groups needed at all
    for independent per-effect rolls, only for mutually-exclusive alternatives."""
    if not effect_groups:
        raise ValueError(f"{context} has empty effect_groups")
    for i, group in enumerate(effect_groups):
        group_context = f"{context} effect_groups[{i}]"
        if not isinstance(group, dict):
            raise ValueError(f"{group_context} must be an object")
        if "chance" in group:
            chance = group["chance"]
            if not isinstance(chance, (int, float)) or chance < 0:
                raise ValueError(f"{group_context} chance must be a number >= 0")
        unknown = group.keys() - {"chance", "effects"}
        if unknown:
            raise ValueError(f"{group_context} has unknown key(s): {sorted(unknown)}")
        if "effects" not in group:
            raise ValueError(f"{group_context} has no effects")
        _validate_effects(group["effects"], group_context)


def _validate_effects_or_groups(entry: dict, context: str) -> None:
    """Every skill/consumable/monster-skill authors EXACTLY ONE of "effects" (the common case, a
    flat list -- see _validate_effects) or "effect_groups" (the mutually-exclusive-alternatives
    case -- see _validate_effect_groups), never both, never neither. Centralized here since all
    three loaders need the identical XOR check."""
    has_effects = "effects" in entry
    has_groups = "effect_groups" in entry
    if has_effects == has_groups:
        raise ValueError(f"{context} must have exactly one of 'effects' or 'effect_groups'")
    if has_effects:
        _validate_effects(entry["effects"], context)
    else:
        _validate_effect_groups(entry["effect_groups"], context)


def _effect_lists(entry: dict) -> list[list[dict]]:
    """Every flat effects list `entry` could possibly apply, regardless of which of the two shapes
    _validate_effects_or_groups accepted -- one list for plain "effects", one per group for
    "effect_groups". For code that needs to scan every effect an entry could ever produce without
    caring which shape authored it (e.g. _validate_monster_skill's player-only-effect-type check)."""
    if "effect_groups" in entry:
        return [g["effects"] for g in entry["effect_groups"]]
    return [entry.get("effects", [])]


DEFAULT_EFFECT_GROUP_CHANCE = 1.0


def resolve_cast_effects(entry: dict) -> list[dict]:
    """The actual effects list one cast of `entry` (a skill/consumable/monster-skill dict) applies
    THIS time -- two independent randomization layers, matching _validate_effect_groups/
    _validate_effects' own docstrings:
      1. If `entry` authored "effect_groups" (mutually-exclusive alternatives), pick exactly ONE
         group via a weighted random choice on each group's own "chance" (a relative weight,
         defaulting to DEFAULT_EFFECT_GROUP_CHANCE -- same convention monsters_for_room's own
         monster_groups already uses). A plain "effects" list is just the one implicit group,
         always "chosen" since there's nothing to pick between.
      2. Independently roll each effect *within* whichever list step 1 produced against its own
         optional "chance" (a true 0-1 probability, defaulting to 1 -- always fires) -- so a
         50%-stun + 75%-damage skill can land both, either, or neither this cast.
    Called once per actual cast, right where a skill/item's raw "effects" used to be read directly
    (dungeon_view.py's _resolve_combat_turn/_resolve_party_turn/_resolve_duel_turn/
    _resolve_monster_attack and their own _handle_*_action/_handle_*_use_item callers) --
    everything downstream of this point (aoe/target resolution, dodge, damage rolls, ...) stays
    completely unaware any of this happened; it just sees a shorter effects list than what was
    authored. Can return an empty list (every effect whiffed its own roll, or a chosen group had
    nothing left) -- callers already treat "no damage_multiplier/extra_attack present" as "this
    cast dealt no damage" for a genuine skill (dungeon_view._resolve_player_action's
    `is_plain_attack` param is what still lets a skill-less plain Attack always hit unconditionally,
    a case this function is never even called for)."""
    if "effect_groups" in entry:
        groups = entry["effect_groups"]
        weights = [g.get("chance", DEFAULT_EFFECT_GROUP_CHANCE) for g in groups]
        effects = random.choices(groups, weights=weights, k=1)[0]["effects"]
    else:
        effects = entry.get("effects", [])
    return [e for e in effects if random.random() < e.get("chance", 1.0)]


# A monster's own skill can use almost the exact same effect vocabulary a player skill can -- this
# used to be a narrower subset (monsters had no mutable combat stats to buff/debuff, and several
# handlers hardcoded "You"-phrased log text that would misread coming from a monster);
# MonsterInstance now carries real per-instance atk/def_/spatk/spdef/debuff fields and
# timed_effects (dungeon_view.py), and every handler's log line goes through an actor-aware helper,
# so there's nothing left that only works for a player actor -- except taunt/lower_threat
# (_MONSTER_SKILL_EXCLUDED_EFFECT_TYPES below), which are meaningless for a monster's own skill: a
# player already picks their own attack target directly, so there's no "who do I attack" choice for
# a monster to sway on a player's behalf the way taunt/lower_threat sway a monster's own choice.
_MONSTER_SKILL_EXCLUDED_EFFECT_TYPES = {"taunt", "lower_threat"}


def _validate_monster_skill(skill: dict, context: str) -> None:
    """One entry in a monster's own optional "skills" list -- {name, chance, effects|effect_groups}.
    `chance` is a relative WEIGHT, not a strict probability -- see pick_monster_action, which weighs
    it against the monster's own attack_chance and every other skill's chance via random.choices (so
    weights never need to be balanced to sum to anything in particular, and a weight of exactly 0 is
    a legal "this skill is currently disabled" rather than an error). effects/effect_groups reuses
    the same vocabulary skills/consumables already validate via _validate_effects_or_groups -- full
    parity with a player skill's own effect vocabulary except _MONSTER_SKILL_EXCLUDED_EFFECT_TYPES,
    see the comment above this function."""
    name = skill.get("name")
    if not name:
        raise ValueError(f"{context} has a skill with no name")
    chance = skill.get("chance")
    if not isinstance(chance, (int, float)) or chance < 0:
        raise ValueError(f"{context} skill {name!r} chance must be a number >= 0")
    if "special" in skill and not isinstance(skill["special"], bool):
        raise ValueError(f"{context} skill {name!r} special must be a bool")
    skill_context = f"{context} skill {name!r}"
    _validate_effects_or_groups(skill, skill_context)
    for effects in _effect_lists(skill):
        for effect in effects:
            if effect["type"] in _MONSTER_SKILL_EXCLUDED_EFFECT_TYPES:
                raise ValueError(
                    f"{skill_context} effect {effect['type']!r} is player-only, not valid on a monster skill"
                )


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
        chip_cost = entry["chip_cost"]
        if not isinstance(chip_cost, int) or chip_cost < 1:
            raise ValueError(f"dungeon_skills.json: skill {entry_id!r} chip_cost must be a positive int")
        build_chips = compute_stats(entry["main_class"], entry["subclass"])["chips"]
        if chip_cost > build_chips:
            raise ValueError(
                f"dungeon_skills.json: skill {entry_id!r} costs {chip_cost} chips but "
                f"{entry['main_class']}/{entry['subclass']} only has {build_chips} max chips -- would be unusable"
            )
        if "special" in entry and not isinstance(entry["special"], bool):
            raise ValueError(f"dungeon_skills.json: skill {entry_id!r} special must be a bool")
        _validate_effects_or_groups(entry, f"dungeon_skills.json: skill {entry_id!r}")
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
    # Zero is still a hard failure (no reasonable skill to synthesize, and it's a much rarer mistake
    # to make than the other direction) -- but MORE than one (e.g. leftover template scaffolding
    # from a Duplicate that never got re-leveled/re-subclassed before saving) degrades gracefully
    # instead of taking the entire bot down over one bad combo in one build: keep the
    # lowest-id skill deterministically, drop the rest from SKILLS_BY_COMBO (they're still in
    # SKILLS/the admin panel's own list for whoever owns content to find and fix), and log it
    # loudly so the mistake doesn't go unnoticed just because it stopped being fatal.
    for main_class in CLASSES:
        for subclass in SUBCLASSES:
            combo = (main_class, subclass)
            level_ones = [s for s in by_combo.get(combo, []) if s["unlock_level"] == 1]
            if not level_ones:
                raise ValueError(
                    f"dungeon_skills.json: {main_class}/{subclass} has no unlock_level=1 skill"
                )
            if len(level_ones) > 1:
                keep = min(level_ones, key=lambda s: s["id"])
                drop_ids = {s["id"] for s in level_ones if s is not keep}
                print(
                    f"[dungeon] {main_class}/{subclass} has {len(level_ones)} unlock_level=1 skills "
                    f"({', '.join(sorted(s['id'] for s in level_ones))}) -- keeping {keep['id']!r}, "
                    f"dropping the rest so startup doesn't fail entirely. Fix this in the admin panel.",
                    flush=True,
                )
                by_combo[combo] = [s for s in by_combo[combo] if s["id"] not in drop_ids]
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
#
# An item's `effects` reuses the exact same {type, ...params} vocabulary skills/consumables
# already validate via _validate_effects, plus a `trigger` on every entry saying when it fires:
#   "constant": always active while equipped (what a flat stat_bonuses dict used to be) --
#     restricted to CONSTANT_EQUIPMENT_EFFECT_TYPES (the *_buff family), the only types with
#     "always on, no action" semantics. Folded into a character's effective stats by
#     constant_stat_bonuses below, a pure data fold -- never runs through dungeon_view's
#     EFFECT_HANDLERS (those log "Your ATK rises..." lines meant for an in-fight cast, which have
#     nowhere sensible to go for a passive that's just always been on).
#   "on_use": a combat action the wearer can trigger once per fight (dungeon_view.py's
#     _handle_cast_item) -- mechanically identical to using a consumable, just not consumed and
#     gated by a per-fight-used flag instead of an inventory quantity. Unrestricted type-wise.
#   "on_hit": an independent chance (this entry's own `chance`, required, in (0, 1]) to fire when
#     the wearer lands any damage-dealing hit (dungeon_view.py's _roll_on_hit_procs). Restricted to
#     ON_HIT_EQUIPMENT_EFFECT_TYPES -- damage_multiplier/guard/extra_attack are excluded because
#     their handlers don't apply anything themselves, they only populate a `mods` dict a *calling*
#     combat function reads back at one fixed point in its own body (guard's reduction against
#     that same action's monster counter-attack a few lines later; extra_attack's multiplier list
#     drained by a loop right after _apply_effects returns; damage_multiplier has nothing left to
#     multiply once the triggering hit's damage is already rolled) -- that window has already
#     closed by the time an on-hit proc could fire. lifesteal_fraction stays allowed despite having
#     the same "just sets a mods flag" shape, because _roll_on_hit_procs special-cases it to apply
#     directly against the triggering hit's own already-known damage number.
# An item's top-level optional `special` (bool, default False) picks SpAtk/SpDef instead of
# ATK/DEF for an on_use damage roll -- mirrors a skill/consumable's own optional `special` field
# exactly; only meaningful if the item has a damage-shaped on_use effect.

# A fixed tier ladder -- "rarity" used to be flavor-only free text, unvalidated and never read by
# game logic (real content had already drifted to both "epic" and "Epic"). Now does two real
# things: RARITY_EMOJI gives every item a colored-dot prefix wherever it's displayed
# (inventory_view.py), and RARITY_STAT_MULTIPLIERS scales generate_item_constant_effects' budget,
# so a higher tier is authored to hit meaningfully better stats at the same level rather than
# rarity being purely cosmetic. The multiplier curve is a hand-picked, not-evenly-spaced pass (the
# epic->legendary jump is the biggest, so "legendary" reads as a rare standout rather than just one
# more even step) -- same "reasonable pass, not physics" spirit as every other generation constant
# in this section (_item_power_budget etc).
EQUIPMENT_RARITIES = ("common", "uncommon", "rare", "epic", "legendary")
RARITY_EMOJI = {"common": "⚪", "uncommon": "🟢", "rare": "🔵", "epic": "🟣", "legendary": "🟠"}
RARITY_STAT_MULTIPLIERS = {"common": 1.0, "uncommon": 1.25, "rare": 1.6, "epic": 2.1, "legendary": 3.0}

_EQUIPMENT_PATH = os.path.join(os.path.dirname(__file__), "dungeon_equipment.json")
_REQUIRED_EQUIPMENT_FIELDS = {"id", "name", "slot", "rarity", "effects", "flavor", "base_value"}
EQUIPMENT_SLOTS = ("weapon", "armor", "trinket")
EQUIPMENT_EFFECT_TRIGGERS = ("constant", "on_use", "on_hit")
CONSTANT_EQUIPMENT_EFFECT_TYPES = {"atk_buff", "def_buff", "spatk_buff", "spdef_buff", "hp_buff", "speed_buff"}
# speed_buff is allowed here on purpose ("haste on hit") -- it's self-contained (only ever touches
# `actor`, same as the other *_buff types already allowed on_hit), no reason to special-case it out.
# taunt/lower_threat ARE excluded on purpose -- player-only threat manipulation stays scoped to
# skills/consumables, no equipment support (see EFFECT_PARAM_SCHEMAS' own comment on them).
ON_HIT_EQUIPMENT_EFFECT_TYPES = set(EFFECT_PARAM_SCHEMAS) - {
    "damage_multiplier", "guard", "extra_attack", "taunt", "lower_threat",
}
# taunt/lower_threat are excluded from equipment everywhere, not just on_hit above -- "constant" is
# already covered by CONSTANT_EQUIPMENT_EFFECT_TYPES not listing them, but "on_use" has no other
# type restriction at all (see _validate_equipment_effects' else branch), so it needs this explicit
# check of its own.
_ON_USE_EQUIPMENT_EXCLUDED_EFFECT_TYPES = {"taunt", "lower_threat"}
# type -> which stat constant_stat_bonuses folds it into -- the inverse of generate_item_constant_effects'
# own mapping below.
_CONSTANT_EFFECT_STAT = {
    "atk_buff": "atk", "def_buff": "def", "spatk_buff": "spatk", "spdef_buff": "spdef",
    "hp_buff": "hp", "speed_buff": "speed",
}


def _validate_equipment_effects(effects, context: str) -> None:
    """Equipment's own effects-list validation -- checks the two fields no skill/consumable effect
    has (`trigger`, `chance`) and the trigger-dependent type restriction, then delegates the
    shared per-type param checks (value ranges, required/optional params, ...) to _validate_effects
    via a stripped copy of each effect (trigger/chance removed) -- reusing it unmodified on the raw
    dicts would reject every equipment effect as carrying unknown params."""
    if not effects:
        raise ValueError(f"{context} has empty effects")
    for i, effect in enumerate(effects):
        effect_context = f"{context} effect {i}"
        trigger = effect.get("trigger")
        if trigger not in EQUIPMENT_EFFECT_TRIGGERS:
            raise ValueError(f"{effect_context} has unknown trigger {trigger!r}")
        effect_type = effect.get("type")
        if trigger == "on_hit":
            if effect_type not in ON_HIT_EQUIPMENT_EFFECT_TYPES:
                raise ValueError(f"{effect_context} type {effect_type!r} can't be used with trigger 'on_hit'")
            chance = effect.get("chance")
            if not isinstance(chance, (int, float)) or not (0 < chance <= 1):
                raise ValueError(f"{effect_context} (trigger 'on_hit') needs a chance in (0, 1]")
        else:
            if "chance" in effect:
                raise ValueError(f"{effect_context} (trigger {trigger!r}) must not set chance")
            if trigger == "constant" and effect_type not in CONSTANT_EQUIPMENT_EFFECT_TYPES:
                raise ValueError(f"{effect_context} type {effect_type!r} can't be used with trigger 'constant'")
            if trigger == "on_use" and effect_type in _ON_USE_EQUIPMENT_EXCLUDED_EFFECT_TYPES:
                raise ValueError(f"{effect_context} type {effect_type!r} can't be used with trigger 'on_use' (player-only threat effect, skills/consumables only)")
    _validate_effects(
        [{k: v for k, v in e.items() if k not in ("trigger", "chance")} for e in effects], context
    )


def constant_stat_bonuses(item: dict) -> dict[str, int]:
    """An item's `trigger == "constant"` effects collapsed into the old flat {hp, atk, def, spatk,
    spdef} shape -- the one place this mapping lives, reused by compute_effective_stats. Sums if
    the same stat is buffed by more than one constant entry. `item.get("effects", [])` rather than
    `item["effects"]` since this is also called against an in-progress admin-panel entry that may
    not have the key yet."""
    bonuses: dict[str, int] = {}
    for effect in item.get("effects", []):
        if effect.get("trigger") != "constant":
            continue
        stat = _CONSTANT_EFFECT_STAT.get(effect["type"])
        if stat:
            bonuses[stat] = bonuses.get(stat, 0) + effect["value"]
    return bonuses


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
        if entry["rarity"] not in EQUIPMENT_RARITIES:
            raise ValueError(f"dungeon_equipment.json: item {entry_id!r} has unknown rarity {entry['rarity']!r}")
        if "special" in entry and not isinstance(entry["special"], bool):
            raise ValueError(f"dungeon_equipment.json: item {entry_id!r} special must be a bool")
        if entry["base_value"] < 0:
            raise ValueError(f"dungeon_equipment.json: item {entry_id!r} base_value must be >= 0")
        _validate_equipment_effects(entry["effects"], f"dungeon_equipment.json: item {entry_id!r}")
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
_REQUIRED_MATERIAL_FIELDS = {"id", "name", "rarity", "flavor", "base_value"}


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
        if entry["base_value"] < 0:
            raise ValueError(f"dungeon_materials.json: material {entry_id!r} base_value must be >= 0")
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
_REQUIRED_CONSUMABLE_FIELDS = {"id", "name", "kind", "flavor", "base_value"}


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
        if entry["base_value"] < 0:
            raise ValueError(f"dungeon_consumables.json: item {entry_id!r} base_value must be >= 0")
        _validate_effects_or_groups(entry, f"dungeon_consumables.json: item {entry_id!r}")
        consumables[entry_id] = entry
    return consumables


CONSUMABLES = _load_consumables()

# DELVES is instantiated down here, after MONSTERS/MATERIALS/CONSUMABLES all exist, even though
# _load_delves/_validate_action (the functions) are defined much earlier alongside the rest of the
# "Delve content" section -- a choice room's action costs can reference any of the three, so the
# call has to wait for whichever loads last.
DELVES = _load_delves()


# --- Recipes -------------------------------------------------------------------------------
# Crafting turns materials (+ optional currency) into an item of any output_kind in
# RECIPE_OUTPUT_KINDS -- see crafting.py for the orchestration and db.craft_item for the atomic
# consumption transaction. Loaded last among the registries above since a recipe's output_id is
# cross-validated against whichever registry its output_kind points into.
#
# "quest_item" is a valid output_kind (quest items and horse cosmetics are craftable the same as
# any equipment/consumable -- this is a general capability of the recipe system, not tied to any
# specific quest item or cosmetic actually having a recipe) but this module can't validate a
# quest_item output_id itself: quests.py imports dungeon.py, so the reverse would be circular.
# That check is deferred to quests.validate_recipe_quest_items, run right after both registries
# are loaded (see the bottom of quests.py) and wired as a save-time extra_validator for the
# "recipes" content type (admin_schemas.py). horse_clothes.py has no such problem -- it doesn't
# import dungeon.py -- so "horse_clothes" is validated directly below, same as equipment/consumable.
# "housing_item" has the exact same problem as "quest_item" (housing.py imports this module, so the
# reverse would be circular too) -- deferred to quests.validate_recipe_housing_items instead, called
# separately from bot.py once housing.py has actually loaded (see that function's own docstring for
# why it can't just be folded into validate_recipe_quest_items's single eager call here).
_RECIPES_PATH = os.path.join(os.path.dirname(__file__), "dungeon_recipes.json")
_REQUIRED_RECIPE_FIELDS = {"id", "name", "output_kind", "output_id", "materials"}
RECIPE_OUTPUT_KINDS = ("equipment", "consumable", "horse_clothes", "quest_item", "housing_item")


def _load_recipes(path: str = _RECIPES_PATH) -> dict[str, dict]:
    # output_kind -> the registry its output_id is checked against here, or None if it has to be
    # checked elsewhere (see "quest_item"/"housing_item" note above).
    output_registries = {
        "equipment": EQUIPMENT, "consumable": CONSUMABLES, "horse_clothes": horse_clothes.HORSE_CLOTHES,
        "quest_item": None, "housing_item": None,
    }
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
        registry = output_registries[entry["output_kind"]]
        if registry is not None and entry["output_id"] not in registry:
            raise ValueError(
                f"dungeon_recipes.json: recipe {entry_id!r} output_id {entry['output_id']!r} not found in "
                f"{entry['output_kind'].upper()} registry"
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



def compute_effective_stats(
    character: dict, equipped: dict[str, str], housing_stat_bonuses: dict[str, int] | None = None
) -> dict:
    """A character's stored hp/atk/def/spatk/spdef/speed (which already include all permanent
    level growth) plus whatever's currently equipped in each slot, plus (optionally) a
    {stat: value} dict of passive bonuses from placed housing items -- see
    housing.get_house_bonuses(...).get("stat_bonus", {}), the housing analogue of an equipped
    item's own constant_stat_bonuses. `equipped` is {slot: item_id}, e.g. from
    db.get_equipped_items. `housing_stat_bonuses` defaults to none (today's exact behavior) --
    callers that want housing items to count pass one in."""
    hp, atk, def_ = character["hp"], character["atk"], character["def"]
    spatk, spdef, speed = character["spatk"], character["spdef"], character["speed"]
    for item_id in equipped.values():
        item = EQUIPMENT.get(item_id)
        if item is None:
            continue  # defensive: an item removed from the JSON after being equipped
        bonuses = constant_stat_bonuses(item)
        hp += bonuses.get("hp", 0)
        atk += bonuses.get("atk", 0)
        def_ += bonuses.get("def", 0)
        spatk += bonuses.get("spatk", 0)
        spdef += bonuses.get("spdef", 0)
        speed += bonuses.get("speed", 0)
    if housing_stat_bonuses:
        hp += housing_stat_bonuses.get("hp", 0)
        atk += housing_stat_bonuses.get("atk", 0)
        def_ += housing_stat_bonuses.get("def", 0)
        spatk += housing_stat_bonuses.get("spatk", 0)
        spdef += housing_stat_bonuses.get("spdef", 0)
        speed += housing_stat_bonuses.get("speed", 0)
    return {"hp": hp, "atk": atk, "def": def_, "spatk": spatk, "spdef": spdef, "speed": speed}


# --- Combat ------------------------------------------------------------------------------------
# Deliberately lightweight: one attacker at a time, no persistent status effects beyond what a
# skill's own effects apply for the rest of the fight (see dungeon_view.py's EFFECT_HANDLERS).

DAMAGE_VARIANCE_LOW, DAMAGE_VARIANCE_HIGH = 0.85, 1.15


def roll_damage(atk: int, defense: int, multiplier: float = 1.0) -> int:
    """Damage is attacker's ATK (times an optional ability multiplier) minus defender's DEF,
    with +-15% variance, floored at 1 so a fight can never stall."""
    raw = (atk * multiplier - defense) * random.uniform(DAMAGE_VARIANCE_LOW, DAMAGE_VARIANCE_HIGH)
    return max(1, round(raw))


# Dodge (a Physical attack whiffing entirely) and Resist (the same for Special) aren't their own
# stats -- both are a diminishing-returns function of a stat that already exists everywhere DEF/
# SpDef do (every class, every monster, every equipped item), so there's nothing new to add to
# CLASSES/SUBCLASSES/CONSTANT_EQUIPMENT_EFFECT_TYPES/the DB schema to get this working for players AND monsters
# at once. DODGE_K=100 is deliberately gentle -- a fresh mage's DEF=2 dodges ~2%, a fresh
# fighter's DEF=6 dodges ~6%, and even the toughest current monster (z_goolok, DEF=34) only
# reaches ~25%. DODGE_CAP is a hard safety net independent of K -- no amount of DEF/SpDef
# stacking (leveling, gear, buffs) can ever push a target above a 50% chance to fully avoid an
# attack.
DODGE_K = 100
DODGE_CAP = 0.5


def dodge_chance(defense: int) -> float:
    """Chance to completely avoid an attack, given the defender's own DEF (for a Physical attack)
    or SpDef (Special) -- same formula either way, just fed a different stat by the caller."""
    return min(DODGE_CAP, defense / (defense + DODGE_K))


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


DEFAULT_MONSTER_ATTACK_CHANCE = 1.0


def pick_monster_action(monster: dict) -> dict | None:
    """A monster's turn: weighted-random between its plain attack (weight = its own
    attack_chance, defaulting to DEFAULT_MONSTER_ATTACK_CHANCE) and each entry in its optional
    "skills" list (weight = that skill's own chance). Returns None for a plain attack, or the
    chosen skill dict. No mana/cooldown -- a monster can reuse the same skill as often as it
    randomly comes up (see admin_schemas.py's "skills" field hint on the monsters content type)."""
    skills = monster.get("skills", [])
    if not skills:
        return None
    weights = [monster.get("attack_chance", DEFAULT_MONSTER_ATTACK_CHANCE)] + [s["chance"] for s in skills]
    if sum(weights) <= 0:
        return None
    return random.choices([None, *skills], weights=weights, k=1)[0]


# --- Turn order (Speed) --------------------------------------------------------------------
# Final Fantasy X's actual Conditional Turn-Based (CTB) system, not a real-time ATB gauge --
# nothing fills while a player is deciding their move. Every combatant has a persistent "turn
# clock" (dungeon_view.py's MonsterInstance/DelveSession/PartyMember, reset to 0 at the same point
# Chips/used_item_effects already reset -- fight start); after acting, ONLY that combatant's own
# clock advances, by an amount inversely proportional to their own Speed (faster = smaller
# advance = comes back around sooner). "Whose turn is it" and "what are the next N turns" are the
# exact same computation (preview_next_turns with count=1 vs. count=8-10) -- there is deliberately
# only one function that decides turn order, not a separate "current turn" pointer that could
# drift out of sync with a "preview" list computed some other way.

BASE_TURN_INTERVAL = 1000  # arbitrary scale -- only relative intervals between combatants matter


def turn_interval(speed: int) -> float:
    """How much a combatant's own turn_clock advances after they act -- higher speed means a
    smaller interval, so their clock crosses back below everyone else's sooner and they're picked
    again more often. Always computed fresh from a combatant's CURRENT speed (never cached) --
    see preview_next_turns."""
    return BASE_TURN_INTERVAL / max(1, speed)


def preview_next_turns(combatants: list[dict], count: int) -> list[str]:
    """The next `count` combatant ids to act, in order -- pure and non-mutating (simulates forward
    on a local copy of each combatant's clock, never touches the real state a caller passed in).
    `combatants` is every currently-LIVING combatant as `{"id": ..., "speed": ..., "clock": ...}`
    (already-effective speed, i.e. base minus any speed_debuff -- this function has no opinion on
    where that number came from). Calling with count=1 answers "whose turn is it right now"; the
    same call with a larger count is the card-strip preview -- same function, so the two can never
    disagree with each other. A combatant can appear more than once in the result if they're fast
    enough to act again before someone else's clock catches up -- expected, not a bug (this is
    exactly what "a fast unit gets more turns" looks like). Ties break on higher speed, then stable
    input order -- every fight starts with every combatant's clock at 0 (see
    dungeon_view.py's reset points), so a naive "first in the list wins" tie-break would make the
    very first turn of every fight ignore speed entirely (whoever happened to be built into the
    combatants list first would always go first) -- exactly the "fast monster ambushes a slow
    player" case this system exists to produce, so ties can't be allowed to silently ignore speed."""
    clocks = {c["id"]: c["clock"] for c in combatants}
    speeds = {c["id"]: c["speed"] for c in combatants}
    intervals = {c["id"]: turn_interval(c["speed"]) for c in combatants}
    order = []
    for _ in range(count):
        next_id = min(clocks, key=lambda cid: (clocks[cid], -speeds[cid]))
        order.append(next_id)
        clocks[next_id] += intervals[next_id]
    return order


# --- Party threat (which member a monster attacks) -----------------------------------------
# Party-only: solo has one player, so a monster always attacks them directly, no selection needed.
# Each monster tracks its OWN threat per party member (dungeon_view.MonsterInstance.threat, a
# {user_id: threat} dict) rather than one shared pool, so an arbitrary number of monsters can each
# independently be most annoyed at a different member. Damage dealt to a monster raises the
# attacker's threat against THAT monster (THREAT_PER_DAMAGE, applied in dungeon_view.py right where
# damage lands) -- taunt/lower_threat (EFFECT_PARAM_SCHEMAS below) are how a player counteracts
# that pull, RPG-style.
THREAT_PER_DAMAGE = 1.0  # 1:1 baseline -- the one knob to retune the whole system's balance
THREAT_FLOOR = 0.01  # correctness floor for pick_target_by_threat's weights (random.choices needs
# a positive weight) -- NOT a balance lever, real damage/taunt values dwarf it at any level range.


def pick_target_by_threat(candidates: list[dict]) -> int:
    """Weighted-random pick among `candidates` (`[{"id": ..., "threat": ...}]`, one entry per
    living party member) by relative threat against ONE specific monster -- mirrors
    preview_next_turns' own "dungeon_view builds plain dicts, dungeon.py does the pure math" split.
    Read fresh every time a monster's turn comes up (never cached), so a taunt/lower_threat used
    mid-fight changes who that monster goes after starting on its very next turn."""
    weights = [max(THREAT_FLOOR, c["threat"]) for c in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]["id"]


# --- Monster/equipment stat generation & level estimation ------------------------------------
# RECONSTRUCTION NOTE: this whole section (MONSTER_ARCHETYPES, generate_monster_stats,
# generate_item_constant_effects, estimate_monster_level, estimate_item_level, estimate_group_level)
# was rebuilt from scratch after this file was accidentally truncated -- the original formulas
# were not recovered, only their call sites/contracts (admin_server.py's "Generate stats for
# level" tooling and the delve room editor's level-range display). Confirmed with the author:
# tank/balanced/glass_cannon is the right archetype set but the exact weightings weren't
# remembered, and the item stat-bonus formula was always just "generated to be balanced" rather
# than tied to any specific original numbers -- so both are a fresh reasonable pass, not a
# recovery of prior tuning. Both estimate_* functions are the algebraic inverse of their
# matching generate_* function, so a freshly generated entry round-trips back to the same level.

# `speed` weights are multiplicative (against _monster_speed_budget, itself a plain scalar, not
# something split across a pair like spatk/spdef are) rather than the 0-1 fractional split the
# other stats use -- 1.0 is "the balanced archetype's own baseline," tank sits below it (heavy,
# slow) and glass_cannon above it (a striker archetype), same "roughly half above/below center"
# spread the other archetype columns already use.
MONSTER_ARCHETYPES = {
    "tank": {"hp": 0.55, "atk": 0.20, "def": 0.25, "spatk": 0.3, "spdef": 0.7, "speed": 0.7},
    "balanced": {"hp": 0.45, "atk": 0.30, "def": 0.25, "spatk": 0.5, "spdef": 0.5, "speed": 1.0},
    "glass_cannon": {"hp": 0.30, "atk": 0.55, "def": 0.15, "spatk": 0.7, "spdef": 0.3, "speed": 1.3},
}


def _monster_power_budget(level: int) -> float:
    """hp + 2*atk + 2*def, the single "power" scalar generate_monster_stats/estimate_monster_level
    both key off -- weighted 2x on atk/def since one point of either moves roll_damage's output by
    roughly twice what one point of HP is worth (ATK and DEF trade off the same subtraction; HP is
    just what's left standing after it)."""
    return 35 + 8 * level


def _monster_special_budget(level: int) -> float:
    """spatk/spdef's own scalar, deliberately independent of _monster_power_budget rather than
    folded into it -- every one of the 16 real monsters was backfilled with spatk=atk/spdef=def
    (see dungeon_monsters.json), so combining the budgets would silently double
    estimate_monster_level's "≈ balanced for level X" hint for every one of them the moment this
    shipped, even though their actual hp/atk/def/intended_level never changed. Kept at a similar
    scale to _monster_power_budget on purpose (no /2 split needed -- spatk/spdef aren't
    2x-weighted the way atk/def are here, there's no existing convention this needs to match)."""
    return 15 + 4 * level


def _monster_speed_budget(level: int) -> float:
    """speed's own scalar, independent of the other two budgets for the same reason
    _monster_special_budget is -- keeps generate_monster_stats' speed output in roughly the same
    numeric range as a player build's own speed (CLASSES/SUBCLASSES, currently 6-16) rather than
    scaling off hp/atk/def's much larger numbers."""
    return 10 + 0.4 * level


def generate_monster_stats(level: int, archetype: dict | None = None) -> dict:
    """hp/atk/def/spatk/spdef/speed for a monster meant to feel "right" at `level`, split by
    `archetype`'s weights (defaults to balanced -- see MONSTER_ARCHETYPES). Doesn't touch
    intended_level itself; callers (admin_server.py's _apply_generate_level) set that alongside
    this."""
    archetype = archetype or MONSTER_ARCHETYPES["balanced"]
    budget = _monster_power_budget(level)
    special_budget = _monster_special_budget(level)
    speed_budget = _monster_speed_budget(level)
    return {
        "hp": max(1, round(budget * archetype["hp"])),
        "atk": max(1, round(budget * archetype["atk"] / 2)),
        "def": max(1, round(budget * archetype["def"] / 2)),
        "spatk": max(1, round(special_budget * archetype["spatk"])),
        "spdef": max(1, round(special_budget * archetype["spdef"])),
        "spd": max(1, round(speed_budget * archetype["speed"])),
    }


def estimate_monster_level(monster: dict) -> float:
    """Inverse of generate_monster_stats/_monster_power_budget -- a monster's hp/atk/def read back
    as "the level this would have been generated for", regardless of archetype (the split cancels
    out since power_budget sums all three back into one scalar). Deliberately excludes spatk/spdef
    AND speed from this formula, same reason _monster_special_budget's docstring already gives for
    spatk/spdef -- every real monster has speed backfilled as a function of atk/def (see
    dungeon_monsters.json), so folding it in here would double-count and inflate this estimate for
    every one of them despite hp/atk/def/intended_level never having changed. Used as
    intended_level's fallback wherever a monster predates that field, or has it unset."""
    budget = monster["hp"] + 2 * monster["atk"] + 2 * monster["def"]
    return max(1.0, (budget - 35) / 8)


def estimate_group_level(monsters: list[dict]) -> float:
    """A combat room's overall difficulty across however many monsters spawn together in one
    group -- sums each monster's own power budget rather than averaging their levels (a group of
    three shouldn't read as "easy" just because each one is individually low-level) before
    converting back to a level-equivalent number. Returns 0 for an empty group -- callers (the
    delve flowchart editor's room summary) filter that out of a displayed range rather than
    treating it as a real level 0."""
    if not monsters:
        return 0.0
    total_budget = sum(m["hp"] + 2 * m["atk"] + 2 * m["def"] for m in monsters)
    return max(1.0, (total_budget - 35) / 8)


_EQUIPMENT_SLOT_WEIGHTS = {
    # weapon stays physical-only for spatk (no sword/staff sub-type to hang a magic lean off of),
    # but picks up a small speed share -- a "quick blade" trope -- shaved off atk to keep summing
    # to 1.0.
    "weapon": {"hp": 0.0, "atk": 0.7, "def": 0.2, "spatk": 0.0, "spdef": 0.0, "speed": 0.1},
    # armor picks up a modest spdef share (magic-resistant armor is a normal RPG trope) and a
    # small speed share (light/agile armor), shaved off hp/def evenly to keep summing to 1.0.
    "armor": {"hp": 0.35, "atk": 0.0, "def": 0.35, "spatk": 0.0, "spdef": 0.2, "speed": 0.1},
    # trinkets are already omni-stat in real content -- full even split across all six now.
    "trinket": {"hp": 0.17, "atk": 0.17, "def": 0.17, "spatk": 0.17, "spdef": 0.17, "speed": 0.15},
}


def _item_power_budget(level: int) -> float:
    """hp + 2*atk + 2*def + 2*spatk + 2*spdef + 2*speed power scalar, scaled way down from
    _monster_power_budget -- a piece of gear is one small increment on top of a whole character's
    own stats, not a whole combatant's total. Unlike the monster case, spatk/spdef/speed fold into
    this SAME budget rather than getting an independent one -- necessary, not just simpler: no
    equipment gets backfilled with spatk/spdef/speed (existing gear just omits the keys, worth 0
    here), so a stat total summed over whatever's in constant_stat_bonuses stays correct for old
    gear, and a fresh item at a given level isn't handed extra "free" power an old item of the
    same level never had a chance to also carry."""
    return 1.5 * level


# type -> stat this budget-weight key feeds into an effect for -- the inverse of dungeon.py's own
# _CONSTANT_EFFECT_STAT mapping near constant_stat_bonuses.
_STAT_TO_CONSTANT_EFFECT_TYPE = {
    "hp": "hp_buff", "atk": "atk_buff", "def": "def_buff", "spatk": "spatk_buff", "spdef": "spdef_buff",
    "speed": "speed_buff",
}


def generate_item_constant_effects(level: int, slot: str, rarity: str = "common") -> list[dict]:
    """Constant-trigger effects for a piece of gear meant to feel "right" at `level` in `slot` and
    `rarity` -- weapon leans almost entirely ATK, armor splits HP/DEF/SpDef, trinket is even across
    all six (see _EQUIPMENT_SLOT_WEIGHTS), then the whole budget is scaled by
    RARITY_STAT_MULTIPLIERS so a higher tier is authored to meaningfully outclass a lower one at
    the same level. A weight of exactly 0 omits that stat entirely rather than writing a 0-value
    effect, matching how real equipment entries only list the stats they actually touch. Renamed
    from the pre-effects-system generate_item_stat_bonuses now that the return shape is an effects
    list, not a flat stat_bonuses dict -- stays scoped to constant-only, on_use/on_hit effects are
    always hand-authored, never auto-rolled."""
    weights = _EQUIPMENT_SLOT_WEIGHTS.get(slot, _EQUIPMENT_SLOT_WEIGHTS["trinket"])
    budget = _item_power_budget(level) * RARITY_STAT_MULTIPLIERS.get(rarity, 1.0)
    divisors = {"hp": 1, "atk": 2, "def": 2, "spatk": 2, "spdef": 2, "speed": 2}
    effects = []
    for stat, weight in weights.items():
        if weight:
            value = max(1, round(budget * weight / divisors[stat]))
            effects.append({"type": _STAT_TO_CONSTANT_EFFECT_TYPE[stat], "trigger": "constant", "value": value})
    return effects


def estimate_item_level(item: dict) -> float:
    """Inverse of generate_item_constant_effects/_item_power_budget -- divides back out the same
    rarity multiplier generation scaled up by, so a legendary item's stats read as "balanced for
    level X" against the actual level it was generated for, not an inflated one just because
    rarity made its raw numbers bigger."""
    bonuses = constant_stat_bonuses(item)
    budget = (
        bonuses.get("hp", 0) + 2 * bonuses.get("atk", 0) + 2 * bonuses.get("def", 0)
        + 2 * bonuses.get("spatk", 0) + 2 * bonuses.get("spdef", 0) + 2 * bonuses.get("speed", 0)
    )
    rarity_mult = RARITY_STAT_MULTIPLIERS.get(item.get("rarity"), 1.0)
    return max(1.0, budget / 1.5 / rarity_mult)


# --- Delve drafts ----------------------------------------------------------------------------
# RECONSTRUCTION NOTE: rebuilt from scratch after this file was accidentally truncated, from
# admin_server.py's call sites/docstrings (delve_autosave_view, delve_publish_view, edit_view,
# list_view) -- those describe the contract in detail (a draft is a full delve-entry dict, keyed
# by id, autosaved with no validation gate; Publish is the only place a broken delve is ever
# rejected outright) even though the original implementation here wasn't recovered. The actual
# draft data (dungeon_delve_drafts.json) was never touched and is untouched by this rebuild.

_DELVE_DRAFTS_PATH = os.path.join(os.path.dirname(__file__), "dungeon_delve_drafts.json")


def load_delve_drafts() -> dict[str, dict]:
    if not os.path.exists(_DELVE_DRAFTS_PATH):
        return {}
    with open(_DELVE_DRAFTS_PATH) as f:
        return json.load(f)


def _write_delve_drafts(drafts: dict[str, dict]) -> None:
    with open(_DELVE_DRAFTS_PATH, "w") as f:
        json.dump(drafts, f, indent=2)


def save_delve_draft(entry: dict) -> None:
    drafts = load_delve_drafts()
    drafts[entry["id"]] = entry
    _write_delve_drafts(drafts)


def delete_delve_draft(item_id: str) -> None:
    """No-op if item_id isn't actually a draft -- callers (admin_server.py's delve_autosave_view/
    delve_publish_view) call this speculatively during id-rename/publish cleanup without checking
    existence first."""
    drafts = load_delve_drafts()
    if item_id in drafts:
        del drafts[item_id]
        _write_delve_drafts(drafts)


def check_delve_problems(entry: dict, other_ids: set[str]) -> list[dict]:
    """Non-raising sibling of _load_delves' per-entry validation -- same rules, but collects every
    problem found (instead of raising on the first) as {"room_id", "action_index", "message"}
    dicts so the flowchart editor can highlight every broken room/action at once rather than
    forcing one-fix-at-a-time. room_id/action_index are None for a delve-level problem (e.g. a
    duplicate id against `other_ids` -- the currently-published delves this entry isn't itself).
    Used for a draft that's allowed to be in an arbitrarily broken mid-edit state, so unlike
    _load_delves this never raises -- an empty return means "would pass Publish"."""
    problems: list[dict] = []

    def add(message: str, room_id: str | None = None, action_index: int | None = None):
        problems.append({"room_id": room_id, "action_index": action_index, "message": message})

    entry_id = entry.get("id", "")
    if entry_id and entry_id in other_ids:
        add(f"id {entry_id!r} is already used by another delve")
    missing = _REQUIRED_DELVE_FIELDS - entry.keys()
    if missing:
        add(f"missing field(s): {sorted(missing)}")

    rooms = entry.get("rooms") or []
    if not rooms:
        add("has no rooms")
        return problems

    room_ids: set[str] = set()
    for room in rooms:
        room_id = room.get("id")
        if not room_id:
            add("a room has no id")
            continue
        if room_id in room_ids:
            add(f"duplicate room id {room_id!r}", room_id=room_id)
        room_ids.add(room_id)
        room_type = room.get("type")
        if room_type not in ROOM_TYPES:
            add(f"unknown room type {room_type!r}", room_id=room_id)
            continue
        required = _REQUIRED_ROOM_FIELDS_BY_TYPE[room_type]
        missing_room = required - room.keys()
        if missing_room:
            add(f"missing field(s): {sorted(missing_room)}", room_id=room_id)
        if room_type == "combat":
            for group in room.get("monster_groups") or []:
                for monster_id in (group.get("monsters") or []) if isinstance(group, dict) else []:
                    if monster_id not in MONSTERS:
                        add(f"references unknown monster {monster_id!r}", room_id=room_id)
        else:
            if not room.get("prompt"):
                add("choice room has no prompt", room_id=room_id)
            actions = room.get("actions") or []
            if not actions:
                add("choice room has no actions", room_id=room_id)
            for i, action in enumerate(actions):
                try:
                    _validate_action(action, "action")
                except ValueError as e:
                    add(str(e), room_id=room_id, action_index=i)

    start_room = entry.get("start_room")
    if start_room and start_room not in room_ids:
        add(f"start_room {start_room!r} is not a room here")

    for room in rooms:
        room_id = room.get("id")
        if not room_id or room.get("type") != "combat":
            continue
        next_room = room.get("next")
        if next_room is not None and next_room not in room_ids:
            add(f"next {next_room!r} is not a room here", room_id=room_id)

    return problems
