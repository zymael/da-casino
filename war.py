"""!war and !raid -- pure rules, no Discord. Players go to war over Luck, which (see db.py's
"luck" column comment) does nothing at all. Every screen treats it as the most precious resource
in the world anyway; that's the joke, so nothing here should ever make luck actually matter.

Reuses what players already own instead of inventing a new resource:
  - Horses are the soldiers. Speed is the charge (how hard, and who strikes first), Endurance is
    how much punishment a horse soaks before it's knocked out, Spirit is the rally chance while
    losing -- the same "burst while behind" idea Spirit already has in horserace.simulate_race.
  - A dungeon character is the general: its effective stats (gear and housing included) give the
    army a % buff, so a heavy-hitting class leads harder charges and a high-DEF one holds the line
    longer. With no healthy horses, the character fights on foot as a one-unit army instead.
  - The defender's ranch facility tier is a home fortress (+HP).
  - Horse clothes with optional war_speed/war_endurance/war_spirit fields are barding.

Knocked-out horses are injured (horserace.is_injured), never stolen -- stealing is !raid's job.
"""

import copy
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import horse_clothes

# Limits -- all stored as unix timestamps in the generic flags table (see db.claim_war_attack /
# db.apply_war_result / db.claim_raid for the keys).
ATTACK_COOLDOWN_SECONDS = 12 * 3600
LOSER_SHIELD_SECONDS = 24 * 3600
RAID_COOLDOWN_SECONDS = 7 * 24 * 3600
WAR_INJURY_HOURS = 24
RAID_INJURY_HOURS = 72

# Spoils -- deliberately a bigger haul than !rub's RUB_LUCK_RANGE (1-5), since war is the
# "serious" way to take luck.
LUCK_SPOILS_RANGE = (5, 15)

# Battle physics. A unit's HP is BASE_HP + Endurance; each strike deals (Speed x jitter + flat
# noise) x DAMAGE_SCALE. FLAT_NOISE_MAX is the same lesson horserace.py's own FLAT_NOISE_MAX
# learned: without a stat-independent term, small stat gaps compound into a sure thing, and a
# war should favor the stronger army without ever being a lock. Tuned so an even 1v1 between two
# average horses is a coin flip that ends in a KO about half the time, a clear stat favorite
# wins ~80%, and outnumbering the enemy is close to decisive.
ROUNDS = 4
BASE_HP = 40
DAMAGE_SCALE = 0.25
JITTER_LOW, JITTER_HIGH = 0.85, 1.15
FLAT_NOISE_MAX = 30
# Each side's whole-battle fortune: one roll per side multiplying every unit's charge for the
# entire war. Unlike per-strike noise (which averages out over a dozen strikes), this doesn't
# wash out -- it's what gives an underdog army a real puncher's chance.
FORTUNE_LOW, FORTUNE_HIGH = 0.6, 1.4
RALLY_CHANCE_MAX = 0.45
RALLY_BONUS_MAX = 0.6
FORTRESS_HP_BONUS_PER_TIER = 0.05
# General bonus: GENERAL_BONUS_PER_POINT per point of the relevant stat, capped -- a level-1
# character barely moves the needle, a maxed one tops out at GENERAL_BONUS_CAP.
GENERAL_BONUS_PER_POINT = 0.005
GENERAL_BONUS_CAP = 0.15
ON_FOOT_SPIRIT = 50

# Raid: base chance shifted by the raider's Speed vs the target's best guard Endurance.
RAID_BASE_CHANCE = 0.30
RAID_STAT_SWING = 0.005
RAID_CHANCE_MIN, RAID_CHANCE_MAX = 0.10, 0.50

ATTACKER, DEFENDER = "attacker", "defender"
WAR_STATS = ("speed", "endurance", "spirit")


@dataclass
class Unit:
    name: str
    horse_index: int | None  # None for a character fighting on foot
    charge: float
    max_hp: float
    spirit: float
    hp: float = 0.0

    def __post_init__(self):
        self.hp = self.max_hp

    @property
    def alive(self) -> bool:
        return self.hp > 0


@dataclass
class Army:
    units: list[Unit]
    on_foot: bool
    general_charge_bonus: float = 0.0
    general_hp_bonus: float = 0.0
    fortress_bonus: float = 0.0
    fortune: float = 1.0  # rolled once per battle by simulate_battle

    @property
    def living(self) -> list[Unit]:
        return [u for u in self.units if u.alive]

    def hp_fraction(self) -> float:
        total = sum(u.max_hp for u in self.units)
        return sum(max(0.0, u.hp) for u in self.units) / total if total else 0.0


@dataclass
class RoundLog:
    first_striker: str
    damage: dict[str, float] = field(default_factory=lambda: {ATTACKER: 0.0, DEFENDER: 0.0})
    rallies: list[tuple[str, str]] = field(default_factory=list)  # (side, unit name)
    knockouts: list[tuple[str, str]] = field(default_factory=list)  # (side the KO'd unit was on, name)


@dataclass
class BattleResult:
    winner: str
    rounds: list[RoundLog]
    attacker: Army
    defender: Army
    # Both armies' state before the first charge, then after each round -- what war_view replays.
    snapshots: list[dict[str, Army]]

    def knocked_out_horses(self) -> list[int]:
        """Every horse_index KO'd on either side -- the casualty list db.apply_war_result injures."""
        return [
            u.horse_index for army in (self.attacker, self.defender) for u in army.units
            if not u.alive and u.horse_index is not None
        ]


def barding_bonuses(worn: dict[str, str]) -> dict[str, int]:
    """Summed war_speed/war_endurance/war_spirit across a horse's equipped clothes ({slot: item_id},
    one entry of db.get_guild_horse_clothes). Clothes without those optional fields add nothing."""
    totals = {stat: 0 for stat in WAR_STATS}
    for item_id in worn.values():
        item = horse_clothes.HORSE_CLOTHES.get(item_id)
        if item is None:
            continue  # defensive: an item removed from the JSON while still equipped
        for stat in WAR_STATS:
            totals[stat] += item.get(f"war_{stat}", 0)
    return totals


def horse_unit(horse_index: int, horse: dict, worn: dict[str, str]) -> Unit:
    barding = barding_bonuses(worn)
    return Unit(
        name=horse["name"],
        horse_index=horse_index,
        charge=horse["speed"] + barding["speed"],
        max_hp=BASE_HP + horse["endurance"] + barding["endurance"],
        spirit=horse["spirit"] + barding["spirit"],
    )


def on_foot_unit(name: str, effective: dict) -> Unit:
    """A character fighting alone -- effective is dungeon.compute_effective_stats' output. HP+DEF
    is halved so a maxed-out character lands around one good legend, not a whole cavalry."""
    return Unit(
        name=name,
        horse_index=None,
        charge=max(effective["atk"], effective["spatk"]),
        max_hp=BASE_HP + (effective["hp"] + effective["def"]) / 2,
        spirit=ON_FOOT_SPIRIT,
    )


def general_bonuses(effective: dict | None) -> tuple[float, float]:
    """(charge_bonus, hp_bonus) a character gives the horses it leads. The better of ATK/Magic
    drives the charge, DEF drives the line -- so each class leans the way it already plays."""
    if effective is None:
        return 0.0, 0.0
    charge = min(GENERAL_BONUS_CAP, max(effective["atk"], effective["spatk"]) * GENERAL_BONUS_PER_POINT)
    hp = min(GENERAL_BONUS_CAP, effective["def"] * GENERAL_BONUS_PER_POINT)
    return charge, hp


def build_army(
    horses: list[tuple[int, dict]], clothes: dict[int, dict[str, str]], character_name: str | None,
    effective: dict | None, fortress_tier: int = 0,
) -> Army | None:
    """horses: (horse_index, horse dict) for every healthy horse fighting. With none, the character
    fights on foot; with no character either, there's no army (None). fortress_tier is only ever
    non-zero for the defender."""
    fortress = fortress_tier * FORTRESS_HP_BONUS_PER_TIER
    if horses:
        charge_bonus, hp_bonus = general_bonuses(effective)
        units = [horse_unit(i, h, clothes.get(i, {})) for i, h in horses]
        on_foot = False
    elif effective is not None:
        charge_bonus, hp_bonus = 0.0, 0.0
        units = [on_foot_unit(character_name, effective)]
        on_foot = True
    else:
        return None
    for unit in units:
        unit.charge *= 1 + charge_bonus
        unit.max_hp *= 1 + hp_bonus + fortress
        unit.hp = unit.max_hp
    return Army(units, on_foot, charge_bonus, hp_bonus, fortress)


def _average_charge(army: Army) -> float:
    living = army.living
    return sum(u.charge for u in living) / len(living) if living else 0.0


def _strike(side: str, army: Army, enemy: Army, behind: bool, log: RoundLog):
    """Every living unit on `side` hits a random living enemy; overflow past a KO carries into
    another random living enemy, so a big charge can roll through a weak line."""
    for unit in army.living:
        if not enemy.living:
            return
        power = unit.charge * random.uniform(JITTER_LOW, JITTER_HIGH) + random.uniform(0, FLAT_NOISE_MAX)
        if behind and random.random() < unit.spirit / 100 * RALLY_CHANCE_MAX:
            power *= 1 + unit.spirit / 100 * RALLY_BONUS_MAX
            log.rallies.append((side, unit.name))
        damage = power * DAMAGE_SCALE
        log.damage[side] += damage
        while damage > 0 and enemy.living:
            target = random.choice(enemy.living)
            dealt = min(damage, target.hp)
            target.hp -= dealt
            damage -= dealt
            if not target.alive:
                log.knockouts.append((DEFENDER if side == ATTACKER else ATTACKER, target.name))


def simulate_battle(attacker: Army, defender: Army) -> BattleResult:
    """Up to ROUNDS rounds. Each round one side strikes first, weighted by each living line's
    average charge -- a faster army usually gets the jump and can knock units out before they
    ever swing back, but not every round. Ends early if a side is wiped; otherwise the side holding the larger share of
    its own HP wins, ties to the defender (holding the field is a win)."""
    armies = {ATTACKER: attacker, DEFENDER: defender}
    for army in armies.values():
        army.fortune = random.uniform(FORTUNE_LOW, FORTUNE_HIGH)
        for unit in army.units:
            unit.charge *= army.fortune
    rounds = []
    snapshots = [copy.deepcopy(armies)]
    for _ in range(ROUNDS):
        attacker_charge, defender_charge = _average_charge(attacker), _average_charge(defender)
        first = ATTACKER if random.random() < attacker_charge / (attacker_charge + defender_charge) else DEFENDER
        second = DEFENDER if first == ATTACKER else ATTACKER
        log = RoundLog(first_striker=first)
        for side, other in ((first, second), (second, first)):
            behind = armies[side].hp_fraction() < armies[other].hp_fraction()
            _strike(side, armies[side], armies[other], behind, log)
        rounds.append(log)
        snapshots.append(copy.deepcopy(armies))
        if not attacker.living or not defender.living:
            break
    if not defender.living:
        winner = ATTACKER
    elif not attacker.living:
        winner = DEFENDER
    else:
        winner = ATTACKER if attacker.hp_fraction() > defender.hp_fraction() else DEFENDER
    return BattleResult(winner, rounds, attacker, defender, snapshots)


def roll_spoils() -> int:
    return random.randint(*LUCK_SPOILS_RANGE)


def injured_until(hours: int) -> str:
    return (datetime.now() + timedelta(hours=hours)).isoformat()


def best_guard_endurance(horses: list[tuple[int, dict]], clothes: dict[int, dict[str, str]]) -> float:
    """The target's single toughest healthy horse (barding included) -- 0 with nobody on guard."""
    return max(
        (h["endurance"] + barding_bonuses(clothes.get(i, {}))["endurance"] for i, h in horses),
        default=0.0,
    )


def raid_chance(raider: dict, raider_worn: dict[str, str], guard_endurance: float) -> float:
    speed = raider["speed"] + barding_bonuses(raider_worn)["speed"]
    chance = RAID_BASE_CHANCE + (speed - guard_endurance) * RAID_STAT_SWING
    return max(RAID_CHANCE_MIN, min(RAID_CHANCE_MAX, chance))
