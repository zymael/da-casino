"""Headless 1v1 DUEL simulator for PvP balance work -- the duel sibling of party_sim.py. Builds every
class/subclass at one level straight from the class/subclass JSON (no DB, no gear, no housing,
exactly like a real duel), then plays a round-robin through dungeon_view.py's real duel code:
_resolve_player_action with the duel's crit chance and pvp=True, _tick_timed_effects, _try_corner,
and the real CTB turn order (dungeon.preview_next_turns). Only the Discord orchestration
(_advance_duel_turns/_resolve_duel_turn's messaging, wagers, ratings) is replaced by a small loop,
which mirrors theirs step for step: keep it in sync when the duel turn flow changes.

The player policy is party_sim.choose() with no Chip reserve (a duel is one fight), plus Goading
whenever it can (see choose): a "sensible player", not an optimal one. Read results as comparisons between builds, not true win rates.
Seats alternate every trial, so going first isn't baked into any matchup.

    python duel_sim.py                    # level 40, 200 duels per pairing
    python duel_sim.py --matrix           # also print the full 16x16 matchup grid
    python duel_sim.py --level 20 --trials 400
    python duel_sim.py --set stat_pull_hp=0.75 --set damage_multiplier=0.6   # try settings
"""

import argparse
import json
import os
import random
import statistics

import duel
import dungeon
import dungeon_view as dv
import party_sim as ps

TURN_SLOT_CAP = 400  # safety cap on one duel's total turns -> counted as a draw
_BUILDS_PATH = os.path.join(os.path.dirname(__file__), "dungeon_class_builds.json")


def builds() -> list[tuple[str, str]]:
    return [(mc, sc) for mc in dungeon.CLASSES for sc in dungeon.SUBCLASSES]


def build_names() -> dict:
    with open(_BUILDS_PATH, encoding="utf-8") as f:
        return {(b["main_class"], b["subclass"]): b["display_name"].replace("The ", "") for b in json.load(f)}


def make_duelist(main_class: str, subclass: str, uid: int, level: int) -> dv.PartyMember:
    """A fresh level-`level` duelist, mirroring DuelChallengeView.accept_button: no gear, no housing,
    then the real duel rules (dungeon_view._apply_duel_rules: stat pull, duel skills, full HP)."""
    member = ps.make_member(main_class, subclass, uid, level, 1.0)
    dv._apply_duel_rules(member, level)
    return member


def _best_hit(who, target, skills) -> float:
    """Rough damage of `who`'s best option this turn: its best affordable damage skill or a plain Attack."""
    affordable = [s for s in skills if s["chip_cost"] <= who.chips and ps.is_damage_skill(s)]
    return max([ps.score(s, who, target, 1) for s in affordable] + [ps.attack_power(who)])


def choose(actor, foe):
    """party_sim.choose, plus the duel-only call a sensible player makes: Goad (a taunt skill) when
    the opponent can be Goaded and what it denies them (their best skill over a plain Attack) beats
    what it costs us (our best hit over the taunt skill's own damage). Goaded turns are Attack only."""
    if actor.attack_only:
        actor.own_turns += 1
        return None
    # In Your Corner on yourself, once, while there's still a fight ahead (party_sim only ever
    # corners an ally).
    if actor.corner is None and not actor.corner_spent and actor.hp > actor.max_hp / 2:
        corner = next((s for s in actor.unlocked_skills if s["chip_cost"] <= actor.chips and ps.has(s, "corner")), None)
        if corner is not None:
            actor.own_turns += 1
            return corner
    # A stun or charm takes a whole turn from them (_duel_turn_skip_line): worth it when that turn's
    # best hit beats what we give up casting it. Never one that stuns us too.
    if dv._duel_turn_skip_line(foe) is None and not foe.lost_last_turn:
        skippers = [
            s for s in actor.unlocked_skills
            if s["chip_cost"] <= actor.chips
            and any(e["type"] in ("stun", "charm") and e.get("target") != "self" for e in ps.effects_of(s))
            and not any(e["type"] == "stun" and e.get("target") == "self" for e in ps.effects_of(s))
        ]
        if skippers:
            own = lambda s: ps.score(s, actor, foe, 1) if ps.is_damage_skill(s) else 0.0
            skip = max(skippers, key=own)
            if _best_hit(foe, actor, foe.unlocked_skills) > _best_hit(actor, foe, actor.unlocked_skills) - own(skip):
                actor.own_turns += 1
                return skip
    if not (foe.goaded or foe.goad_immune):
        taunts = [s for s in actor.unlocked_skills if s["chip_cost"] <= actor.chips and ps.has(s, "taunt")]
        if taunts:
            taunt = max(taunts, key=lambda s: ps.score(s, actor, foe, 1) if ps.is_damage_skill(s) else 0.0)
            denied = _best_hit(foe, actor, foe.unlocked_skills) - ps.attack_power(foe)
            cost = _best_hit(actor, foe, actor.unlocked_skills) - (ps.score(taunt, actor, foe, 1) if ps.is_damage_skill(taunt) else 0.0)
            if denied > cost:
                actor.own_turns += 1
                return taunt
    return ps.choose(actor, _View(actor, foe))[0]


class _View:
    """What party_sim.choose expects, from one duelist's point of view."""

    def __init__(self, me, foe):
        self.me, self.foe = me, foe

    def living_members(self):
        return [self.me]

    def living_monsters(self):
        return [self.foe] if self.foe.hp > 0 else []


def run_duel(a_build, b_build, level: int) -> float:
    """1.0 if the first build wins, 0.0 if the second does, 0.5 for a draw or a stalled duel."""
    a = make_duelist(*a_build, 1, level)
    b = make_duelist(*b_build, 2, level)
    for _ in range(TURN_SLOT_CAP):
        combatants = [{"id": d.user_id, "speed": max(0, d.speed - d.speed_debuff), "clock": d.turn_clock} for d in (a, b)]
        actor, foe = (a, b) if dungeon.preview_next_turns(combatants, 1)[0] == 1 else (b, a)
        # _advance_duel_turns: cc read before the tick, then the tick, then the corner check.
        cc = dv._duel_turn_skip_line(actor)
        dv._tick_timed_effects([actor], [], owned_on=[foe])
        for d in (actor, foe):
            dv._try_corner(d, [])
        if a.hp <= 0 or b.hp <= 0:
            break
        if cc is not None:
            actor.lost_last_turn = True
            actor.turn_clock += dungeon.turn_interval(max(1, actor.speed - actor.speed_debuff))
            continue
        # _handle_duel_action + _resolve_duel_turn
        dv._begin_duel_turn(actor)
        skill = choose(actor, foe)
        effects = [] if skill is None else dungeon.resolve_cast_effects(skill)
        if skill is not None:
            actor.chips -= skill["chip_cost"]
        dv._resolve_player_action(
            actor, [actor], [foe], foe, effects, bool(skill and skill.get("special")), "hits",
            actor.label, f"{actor.label}'s", "drains", duel.damage_multiplier(), [], False, [],
            is_plain_attack=skill is None, crit_chance=duel.crit_chance(), pvp=True,
        )
        actor.turn_clock += dungeon.turn_interval(max(1, actor.speed - actor.speed_debuff))
        for d in (actor, foe):
            dv._try_corner(d, [])
        if a.hp <= 0 or b.hp <= 0:
            break
    if (a.hp <= 0) == (b.hp <= 0):
        return 0.5
    return 1.0 if b.hp <= 0 else 0.0


def round_robin(level: int, trials: int) -> tuple[list, list[list[float]]]:
    """(builds, matrix) where matrix[i][j] is build i's average score against build j."""
    ps.chip_reserve = 0
    bs = builds()
    n = len(bs)
    matrix = [[0.5] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            total = 0.0
            for t in range(trials):
                if t % 2 == 0:
                    total += run_duel(bs[i], bs[j], level)
                else:
                    total += 1 - run_duel(bs[j], bs[i], level)
            matrix[i][j] = total / trials
            matrix[j][i] = 1 - matrix[i][j]
    return bs, matrix


def report(bs, matrix, show_matrix: bool) -> None:
    names = build_names()
    n = len(bs)
    field = [sum(matrix[i][j] for j in range(n) if j != i) / (n - 1) for i in range(n)]
    by_class: dict[str, list[float]] = {}
    for i, (mc, _) in enumerate(bs):
        by_class.setdefault(mc, []).append(field[i])
    lopsided = sum(1 for i in range(n) for j in range(n) if i != j and matrix[i][j] >= 0.9)
    print(f"spread (stdev) {statistics.pstdev(field):.3f}   range {min(field):.0%}-{max(field):.0%}   "
          f"matchups won >=90%: {lopsided}")
    print("by class: " + "  ".join(f"{c} {statistics.mean(v):.0%}" for c, v in by_class.items()))
    print()
    for i in sorted(range(n), key=lambda k: -field[k]):
        print(f"  {names[bs[i]]:<13} {bs[i][0]:<8} {field[i]:5.1%}")
    if show_matrix:
        short = [names[b][:5] for b in bs]
        print("\n" + " " * 14 + " ".join(f"{s:>5}" for s in short))
        for i in range(n):
            print(f"{names[bs[i]]:<13} " + " ".join("  -  " if i == j else f"{matrix[i][j]:5.0%}" for j in range(n)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--trials", type=int, default=200, help="duels per pairing")
    parser.add_argument("--level", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--matrix", action="store_true", help="print the full matchup grid")
    parser.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                        help="try a dungeon_duel.json value without editing the file, e.g. --set stat_pull_hp=0.5")
    args = parser.parse_args()
    for pair in args.set:
        field, value = pair.split("=")
        if field not in duel.settings():
            parser.error(f"unknown duel setting {field!r}")
        duel.settings()[field] = float(value)
    random.seed(args.seed)
    print(f"Duel round-robin: level {args.level}, {args.trials} duels per pairing")
    print("duel settings: " + ", ".join(f"{k}={v}" for k, v in duel.settings().items() if k != "id"))
    report(*round_robin(args.level, args.trials), args.matrix)


if __name__ == "__main__":
    main()
