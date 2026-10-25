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

!raid is a menu of covert operations (OPERATIONS below), each its own stat matchup, cooldown and
payoff. Every op's odds share one shape (op_chance): a base shifted by the operative's stat vs the
target's guard stat, minus the operative's Heat (ops run recently) and the target's sentry. A
sentry is one horse a player stations on watch: it guards against ops but sits out wars.
"""

import copy
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import horse_clothes

# Limits -- all stored as unix timestamps in the generic flags table (see db.claim_war_attack /
# db.apply_war_result / db.claim_op for the keys). Each raid op's own cooldown is on OPERATIONS.
ATTACK_COOLDOWN_SECONDS = 12 * 3600
LOSER_SHIELD_SECONDS = 24 * 3600
WAR_INJURY_HOURS = 24
RAID_INJURY_HOURS = 72  # a caught horse thief or rescuer

# Spoils -- deliberately a bigger haul than !rub's RUB_LUCK_RANGE (1-5), since war is the
# "serious" way to take luck.
LUCK_SPOILS_RANGE = (5, 15)

# Horses per side in one war. The attacker picks up to this many; a defender with more healthy
# horses than this fields a random selection of them, so a big stable isn't an unbreakable wall.
MAX_ARMY_SIZE = 5

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

# Raid operations. Every op's chance moves OP_STAT_SWING per point of (operative stat - guard
# stat), then loses HEAT_PENALTY per distinct op the player ran in the last HEAT_WINDOW_SECONDS
# and SENTRY_PENALTY if the target has a sentry on duty, clamped to the op's own [min, max].
OP_STAT_SWING = 0.005
HEAT_WINDOW_SECONDS = 72 * 3600
HEAT_PENALTY = 0.05
SENTRY_PENALTY = 0.10
# A target hit by an op with `immunity` can't be hit by that same op again, by anyone, for this long.
OP_IMMUNITY_SECONDS = 48 * 3600

HITMAN_INJURY_HOURS = 48
HITMAN_LOCKOUT_SECONDS = 24 * 3600  # a caught hitman lies low: no !war and no ops
SMUGGLER_INJURY_HOURS = 24
SMUGGLE_FRACTION, SMUGGLE_CAP = 0.05, 500
ARSON_SECONDS = 48 * 3600
ARSON_GUARD_PER_TIER = 10  # each facility tier adds this much to the arsonist's Endurance check
BAIL_FRACTION, BAIL_CAP = 0.05, 300
PROPAGANDA_SECONDS = 48 * 3600
PROPAGANDA_SPIRIT_PENALTY = 20
INTEL_SECONDS = 24 * 3600
INTEL_FORTUNE_LOW = 0.8  # replaces FORTUNE_LOW for an army marching on a spy's intel
JOB_INTEL_BONUS = 0.15  # a spy's intel on the next (non-spy) job against that target
RESCUE_WINDOW_SECONDS = 7 * 24 * 3600
# Pile-on guard: a target hit by PILE_ON_HITS successful jobs (from anyone) within the window is on
# high alert against every job until the oldest of those hits ages out. Spy and Rescue are exempt
# both ways -- spying is covert, and a rescue is a victim taking back their own horse.
PILE_ON_WINDOW_SECONDS = 24 * 3600
PILE_ON_EXEMPT = frozenset({"spy", "rescue"})


@dataclass(frozen=True)
class Operation:
    id: str
    name: str
    emoji: str
    blurb: str  # the op picker's one-line summary
    operative: str  # "horse" or "character"
    op_stat: str  # horse: speed/endurance/spirit; character: atk/spatk, or "power" (the better of the two)
    guard_stat: str  # the target's guard horse stat it's checked against
    base: float
    min_chance: float
    max_chance: float
    cooldown_seconds: int
    immunity: bool = False

    @property
    def last_flag(self) -> str:
        # The thief keeps the original !raid's key so cooldowns running at deploy time carry over.
        return "raid:last" if self.id == "thief" else f"raid:{self.id}:last"

    @property
    def immune_flag(self) -> str:
        return f"ops:{self.id}:immune_until"

    @property
    def caught_flag(self) -> str:
        """When this player last got caught running this op -- what Heat counts."""
        return f"raid:{self.id}:caught"


OPERATIONS: dict[str, Operation] = {op.id: op for op in (
    Operation("spy", "Spy", "🕵️", "Case their stable. Speed vs Spirit.",
              "horse", "speed", "spirit", 0.60, 0.30, 0.85, 12 * 3600),
    Operation("thief", "Horse Thief", "🥷", "Steal a horse. Speed vs Endurance.",
              "horse", "speed", "endurance", 0.30, 0.10, 0.50, 7 * 24 * 3600, immunity=True),
    Operation("hitman", "Hitman", "🔪", "Your character injures one of their horses. ATK/Magic vs Endurance.",
              "character", "power", "endurance", 0.35, 0.15, 0.60, 3 * 24 * 3600, immunity=True),
    Operation("smuggler", "Smuggler", "💰", "Lift some of their flakes. Speed vs Spirit.",
              "horse", "speed", "spirit", 0.40, 0.15, 0.65, 2 * 24 * 3600, immunity=True),
    Operation("arsonist", "Arsonist", "🔥", "Your character torches their fortress. ATK vs Endurance + facility.",
              "character", "atk", "endurance", 0.30, 0.10, 0.55, 5 * 24 * 3600, immunity=True),
    Operation("propaganda", "Propaganda", "🪧", "Your character sours their army's Spirit. Magic vs Spirit.",
              "character", "spatk", "spirit", 0.40, 0.15, 0.65, 2 * 24 * 3600),
    Operation("rescue", "Rescue", "🛟", "Take back a horse stolen from you this week. Speed vs Endurance.",
              "horse", "speed", "endurance", 0.50, 0.25, 0.80, 0),
)}

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
    fortune_low: float = FORTUNE_LOW  # raised to INTEL_FORTUNE_LOW by a spy's intel
    spirit_penalty: float = 0.0  # PROPAGANDA_SPIRIT_PENALTY while a propaganda op is live

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
    effective: dict | None, fortress_tier: int = 0, intel: bool = False, propaganda: bool = False,
) -> Army | None:
    """horses: (horse_index, horse dict) for every healthy horse fighting. With none, the character
    fights on foot; with no character either, there's no army (None). fortress_tier is only ever
    non-zero for the defender. intel/propaganda are the two !raid ops that carry into a war."""
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
    spirit_penalty = PROPAGANDA_SPIRIT_PENALTY if propaganda else 0.0
    for unit in units:
        unit.charge *= 1 + charge_bonus
        unit.max_hp *= 1 + hp_bonus + fortress
        unit.hp = unit.max_hp
        unit.spirit = max(0.0, unit.spirit - spirit_penalty)
    return Army(
        units, on_foot, charge_bonus, hp_bonus, fortress,
        fortune_low=INTEL_FORTUNE_LOW if intel else FORTUNE_LOW, spirit_penalty=spirit_penalty,
    )


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
        army.fortune = random.uniform(army.fortune_low, FORTUNE_HIGH)
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


def horse_stat(horse: dict, worn: dict[str, str], stat: str) -> float:
    """A horse's speed/endurance/spirit with its barding added."""
    return horse[stat] + barding_bonuses(worn)[stat]


def character_stat(effective: dict, stat: str) -> float:
    """An op's character stat off dungeon.compute_effective_stats' output. "power" is the better
    of ATK and Magic, same as general_bonuses' charge."""
    return max(effective["atk"], effective["spatk"]) if stat == "power" else effective[stat]


def guard_stat(
    op: Operation, sentry: tuple[int, dict] | None, guards: list[tuple[int, dict]],
    clothes: dict[int, dict[str, str]], facility_tier: int = 0,
) -> float:
    """What `op` is checked against: the sentry's stat if one is on duty, otherwise the target's
    single best healthy horse at that stat (0 with nobody home). An arsonist also has to get past
    the facility itself."""
    if sentry is not None:
        best = horse_stat(sentry[1], clothes.get(sentry[0], {}), op.guard_stat)
    else:
        best = max((horse_stat(h, clothes.get(i, {}), op.guard_stat) for i, h in guards), default=0.0)
    if op.id == "arsonist":
        best += facility_tier * ARSON_GUARD_PER_TIER
    return best


def heat(last_caught: dict[str, int], now: int) -> int:
    """How many distinct ops this player got caught running in the last HEAT_WINDOW_SECONDS.
    last_caught maps each op's caught_flag to its unix time (0 = never). A job that lands adds no
    heat; only getting caught does."""
    return sum(1 for t in last_caught.values() if t and now - t < HEAT_WINDOW_SECONDS)


def op_chance(
    op: Operation, operative_stat: float, guard: float, heat_level: int, sentry_on_duty: bool, bonus: float = 0.0,
) -> float:
    """bonus is JOB_INTEL_BONUS when the operative holds a spy's intel; it still can't push past
    the op's own max_chance."""
    chance = op.base + bonus + (operative_stat - guard) * OP_STAT_SWING - heat_level * HEAT_PENALTY
    if sentry_on_duty:
        chance -= SENTRY_PENALTY
    return max(op.min_chance, min(op.max_chance, chance))


def defender_lineup(available: list[tuple[int, dict]], scouted: list[int]) -> list[tuple[int, dict]]:
    """The defender's war army: random MAX_ARMY_SIZE of `available` (horse_index, horse), unless a
    spy scouted the lineup -- then those horses still available fight, topped up at random if
    some got injured, sold, or posted as sentry since."""
    if len(available) <= MAX_ARMY_SIZE:
        return available
    by_index = dict(available)
    lineup = [(i, by_index[i]) for i in scouted if i in by_index][:MAX_ARMY_SIZE]
    rest = [pair for pair in available if pair[0] not in {i for i, _ in lineup}]
    return lineup + random.sample(rest, MAX_ARMY_SIZE - len(lineup))


def smuggle_amount(balance: int) -> int:
    return max(0, min(SMUGGLE_CAP, int(balance * SMUGGLE_FRACTION)))


def bail_amount(balance: int) -> int:
    return max(0, min(BAIL_CAP, int(balance * BAIL_FRACTION)))
