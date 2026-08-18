import colorsys
import random

import db

# Base template for the fixed legend roster, each defined by three stats (0-100):
#   Speed     — top speed at the start of the race, before fatigue sets in.
#   Endurance — blunts how much speed is lost to fatigue each leg.
#   Spirit    — while behind the leader, a chance of a burst of extra speed that leg
#               (bigger spirit = more likely *and* bigger when it lands), like a crit chance.
# Named after real Thoroughbred greats (per Wikipedia's "List of racehorses"), with stats
# loosely nodding to their real reputations (Seabiscuit's famous comebacks -> high Spirit,
# Arkle's steeplechase stamina -> high Endurance, etc). This only seeds a guild's copy of each
# legend the first time it's touched there (db.seed_legend) — after that, a guild's horses
# table is the source of truth, since stats/age/name/ownership all become guild-specific and
# training can move stats away from these starting values.
HORSES = [
    {"name": "Secretariat", "color": (195, 60, 55, 255), "speed": 88, "endurance": 65, "spirit": 55},
    {"name": "Man o' War", "color": (50, 95, 200, 255), "speed": 85, "endurance": 70, "spirit": 50},
    {"name": "Cigar", "color": (25, 25, 25, 255), "speed": 76, "endurance": 60, "spirit": 78},
    {"name": "Black Caviar", "color": (160, 160, 170, 255), "speed": 82, "endurance": 48, "spirit": 50},
    {"name": "Affirmed", "color": (205, 120, 30, 255), "speed": 79, "endurance": 62, "spirit": 55},
    {"name": "Seabiscuit", "color": (45, 45, 50, 255), "speed": 67, "endurance": 55, "spirit": 90},
    {"name": "Arkle", "color": (140, 75, 30, 255), "speed": 70, "endurance": 90, "spirit": 50},
    {"name": "Barbaro", "color": (110, 50, 150, 255), "speed": 73, "endurance": 52, "spirit": 45},
]
LEGEND_COUNT = len(HORSES)
LEGEND_START_AGE = 10  # legends are already mature, so they're race-eligible from the start

RACE_LEGS = 4

# Race physics: each leg, current speed decays by LEG_DECAY_BASE, reduced by up to
# ENDURANCE_MITIGATION_MAX at endurance=100. A horse behind the leader gets a spirit-rolled
# chance of a burst that leg, scaled up to SPIRIT_BURST_CHANCE_MAX / SPIRIT_BURST_BONUS_MAX at
# spirit=100. JITTER and FLAT_NOISE_MAX add race-to-race variance on top of the stats — jitter
# scales with a horse's own speed (a fast horse has more to gain/lose from a good/bad leg),
# while flat noise is stat-independent day-of-race luck so even a stat-underdog is never a
# lock to lose. Without a meaningful flat-noise term, small stat gaps compound over 4 legs
# into near-certain outcomes for the fastest horse — this keeps the field honestly competitive.
LEG_DECAY_BASE = 0.12
ENDURANCE_MITIGATION_MAX = 0.7
SPIRIT_BURST_CHANCE_MAX = 0.45
SPIRIT_BURST_BONUS_MAX = 0.6
JITTER_LOW, JITTER_HIGH = 0.85, 1.15
FLAT_NOISE_MAX = 26

# Betting: payout multiplier is TARGET_RTP / (that horse's actual win probability), so every
# horse has the same expected return regardless of how strong it is. The win probability
# itself comes from real accumulated race results (db.horses) rather than the stat simulation
# directly — a horse with no races yet is seeded with SEED_RACE_COUNT "virtual" races at its
# stat-simulated fair rate against the rest of the currently-eligible field, so odds start
# sane and then drift to reflect what it actually does on the track.
TARGET_RTP = 0.97
WIN_PROBABILITY_TRIALS = 4000
SEED_RACE_COUNT = 40

# Ownership: horses are expensive, priced off how likely they are to win (a proven favorite
# costs the most since it pays its owner a cut most often; a long shot is a cheap speculative
# buy). Owners collect OWNER_CUT_FRACTION of the total amount bet on their horse whenever it
# wins, funded by the house rather than skimmed from bettors' own winnings.
BASE_HORSE_PRICE = 150000
OWNER_CUT_FRACTION = 0.05
MAX_HORSE_NAME_LEN = 16

# Foals: a cheap entry point into ownership — a raw, unraced prospect strictly worse than even
# Barbaro (the weakest legend: speed 73 / endurance 52 / spirit 45), that only becomes
# competitive through training. Training (once/day, owner-only) raises all three stats and
# ages the horse by 1; a horse can't race until it reaches MIN_RACING_AGE. Stats/age growth
# without decline "for now" — an eventual decline past a peak age is a later addition, not yet
# implemented, so there's no cap on how often a mature horse keeps training either.
FOAL_PRICE = 1000
FOAL_BASE_STATS = {"speed": 45, "endurance": 32, "spirit": 28}
MIN_RACING_AGE = 5
TRAIN_STAT_GAIN_MIN, TRAIN_STAT_GAIN_MAX = 2, 5
STAT_CAP = 100

# Actually racing ages a horse too, just much more slowly than training — it takes this many
# real race starts (not the virtual seed races used to calibrate odds) to add 1 age.
RACE_AGE_INTERVAL = 10


def color_for_index(horse_index: int) -> tuple[int, int, int, int]:
    """A legend keeps its hand-picked color; a foal gets one spread around the hue wheel by the
    golden ratio conjugate, which keeps consecutive foals visually distinct indefinitely."""
    if horse_index < LEGEND_COUNT:
        return HORSES[horse_index]["color"]
    hue = ((horse_index - LEGEND_COUNT) * 0.618033988749895) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.75)
    return (int(r * 255), int(g * 255), int(b * 255), 255)


def get_roster(guild_id: int) -> dict[int, dict]:
    """Full stable for this guild: every legend (seeding it from the template on first touch)
    plus any foals bought in. Includes horses too young to race yet."""
    for i, base in enumerate(HORSES):
        db.seed_legend(guild_id, i, base["name"], base["speed"], base["endurance"], base["spirit"], LEGEND_START_AGE)
    return db.get_guild_horses(guild_id)


def eligible_indices(roster: dict[int, dict]) -> list[int]:
    """Horses old enough to actually race, oldest-index-first for stable button/track ordering."""
    return sorted(i for i, h in roster.items() if h["age"] >= MIN_RACING_AGE)


RACE_FIELD_SIZE = 8


def select_race_field(eligible: list[int], size: int = RACE_FIELD_SIZE) -> list[int]:
    """Draws up to `size` horses from the eligible pool to actually run this race. While the
    stable is small (8 legends is the common case) every eligible horse races every time; once
    training grows the stable past `size`, each race features a different random subset."""
    if len(eligible) <= size:
        return eligible
    return sorted(random.sample(eligible, size))


def simulate_race(stat_roster: list[dict]) -> list[list[float]]:
    """stat_roster: ordered list of {"speed", "endurance", "spirit"} for the horses actually
    running. Returns RACE_LEGS frames, each a list of per-position cumulative distance — the
    winner is whichever position is furthest ahead in the final frame, nothing decided in
    advance."""
    n = len(stat_roster)
    current_speed = [float(h["speed"]) for h in stat_roster]
    distances = [0.0] * n
    frames = []
    for _leg in range(RACE_LEGS):
        leader = max(distances)
        for i, horse in enumerate(stat_roster):
            speed = current_speed[i]
            if distances[i] < leader:
                chance = (horse["spirit"] / 100) * SPIRIT_BURST_CHANCE_MAX
                if random.random() < chance:
                    speed *= 1 + (horse["spirit"] / 100) * SPIRIT_BURST_BONUS_MAX
            distances[i] += speed * random.uniform(JITTER_LOW, JITTER_HIGH) + random.uniform(
                -FLAT_NOISE_MAX, FLAT_NOISE_MAX
            )
        frames.append(list(distances))
        for i, horse in enumerate(stat_roster):
            mitigation = (horse["endurance"] / 100) * ENDURANCE_MITIGATION_MAX
            current_speed[i] *= 1 - LEG_DECAY_BASE * (1 - mitigation)
    return frames


def winner_of(frames: list[list[float]]) -> int:
    final = frames[-1]
    return max(range(len(final)), key=lambda i: final[i])


def _simulate_stat_win_probabilities(stat_roster: list[dict], trials: int = WIN_PROBABILITY_TRIALS) -> list[float]:
    """Monte-Carlo win rate for each position under its current stats, run through the exact
    same simulate_race() used for the actual race. Used only to seed a horse's very first
    record — once real races exist, current_win_probabilities() below takes over."""
    wins = [0] * len(stat_roster)
    for _ in range(trials):
        wins[winner_of(simulate_race(stat_roster))] += 1
    # Floor every count at 1 so a horse that never won in the sample still gets a (very long)
    # finite price/payout instead of a division-by-zero — it's a true long shot, not impossible.
    return [max(w, 1) / trials for w in wins]


def current_win_probabilities(guild_id: int) -> tuple[dict[int, dict], list[int], dict[int, float]]:
    """Returns (full_roster, eligible_horse_indices, {horse_index: win_probability}) — the
    probability dict only covers eligible (old enough to race) horses. Any eligible horse with
    no races yet gets seeded first, from a stat-simulated rate against the rest of the current
    field. Touches the database — call via asyncio.to_thread, once per command, and reuse the
    result rather than having every call site query separately."""
    roster = get_roster(guild_id)
    eligible = eligible_indices(roster)
    unseeded = [i for i in eligible if roster[i]["races"] == 0]
    if unseeded:
        stat_roster = [
            {"speed": roster[i]["speed"], "endurance": roster[i]["endurance"], "spirit": roster[i]["spirit"]}
            for i in eligible
        ]
        seed_probs = _simulate_stat_win_probabilities(stat_roster)
        for position, i in enumerate(eligible):
            if i in unseeded:
                wins = max(1, round(seed_probs[position] * SEED_RACE_COUNT))
                db.seed_race_history(guild_id, i, wins, SEED_RACE_COUNT)
        roster = db.get_guild_horses(guild_id)
    probabilities = {i: roster[i]["wins"] / roster[i]["races"] for i in eligible}
    return roster, eligible, probabilities


def _multiplier_of(horse_index: int, probabilities: dict[int, float]) -> float:
    """Total-return multiplier for a winning bet: stake back plus profit."""
    return TARGET_RTP / probabilities[horse_index]


def price_of(horse_index: int, probabilities: dict[int, float]) -> int:
    return round(BASE_HORSE_PRICE / _multiplier_of(horse_index, probabilities) / 50) * 50


def payout_multiplier(horse_index: int, winner_index: int, probabilities: dict[int, float]) -> float:
    return _multiplier_of(horse_index, probabilities) if horse_index == winner_index else 0.0


def describe_odds(horse_index: int, probabilities: dict[int, float]) -> str:
    ratio = round(_multiplier_of(horse_index, probabilities) - 1, 1)
    return f"{ratio:g}-1"
