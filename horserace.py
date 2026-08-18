import random

# Fixed field of horses. `odds` is the profit-to-stake ratio shown to players (e.g. 2.0 means
# "2-1" — a winning bet returns 3x the stake: stake back plus 2x profit). Win probabilities are
# derived from the odds themselves (p = (1/return) / overround for every horse), so every horse
# pays out at roughly the same expected return regardless of how big a favorite or long shot it
# is — the dark horse at the back of the field always has a real (if small) chance to hit.
# Named after real Thoroughbred greats (per Wikipedia's "List of racehorses"), ordered
# favorite to long shot — the naming has no bearing on the odds, just flavor.
HORSES = [
    {"name": "Secretariat", "odds": 2.0, "color": (195, 60, 55, 255)},
    {"name": "Man o' War", "odds": 3.5, "color": (50, 95, 200, 255)},
    {"name": "Seabiscuit", "odds": 5.5, "color": (45, 45, 50, 255)},
    {"name": "Affirmed", "odds": 8.5, "color": (205, 120, 30, 255)},
    {"name": "Black Caviar", "odds": 13.0, "color": (160, 160, 170, 255)},
    {"name": "Arkle", "odds": 20.0, "color": (140, 75, 30, 255)},
    {"name": "Barbaro", "odds": 31.0, "color": (110, 50, 150, 255)},
    {"name": "Cigar", "odds": 49.0, "color": (25, 25, 25, 255)},
]

RACE_LEGS = 4
TRACK_LENGTH = 100.0

# Ownership: horses are expensive, priced off how likely they are to win (a proven favorite
# costs the most since it pays its owner a cut most often; a long shot is a cheap speculative
# buy). Owners collect OWNER_CUT_FRACTION of the total amount bet on their horse whenever it
# wins, funded by the house rather than skimmed from bettors' own winnings.
BASE_HORSE_PRICE = 150000
OWNER_CUT_FRACTION = 0.05
MAX_HORSE_NAME_LEN = 16


def price_of(horse_index: int) -> int:
    return round(BASE_HORSE_PRICE / _total_return(HORSES[horse_index]) / 50) * 50


def _total_return(horse: dict) -> float:
    """Total-return multiplier for a winning bet: stake back plus profit, e.g. odds=2.0 -> 3x."""
    return horse["odds"] + 1


_inv_return = [1 / _total_return(h) for h in HORSES]
_overround = sum(_inv_return)
WIN_PROBABILITIES = [inv / _overround for inv in _inv_return]
RTP = 1 / _overround  # expected return is the same for every horse, favorite or long shot


def pick_winner() -> int:
    return random.choices(range(len(HORSES)), weights=WIN_PROBABILITIES)[0]


def simulate_race(winner: int) -> list[list[float]]:
    """Returns RACE_LEGS frames, each a list of per-horse cumulative distance. Horses jostle for
    position through the early legs; the final leg is nudged just enough so the pre-drawn winner
    is the one actually in front when the field crosses the line."""
    n = len(HORSES)
    distances = [0.0] * n
    frames = []
    per_leg = TRACK_LENGTH / RACE_LEGS
    for leg in range(RACE_LEGS):
        for i in range(n):
            distances[i] += per_leg * random.uniform(0.6, 1.3)
        if leg == RACE_LEGS - 1:
            lead = max(d for i, d in enumerate(distances) if i != winner)
            if distances[winner] <= lead:
                distances[winner] = lead + random.uniform(2, 8)
        frames.append(list(distances))
    return frames


def payout_multiplier(horse_index: int, winner: int) -> float:
    return _total_return(HORSES[horse_index]) if horse_index == winner else 0.0


def describe_odds(horse_index: int) -> str:
    odds = HORSES[horse_index]["odds"]
    text = f"{odds:g}" if odds == int(odds) else f"{odds}"
    return f"{text}-1"
