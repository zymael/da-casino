"""Horse necromancy -- pure rules, no Discord (see necro_view.py for UI, bot.py's !glue for the
command). Phase 1 is the Nature Sanctuary and Horse Graveyard (publicly a memorial near The Front; really
the glue factory): a player "lays to rest" -- renders -- a horse they own into Ectoglue, the
resource later phases spend on war spells (and on raising the glued dead to fight again).

Gluing is permanent, for legends too: a glued horse keeps its row in `horses` (glued_at set, owner
NULL -- see db.glue_horse for why the row is never deleted), so a glued legend is gone from the
whole guild for good, and the row doubles as the graveyard later phases raise ghosts from.

The Sanctuary's front room (horse_sanctuary) is open to everyone: a Guest Area with the public
graveyard. Gluing and spells live in ROOM_ID, the Back Rooms, locked by that room's "unlock_trigger"
(rooms.json, see rooms.py's docstring) -- holding a Slimy Bathrobe (a quest item the Bathhouse's
cloaked goo cultists drop), the disguise that gets you past the rope. Without it the exit back is
hidden and !glue/!cast refuse; every Back Rooms command checks quests.room_unlocked(ROOM_ID). The
gate is entirely that trigger, so changing who gets in is an admin-panel edit, not code.

Ectoglue is a set of graded quest items (quest_items.json, see GRADES) so it shows in !inventory
like everything else; GRADES is the one set of ids this code depends on, checked below at import
and again on every admin-panel quest-item save.
"""

import json
import math
import os
from typing import NamedTuple

import quests
import war

ROOM_ID = "sanctuary_back_rooms"  # where gluing and spells happen -- its unlock_trigger is the gate

# Ectoglue grades, worst to best -- each its own quest item, since the inventory table is just
# item_id -> qty with no per-unit quality. Strong horses make *more* glue; old horses make *better*
# glue. A grade is picked by age (GRADE_MIN_AGES[i] is the youngest age that renders grade i), then
# bumped one step for a legend and one for an irradiated horse, capped at the best grade. Later
# phases scale a spell's strength by the grade it's paid with.
GRADES = ("faint_ectoglue", "ectoglue", "potent_ectoglue", "ancient_ectoglue")
GRADE_MIN_AGES = (0, 10, 20, 40)  # placeholders -- check real ages in the live db before settling

# !glue's send-off, escalating with what came out: (smokestack line, reaction after the receipt),
# keyed by grade item id -- None is a horse that gave nothing (its reaction is the "bad person"
# ending, written in bot.py since it names the horse and player).
SEND_OFF = {
    None: ("The smokestacks don't even bother.", None),
    "faint_ectoglue": (
        "A thin, apologetic wisp drifts out of one smokestack.",
        "It's barely there. The caretaker hands it over without making eye contact.",
    ),
    "ectoglue": ("The smokestacks cough a fresh plume of green fog.", "It's still warm."),
    "potent_ectoglue": (
        "Thick green fog pours out of the smokestacks and rolls downhill toward the Ranch.",
        "The jar hums in your hands.",
    ),
    "ancient_ectoglue": (
        "The smokestacks go silent. Then every light in Greener Pastures flickers, and the fog comes out nearly black.",
        "The jar is cold, and every so often, it whinnies.",
    ),
}
if set(SEND_OFF) != {None, *GRADES}:
    raise ValueError("necromancy.SEND_OFF must have exactly one entry per grade, plus None")

# Quantity: (total stats - STAT_FLOOR) / STATS_PER_GLUE, plus wins / WINS_PER_GLUE, times an age
# factor that's 0 through YOUNG_AGE, ramps linearly to 1.0 at FULL_AGE, and keeps climbing to
# AGE_FACTOR_CAP. Rounded down. A fresh foal (105 total, age 0) and a weakling (<= STAT_FLOOR) both
# render into nothing at all -- !glue lets them, then calls them a bad person for it.
STAT_FLOOR = 130
STATS_PER_GLUE = 10
WINS_PER_GLUE = 3
YOUNG_AGE = 4
FULL_AGE = 12
AGE_FACTOR_CAP = 1.5


def _age_factor(age: int) -> float:
    return min(AGE_FACTOR_CAP, max(0.0, (age - YOUNG_AGE) / (FULL_AGE - YOUNG_AGE)))


def ectoglue_yield(horse: dict, is_legend: bool, irradiated: bool) -> tuple[str | None, int]:
    """(grade item id, quantity) rendering this horse is worth -- (None, 0) for a horse too young
    or too weak to give anything. `horse` needs speed/endurance/spirit/wins/age (db.get_ranch_horses'
    dicts have them)."""
    total_stats = horse["speed"] + horse["endurance"] + horse["spirit"]
    if total_stats <= STAT_FLOOR:
        return None, 0
    amount = math.floor(
        ((total_stats - STAT_FLOOR) / STATS_PER_GLUE + horse["wins"] / WINS_PER_GLUE) * _age_factor(horse["age"])
    )
    if amount <= 0:
        return None, 0
    grade = max(i for i, min_age in enumerate(GRADE_MIN_AGES) if horse["age"] >= min_age)
    grade = min(len(GRADES) - 1, grade + int(is_legend) + int(irradiated))
    return GRADES[grade], amount


def validate_ectoglue_items(quest_items: dict[str, dict] | None = None):
    """Every grade's quest item must exist -- !glue grants them by id, and !inventory refuses to
    render an id it doesn't recognize. Run at import and as a quest_items save-time
    extra_validator (admin_schemas.py) so none can be deleted or renamed out from under the code."""
    registry = quests.QUEST_ITEMS if quest_items is None else quest_items
    missing = [item_id for item_id in GRADES if item_id not in registry]
    if missing:
        raise ValueError(f"quest_items.json: necromancy needs quest item(s) {missing}")


validate_ectoglue_items()


# ---------------------------------------------------------------------------------------------
# Phase 2: spells. Content is necromancy_spells.json (admin panel: "Necromancy Spells"), each entry
# {id, name, emoji, description, cost, cooldown_hours} plus any of war.SPELL_EFFECTS as optional
# integers. !cast spends `cost` ectoglue of one grade the caster picks and *prepares* the spell; the
# caster's next war -- attacking or defending -- applies every prepared spell and spends them all.
# The grade scales every effect by GRADE_POWER. All state is flags on the caster (keys below), so
# this needed no new table.
# ---------------------------------------------------------------------------------------------

_SPELLS_PATH = os.path.join(os.path.dirname(__file__), "necromancy_spells.json")
_REQUIRED_SPELL_FIELDS = {"id", "name", "emoji", "description", "cost", "cooldown_hours"}

GRADE_POWER = (0.75, 1.0, 1.25, 1.5)  # per GRADES index
PREPARED_SECONDS = 48 * 3600  # a prepared spell fizzles if no war comes within this long
MAX_PREPARED = 2  # spells a player can hold ready at once (so at most 2 ghosts per side, of 5 slots)
# Optional on a spell: raise one glued horse (anyone's -- the caster picks from the whole guild's
# graveyard) to fight in the caster's next war at this % of its final stats, times GRADE_POWER,
# capped at war.GHOST_STAT_CAP_PCT. A ghost takes one of the army's MAX_ARMY_SIZE slots.
SUMMON_FIELD = "summon_ghost_pct"


def _load_spells(path: str = _SPELLS_PATH) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    spells: dict[str, dict] = {}
    for entry in raw:
        spell_id = entry.get("id", "?")
        missing = _REQUIRED_SPELL_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"necromancy_spells.json: spell {spell_id!r} missing field(s): {sorted(missing)}")
        if spell_id in spells:
            raise ValueError(f"necromancy_spells.json: duplicate spell id {spell_id!r}")
        if not isinstance(entry["cost"], int) or entry["cost"] < 1:
            raise ValueError(f"necromancy_spells.json: spell {spell_id!r} cost must be an integer >= 1")
        if not isinstance(entry["cooldown_hours"], int) or entry["cooldown_hours"] < 0:
            raise ValueError(f"necromancy_spells.json: spell {spell_id!r} cooldown_hours must be an integer >= 0")
        effects = {k: entry[k] for k in (*war.SPELL_EFFECTS, SUMMON_FIELD) if entry.get(k) is not None}
        for key, value in effects.items():
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"necromancy_spells.json: spell {spell_id!r} {key} must be an integer >= 0")
        if not any(effects.values()):
            raise ValueError(
                f"necromancy_spells.json: spell {spell_id!r} has no effect -- set at least one of "
                f"{[*war.SPELL_EFFECTS, SUMMON_FIELD]}"
            )
        spells[spell_id] = entry
    return spells


SPELLS = _load_spells()


def is_summon(spell: dict) -> bool:
    return bool(spell.get(SUMMON_FIELD))


def ghost_stat_pct(spell: dict, grade: int) -> float:
    return min(war.GHOST_STAT_CAP_PCT, spell[SUMMON_FIELD] * GRADE_POWER[grade])


def prepared_flag(spell_id: str) -> str:
    """On the caster's row: unix time this prepared spell fizzles at (0 = not prepared)."""
    return f"necro:prepared:{spell_id}"


def prepared_grade_flag(spell_id: str) -> str:
    """On the caster's row: GRADES index + 1 the prepared spell was cast with."""
    return f"necro:prepared:{spell_id}:grade"


def prepared_ghost_flag(spell_id: str) -> str:
    """On the caster's row: horse_index + 1 of the ghost a prepared summon spell will raise."""
    return f"necro:prepared:{spell_id}:ghost"


def bound_flag(horse_index: int) -> str:
    """At GUILD_FLAG_USER: unix time a glued horse's ghost stays bound to whoever's summon spell
    holds it (0 = free). Keeps two players from raising the same ghost at once; cleared by the war
    that spends the spell, or it just runs out with the spell (PREPARED_SECONDS)."""
    return f"necro:bound:{horse_index}"


def cast_last_flag(spell_id: str) -> str:
    """On the caster's row: unix time this spell was last cast, for its cooldown."""
    return f"necro:cast:{spell_id}:last"


# Praying (!pray, open to everyone in the Guest Area): once per PRAY_COOLDOWN_SECONDS per player, at
# any grave. Each prayer adds to that grave's public count and puts it at peace for PEACE_SECONDS --
# no summon spell can pick it meanwhile, the counterplay to "anyone can raise anyone's dead". A ghost
# already bound when someone prays stays bound (its caster already paid); the peace covers after.
PRAY_COOLDOWN_SECONDS = 24 * 3600
PEACE_SECONDS = 24 * 3600
PRAY_LAST_FLAG = "necro:pray:last"  # on the praying player's row: unix time of their last prayer


def prayers_flag(horse_index: int) -> str:
    """At GUILD_FLAG_USER: how many prayers this grave has ever received."""
    return f"necro:prayers:{horse_index}"


def peace_flag(horse_index: int) -> str:
    """At GUILD_FLAG_USER: unix time this grave stays at peace (unraisable) until."""
    return f"necro:peace:{horse_index}"


# Hauntings: a glued horse haunts whoever glued it (db.glued_from_flag). Purely cosmetic, the same
# kind of running gag as roulette_view's Sickly Victorian Daughters, but for anyone with blood on
# their hooves: after a casino game, each haunted player who came out ahead rolls HAUNT_WIN_CHANCE
# for one of their ghosts to show up and make it weird (necro_view.maybe_haunt_winners). A grave
# someone has prayed at (peace_flag) rests and doesn't haunt anyone.
HAUNT_WIN_CHANCE = 0.05
HAUNT_IMAGE_PATH = "assets/ghost_horse.png"
# ...and in the /play rooms: each room a haunted player opens has HAUNT_ROOM_CHANCE of one of their
# ghosts turning up faintly in the banner, ghost-photo style (npc_render._composite_ghost), with
# HAUNT_ROOM_LINE under it.
HAUNT_ROOM_CHANCE = 0.10
HAUNT_OVERLAY_PATH = "assets/ghost_horse_overlay.png"
HAUNT_ROOM_LINE = "👻 {name} is standing behind you."
HAUNT_WIN_LINES = (
    "The ghost of **{name}** drifts up to the table in a bedsheet and asks, very quietly, whether "
    "these winnings are worth more than it was.",
    "The ghost of **{name}** has been standing behind your chair this whole time. It watched every "
    "bet. It doesn't say anything. It doesn't have to.",
    "The ghost of **{name}** taps you on the shoulder to say congratulations. It would clap, but it "
    "doesn't have hands. It never did.",
)


def prepared_keys() -> list[str]:
    """Every flag key a war needs to read to find a player's prepared spells."""
    return [
        key for spell_id in SPELLS
        for key in (prepared_flag(spell_id), prepared_grade_flag(spell_id), prepared_ghost_flag(spell_id))
    ]


class Prepared(NamedTuple):
    spell: dict
    grade: int  # GRADES index
    ghost_index: int | None  # the glued horse a summon spell will raise, else None


def prepared_spells(flags: dict[str, int], now: int) -> list[Prepared]:
    """Every spell still prepared in `flags` (a db.get_flags result over prepared_keys()). A spell
    deleted from the JSON while prepared just never shows up."""
    ready = []
    for spell_id, spell in SPELLS.items():
        if flags.get(prepared_flag(spell_id), 0) > now:
            grade = min(len(GRADES) - 1, max(0, flags.get(prepared_grade_flag(spell_id), 1) - 1))
            ghost = flags.get(prepared_ghost_flag(spell_id), 0)
            ready.append(Prepared(spell, grade, ghost - 1 if ghost and is_summon(spell) else None))
    return ready


def spell_effects(spell: dict, grade: int) -> dict[str, float]:
    """One spell's war effects (not its summon), scaled by the grade of ectoglue it was cast with."""
    return {k: spell[k] * GRADE_POWER[grade] for k in war.SPELL_EFFECTS if spell.get(k)}


def combined_effects(prepared: list[Prepared]) -> dict[str, float]:
    """Every prepared spell's scaled effects summed -- war.apply_spell_effects caps the totals."""
    total: dict[str, float] = {}
    for p in prepared:
        for key, value in spell_effects(p.spell, p.grade).items():
            total[key] = total.get(key, 0.0) + value
    return total


_EFFECT_TEXT = {
    "charge_pct": "+{:g}% charge",
    "hp_pct": "+{:g}% HP",
    "spirit": "+{:g} Spirit",
    "enemy_charge_pct": "enemy −{:g}% charge",
    "enemy_spirit": "enemy −{:g} Spirit",
}


def describe_spell(spell: dict, grade: int) -> str:
    """One-line summary of what this spell does when cast with `grade`."""
    parts = [_EFFECT_TEXT[k].format(round(v, 1)) for k, v in spell_effects(spell, grade).items()]
    if is_summon(spell):
        parts.insert(0, f"raises a ghost at {ghost_stat_pct(spell, grade):g}% of its old stats")
    return ", ".join(parts)
