"""Dungeon RPG: pure logic and content (no discord import), mirroring horserace.py's split
between game logic here and Discord UI in dungeon_view.py.

Characters are a permanent one-time choice: a main class (face rank) x a subclass (suit) = 16
builds. Combat is deliberately lightweight -- HP/ATK/DEF only, no status effects, one signature
ability per class usable once per fight. Monster content lives in dungeon_monsters.json (not
here) specifically so new monsters can be added without touching this file -- see MONSTERS below.
"""

import json
import os
import random

# Base HP/ATK/DEF per class, before subclass modifiers. Kept to three stats total rather than a
# full JRPG sheet. Archetypes: Fighter tanks (high HP/DEF, modest ATK), Mage nukes (high ATK,
# fragile), Rogue is balanced/quick, Healer leans on HP + its Heal ability to outlast fights.
# Healer's ATK was originally 4, which combined with tougher monsters' DEF made roll_damage floor
# at 1 almost every hit -- an unwinnable slog regardless of how tanky Healer otherwise is. Bumped
# to 6 so Healer can still meaningfully damage things; simulated combat confirms this fixed it
# without needing to touch any other class. `ability` is the signature move each class gets,
# usable once per fight -- see FIREBALL_MULTIPLIER etc below for what each one actually does.
CLASSES = {
    "fighter": {"rank": "A", "hp": 32, "atk": 6, "def": 6, "ability": "Guard"},
    "healer": {"rank": "K", "hp": 26, "atk": 6, "def": 5, "ability": "Heal"},
    "mage": {"rank": "Q", "hp": 16, "atk": 10, "def": 2, "ability": "Fireball"},
    "rogue": {"rank": "J", "hp": 22, "atk": 7, "def": 3, "ability": "Sneak Attack"},
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

_MONSTERS_PATH = os.path.join(os.path.dirname(__file__), "dungeon_monsters.json")
_REQUIRED_MONSTER_FIELDS = {
    "id", "name", "tier", "hp", "atk", "def", "shape", "color", "flavor", "loot_min", "loot_max",
}


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
        monsters[entry_id] = entry
    return monsters


MONSTERS = _load_monsters()

ROOM_COUNT = 3


def monster_for_room(room_index: int) -> dict:
    """room_index is 0-based (0, 1, 2 for a 3-room dungeon); tier is 1-based to match the JSON
    content, so room N faces a monster of tier N+1."""
    tier = room_index + 1
    candidates = [m for m in MONSTERS.values() if m["tier"] == tier]
    if not candidates:
        raise ValueError(f"No monsters defined for tier {tier} in dungeon_monsters.json")
    return random.choice(candidates)


# --- Combat ------------------------------------------------------------------------------------
# Deliberately lightweight: one attacker at a time, no status effects. Signature abilities are
# one-time modifiers to the same roll_damage/heal calls rather than a separate resolution system.

DAMAGE_VARIANCE_LOW, DAMAGE_VARIANCE_HIGH = 0.85, 1.15

FIREBALL_MULTIPLIER = 1.8      # Mage: one big hit instead of a normal attack
SNEAK_ATTACK_MULTIPLIER = 1.5  # Rogue: bonus damage on one attack
GUARD_DAMAGE_REDUCTION = 0.5   # Fighter: halves the monster's next hit this exchange
HEAL_FRACTION = 0.4            # Healer: restores this fraction of max HP instead of attacking


def roll_damage(atk: int, defense: int, multiplier: float = 1.0) -> int:
    """Damage is attacker's ATK (times an optional ability multiplier) minus defender's DEF,
    with +-15% variance, floored at 1 so a fight can never stall."""
    raw = (atk * multiplier - defense) * random.uniform(DAMAGE_VARIANCE_LOW, DAMAGE_VARIANCE_HIGH)
    return max(1, round(raw))


def roll_loot(monster: dict, loot_mult: float = 1.0) -> int:
    return round(random.randint(monster["loot_min"], monster["loot_max"]) * loot_mult)
