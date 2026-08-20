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
# is a JSON edit, not a code change. Found as dungeon loot (roll_equipment_drop), never bought.

_EQUIPMENT_PATH = os.path.join(os.path.dirname(__file__), "dungeon_equipment.json")
_REQUIRED_EQUIPMENT_FIELDS = {
    "id", "name", "slot", "tier", "rarity", "drop_weight", "stat_bonuses", "flavor",
}
EQUIPMENT_SLOTS = ("weapon", "armor", "trinket")
_STAT_BONUS_KEYS = {"hp", "atk", "def"}

EQUIPMENT_DROP_CHANCE = 0.25  # per room win, independent of whether currency loot also lands


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
        if entry["tier"] < 1 or entry["drop_weight"] <= 0:
            raise ValueError(f"dungeon_equipment.json: item {entry_id!r} has invalid tier/drop_weight")
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


def roll_equipment_drop(room_index: int) -> dict | None:
    """None most of the time (EQUIPMENT_DROP_CHANCE). When it hits, picks one item from this
    room's tier, weighted by drop_weight so rarer/stronger items are less likely. quest_only
    items (granted exclusively through a quest turn-in, e.g. Mondor's Greasy Pencil) are never
    candidates here."""
    if random.random() > EQUIPMENT_DROP_CHANCE:
        return None
    tier = room_index + 1
    candidates = [item for item in EQUIPMENT.values() if item["tier"] == tier and not item.get("quest_only")]
    if not candidates:
        return None
    weights = [item["drop_weight"] for item in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]


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
