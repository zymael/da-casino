"""Headless 4-player PARTY combat simulator for balance work -- the party sibling of dungeon_sim.py
(solo, real characters from casino.db) and skill_balance.py (damage output only). Builds synthetic
characters at a chosen level straight from dungeon_classes/subclasses (no DB at all), then runs them
through dungeon_view.py's real combat code: _resolve_player_action for every player action,
_resolve_monster_attack + _after_monster_hit for monster turns, _tick_timed_effects, charm/stun/
intercept/corner handling, and the real CTB turn order (dungeon.preview_next_turns). Only the outer
Discord/DB orchestration (_advance_party_turns, rewards) is replaced by a small synchronous loop.

The one deliberate approximation is the player POLICY (choose(), below): heal anyone under half HP,
spend a class resource only when the fight looks long enough to pay it back (turns_left), taunt now
and then, otherwise cast the highest-expected-damage affordable skill. It is a "sensible player", not
an optimal one -- read results as comparisons between kits/parties, not as absolute win rates.

Use --compare REF to run every party twice, once with dungeon_skills.json as committed at a git ref
(e.g. main) and once with the working copy, to see what a skill-file change actually did.

Known blind spot: damage dealt on a MONSTER's turn (hex detonations, DoT ticks, Counter) isn't
credited to any member's "dmg" column, though it still counts toward the win and fight length.

    python party_sim.py                               # normal boss + trash fights, level 40
    python party_sim.py --fights allin --gear 1.5     # All In tiers 1-3 with a rough gear bump
    python party_sim.py --compare main --trials 100   # old kit (main) vs working copy
    python party_sim.py --start-chips 0.2             # start low on Chips, like late in a delve
"""

import argparse
import os
import random
import statistics
import subprocess
import tempfile

import all_in
import dungeon
import dungeon_view as dv

TURN_SLOT_CAP = 400  # safety cap on total turn-slots (both sides) in one fight -> counted as a loss
CHIP_RESERVE = 20  # below this many chips, only cheap skills -- chips last the whole delve, not one fight
# The reserve in effect: CHIP_RESERVE normally, 0 with --start-chips below 1 (a late-delve fight,
# where there's nothing left to save Chips for).
chip_reserve = CHIP_RESERVE

PARTIES = {
    "A Jinx/Mechanic/Muscle/Chaplain": [("mage", "spades"), ("mage", "diamonds"), ("fighter", "clubs"), ("healer", "hearts")],
    "B Shark/Minder/Hitman/Pusher": [("fighter", "spades"), ("fighter", "hearts"), ("rogue", "spades"), ("healer", "diamonds")],
    "C Collector/Wildcard/Enchanter/Fixer": [("fighter", "diamonds"), ("mage", "clubs"), ("mage", "hearts"), ("healer", "spades")],
    "D Cutman/BarFighter/Heartbreaker/Mastermind": [("healer", "clubs"), ("rogue", "clubs"), ("rogue", "hearts"), ("rogue", "diamonds")],
}
# fight name -> list of monster ids, or "allin:<tier>" for the All In benchmark boss at that tier
FIGHT_SETS = {
    "normal": {"boss Old God Pizza": ["old_god_pizza"], "trash 3x Pizza Cultist": ["pizza_cultist"] * 3},
    "allin": {f"All In {t} boss": f"allin:{t}" for t in (1, 2, 3)},
}


# --- Skill sets ---------------------------------------------------------------------------------

def load_skills_at_ref(ref: str) -> dict:
    """dungeon_skills.json as committed at git `ref`, loaded through the real loader."""
    raw = subprocess.run(
        ["git", "show", f"{ref}:dungeon_skills.json"], capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(raw)
    try:
        return dungeon._load_skills(f.name)
    finally:
        os.unlink(f.name)


# --- Setup ----------------------------------------------------------------------------------------

def make_member(main_class: str, subclass: str, uid: int, level: int, gear: float) -> dv.PartyMember:
    """A synthetic level-`level` character; `gear` multiplies HP/ATK/DEF/Magic as a stand-in for
    equipment (speed untouched, matching how All In gear barely moves it). Call with dungeon.SKILLS
    already pointing at the skill set under test -- PartyMember reads its unlocked skills from it."""
    ch = dict(dungeon.compute_stats_at_level(main_class, subclass, level))
    ch.update(main_class=main_class, subclass=subclass, level=level, current_hp=10**6)
    for stat in ("hp", "atk", "def", "spatk"):
        ch[stat] = round(ch[stat] * gear)
    member = dv.PartyMember(0, uid, f"{main_class[:3]}{subclass[:2]}", ch, {}, uid == 1)
    member.dealt = 0
    member.own_turns = 0
    return member


def make_monsters(spec, party_size: int) -> list:
    """The fight's monsters, scaled the way a real party fight scales them (party_hp_multiplier;
    plus All In's tier scaling and per-member power for an "allin:<tier>" spec)."""
    hp_mult = dungeon.party_hp_multiplier(party_size)
    if isinstance(spec, str) and spec.startswith("allin:"):
        tier = int(spec.split(":")[1])
        delve = dungeon.DELVES[all_in.settings()["benchmark_delve"]]
        boss_id = next(
            m for room in delve["rooms"] if room["id"] == all_in.boss_room_id(delve)
            for g in room["monster_groups"] for m in g["monsters"]
        )
        scaled = all_in.scale_monster(dungeon.MONSTERS[boss_id], tier, all_in.delve_factor(delve))
        power = all_in.party_monster_mult(party_size)
        inst = dv.MonsterInstance(scaled, 0)
        inst.max_hp = inst.hp = round(scaled["hp"] * hp_mult * power)
        inst.atk, inst.spatk = round(inst.atk * power), round(inst.spatk * power)
        monsters = [inst]
    else:
        monsters = []
        for slot, monster_id in enumerate(spec):
            inst = dv.MonsterInstance(dungeon.MONSTERS[monster_id], slot)
            inst.max_hp = inst.hp = round(inst.monster["hp"] * hp_mult)
            monsters.append(inst)
    dv._finalize_monster_group(monsters)
    return monsters


class Fight:
    def __init__(self, members: list, monsters: list):
        self.members, self.monsters = members, monsters

    def living_members(self):
        return [m for m in self.members if not m.knocked_out]

    def living_monsters(self):
        return [m for m in self.monsters if m.hp > 0]


# --- Policy ---------------------------------------------------------------------------------------

def effects_of(skill: dict) -> list[dict]:
    return [e for group in dungeon._effect_lists(skill) for e in group]


def has(skill: dict, effect_type: str) -> bool:
    return any(e["type"] == effect_type for e in effects_of(skill))


def is_refuel_skill(skill: dict) -> bool:
    """A free skill that gives its caster Chips back (Loose Change, Palm a Chip, Workman's Comp)."""
    return skill["chip_cost"] == 0 and any(
        e["type"] == "workmans_comp" or (e["type"] == "chip_gain" and e.get("target") == "self")
        for e in effects_of(skill)
    )


def is_damage_skill(skill: dict) -> bool:
    return any(e["type"] in dv.DAMAGE_EFFECT_TYPES for e in effects_of(skill))


def base_damage(skill: dict, actor) -> float:
    """Expected pre-mitigation damage of one cast, averaged over effect_groups and per-effect chance."""
    groups = dungeon._effect_lists(skill)
    total = 0.0
    for group in groups:
        mult, extras, hits = 1.0, 0.0, False
        for e in group:
            p = e.get("chance", 1.0)
            if e["type"] == "damage_multiplier":
                mult *= e["value"] * p + (1 - p)
                hits = True
            elif e["type"] == "extra_attack":
                extras += e.get("multiplier", 1.0) * p
                hits = True
        if hits:
            total += (mult + extras) / len(groups)
    return total * (actor.spatk if skill.get("special") else actor.atk)


def resource_bonus(skill: dict, actor, target) -> float:
    """The multiplier a resource would add to this skill's hit right now (meters, Tells, Pot, Charge)."""
    bonus = 1.0
    for e in effects_of(skill):
        meter = dv._METER_SPEND_TYPES.get(e["type"])
        if meter:
            bonus *= 1 + e["per_point"] * actor.meters.get(meter, 0)
        elif e["type"] == "tells_spend":
            bonus *= 1 + e["per_point"] * target.tells.get(actor.user_id, 0)
        elif e["type"] == "pot_cash":
            bonus *= 1 + actor.pot / 2  # only the winning half of a coin toss carries it
    if actor.charge and skill.get("special") and is_damage_skill(skill):
        bonus *= actor.charge["mult"]
    return bonus


def score(skill: dict, actor, target, n_enemies: int) -> float:
    aoe = any(e.get("aoe") for e in effects_of(skill) if e["type"] in dv.DAMAGE_EFFECT_TYPES)
    return base_damage(skill, actor) * (n_enemies if aoe else 1) * resource_bonus(skill, actor, target)


def turns_left(fight: Fight) -> float:
    """Rough rounds of party output left in this fight -- what decides whether a setup action
    (charge, hex, contract...) can still pay for the turn it costs."""
    enemies = fight.living_monsters()
    avg_def = sum(e.def_ for e in enemies) / len(enemies)
    per_round = sum(
        max([base_damage(s, m) for s in m.unlocked_skills] + [m.atk]) * 25 / (25 + avg_def)
        for m in fight.living_members()
    )
    return sum(e.hp for e in enemies) / max(1.0, per_round)


def choose(actor, fight: Fight):
    """(skill or None for a plain Attack, ally target, enemy target)."""
    skills = [s for s in actor.unlocked_skills if s["chip_cost"] <= actor.chips]
    if actor.chips < chip_reserve:
        skills = [s for s in skills if s["chip_cost"] <= 5]
    enemies = fight.living_monsters()
    allies = fight.living_members()
    target = min(enemies, key=lambda m: m.hp)
    actor.own_turns += 1

    hurt = min(allies, key=lambda m: m.hp / m.max_hp)
    if hurt.hp / hurt.max_hp < 0.5:
        heal = next((s for s in skills if has(s, "heal_fraction")), None)
        if heal:
            return heal, hurt, target

    left = turns_left(fight)
    # Refuel only when broke: no paid skill is affordable, and the fight runs long enough to spend it.
    paid_costs = [s["chip_cost"] for s in actor.unlocked_skills if s["chip_cost"] > 0]
    if paid_costs and actor.chips < min(paid_costs) and left >= 2:
        # Drawing fire for Workman's Comp is for when you can take it.
        refuel = next((s for s in skills if is_refuel_skill(s)
                       and (not has(s, "workmans_comp") or actor.hp >= actor.max_hp / 2)), None)
        if refuel is not None:
            return refuel, actor, target
    single = len(enemies) == 1
    others = [a for a in allies if a is not actor]
    for s in skills:
        if has(s, "charge") and single:
            e = next(e for e in effects_of(s) if e["type"] == "charge")
            stacks = actor.charge["stacks"] if actor.charge and actor.charge["kind"] == "charge" else 0
            safe = min(e.get("max_stacks", 1), e.get("safe_stacks", e.get("max_stacks", 1)))
            if stacks < safe and left >= 3 + stacks:
                return s, actor, target
        if has(s, "overload") and single and left >= 3:
            ch = actor.charge
            if ch is None or (ch["kind"] == "overload" and ch["mult"] < 2.0 and ch["rolls"] < 2):
                return s, actor, target
        if has(s, "hex") and left >= 4:
            big = max(enemies, key=lambda m: m.hp)
            if not any(e["type"] == "hex" for e in big.timed_effects):
                return s, actor, big
        if has(s, "hype_spend"):
            hype = actor.meters.get("hype", 0)
            if hype >= 6 or (left < 2 and hype >= 2):
                return s, actor, target
        if has(s, "intercept") and others and actor.own_turns % 3 == 1:
            low = min(others, key=lambda a: a.hp / a.max_hp)
            if low.hp / low.max_hp < 0.7:
                return s, low, target
        if has(s, "corner") and others and left >= 4 and not getattr(actor, "cornered_once", False):
            actor.cornered_once = True
            return s, min(others, key=lambda a: a.max_hp), target
        if has(s, "hook") and left >= 3 and not any(e["type"] == "hook" for e in target.timed_effects):
            return s, actor, target
        if has(s, "contract") and target.contract is None and left >= 3:
            return s, actor, target
        if has(s, "plate_spend") and (actor.plate >= actor.max_hp * 0.5 or (left < 2 and actor.plate)):
            return s, actor, target
    for s in skills:
        if has(s, "taunt") and actor.own_turns % 4 == 1:
            return s, actor, target
    damage = [s for s in skills if is_damage_skill(s)]
    best = max(damage, key=lambda s: score(s, actor, target, len(enemies)), default=None)
    if best is not None and score(best, actor, target, len(enemies)) > actor.atk:
        return best, actor, target
    return None, actor, target


# --- Turns ----------------------------------------------------------------------------------------

def player_turn(actor, fight: Fight) -> None:
    skill, ally, target = choose(actor, fight)
    before = {id(m): m.hp for m in fight.monsters}
    effects = [] if skill is None else dungeon.resolve_cast_effects(skill)
    if skill is not None:
        actor.chips -= skill["chip_cost"]
    dv._resolve_player_action(
        actor, fight.living_members(), fight.living_monsters(), target, effects,
        bool(skill and skill.get("special")), "hits", "x", "x", "drains", 1.0, [], True, [],
        ally_target=ally, is_plain_attack=skill is None,
    )
    actor.dealt += sum(max(0, before[id(m)] - max(m.hp, 0)) for m in fight.monsters)
    actor.turn_clock += dungeon.turn_interval(max(1, actor.speed - actor.speed_debuff))


def monster_turn(monster, fight: Fight) -> None:
    """Mirrors _advance_party_turns' monster branch: cc/charm read before the tick, threat or taunt
    picks the target, a covering Minder steps in, then the real attack and its on-hit reactions."""
    log: list[str] = []
    cc = dv._active_cc_type(monster)
    charmed = any(e["type"] == "charm" for e in monster.timed_effects)
    dv._tick_timed_effects([monster], log)
    monster.turn_clock += dungeon.turn_interval(max(1, monster.speed - monster.speed_debuff))
    if monster.hp <= 0 or cc:
        return
    if charmed:
        dv._charmed_turn(monster, fight.living_monsters(), log)
        return
    living = fight.living_members()
    if not living:
        return
    forced, monster.forced_target_id = monster.forced_target_id, None
    if any(m.user_id == forced for m in living):
        target_id = forced
    else:
        target_id = dungeon.pick_target_by_threat([{"id": m.user_id, "threat": monster.threat.get(m.user_id, 0)} for m in living])
    target = next(m for m in living if m.user_id == target_id)
    protector, target.protector = target.protector, None
    if protector is not None and not protector.knocked_out and protector is not target:
        protector.intercepting = getattr(protector, "intercept_value", 0.5)
        target = protector
    results, *_ = dv._resolve_monster_attack(monster, living, target, None, log, monster_group=fight.living_monsters())
    for hit_target, dmg, dodged in results:
        if dodged:
            dv._gain_meter(fight.living_members(), "hype")
            continue
        dmg = dv._consume_guard_charge(hit_target, dmg, log)
        hit_target.hp -= dmg
        dv._on_damage_taken(hit_target, dmg, log)
        dv._after_monster_hit(monster, hit_target, dmg, fight.living_members(), log)
        dv._try_corner(hit_target, log)
        if hit_target.hp <= 0:
            hit_target.knocked_out = True


def use_skills(skills: dict) -> None:
    """Points the game at a skill set. Characters read their kit through dungeon.SKILLS_BY_COMBO
    (dungeon.unlocked_skills), not dungeon.SKILLS, so both have to be swapped together."""
    dungeon.SKILLS = skills
    dungeon.SKILLS_BY_COMBO = dungeon._build_skills_by_combo(skills)


def run_fight(builds, monster_spec, skills: dict, level: int, gear: float, start_chips: float = 1.0) -> dict:
    use_skills(skills)
    members = [make_member(mc, sc, i + 1, level, gear) for i, (mc, sc) in enumerate(builds)]
    for m in members:
        m.chips = round(m.max_chips * start_chips)
    fight = Fight(members, make_monsters(monster_spec, len(members)))
    for m in fight.monsters:
        m.turn_clock = dungeon.turn_interval(max(1, m.speed))  # no free first hit, same as a real spawn
    slots = 0
    while fight.living_monsters() and fight.living_members() and slots < TURN_SLOT_CAP:
        slots += 1
        combatants = [
            {"id": ("p", m.user_id), "speed": max(0, m.speed - m.speed_debuff), "clock": m.turn_clock}
            for m in fight.living_members()
        ] + [
            {"id": ("m", m.slot), "speed": max(0, m.speed - m.speed_debuff), "clock": m.turn_clock}
            for m in fight.living_monsters()
        ]
        kind, cid = dungeon.preview_next_turns(combatants, 1)[0]
        if kind == "m":
            monster_turn(next(m for m in fight.monsters if m.slot == cid), fight)
            continue
        actor = next(m for m in members if m.user_id == cid)
        cc = dv._active_cc_type(actor)
        dv._tick_timed_effects([actor], [], owned_on=fight.living_monsters())
        dv._try_corner(actor, [])
        if actor.hp <= 0:
            actor.knocked_out = True
        elif not fight.living_monsters():
            continue
        elif cc:
            actor.turn_clock += dungeon.turn_interval(max(1, actor.speed - actor.speed_debuff))
        else:
            player_turn(actor, fight)
    return {
        "won": not fight.living_monsters(), "slots": slots,
        "kos": sum(m.knocked_out for m in members), "dealt": {m.player_name: m.dealt for m in members},
    }


def summarize(builds, monster_spec, skills, trials, level, gear, start_chips=1.0) -> dict:
    runs = [run_fight(builds, monster_spec, skills, level, gear, start_chips) for _ in range(trials)]
    won = [r for r in runs if r["won"]]
    names = runs[0]["dealt"].keys()
    return {
        "win": len(won) / trials,
        "slots": statistics.mean(r["slots"] for r in won) if won else float("nan"),
        "kos": statistics.mean(r["kos"] for r in runs),
        "dealt": {n: statistics.mean(r["dealt"][n] for r in runs) for n in names},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--level", type=int, default=40)
    parser.add_argument("--gear", type=float, default=1.0, help="multiplier on HP/ATK/DEF/Magic standing in for equipment")
    parser.add_argument("--fights", choices=sorted(FIGHT_SETS), default="normal")
    parser.add_argument("--compare", metavar="REF", help="also run the dungeon_skills.json committed at this git ref")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--start-chips", type=float, default=1.0,
                        help="fraction of max Chips each member starts with (Chips carry across a delve's fights)")
    args = parser.parse_args()
    global chip_reserve
    chip_reserve = CHIP_RESERVE if args.start_chips >= 1 else 0

    random.seed(args.seed)
    kits = [("current", dict(dungeon.SKILLS))]
    if args.compare:
        kits.insert(0, (args.compare, load_skills_at_ref(args.compare)))
    width = max(len(k) for k, _ in kits)
    for fight_name, spec in FIGHT_SETS[args.fights].items():
        print(f"\n######## {fight_name}  (level {args.level}, gear x{args.gear:g}, chips x{args.start_chips:g}, {args.trials} trials)")
        for party_name, builds in PARTIES.items():
            print(f"\n{party_name}")
            for kit_name, skills in kits:
                s = summarize(builds, spec, skills, args.trials, args.level, args.gear, args.start_chips)
                dealt = "  ".join(f"{n} {v:5.0f}" for n, v in s["dealt"].items())
                print(f"  {kit_name:<{width}}: win {s['win']:4.0%}  turn-slots {s['slots']:5.1f}  KOs {s['kos']:.2f}  | dmg: {dealt}")


if __name__ == "__main__":
    main()
