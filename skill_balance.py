"""Pure analysis/simulation helpers powering the admin panel's Skill Balance page
(admin_server.py's skill_balance_view) -- same "engine here, Discord view elsewhere" split
horserace.py has from horserace_view.py. Built on top of dungeon.py's real production primitives
(compute_stats, roll_damage, resolve_cast_effects, dodge_chance) rather than reimplementing combat
math -- only the surrounding turn/rotation loop is new, since dungeon_view.py's own combat
resolution is too tightly coupled to Discord interactions/embeds to call directly (see that
module's EFFECT_HANDLERS).

Scope: this models a build's own OUTPUT (damage dealt, Chip economy) -- not incoming monster
damage, HP-over-time survivability, equipment, or housing bonuses (raw class+subclass stats only).
dungeon.compute_stats takes no level parameter -- stats are level-independent, only which skills
are *unlocked* changes with level. Leveling growth (dungeon.CLASSES' own level_hp_gain etc.) is
per-class now, but still doesn't affect *relative* balance between builds within the same class
(clubs vs. spades on the same class levels identically) -- this module simulates every build at
SIMULATION_LEVEL (high enough to unlock its full current skill kit), as if fully built out.
"""

import random
import statistics

import dungeon

SIMULATION_TRIALS = 300
SIMULATION_LEVEL = 999  # unlocks every skill regardless of unlock_level -- see module docstring
ROTATION_TURN_CAP = 40  # safety cap on one simulated fight, in case output can't outpace monster HP
RAMP_TURN_COUNT = 8  # how many turns the damage-ramp chart (damage_ramp_for_build) covers
# Synthetic monster difficulty tiers -- (label, min intended_level, max intended_level), grounded in
# dungeon_monsters.json's own real content (median DEF of every monster whose intended_level
# falls in the band) rather than invented numbers, so "how does this rotation's damage ramp against
# an early/mid/late-game monster" reads directly off the game's own actual difficulty curve instead
# of needing a specific delve authored to test against. Adjust the bands here if the monster
# roster's own level spread shifts enough to leave one sparse/empty -- see monster_tiers.
MONSTER_TIER_LEVEL_BANDS = [("Early", 1, 7), ("Mid", 8, 20), ("Late", 21, 40)]
# A skill or build whose damage/turn or damage/chip sits further than this fraction from its
# cohort's median gets flagged in the balance tables -- starting number, tune after playtesting,
# same "documented, adjustable constant" style as horserace.TARGET_RTP/FAVORITE_LONGSHOT_BIAS.
OUTLIER_THRESHOLD = 0.4

# Effect types that make a skill fundamentally a "damage" skill for classify_skill's purposes, even
# if it also buffs/debuffs alongside dealing damage.
_DAMAGE_TYPES = {"damage_multiplier", "extra_attack", "dot"}
_HEAL_TYPES = {"heal_fraction", "hot"}
_BUFF_TYPES = {
    "atk_buff", "def_buff", "spatk_buff", "hp_buff", "chip_gain", "speed_buff",
    "dodge_buff", "resist_buff", "guard", "cleanse_dot", "cleanse_cc",
}
_DEBUFF_CC_TYPES = {
    "atk_debuff", "spatk_debuff", "speed_debuff", "def_shred",
    "stun", "sap", "taunt", "lower_threat",
}


def all_builds() -> list[tuple[str, str]]:
    """Every real (main_class, subclass) combo -- 16 today (4 classes x 4 subclasses).
    dungeon.SUBCLASSES never includes dungeon.NO_SUBCLASS (that's a transient pre-level-5 state
    synthesized separately, see dungeon.subclass_entry), so this is naturally just the 16 real
    builds worth balancing against each other."""
    return [(mc, sc) for mc in dungeon.CLASSES for sc in dungeon.SUBCLASSES]


def build_label(main_class: str, subclass: str) -> str:
    entry = dungeon.CLASS_BUILDS.get(f"{main_class}_{subclass}")
    return entry["display_name"] if entry else f"{main_class}/{subclass}"


def _reference_defense() -> float:
    """Median DEF across every real dungeon.MONSTERS entry, recomputed live (never cached) so it
    never goes stale after a monsters.json edit through the admin panel -- what the isolated
    per-skill damage table measures every skill against, since a skill's real damage always depends
    on who it's hitting. A single number now that damage mitigation is always DEF, physical or
    magic alike."""
    defs = sorted(m["def"] for m in dungeon.MONSTERS.values())
    return statistics.median(defs)


def _effect_types(skill: dict) -> set[str]:
    """Every effect type this skill could possibly produce on some cast, across both plain "effects"
    and every "effect_groups" alternative -- mirrors dungeon._effect_lists' shape without depending
    on that module-private helper."""
    if "effect_groups" in skill:
        lists = [g["effects"] for g in skill["effect_groups"]]
    else:
        lists = [skill.get("effects", [])]
    return {e["type"] for effects in lists for e in effects}


def classify_skill(skill: dict) -> str:
    """One-word tag for the balance tables. Priority order matters: a skill that both damages and
    does something else (buffs, debuffs) is still fundamentally a "damage" skill for this purpose."""
    types = _effect_types(skill)
    if types & _DAMAGE_TYPES:
        return "damage"
    if types & _HEAL_TYPES:
        return "heal"
    if types & _BUFF_TYPES:
        return "buff"
    if types & _DEBUFF_CC_TYPES:
        return "debuff/cc"
    return "utility"


def simulate_skill_cast(
    skill: dict, atk: float, spatk: float, defense: float, max_hp: float,
    trials: int = SIMULATION_TRIALS,
) -> dict:
    """Average damage dealt and HP healed by one cast of `skill`, Monte-Carlo'd through the exact
    same dungeon.resolve_cast_effects (handles effect_groups + per-effect chance rolls) and
    dungeon.roll_damage production code every real cast uses. Mirrors dungeon_view._resolve_player_
    action's own "is_damage_action" rule exactly: a damage roll only happens if damage_multiplier or
    extra_attack actually resolved THIS cast -- a heal/buff-only skill (or a damage skill whose own
    chance-gated damage effect whiffed) deals no phantom damage. Ignores dodge/resist (this is a
    clean, unconfounded "how hard does this skill hit" reference number -- simulate_build_through_
    delve below is where dodge/resist/buffs/DoTs interact turn-to-turn against a real fight)."""
    is_special = skill.get("special", False)
    base_atk = spatk if is_special else atk
    eff_def = defense  # mitigation is always DEF now, physical or magic alike
    total_damage = 0.0
    total_healed = 0.0
    for _ in range(trials):
        multiplier = 1.0
        extra_multipliers: list[float] = []
        is_damage_action = False
        for effect in dungeon.resolve_cast_effects(skill):
            etype = effect["type"]
            if etype == "damage_multiplier":
                multiplier *= effect["value"]
                is_damage_action = True
            elif etype == "extra_attack":
                extra_multipliers.append(effect.get("multiplier", 1.0))
                is_damage_action = True
            elif etype == "execute_multiplier":
                # No live target HP here (this is the isolated "how hard does this skill hit"
                # reference number, not a real fight) -- assumes full health, i.e. just `base`,
                # same conservative floor _run_ramp uses for its own undying target below.
                multiplier *= effect["base"]
            elif etype == "heal_fraction":
                total_healed += round(max_hp * effect["value"]) / trials
            elif etype == "dot":
                # One cast's total DoT damage over its own duration, folded into this cast's
                # average -- simulate_build_through_delve models the turn-by-turn tick instead.
                total_damage += effect["value"] * effect["duration"] / trials
        if is_damage_action:
            total_damage += dungeon.roll_damage(base_atk, eff_def, multiplier) / trials
            for extra in extra_multipliers:
                total_damage += dungeon.roll_damage(base_atk, eff_def, extra) / trials
    return {"avg_damage": total_damage, "avg_healed": total_healed}


def per_skill_table() -> list[dict]:
    """One row per skill across every build: {main_class, subclass, build_label, skill_id, name,
    chip_cost, unlock_level, type, avg_damage, dmg_per_chip, avg_healed, heal_per_chip,
    dmg_outlier, heal_outlier} -- avg_damage/avg_healed are None for skills where they don't apply
    (a pure buff/debuff/utility skill has neither). Outlier flags compare against the median of
    every OTHER row that has a value for that same metric (cross-skill, cross-build)."""
    ref_def = _reference_defense()
    rows = []
    for main_class, subclass in all_builds():
        stats = dungeon.compute_stats(main_class, subclass)
        label = build_label(main_class, subclass)
        for skill in dungeon.unlocked_skills(main_class, subclass, SIMULATION_LEVEL):
            skill_type = classify_skill(skill)
            result = simulate_skill_cast(
                skill, stats["atk"], stats["spatk"], ref_def, stats["hp"],
            )
            chip_cost = skill["chip_cost"]
            avg_damage = result["avg_damage"] if skill_type == "damage" else None
            avg_healed = result["avg_healed"] if skill_type == "heal" else None
            rows.append({
                "main_class": main_class, "subclass": subclass, "build_label": label,
                "skill_id": skill["id"], "name": skill["name"], "chip_cost": chip_cost,
                "unlock_level": skill["unlock_level"], "type": skill_type,
                # Every effect type this skill can produce, not just its primary classification --
                # a "damage" skill that's also a guaranteed stun + AOE ATK buff (e.g. Commanding
                # Voice) will correctly show weak damage/chip, but this is what tells a reviewing
                # admin WHY (it's a support skill with bonus damage, not an underpowered nuke) rather
                # than just a bare, easy-to-misread number.
                "effect_types": sorted(_effect_types(skill)),
                "avg_damage": avg_damage,
                "dmg_per_chip": (avg_damage / chip_cost) if avg_damage is not None and chip_cost else None,
                "avg_healed": avg_healed,
                "heal_per_chip": (avg_healed / chip_cost) if avg_healed is not None and chip_cost else None,
            })
    _flag_outliers(rows, "dmg_per_chip", "dmg_outlier")
    _flag_outliers(rows, "heal_per_chip", "heal_outlier")
    return rows


def _flag_outliers(rows: list[dict], value_key: str, flag_key: str) -> None:
    """Mutates every row in place, adding flag_key: "high"/"low"/None -- "high"/"low" for a row
    whose value_key sits further than OUTLIER_THRESHOLD (relative) above/below the median of every
    row that has a value there, None otherwise. A string (not a bool) so callers get the direction
    for free instead of having to re-derive "is this the good kind of outlier or the bad kind" from
    the raw value themselves."""
    values = [r[value_key] for r in rows if r[value_key] is not None]
    if len(values) < 2:
        for r in rows:
            r[flag_key] = None
        return
    median = statistics.median(values)
    for r in rows:
        v = r[value_key]
        if v is None or median <= 0 or abs(v - median) / median <= OUTLIER_THRESHOLD:
            r[flag_key] = None
        else:
            r[flag_key] = "high" if v > median else "low"


def default_delve_id() -> str | None:
    """The first active delve in file order, same "first active" default the balance page's ?delve=
    query param falls back to when unset."""
    active = dungeon.active_delves()
    return next(iter(active), None)


def _delve_fight_sequence(delve_id: str) -> list[dict]:
    """One representative path through delve_id's real rooms, combat room by combat room, each
    resolved to a single synthetic monster ({"hp", "def"}: summed HP, averaged DEF) standing in
    for that room's actual fight -- multi-monster groups are collapsed into one target
    rather than modeling per-monster turn order/targeting, which is out of scope for a damage/chip-
    economy tool. Deterministically takes each combat room's highest-chance monster_groups entry
    (not a random roll) so the "representative path" is reproducible run to run. A branching choice
    room takes its first listed action's on_success outcome -- one representative path through the
    delve's room graph, not full coverage of every branch."""
    delve = dungeon.DELVES[delve_id]
    rooms = dungeon.rooms_by_id(delve)
    fights = []
    room_id = delve["start_room"]
    visited: set[str] = set()
    while room_id and room_id not in visited:
        visited.add(room_id)
        room = rooms.get(room_id)
        if room is None:
            break
        if room["type"] == "combat":
            group = max(
                room["monster_groups"],
                key=lambda g: g.get("chance", dungeon.DEFAULT_MONSTER_GROUP_CHANCE),
            )
            monsters = [dungeon.MONSTERS[mid] for mid in group["monsters"]]
            fights.append({
                "hp": sum(m["hp"] for m in monsters),
                "def": statistics.mean(m["def"] for m in monsters),
            })
            room_id = room.get("next")
        else:
            actions = room.get("actions") or []
            if not actions:
                break
            room_id = (actions[0].get("on_success") or {}).get("next")
    return fights


# A "rotation" is just the strategy a build uses to decide which skill to cast each turn, given
# whatever Chips it has left -- these are the candidate strategies real players might plausibly
# follow, not an exhaustive search of every possible turn order (that's combinatorially enormous
# and not what a balance overview needs). Each maps to a human-readable one-line description shown
# directly on the admin page, so "rotation" never has to be explained via a separate glossary --
# picking a policy from this dict IS the explanation.
ROTATION_POLICIES = {
    "burst": "Always casts the single strongest affordable skill (ranked by isolated avg damage) -- "
             "spends Chips as fast as possible for the biggest hit available each turn.",
    "efficient": "Always casts the best-value affordable skill (ranked by damage per Chip) -- trades "
                 "some peak damage for more total casts across the delve.",
    "paced": "Splits the Chip pool evenly across the delve's fights up front, then bursts within "
             "just that fight's slice -- won't blow the whole pool on an early fight.",
}


def _rank_damage_skills(damage_skills: list[dict], stats: dict, ref_def: float, key: str) -> list[dict]:
    """damage_skills sorted best-first by `key` ("avg_damage" for "burst"/"paced", "dmg_per_chip"
    for "efficient") -- both computed once, isolated, against the game's real median monster
    defense (see per_skill_table), not re-simulated every turn of every trial."""
    scored = []
    for skill in damage_skills:
        result = simulate_skill_cast(
            skill, stats["atk"], stats["spatk"], ref_def, stats["hp"], trials=50,
        )
        value = result["avg_damage"] if key == "avg_damage" else (
            result["avg_damage"] / skill["chip_cost"] if skill["chip_cost"] else 0.0
        )
        scored.append((value, skill))
    return [skill for _, skill in sorted(scored, key=lambda pair: pair[0], reverse=True)]


def _take_turn(state: dict, ranked: list[dict], hp_fraction: float | None = None) -> float:
    """Resolves exactly one turn against `state` (a mutable {"atk", "spatk", "def_", "chips",
    "dot_ticks"} dict, updated in place) using `ranked`'s ordering to pick the strongest/
    most-affordable skill out of state["chips"], falling back to a free plain Attack once nothing
    is affordable. Returns the total damage dealt this turn (DoT ticks + this turn's own action).

    `hp_fraction` is the target's own current_hp/max_hp, for execute_multiplier -- None (the
    default) means "assume full health," what _run_ramp passes for its undying target; _run_fight
    passes the real live fraction each turn since that's the one place a target's HP actually
    depletes.

    Ordering matches dungeon_view._resolve_player_action exactly, not just approximately: the
    avoid roll is made ONCE per cast, against the target's DEF or Magic (dungeon.resolved_avoid_type
    of the skill, not whether its damage is Physical/Special -- see that function's own docstring)
    as it stood BEFORE this cast's own effects -- so a successful dodge/resist blocks this cast's
    damage AND any enemy-targeted debuff it carried (def_shred/dot) alike, while a self-targeted
    buff (atk_buff/spatk_buff) is never avoid-gated, same "self/ally effects don't care about the
    enemy's avoid roll" rule production follows. Any def_shred/atk_buff/spatk_buff THIS SAME cast
    applies still affects THIS SAME cast's own damage roll (buffs/debuffs resolve before the roll
    reads atk/spatk/def_, same as production) -- this is what makes a "soften them up then swing"
    skill (single cast, both effects) actually ramp within its own turn, not just on some later
    cast. Damage MITIGATION (once a hit lands) is always DEF, regardless of avoid type."""
    damage = 0.0
    for tick in state["dot_ticks"]:
        damage += tick[0]
        tick[1] -= 1
    state["dot_ticks"] = [t for t in state["dot_ticks"] if t[1] > 0]

    affordable = [s for s in ranked if s["chip_cost"] <= state["chips"]]
    skill = affordable[0] if affordable else None
    if skill is None:
        if random.random() < dungeon.dodge_chance(state["def_"]):
            return damage
        return damage + dungeon.roll_damage(state["atk"], state["def_"], 1.0)

    state["chips"] -= skill["chip_cost"]
    is_special = skill.get("special", False)
    use_resist = dungeon.resolved_avoid_type(skill) == "resist"
    dodged = random.random() < dungeon.dodge_chance(state["spatk"] if use_resist else state["def_"])

    multiplier, extras, is_dmg = 1.0, [], False
    for effect in dungeon.resolve_cast_effects(skill):
        etype = effect["type"]
        if etype == "atk_buff":
            state["atk"] += effect["value"]
            continue  # self-targeted -- never dodge-gated
        if etype == "spatk_buff":
            state["spatk"] += effect["value"]
            continue  # self-targeted -- never dodge-gated
        if dodged:
            continue  # every remaining type here (damage/extra_attack/def_shred/dot) is enemy-targeted
        if etype == "damage_multiplier":
            multiplier *= effect["value"]
            is_dmg = True
        elif etype == "extra_attack":
            extras.append(effect.get("multiplier", 1.0))
            is_dmg = True
        elif etype == "def_shred":
            state["def_"] = max(0, state["def_"] - effect["value"])
        elif etype == "execute_multiplier":
            missing_frac = 1 - (hp_fraction if hp_fraction is not None else 1.0)
            multiplier *= effect["base"] + effect["scale"] * missing_frac
        elif etype == "dot":
            state["dot_ticks"].append([effect["value"], effect["duration"]])

    if is_dmg:
        base_atk = state["spatk"] if is_special else state["atk"]
        eff_def = state["def_"]  # mitigation is always DEF now
        dmg = dungeon.roll_damage(base_atk, eff_def, multiplier)
        for extra in extras:
            dmg += dungeon.roll_damage(base_atk, eff_def, extra)
        damage += dmg
    return damage


def _run_fight(
    atk: float, spatk: float, def_: float, hp: float, ranked: list[dict], chips: float,
) -> tuple[float, int, float]:
    """Simulates one fight turn-by-turn via _take_turn until `hp` reaches 0 or ROTATION_TURN_CAP is
    hit. Returns (damage dealt, turns taken, Chips left over) -- `chips` here is whatever this fight
    is allowed to spend, which for "paced" is a per-fight slice, not the build's whole remaining
    pool (see simulate_build_rotations)."""
    state = {"atk": atk, "spatk": spatk, "def_": def_, "chips": chips, "dot_ticks": []}
    max_hp = hp
    turns = 0
    damage_total = 0.0
    while hp > 0 and turns < ROTATION_TURN_CAP:
        turns += 1
        dmg = _take_turn(state, ranked, hp_fraction=hp / max_hp)
        hp -= dmg
        damage_total += dmg
    return damage_total, turns, state["chips"]


def _run_ramp(atk: float, spatk: float, def_: float, chips: float, ranked: list[dict], turns: int) -> list[float]:
    """Same per-turn mechanics as _run_fight (via _take_turn) but always runs exactly `turns` turns
    against an undying target -- no HP tracked at all -- for charting how a rotation's own damage
    output ramps turn to turn (buffs/debuffs stacking, Chips running dry, ...) independent of how
    long a real fight at that difficulty would actually last. See damage_ramp_for_build."""
    state = {"atk": atk, "spatk": spatk, "def_": def_, "chips": chips, "dot_ticks": []}
    return [_take_turn(state, ranked) for _ in range(turns)]


def monster_tiers() -> dict[str, dict]:
    """{tier_label: {"def", "monster_count"}} for every non-empty MONSTER_TIER_LEVEL_BANDS
    entry, computed live from dungeon.MONSTERS (never cached) so a monsters.json edit through the
    admin panel is reflected the next time this is called, same "read the live registry inside the
    function body" rule skill_balance_view itself follows."""
    tiers = {}
    for label, lo, hi in MONSTER_TIER_LEVEL_BANDS:
        group = [m for m in dungeon.MONSTERS.values() if lo <= m.get("intended_level", 0) <= hi]
        if not group:
            continue
        tiers[label] = {
            "def": statistics.median(m["def"] for m in group),
            "monster_count": len(group),
        }
    return tiers


def damage_ramp_for_build(
    main_class: str, subclass: str, policy: str = "burst", trials: int = SIMULATION_TRIALS,
) -> dict[str, list[float]]:
    """{tier_label: [avg damage turn 1, avg damage turn 2, ..., turn RAMP_TURN_COUNT]} for this
    build+policy against every monster_tiers() tier. Each tier is an independent, fresh engagement
    (full Chips, no monster HP/death tracked -- always runs the full RAMP_TURN_COUNT turns) rather
    than a delve's real, sequential fight-to-fight walk, so this reads on the game's overall
    difficulty curve without needing a specific delve authored long/varied enough to explore it.
    Complements (not replaces) simulate_build_through_delve/per_build_delve_table, which instead
    shows Chip economy carrying across several REAL, sequential fights in one specific delve -- two
    different questions ("how does a rotation's own output ramp turn to turn against tougher
    monsters" vs. "how does a rotation hold up across a whole real run"), both worth keeping.
    `policy` is "burst" or "efficient" only -- "paced" splits Chips across several DISTINCT fights,
    which a single continuous engagement doesn't have; see simulate_build_rotations for that one."""
    stats = dungeon.compute_stats(main_class, subclass)
    ref_def = _reference_defense()
    damage_skills = [
        s for s in dungeon.unlocked_skills(main_class, subclass, SIMULATION_LEVEL)
        if classify_skill(s) == "damage"
    ]
    rank_key = "dmg_per_chip" if policy == "efficient" else "avg_damage"
    ranked = _rank_damage_skills(damage_skills, stats, ref_def, rank_key)

    result = {}
    for label, tier in monster_tiers().items():
        per_turn_samples: list[list[float]] = [[] for _ in range(RAMP_TURN_COUNT)]
        for _ in range(trials):
            turn_damages = _run_ramp(
                stats["atk"], stats["spatk"], tier["def"], stats["chips"], ranked, RAMP_TURN_COUNT,
            )
            for i, dmg in enumerate(turn_damages):
                per_turn_samples[i].append(dmg)
        result[label] = [statistics.mean(vals) for vals in per_turn_samples]
    return result


def damage_ramp_report(main_class: str, subclass: str, trials: int = SIMULATION_TRIALS) -> dict[str, dict[str, list[float]]]:
    """{policy: damage_ramp_for_build(...)} for "burst" and "efficient" -- the full comparison the
    admin page's Rotation Explorer shows for one build's damage-ramp charts."""
    return {
        policy: damage_ramp_for_build(main_class, subclass, policy=policy, trials=trials)
        for policy in ("burst", "efficient")
    }


def simulate_build_through_delve(
    main_class: str, subclass: str, delve_id: str, policy: str = "burst", trials: int = SIMULATION_TRIALS,
) -> dict:
    """Runs `policy` (a key of ROTATION_POLICIES) for this build straight through delve_id's
    representative fight sequence, with ONE Chip pool spent across the whole delve -- never
    refilled between fights (exercises the behavior dungeon_view.py's Chips-reset fix now produces
    for real). Returns {"per_fight_dpt": [avg dmg/turn per fight position], "overall_dpt": mean of
    that, "chips_leftover": avg unspent Chips at delve end}. "burst"/"efficient" rank once, up
    front, and spend from the build's single running pool; "paced" additionally splits that pool
    into an even per-fight slice before ranking within each fight, so it never front-loads."""
    stats = dungeon.compute_stats(main_class, subclass)
    ref_def = _reference_defense()
    damage_skills = [
        s for s in dungeon.unlocked_skills(main_class, subclass, SIMULATION_LEVEL)
        if classify_skill(s) == "damage"
    ]
    fights = _delve_fight_sequence(delve_id)
    if not fights:
        return {"per_fight_dpt": [], "overall_dpt": 0.0, "chips_leftover": 0.0}

    rank_key = "dmg_per_chip" if policy == "efficient" else "avg_damage"
    ranked = _rank_damage_skills(damage_skills, stats, ref_def, rank_key)
    fight_budget = stats["chips"] / len(fights) if policy == "paced" else None

    per_fight_dpt: list[list[float]] = [[] for _ in fights]
    leftover_samples: list[float] = []

    for _ in range(trials):
        chips = stats["chips"]
        atk, spatk = stats["atk"], stats["spatk"]
        for fight_index, monster in enumerate(fights):
            spend_limit = min(chips, fight_budget) if fight_budget is not None else chips
            dmg, turns, remaining = _run_fight(
                atk, spatk, monster["def"], monster["hp"], ranked, spend_limit,
            )
            chips -= (spend_limit - remaining)
            per_fight_dpt[fight_index].append(dmg / max(1, turns))
        leftover_samples.append(chips)

    return {
        "per_fight_dpt": [statistics.mean(vals) for vals in per_fight_dpt],
        "overall_dpt": statistics.mean([v for vals in per_fight_dpt for v in vals]),
        "chips_leftover": statistics.mean(leftover_samples),
    }


def simulate_build_rotations(main_class: str, subclass: str, delve_id: str, trials: int = SIMULATION_TRIALS) -> dict:
    """{policy: simulate_build_through_delve(...)} for every ROTATION_POLICIES key -- the full
    comparison the admin page's Rotation Explorer section shows for one build."""
    return {
        policy: simulate_build_through_delve(main_class, subclass, delve_id, policy=policy, trials=trials)
        for policy in ROTATION_POLICIES
    }


def per_build_delve_table(delve_id: str) -> list[dict]:
    """One row per build, each using whichever ROTATION_POLICIES entry actually scores highest for
    THAT build in THIS delve (its "detected optimal rotation") -- comparing every build at its own
    best strategy rather than one fixed policy applied uniformly, since a chip-hungry burst build
    and a chip-light sustain build don't necessarily share the same optimal play. Row shape:
    {main_class, subclass, build_label, policy (the winning one's key), per_fight_dpt, overall_dpt,
    outlier}."""
    rows = []
    for main_class, subclass in all_builds():
        by_policy = simulate_build_rotations(main_class, subclass, delve_id)
        best_policy, best = max(by_policy.items(), key=lambda kv: kv[1]["overall_dpt"])
        rows.append({
            "main_class": main_class, "subclass": subclass,
            "build_label": build_label(main_class, subclass),
            "policy": best_policy, "per_fight_dpt": best["per_fight_dpt"], "overall_dpt": best["overall_dpt"],
        })
    _flag_outliers(rows, "overall_dpt", "outlier")
    return rows
