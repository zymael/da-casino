"""Horse necromancy -- pure rules, no Discord (see necro_view.py for UI, bot.py's !glue for the
command). Phase 1 is the Nature Sanctuary and Horse Graveyard (publicly a memorial near The Front; really
the glue factory): a player "lays to rest" -- renders -- a horse they own into Ectoglue, the
resource later phases spend on war spells (and on raising the glued dead to fight again).

Gluing is permanent, for legends too: a glued horse keeps its row in `horses` (glued_at set, owner
NULL -- see db.glue_horse for why the row is never deleted), so a glued legend is gone from the
whole guild for good, and the row doubles as the graveyard later phases raise ghosts from.

The whole feature is locked behind the sanctuary room's "unlock_trigger" (rooms.json, see
rooms.py's docstring) -- the exit into it is hidden and !glue refuses until that trigger holds. It
ships as flag_at_least on UNLOCK_FLAG, which nobody has (an owner can grant it with !necrounlock
for testing); unlocking via a quest later is just swapping that trigger for a quest_complete in
the admin panel. Every necromancy command checks quests.room_unlocked(ROOM_ID) the same way.

Ectoglue is a set of graded quest items (quest_items.json, see GRADES) so it shows in !inventory
like everything else; GRADES is the one set of ids this code depends on, checked below at import
and again on every admin-panel quest-item save.
"""

import math

import quests

ROOM_ID = "horse_sanctuary"
UNLOCK_FLAG = "necro:unlocked"  # the placeholder gate rooms.json's horse_sanctuary unlock_trigger checks

# Ectoglue grades, worst to best -- each its own quest item, since the inventory table is just
# item_id -> qty with no per-unit quality. Strong horses make *more* glue; old horses make *better*
# glue. A grade is picked by age (GRADE_MIN_AGES[i] is the youngest age that renders grade i), then
# bumped one step for a legend and one for an irradiated horse, capped at the best grade. Later
# phases scale a spell's strength by the grade it's paid with.
GRADES = ("faint_ectoglue", "ectoglue", "potent_ectoglue", "ancient_ectoglue")
GRADE_MIN_AGES = (0, 10, 20, 40)  # placeholders -- check real ages in the live db before settling

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
