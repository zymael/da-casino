import random

# symbol -> (spawn weight, total-return multiplier for three-of-a-kind)
SYMBOLS = {
    "🍒": {"weight": 30, "triple": 6},
    "🍋": {"weight": 25, "triple": 9},
    "🔔": {"weight": 20, "triple": 14},
    "🍀": {"weight": 15, "triple": 25},
    "💎": {"weight": 7, "triple": 45},
    "7️⃣": {"weight": 3, "triple": 90},
}
CHERRY = "🍒"
CHERRY_PAIR_PAYOUT = 2  # any two (not three) cherries

_POOL = list(SYMBOLS.keys())
_WEIGHTS = [SYMBOLS[symbol]["weight"] for symbol in _POOL]


def spin() -> list[str]:
    return random.choices(_POOL, weights=_WEIGHTS, k=3)


def payout_multiplier(reels: list[str]) -> float:
    """Total-return multiplier: 0 = lose the bet, 1 = push, >1 = profit."""
    if reels[0] == reels[1] == reels[2]:
        return SYMBOLS[reels[0]]["triple"]
    if reels.count(CHERRY) == 2:
        return CHERRY_PAIR_PAYOUT
    return 0
