"""Headless, BIDIRECTIONAL combat simulator -- answers "how hard is this delve" against real
characters, complementing skill_balance.py (which deliberately only models a build's own damage
OUTPUT, never incoming monster damage or the player's own HP depletion -- see that module's
docstring). Drives dungeon_view.py's actual production combat resolver directly (DelveSession,
_resolve_player_action, _resolve_monster_attack, _tick_timed_effects, _active_cc_type, ...) wrapped
in a synchronous, no-Discord, no-DB-write turn loop of its own -- only the outer orchestration
(interaction responses, _award_kill/_forfeit's DB writes, achievement/loot side effects) is skipped;
the dodge/damage/effect/CTB-turn-order math itself is never reimplemented.

Scope: gnome_bathhouse's early rooms only -- The Sewer -> The Doorman (choice) -> Fight the Doorman
(if triggered) -> The Bathhouse -> A Private Bath -- against real casino.db characters for one
guild. Out of scope for this pass: equipment on_hit procs (constant stat bonuses from gear ARE
included, via dungeon.compute_effective_stats), housing bonuses, consumable items, the moon-night
combat multiplier (held neutral for a reproducible baseline), anything past A Private Bath's branch
fan-out, and party/multiplayer delve mechanics (solo-only, matching how these characters would
actually enter these rooms). No DB writes, ever -- casino.db is opened read-only.

The player-side action policy (rank_action, below) is the one place this necessarily approximates
real play rather than deterministic game rules: it never opens with a pure buff/debuff/CC skill,
and its damage-skill ranking is a one-step expected-value estimate against the target's current
(debuff-adjusted) defense, not a lookahead search. Same honesty skill_balance.ROTATION_POLICIES'
own docstring already draws between "faithful to the rules" and "approximates a player's choices".
"""

import argparse
import random
import sqlite3
import statistics

import db
import dungeon
import dungeon_view
import skill_balance

SIM_GUILD_ID = 1311918529951301693
DELVE_ID = "gnome_bathhouse"
TRIALS = 300  # ~+-3% standard error on a win-rate estimate at p=0.5 -- fine for triage; raise for a final pass
ROOM_TURN_CAP = 60  # safety cap on total turn-slots (both sides) in one combat room; a run hitting
                     # this is bucketed "timeout", distinct from "won"/"died", and should be rare
HEAL_HP_THRESHOLD = 0.5  # below this HP fraction, rank_action prefers healing over damage

ROOM_ORDER = ["The Sewer", "Fight the Doorman", "The Bathhouse", "A Private Bath"]


# --- Character loading (read-only) --------------------------------------------------------------

def _connect_readonly() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)


def load_target_characters(guild_id: int) -> list[dict]:
    """Every real dungeon character for `guild_id`, shaped like db.get_character's return (so it
    drops straight into dungeon.compute_effective_stats) plus its equipped items. No existing
    db.py helper lists characters guild-wide -- single-user lookups only -- so this is new SQL,
    issued against a read-only connection since this module must never write to casino.db."""
    conn = _connect_readonly()
    try:
        rows = conn.execute(
            "SELECT user_id, main_class, subclass, hp, atk, def, spatk, spdef, speed, level, current_hp "
            "FROM characters WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        out = []
        for user_id, main_class, subclass, hp, atk, def_, spatk, spdef, speed, level, current_hp in rows:
            character = {
                "main_class": main_class, "subclass": subclass, "level": level,
                "hp": hp, "atk": atk, "def": def_, "spatk": spatk, "spdef": spdef, "speed": speed,
                "current_hp": current_hp,
            }
            equip_rows = conn.execute(
                "SELECT slot, item_id FROM character_equipment WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchall()
            equipped = {slot: item_id for slot, item_id in equip_rows}
            out.append({"user_id": user_id, "character": character, "equipped": equipped})
        return out
    finally:
        conn.close()


def _full_hp_character(character: dict, equipped: dict[str, str]) -> dict:
    """A copy of `character` starting at full EFFECTIVE HP (post-equipment), not just its own base
    `hp` column -- DelveSession.__init__ does `self.hp = min(character["current_hp"], self.max_hp)`,
    so current_hp has to be at least the equipped max for that min() to actually land on full, not
    silently clamp to the character's pre-equipment base. This answers "how hard is this build," a
    repeatable property of the build -- not "how hard is it right now given whatever HP a real
    character happens to be sitting at from unrelated prior play" (see --use-persisted-hp)."""
    effective = dungeon.compute_effective_stats(character, equipped)
    character = dict(character)
    character["current_hp"] = effective["hp"]
    return character


# --- Session/room plumbing -- thin wrappers around real dungeon_view internals -------------------

def build_session(guild_id: int, user_id: int, character: dict, equipped: dict[str, str], delve: dict) -> dungeon_view.DelveSession:
    return dungeon_view.DelveSession(guild_id, user_id, character, equipped, delve)


def enter_combat_room(session: dungeon_view.DelveSession, room_id: str) -> None:
    """Mirrors dungeon_view._goto_room's combat branch -- rolls a fresh monster group and resets
    per-fight state. Deliberately does NOT touch session.chips: chips are delve-scoped, not
    per-fight (verified directly against _goto_room's own comment and __init__ -- one pool spent
    across the whole delve, never refilled between rooms), so a chained run must carry one pool
    across every room exactly like a real play-through would."""
    session.current_room_id = room_id
    room = session.rooms_by_id[room_id]
    session.monsters, session.group_next_override = dungeon_view._roll_monster_instances(room)
    session.current_target_slot = 0
    session.used_item_effects = set()
    session.turn_clock = 0.0


def build_isolated_session(
    guild_id: int, user_id: int, character: dict, equipped: dict[str, str], delve: dict, room_id: str,
) -> dungeon_view.DelveSession:
    """A session dropped straight into `room_id` at full HP and a full Chip pool, independent of
    whatever a chained run would have already spent getting there -- the "how hard is THIS room on
    its own" reading, mirroring skill_balance.simulate_skill_cast's own isolated/unconfounded lens
    (chips are already full straight out of DelveSession.__init__, so no extra reset needed here)."""
    session = build_session(guild_id, user_id, character, equipped, delve)
    enter_combat_room(session, room_id)
    return session


# --- Player action policy (the approximate part -- see module docstring) -------------------------

def _all_effect_lists(skill: dict) -> list[list[dict]]:
    """Mirrors dungeon._effect_lists' shape without depending on that module-private helper -- same
    reasoning skill_balance._effect_types already documents for doing this itself."""
    if "effect_groups" in skill:
        return [g["effects"] for g in skill["effect_groups"]]
    return [skill.get("effects", [])]


def _expected_hit(atk: float, defense: float, multiplier: float = 1.0) -> float:
    """dungeon.roll_damage's mean, without consuming a random draw -- used only for ranking which
    skill to cast, never for an actual damage roll (real casts still go through
    dungeon.resolve_cast_effects + dungeon.roll_damage inside _resolve_player_action)."""
    mitigation = dungeon.DEF_MITIGATION_K / (dungeon.DEF_MITIGATION_K + max(0, defense))
    return atk * multiplier * mitigation


def _estimate_skill_damage(skill: dict, atk: float, spatk: float, def_: float, spdef: float) -> float:
    """Expected damage of one cast, averaged over effect_groups (weighted by their own "chance") and
    each effect's own "chance" -- a live point estimate against the target's CURRENT (debuff-
    adjusted) defense, not a lookahead search across future turns."""
    is_special = skill.get("special", False)
    base_atk = spatk if is_special else atk
    eff_def = spdef if is_special else def_
    lists = _all_effect_lists(skill)
    weights = (
        [g.get("chance", dungeon.DEFAULT_EFFECT_GROUP_CHANCE) for g in skill["effect_groups"]]
        if "effect_groups" in skill else [1.0]
    )
    total_weight = sum(weights) or 1.0
    expected = 0.0
    for weight, effects in zip(weights, lists):
        group_dmg = 0.0
        for effect in effects:
            chance = effect.get("chance", 1.0)
            if effect["type"] == "damage_multiplier":
                group_dmg += chance * _expected_hit(base_atk, eff_def, effect["value"])
            elif effect["type"] == "extra_attack":
                group_dmg += chance * _expected_hit(base_atk, eff_def, effect.get("multiplier", 1.0))
            elif effect["type"] == "dot":
                group_dmg += chance * effect["value"] * effect["duration"]
        expected += (weight / total_weight) * group_dmg
    return expected


def _estimate_heal_value(skill: dict) -> float:
    """Best-case heal/HoT fraction of max HP this skill could restore -- used only to rank which
    heal-classified skill to prefer, not to predict an exact amount."""
    best = 0.0
    for effects in _all_effect_lists(skill):
        for effect in effects:
            chance = effect.get("chance", 1.0)
            if effect["type"] == "heal_fraction":
                best = max(best, chance * effect["value"])
            elif effect["type"] == "hot":
                best = max(best, chance * effect["value"] * effect.get("duration", 1))
    return best


def rank_action(session: dungeon_view.DelveSession) -> dict | None:
    """Returns the skill to cast this turn, or None for a plain Attack. Below HEAL_HP_THRESHOLD,
    prefers the best affordable heal-classified skill if one exists; otherwise casts the highest
    expected-damage affordable skill; otherwise Attack. Never chooses a pure buff/debuff/CC skill --
    see module docstring."""
    hp_fraction = session.hp / session.max_hp if session.max_hp else 0.0
    affordable = [s for s in session.unlocked_skills if s["chip_cost"] <= session.chips]

    if hp_fraction <= HEAL_HP_THRESHOLD:
        heals = [s for s in affordable if skill_balance.classify_skill(s) == "heal"]
        if heals:
            return max(heals, key=_estimate_heal_value)

    target = session.current_target()
    damages = [s for s in affordable if skill_balance.classify_skill(s) == "damage"]
    if damages and target is not None:
        atk = max(0, session.atk - session.atk_debuff)
        spatk = max(0, session.spatk - session.spatk_debuff)
        def_ = max(0, target.def_ - target.def_debuff)
        spdef = max(0, target.spdef - target.spdef_debuff)
        return max(damages, key=lambda s: _estimate_skill_damage(s, atk, spatk, def_, spdef))

    return None


# --- Turn resolution -- thin orchestration wrapping real production functions ---------------------

def resolve_player_turn(session: dungeon_view.DelveSession, log_lines: list[str]) -> None:
    skill = rank_action(session)
    if skill is not None:
        session.chips -= skill["chip_cost"]
        effects = dungeon.resolve_cast_effects(skill)
        special = bool(skill.get("special"))
        verb = f"unleash **{skill['name']}**"
        is_plain_attack = False
    else:
        effects, special, verb, is_plain_attack = [], False, "attack", True

    dungeon_view._resolve_player_action(
        session, [session], session.living_monsters(), session.current_target(),
        effects, special, verb, "You", "your", "drain", 1.0, [], False, log_lines,
        is_plain_attack=is_plain_attack,
    )
    session.turn_clock += dungeon.turn_interval(max(1, session.speed - session.speed_debuff))


def resolve_monster_turn(session: dungeon_view.DelveSession, monster: dungeon_view.MonsterInstance, log_lines: list[str]) -> None:
    """Mirrors _advance_solo_turns' monster branch verbatim, including reading cc_type BEFORE the
    tick (see dungeon_view._active_cc_type's own docstring on why the order matters)."""
    cc_type = dungeon_view._active_cc_type(monster)
    dungeon_view._tick_timed_effects([monster], log_lines)
    monster.turn_clock += dungeon.turn_interval(max(1, monster.speed - monster.speed_debuff))
    if monster.hp <= 0 or cc_type is not None:
        return

    results, monster_skill, lifesteal_line = dungeon_view._resolve_monster_attack(
        monster, [session], session, None, log_lines, monster_group=session.living_monsters(),
    )
    _, dmg, dodged = results[0]
    verb = f"unleashes **{monster_skill['name']}**" if monster_skill else "strikes back"
    if dodged:
        log_lines.append(f"You dodge **{monster.monster['name']}**'s attack!")
    else:
        dmg = dungeon_view._consume_guard_charge(session, dmg, log_lines)
        log_lines.append(f"**{monster.monster['name']}** {verb} for **{dmg}**.")
        session.hp -= dmg
        dungeon_view._break_sap(session, log_lines)
        if lifesteal_line:
            log_lines.append(lifesteal_line)


def run_combat_room(session: dungeon_view.DelveSession, log_lines: list[str]) -> tuple[str, int]:
    """The real CTB loop (dungeon.preview_next_turns), dispatching to player/monster turn
    resolution, mirroring _advance_solo_turns' own structure and ordering exactly. Returns
    (outcome, turns) where outcome is "won"/"died"/"timeout"."""
    turns = 0
    while turns < ROOM_TURN_CAP:
        if not session.living_monsters():
            return "won", turns

        combatants = [{"id": None, "speed": max(0, session.speed - session.speed_debuff), "clock": session.turn_clock}]
        combatants += [
            {"id": m.slot, "speed": max(0, m.speed - m.speed_debuff), "clock": m.turn_clock}
            for m in session.living_monsters()
        ]
        next_id = dungeon.preview_next_turns(combatants, 1)[0]

        if next_id is None:
            cc_type = dungeon_view._active_cc_type(session)
            dungeon_view._tick_timed_effects([session], log_lines)
            if session.hp <= 0:
                return "died", turns
            if cc_type is not None:
                session.turn_clock += dungeon.turn_interval(max(1, session.speed - session.speed_debuff))
                turns += 1
                continue
            resolve_player_turn(session, log_lines)
        else:
            monster = next(m for m in session.living_monsters() if m.slot == next_id)
            resolve_monster_turn(session, monster, log_lines)

        turns += 1
        if session.hp <= 0:
            return "died", turns

    return "timeout", turns


def resolve_doorman(session: dungeon_view.DelveSession, log_lines: list[str]) -> tuple[str | None, str | None]:
    """Reads The Doorman's own choice-room JSON directly (not hardcoded label strings) -- the action
    carrying a "check" key is "Act Natural" (speed vs DC); success skips straight to its
    on_success.next, failure (or the always-on_success "Beat this nerd up again" a rational player
    never needs) routes to "Fight the Doorman". Returns (fight_outcome, reached_room_id) --
    fight_outcome is None if no fight happened (reached_room_id is then wherever Act Natural's
    success led); if a fight did happen and was lost/timed out, reached_room_id is None."""
    room = session.rooms_by_id["The Doorman"]
    act_natural = next(a for a in room["actions"] if "check" in a)
    check = act_natural["check"]
    stat_value = dungeon_view._stat_value_for_check(session, check["stat"])
    success, _rolled = dungeon.roll_check(stat_value, check["dc"])
    outcome = act_natural["on_success"] if success else act_natural["on_fail"]
    next_room = outcome["next"]
    if next_room != "Fight the Doorman":
        return None, next_room

    enter_combat_room(session, "Fight the Doorman")
    fight_outcome, _turns = run_combat_room(session, log_lines)
    reached = session.rooms_by_id["Fight the Doorman"]["next"] if fight_outcome == "won" else None
    return fight_outcome, reached


# --- Full chained run: realistic cumulative attrition, one delve-scoped Chip pool -----------------

def run_chained_delve(guild_id: int, user_id: int, character: dict, equipped: dict[str, str], delve: dict) -> dict:
    session = build_session(guild_id, user_id, character, equipped, delve)
    log_lines: list[str] = []
    result = {
        "sewer": None, "doorman_fought": False, "doorman_fight": None,
        "bathhouse": None, "private_bath": None, "died_in": None,
    }

    sewer_outcome, _turns = run_combat_room(session, log_lines)
    result["sewer"] = sewer_outcome
    if sewer_outcome != "won":
        result["died_in"] = "The Sewer"
        return _finish_chained(result, session)

    fight_outcome, reached = resolve_doorman(session, log_lines)
    if fight_outcome is not None:
        result["doorman_fought"] = True
        result["doorman_fight"] = fight_outcome
        if fight_outcome != "won":
            result["died_in"] = "Fight the Doorman"
            return _finish_chained(result, session)

    enter_combat_room(session, reached)
    bathhouse_outcome, _turns = run_combat_room(session, log_lines)
    result["bathhouse"] = bathhouse_outcome
    if bathhouse_outcome != "won":
        result["died_in"] = "The Bathhouse"
        return _finish_chained(result, session)

    enter_combat_room(session, "A Private Bath")
    private_bath_outcome, _turns = run_combat_room(session, log_lines)
    result["private_bath"] = private_bath_outcome
    if private_bath_outcome != "won":
        result["died_in"] = "A Private Bath"

    return _finish_chained(result, session)


def _finish_chained(result: dict, session: dungeon_view.DelveSession) -> dict:
    result["final_hp_fraction"] = max(0.0, session.hp) / session.max_hp if session.max_hp else 0.0
    result["final_chips"] = session.chips
    return result


# --- Aggregation & reporting -----------------------------------------------------------------------

def aggregate_room_trials(outcomes: list[str], win_hp_fractions: list[float], turn_counts: list[int]) -> dict:
    n = len(outcomes)
    wins = sum(1 for o in outcomes if o == "won")
    deaths = sum(1 for o in outcomes if o == "died")
    timeouts = sum(1 for o in outcomes if o == "timeout")
    return {
        "trials": n,
        "win_rate": wins / n if n else 0.0,
        "death_rate": deaths / n if n else 0.0,
        "timeout_rate": timeouts / n if n else 0.0,
        "avg_turns": statistics.mean(turn_counts) if turn_counts else 0.0,
        "avg_hp_pct_on_win": statistics.mean(win_hp_fractions) if win_hp_fractions else None,
    }


def simulate_isolated_room(
    guild_id: int, user_id: int, character: dict, equipped: dict[str, str], delve: dict, room_id: str, trials: int,
) -> dict:
    outcomes, hp_fracs, turns_list = [], [], []
    for _ in range(trials):
        session = build_isolated_session(guild_id, user_id, character, equipped, delve, room_id)
        outcome, turns = run_combat_room(session, [])
        outcomes.append(outcome)
        turns_list.append(turns)
        if outcome == "won":
            hp_fracs.append(max(0.0, session.hp) / session.max_hp if session.max_hp else 0.0)
    return aggregate_room_trials(outcomes, hp_fracs, turns_list)


def aggregate_chained_trials(trials: list[dict]) -> dict:
    n = len(trials)
    doorman_fought = sum(1 for t in trials if t["doorman_fought"])
    died_by_room: dict[str, int] = {}
    for t in trials:
        if t["died_in"]:
            died_by_room[t["died_in"]] = died_by_room.get(t["died_in"], 0) + 1
    cleared = n - sum(died_by_room.values())
    return {
        "trials": n,
        "cleared_rate": cleared / n if n else 0.0,
        "act_natural_skip_rate": 1 - (doorman_fought / n) if n else 0.0,
        "doorman_fight_rate": doorman_fought / n if n else 0.0,
        "avg_final_hp_fraction": statistics.mean(t["final_hp_fraction"] for t in trials) if trials else 0.0,
        "avg_final_chips": statistics.mean(t["final_chips"] for t in trials) if trials else 0.0,
        "died_by_room": died_by_room,
    }


def simulate_character(
    guild_id: int, user_id: int, character: dict, equipped: dict[str, str], delve_id: str, trials: int,
    use_persisted_hp: bool = False,
) -> dict:
    delve = dungeon.DELVES[delve_id]
    prepared = character if use_persisted_hp else _full_hp_character(character, equipped)
    isolated = {
        room_id: simulate_isolated_room(guild_id, user_id, prepared, equipped, delve, room_id, trials)
        for room_id in ROOM_ORDER
    }
    chained_trials = [
        run_chained_delve(guild_id, user_id, prepared, equipped, delve) for _ in range(trials)
    ]
    label = f"{user_id} {character['main_class']}/{character['subclass']} Lv{character['level']}"
    return {"user_id": user_id, "label": label, "isolated": isolated, "chained": aggregate_chained_trials(chained_trials)}


def simulate_guild(guild_id: int = SIM_GUILD_ID, delve_id: str = DELVE_ID, trials: int = TRIALS, use_persisted_hp: bool = False) -> list[dict]:
    return [
        simulate_character(guild_id, entry["user_id"], entry["character"], entry["equipped"], delve_id, trials, use_persisted_hp)
        for entry in load_target_characters(guild_id)
    ]


def print_report(rows: list[dict]) -> None:
    for room_id in ROOM_ORDER:
        print(f"\n=== {room_id} (isolated: full HP, full Chips) ===")
        print(f"{'Character':<34}{'Win%':>6}{'Death%':>8}{'Timeout%':>9}{'AvgTurns':>10}{'AvgHP%@Win':>11}")
        for row in rows:
            r = row["isolated"][room_id]
            hp_win = f"{r['avg_hp_pct_on_win'] * 100:.0f}%" if r["avg_hp_pct_on_win"] is not None else "n/a"
            print(
                f"{row['label']:<34}{r['win_rate'] * 100:>5.0f}%{r['death_rate'] * 100:>7.0f}%"
                f"{r['timeout_rate'] * 100:>8.0f}%{r['avg_turns']:>10.1f}{hp_win:>11}"
            )

    print("\n=== Chained run: The Sewer -> The Doorman -> The Bathhouse -> A Private Bath ===")
    print(f"{'Character':<34}{'Cleared%':>9}{'FinalHP%':>9}{'SkipDoorman%':>13}{'AvgChips':>10}  Died in (of N)")
    for row in rows:
        c = row["chained"]
        died_summary = ", ".join(f"{r}:{n}" for r, n in c["died_by_room"].items()) or "-"
        print(
            f"{row['label']:<34}{c['cleared_rate'] * 100:>8.0f}%{c['avg_final_hp_fraction'] * 100:>8.0f}%"
            f"{c['act_natural_skip_rate'] * 100:>12.0f}%{c['avg_final_chips']:>10.1f}  {died_summary}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bidirectional combat simulator for gnome_bathhouse's early rooms, against real casino.db characters."
    )
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument("--guild-id", type=int, default=SIM_GUILD_ID)
    parser.add_argument("--delve-id", default=DELVE_ID)
    parser.add_argument(
        "--use-persisted-hp", action="store_true",
        help="Start each character at its real stored current_hp instead of full effective HP.",
    )
    args = parser.parse_args()
    print_report(simulate_guild(args.guild_id, args.delve_id, args.trials, args.use_persisted_hp))
