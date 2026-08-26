import colorsys
import random

import db

# Sex and coat color/pattern -- every horse (legend or foal) has both. Coat terms are drawn from
# real equine coat color genetics (see Wikipedia's "Equine coat color"): base colors, the cream/
# dun/champagne/silver dilutions, and the roan/pinto/appaloosa patterns.
SEXES = ["male", "female"]
SEX_SYMBOLS = {"male": "♂", "female": "♀"}
COAT_COLORS = [
    "Bay", "Black", "Chestnut", "Brown", "Grey", "Palomino", "Buckskin", "Dun",
    "Cremello", "Perlino", "Smoky Black", "Champagne", "Silver Dapple", "Roan",
    "Tobiano Pinto", "Overo Pinto", "Appaloosa",
]

# "Red Spotted" is deliberately not in COAT_COLORS above -- it's a one-off coat assigned by hand
# to a single specific foal (pizzaface, guild 1311918529951301693, horse_index 8) rather than
# something new foals can randomly roll into. Kept as a named constant so its meaning is
# documented, even though nothing in this file references it directly.
SPECIAL_COAT_RED_SPOTTED = "Red Spotted"


def random_sex() -> str:
    return random.choice(SEXES)


def random_coat() -> str:
    return random.choice(COAT_COLORS)


# Base template for the fixed legend roster, each defined by three stats (0-100):
#   Speed     — top speed at the start of the race, before fatigue sets in.
#   Endurance — blunts how much speed is lost to fatigue each leg.
#   Spirit    — while behind the leader, a chance of a burst of extra speed that leg
#               (bigger spirit = more likely *and* bigger when it lands), like a crit chance.
# Named after real Thoroughbred greats (per Wikipedia's "List of racehorses"), with stats
# loosely nodding to their real reputations (Seabiscuit's famous comebacks -> high Spirit,
# Arkle's steeplechase stamina -> high Endurance, etc). sex/coat are researched real-world values
# for each horse (colt/stallion -> male, filly/mare -> female; geldings are also male here, since
# this only tracks biological sex). Coats favor real facts where a horse's color is iconic to its
# reputation (Secretariat's chestnut "Big Red", Black Caviar's grey), but spread the rest across
# the palette for variety rather than clustering on the two-or-three most common real colors.
# This only seeds a guild's copy of each legend the first time it's touched there
# (db.seed_legend) — after that, a guild's horses table is the source of truth, since stats/age/
# name/ownership all become guild-specific and training can move stats away from these starting
# values.
HORSES = [
    {"name": "Secretariat", "color": (195, 60, 55, 255), "speed": 88, "endurance": 65, "spirit": 55, "sex": "male", "coat": "Chestnut"},
    {"name": "Man o' War", "color": (50, 95, 200, 255), "speed": 85, "endurance": 70, "spirit": 50, "sex": "male", "coat": "Bay"},
    {"name": "Cigar", "color": (25, 25, 25, 255), "speed": 76, "endurance": 60, "spirit": 78, "sex": "male", "coat": "Black"},
    {"name": "Black Caviar", "color": (160, 160, 170, 255), "speed": 82, "endurance": 48, "spirit": 50, "sex": "female", "coat": "Grey"},
    {"name": "Affirmed", "color": (205, 120, 30, 255), "speed": 79, "endurance": 62, "spirit": 55, "sex": "male", "coat": "Palomino"},
    {"name": "Seabiscuit", "color": (45, 45, 50, 255), "speed": 67, "endurance": 55, "spirit": 90, "sex": "male", "coat": "Buckskin"},
    {"name": "Arkle", "color": (140, 75, 30, 255), "speed": 70, "endurance": 90, "spirit": 50, "sex": "male", "coat": "Roan"},
    {"name": "Barbaro", "color": (110, 50, 150, 255), "speed": 73, "endurance": 52, "spirit": 45, "sex": "male", "coat": "Appaloosa"},
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

# Betting: payout multiplier is TARGET_RTP / (that horse's implied win probability), so every
# horse has the same expected return regardless of how strong it is. That implied probability is
# recomputed fresh on every current_probabilities() call -- a Monte-Carlo simulation of the actual
# field's current stats (DYNAMIC_ODDS_TRIALS trials, cheap enough to run per-command rather than
# once-ever-per-horse the way the old WIN_PROBABILITY_TRIALS=4000 seed was), reshaped by
# _crowd_shares to read like a simulated betting crowd's odds rather than a raw fair probability
# -- see that function's own docstring. This means odds actually react to the field a horse is
# racing against and to its current (post-training) stats, unlike the old design, which derived
# odds from a horse's own cumulative career win rate and only recalculated once at its racing
# debut. A horse's real wins/places/shows/races (db.horses) are still recorded after every race
# and still shown as its career record (see bot.py's horses_cmd) -- they just no longer feed odds.
TARGET_RTP = 0.97
DYNAMIC_ODDS_TRIALS = 500
# How much a simulated betting crowd over-favors the favorite and under-backs the longshot
# relative to the field's true simulated probability -- the well-documented real-track
# "favorite-longshot bias" (bettors don't wager in exact proportion to true win rate). 0 leaves
# the raw simulated probability untouched; see _crowd_shares for the exact transform.
FAVORITE_LONGSHOT_BIAS = 0.15
# Final guardrail on top of _crowd_shares -- expressed as a multiple of "fair share" (that pool's
# raw_total / field size) rather than an absolute percent, so it scales sensibly with field size and
# with win's (~1) vs place's (~2) vs show's (~3) different pool totals. Without this, a sufficiently
# dominant horse (or just a lopsided lineup) can get priced *above* TARGET_RTP itself -- flipping its
# displayed odds negative (payout multiplier < 1) -- while _crowd_shares' own reshaping compresses
# the weakest horses' already-tiny probabilities even further, into absurd four-figure odds. Better
# stats should always earn a horse the best odds slot in the field, never a guaranteed win (and worse
# stats the worst slot, never a guaranteed loss) -- see _bounded_shares.
MIN_PROB_OF_FAIR = 0.35
MAX_PROB_OF_FAIR = 3.0

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

# !ranch facilities: a per-owner, permanent, sequential upgrade (can't skip tiers) that boosts
# every owned horse's training gains by a flat percentage. Priced as a long-term credit sink --
# well above a foal (FOAL_PRICE) but each tier still cheap next to a top legend (BASE_HORSE_PRICE)
# -- with a deliberately modest bonus per tier rather than a fast-track. Starting numbers, tune
# after playtesting like everything else balance-related in this file.
FACILITY_TIERS = [
    {"tier": 1, "name": "Training Paddock", "cost": 5_000, "bonus": 0.10},
    {"tier": 2, "name": "Training Grounds", "cost": 20_000, "bonus": 0.20},
    {"tier": 3, "name": "Elite Training Center", "cost": 75_000, "bonus": 0.30},
]

# !boost training items: one per stat, cheap, single-use -- buying one immediately queues a flat
# bonus (on top of the normal TRAIN_STAT_GAIN_MIN/MAX roll) onto that specific horse's very next
# training. Only one can be queued per horse at a time (db.buy_horse_item rejects a second while
# one's still pending).
ITEM_COST = 150
ITEM_BONUS = 3
ITEM_STATS = ("speed", "endurance", "spirit")


def facility_bonus_for_tier(tier: int) -> float:
    return FACILITY_TIERS[tier - 1]["bonus"] if tier > 0 else 0.0


def compute_training_gains(facility_bonus: float, pending_stat: str | None) -> tuple[float, float, float]:
    """Rolls one training's random speed/endurance/spirit gains, applying a facility's flat %
    boost to all three and a queued !boost item's flat bonus to just its one matching stat.
    Shared by !train and the !ranch dashboard's Train button so the math can't drift between the
    two entry points to the same action."""
    speed_gain = random.uniform(TRAIN_STAT_GAIN_MIN, TRAIN_STAT_GAIN_MAX) * (1 + facility_bonus)
    endurance_gain = random.uniform(TRAIN_STAT_GAIN_MIN, TRAIN_STAT_GAIN_MAX) * (1 + facility_bonus)
    spirit_gain = random.uniform(TRAIN_STAT_GAIN_MIN, TRAIN_STAT_GAIN_MAX) * (1 + facility_bonus)
    if pending_stat == "speed":
        speed_gain += ITEM_BONUS
    elif pending_stat == "endurance":
        endurance_gain += ITEM_BONUS
    elif pending_stat == "spirit":
        spirit_gain += ITEM_BONUS
    return speed_gain, endurance_gain, spirit_gain


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
        db.seed_legend(
            guild_id, i, base["name"], base["speed"], base["endurance"], base["spirit"], LEGEND_START_AGE,
            base["sex"], base["coat"],
        )
    roster = db.get_guild_horses(guild_id)
    # Backfill sex/coat for any horse that predates that tracking -- legends get their
    # canonical real-world values, foals (no canonical identity) get a random assignment.
    for i, horse in roster.items():
        if horse["sex"] is not None:
            continue
        if i < LEGEND_COUNT:
            sex, coat = HORSES[i]["sex"], HORSES[i]["coat"]
        else:
            sex, coat = random_sex(), random_coat()
        db.backfill_horse_traits(guild_id, i, sex, coat)
        horse["sex"], horse["coat"] = sex, coat
    return roster


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


def finish_order_of(frames: list[list[float]]) -> list[int]:
    """Ranks every position by final cumulative distance, furthest (1st place) first."""
    final = frames[-1]
    return sorted(range(len(final)), key=lambda i: final[i], reverse=True)


# Bet kinds, by how many finishing positions each one covers. "across" (Across the Board)
# isn't a single threshold -- it's a bundle of one Win + one Place + one Show bet, each at the
# same stake, so it's handled separately (see STAKE_MULTIPLIER / payout_multiplier_across).
BET_KIND_THRESHOLDS = {"win": 1, "place": 2, "show": 3}
ACROSS_LEGS = ("win", "place", "show")

# How many times the entered stake is actually escrowed for each bet kind -- an "across the
# board" bet of X is really three separate X bets (one per leg), X*3 total, same as at a real
# track.
STAKE_MULTIPLIER = {"win": 1, "place": 1, "show": 1, "across": 3}


def _simulate_stat_probabilities(
    stat_roster: list[dict], trials: int = DYNAMIC_ODDS_TRIALS
) -> tuple[list[float], list[float], list[float]]:
    """Monte-Carlo win/place/show rate for each position under its current stats, run through
    the exact same simulate_race() used for the actual race -- the raw, unbiased "true" fair
    probability current_probabilities() then reshapes via _crowd_shares."""
    n = len(stat_roster)
    wins, places, shows = [0] * n, [0] * n, [0] * n
    for _ in range(trials):
        order = finish_order_of(simulate_race(stat_roster))
        for rank, position in enumerate(order, start=1):
            if rank <= 1:
                wins[position] += 1
            if rank <= 2:
                places[position] += 1
            if rank <= 3:
                shows[position] += 1
    # Floor every count at 1 so a horse that never hit in the sample still gets a (very long)
    # finite price/payout instead of a division-by-zero — it's a true long shot, not impossible.
    return (
        [max(w, 1) / trials for w in wins],
        [max(p, 1) / trials for p in places],
        [max(s, 1) / trials for s in shows],
    )


def _crowd_shares(rates: list[float], bias: float = FAVORITE_LONGSHOT_BIAS) -> list[float]:
    """Reshapes raw simulated win/place/show rates into what a simulated betting crowd's pool
    shares would look like, instead of a mathematically fair probability -- real bettors don't
    wager in exact proportion to true win rate, they systematically overbet the favorite and
    underbet the longshot (the "favorite-longshot bias" well documented at real tracks). Each
    rate is raised to the power (1 + bias): since every rate is in (0, 1), a higher exponent
    shrinks *every* rate, but shrinks a small rate proportionally more than a large one (0.5 ** 1.15
    ≈ 0.44, a 13% cut, vs. 0.05 ** 1.15 ≈ 0.035, a 30% cut) -- so after renormalizing, the
    favorite's share rises and the longshot's falls relative to the raw simulation. Renormalized
    to the *raw* total rather than a hardcoded 1.0, since only "win" naturally sums to ~1 across a
    field (exactly one winner); "place"/"show" naturally sum to ~2/~3 (multiple horses place or
    show per race) and must keep that same total, just reshaped across the field. bias=0 is a
    no-op (exponent 1, rates unchanged)."""
    sharpened = [max(rate, 1e-9) ** (1 + bias) for rate in rates]
    sharpened_total = sum(sharpened)
    raw_total = sum(rates)
    return [s / sharpened_total * raw_total for s in sharpened]


def _bounded_shares(rates: list[float]) -> list[float]:
    """Final guardrail after _crowd_shares: clamps every rate into [MIN_PROB_OF_FAIR, MAX_PROB_OF_FAIR]
    x that pool's own fair share (raw_total / field size), then renormalizes back to raw_total --
    same trick _crowd_shares uses to keep place/show's own ~2x/~3x totals intact. Purely elementwise
    and monotonic, so it can never invert a field's stat-based ranking, only compress its extremes --
    "better stats" still always earns the better odds slot, just never an outright lock (and never an
    outright impossibility) regardless of how lopsided the lineup is."""
    n = len(rates)
    fair = sum(rates) / n
    floor, cap = MIN_PROB_OF_FAIR * fair, MAX_PROB_OF_FAIR * fair
    clamped = [min(max(r, floor), cap) for r in rates]
    raw_total = sum(rates)
    clamped_total = sum(clamped)
    return [c / clamped_total * raw_total for c in clamped]


def current_probabilities(
    guild_id: int, field: list[int] | None = None
) -> tuple[dict[int, dict], list[int], dict[str, dict[int, float]]]:
    """Returns (full_roster, eligible_horse_indices, {"win"/"place"/"show": {horse_index: rate}}).
    `field` is which horses to simulate odds for -- pass the specific drawn race_field once one's
    been selected (select_race_field) so a race's odds reflect exactly who's actually running;
    defaults to every eligible horse (e.g. for !horses' whole-stable listing or !buyhorse's
    pricing, where there's no specific race to scope to yet). Each of win/place/show is a fresh
    Monte-Carlo simulation (_simulate_stat_probabilities) against the field's *current* stats,
    reshaped by _crowd_shares -- so odds react immediately to training and to who's actually in
    the field, unlike the old design (derived from a horse's own cumulative career win rate,
    computed once at its racing debut and otherwise static). No database writes -- cheap enough
    to call fresh every time; call via asyncio.to_thread since it still reads db.get_guild_horses."""
    roster = get_roster(guild_id)
    eligible = eligible_indices(roster)
    positions = field if field is not None else eligible
    stat_roster = [
        {"speed": roster[i]["speed"], "endurance": roster[i]["endurance"], "spirit": roster[i]["spirit"]}
        for i in positions
    ]
    sim_win, sim_place, sim_show = _simulate_stat_probabilities(stat_roster)
    probabilities = {
        "win": dict(zip(positions, _bounded_shares(_crowd_shares(sim_win)))),
        "place": dict(zip(positions, _bounded_shares(_crowd_shares(sim_place)))),
        "show": dict(zip(positions, _bounded_shares(_crowd_shares(sim_show)))),
    }
    return roster, eligible, probabilities


def _multiplier_of(horse_index: int, probabilities: dict[int, float]) -> float:
    """Total-return multiplier for a winning bet: stake back plus profit."""
    return TARGET_RTP / probabilities[horse_index]


def price_of(horse_index: int, probabilities: dict[int, float]) -> int:
    return round(BASE_HORSE_PRICE / _multiplier_of(horse_index, probabilities) / 50) * 50


def payout_multiplier(horse_index: int, rank: int | None, threshold: int, probabilities: dict[int, float]) -> float:
    """`rank` is this horse's actual finishing position (1-based) in the race that just ran;
    `threshold` is how many top positions the bet kind covers (1/2/3 for win/place/show)."""
    if rank is None or rank > threshold:
        return 0.0
    return _multiplier_of(horse_index, probabilities)


def payout_multiplier_across(
    horse_index: int, rank: int | None, probabilities: dict[str, dict[int, float]]
) -> float:
    """Combined per-unit multiplier for an Across the Board bet: sums whichever of the Win/
    Place/Show legs actually hit (a win pays all three, a place pays place+show, a show pays
    just show), each leg valued the same as a standalone bet of that kind."""
    return sum(
        payout_multiplier(horse_index, rank, BET_KIND_THRESHOLDS[leg], probabilities[leg]) for leg in ACROSS_LEGS
    )


def describe_odds(horse_index: int, probabilities: dict[int, float]) -> str:
    ratio = round(_multiplier_of(horse_index, probabilities) - 1, 1)
    return f"{ratio:g}-1"
