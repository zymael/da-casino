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
are *unlocked* changes with level, and dungeon.LEVEL_*_GAIN growth is flat and identical across
every build, so it doesn't affect *relative* balance between builds either. This module simulates
every build at SIMULATION_LEVEL (high enough to unlock its full current skill kit), as if fully
built out.
"""

import random
import statistics

import dungeon

SIMULATION_TRIALS = 300
SIMULATION_LEVEL = 999  # unlocks every skill regardless of unlock_level -- see module docstring
ROTATION_TURN_CAP = 40  # safety cap on one simulated fight, in case output can't outpace monster HP
# A skill or build whose damage/turn or damage/chip sits further than this fraction from its
# cohort's median gets flagged in the balance tables -- starting number, tune after playtesting,
# same "documented, adjustable constant" style as horserace.TARGET_RTP/FAVORITE_LONGSHOT_BIAS.
OUTLIER_THRESHOLD = 0.4

# Effect types that make a skill fundamentally a "damage" skill for classify_skill's purposes, even
# if it also buffs/debuffs alongside dealing damage.
_DAMAGE_TYPES = {"damage_multiplier", "extra_attack", "dot"}
_HEAL_TYPES = {"heal_fraction", "hot"}
_BUFF_TYPES = {
    "atk_buff", "def_buff", "spatk_buff", "spdef_buff", "hp_buff", "speed_buff",
    "dodge_buff", "resist_buff", "guard", "cleanse_dot", "cleanse_cc",
}
_DEBUFF_CC_TYPES = {
    "atk_debuff", "spatk_debuff", "spdef_debuff", "speed_debuff", "def_shred",
    "stun", "sap", "taunt", "lower_threat",
}


def all_builds() -> list[tuple[str, str]]:
    """Every (main_class, subclass) combo -- 16 today (4 classes x 4 subclasses)."""
    return [(mc, sc) for mc in dungeon.CLASSES for sc in dungeon.SUBCLASSES]


def build_label(main_class: str, subclass: str) -> str:
    return dungeon.NAMES.get((main_class, subclass), f"{main_class}/{subclass}")


def _reference_defense() -> tuple[float, float]:
    """Median DEF/SpDef across every real dungeon.MONSTERS entry, recomputed live (never cached) so
    it never goes stale after a monsters.json edit through the admin panel -- what the isolated
    per-skill damage table measures every skill against, since a skill's real damage always depends
    on who it's hitting."""
    defs = sorted(m["def"] for m in dungeon.MONSTERS.values())
    spdefs = sorted(m["spdef"] for m in dungeon.MONSTERS.values())
    return statistics.median(defs), statistics.median(spdefs)


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
    skill: dict, atk: float, spatk: float, defense: float, spdefense: float, max_hp: float,
    trials: int = SIMULATION_TRIALS,
) -> dict:
    """Average damage dealt and HP healed by one cast of `skill`, Monte-Carlo'd through the exact
    same dungeon.resolve_cast_effects (handles effect_groups + per-effect chance rolls) and
    dungeon.roll_damage production code every real cast uses. Mirrors dungeon_view._resolve_player_
    action's own "is_damage_action" rule exactly: a damage roll only happens if damage_multiplier or
    extra_attack actually resolved THIS cast -- a heal/buff-only skill (or a damage skill whose own
    chance-gated damage effect whiffed) deals no phantom damage. Ignores dodge (this is a clean,
    unconfounded "how hard does this skill hit" reference number -- simulate_build_through_delve
    below is where dodge/buffs/DoTs interact turn-to-turn against a real fight)."""
    is_special = skill.get("special", False)
    base_atk = spatk if is_special else atk
    eff_def = spdefense if is_special else defense
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
    ref_def, ref_spdef = _reference_defense()
    rows = []
    for main_class, subclass in all_builds():
        stats = dungeon.compute_stats(main_class, subclass)
        label = build_label(main_class, subclass)
        for skill in dungeon.unlocked_skills(main_class, subclass, SIMULATION_LEVEL):
            skill_type = classify_skill(skill)
            result = simulate_skill_cast(
                skill, stats["atk"], stats["spatk"], ref_def, ref_spdef, stats["hp"],
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
    resolved to a single synthetic monster ({"hp", "def", "spdef"}: summed HP, averaged DEF/SpDef)
    standing in for that room's actual fight -- multi-monster groups are collapsed into one target
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
                "spdef": statistics.mean(m["spdef"] for m in monsters),
            })
            room_id = room.get("next")
        else:
            actions = room.get("actions") or []
            if not actions:
                break
            room_id = (actions[0].get("on_success") or {}).get("next")
    return fights


def simulate_build_through_delve(
    main_class: str, subclass: str, delve_id: str, trials: int = SIMULATION_TRIALS,
) -> list[float]:
    """Average damage/turn per fight-position (index 0 = the delve's first fight, index 1 = the
    second, ...) for this build walking straight through delve_id's representative fight sequence
    with ONE Chip pool spent across the whole delve (not refilled between fights -- exercises the
    behavior dungeon_view.py's Chips-reset fix now produces for real). Each fight runs a simple
    greedy rotation -- the strongest affordable damage skill this turn (ranked once, up front, by
    isolated avg_damage against the game's real median monster defense -- see per_skill_table),
    else a free plain Attack -- until the fight's monster HP hits 0 or ROTATION_TURN_CAP is reached.
    A chip-hungry burst-oriented build's output visibly tapering off in later fight positions (vs. a
    sustain-oriented build staying flat) is exactly what this is for."""
    stats = dungeon.compute_stats(main_class, subclass)
    ref_def, ref_spdef = _reference_defense()
    damage_skills = [
        s for s in dungeon.unlocked_skills(main_class, subclass, SIMULATION_LEVEL)
        if classify_skill(s) == "damage"
    ]
    ranked = sorted(
        damage_skills,
        key=lambda s: simulate_skill_cast(
            s, stats["atk"], stats["spatk"], ref_def, ref_spdef, stats["hp"], trials=50,
        )["avg_damage"],
        reverse=True,
    )
    fights = _delve_fight_sequence(delve_id)
    if not fights:
        return []
    per_fight_dpt: list[list[float]] = [[] for _ in fights]

    for _ in range(trials):
        chips = stats["chips"]
        atk, spatk = stats["atk"], stats["spatk"]
        for fight_index, monster in enumerate(fights):
            hp = monster["hp"]
            def_, spdef = monster["def"], monster["spdef"]
            dot_ticks: list[list[float]] = []  # [value, remaining_turns] pairs
            turns = 0
            damage_this_fight = 0.0
            while hp > 0 and turns < ROTATION_TURN_CAP:
                turns += 1
                for tick in dot_ticks:
                    hp -= tick[0]
                    damage_this_fight += tick[0]
                    tick[1] -= 1
                dot_ticks = [t for t in dot_ticks if t[1] > 0]
                if hp <= 0:
                    break

                affordable = [s for s in ranked if s["chip_cost"] <= chips]
                skill = affordable[0] if affordable else None
                if skill is None:
                    if random.random() < dungeon.dodge_chance(def_):
                        continue
                    dmg = dungeon.roll_damage(atk, def_, 1.0)
                    hp -= dmg
                    damage_this_fight += dmg
                    continue

                chips -= skill["chip_cost"]
                is_special = skill.get("special", False)
                base_atk = spatk if is_special else atk
                eff_def = spdef if is_special else def_
                if random.random() < dungeon.dodge_chance(eff_def):
                    continue
                multiplier, extras, is_dmg = 1.0, [], False
                for effect in dungeon.resolve_cast_effects(skill):
                    etype = effect["type"]
                    if etype == "damage_multiplier":
                        multiplier *= effect["value"]
                        is_dmg = True
                    elif etype == "extra_attack":
                        extras.append(effect.get("multiplier", 1.0))
                        is_dmg = True
                    elif etype == "atk_buff":
                        atk += effect["value"]
                    elif etype == "spatk_buff":
                        spatk += effect["value"]
                    elif etype == "def_shred":
                        def_ = max(0, def_ - effect["value"])
                    elif etype == "dot":
                        dot_ticks.append([effect["value"], effect["duration"]])
                if is_dmg:
                    dmg = dungeon.roll_damage(base_atk, eff_def, multiplier)
                    for extra in extras:
                        dmg += dungeon.roll_damage(base_atk, eff_def, extra)
                    hp -= dmg
                    damage_this_fight += dmg

            per_fight_dpt[fight_index].append(damage_this_fight / max(1, turns))

    return [statistics.mean(vals) for vals in per_fight_dpt]


def per_build_delve_table(delve_id: str) -> list[dict]:
    """One row per build: {main_class, subclass, build_label, per_fight_dpt (list, one avg
    damage/turn per fight position), overall_dpt (mean across all fight positions), outlier}."""
    rows = []
    for main_class, subclass in all_builds():
        per_fight = simulate_build_through_delve(main_class, subclass, delve_id)
        overall = statistics.mean(per_fight) if per_fight else 0.0
        rows.append({
            "main_class": main_class, "subclass": subclass,
            "build_label": build_label(main_class, subclass),
            "per_fight_dpt": per_fight, "overall_dpt": overall,
        })
    _flag_outliers(rows, "overall_dpt", "outlier")
    return rows
