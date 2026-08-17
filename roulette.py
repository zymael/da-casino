import random

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
BLACK_NUMBERS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}

OUTSIDE_MULTIPLIER = 2  # red/black/odd/even/low/high pay 1:1 (total return 2x)
STRAIGHT_MULTIPLIER = 36  # single number pays 35:1 (total return 36x)

BET_LABELS = {
    "red": "Red",
    "black": "Black",
    "odd": "Odd",
    "even": "Even",
    "low": "1-18",
    "high": "19-36",
}


def spin() -> int:
    return random.randint(0, 36)


def color_of(number: int) -> str:
    if number == 0:
        return "green"
    return "red" if number in RED_NUMBERS else "black"


def color_emoji(number: int) -> str:
    return {"red": "🔴", "black": "⚫", "green": "🟢"}[color_of(number)]


def payout_multiplier(kind: str, value: int | None, result: int) -> int:
    """Total-return multiplier for a single bet: 0 = lose the bet."""
    if kind == "number":
        return STRAIGHT_MULTIPLIER if value == result else 0
    if result == 0:
        return 0  # 0 loses all outside bets
    result_color = color_of(result)
    if kind == "red":
        return OUTSIDE_MULTIPLIER if result_color == "red" else 0
    if kind == "black":
        return OUTSIDE_MULTIPLIER if result_color == "black" else 0
    if kind == "odd":
        return OUTSIDE_MULTIPLIER if result % 2 == 1 else 0
    if kind == "even":
        return OUTSIDE_MULTIPLIER if result % 2 == 0 else 0
    if kind == "low":
        return OUTSIDE_MULTIPLIER if 1 <= result <= 18 else 0
    if kind == "high":
        return OUTSIDE_MULTIPLIER if 19 <= result <= 36 else 0
    return 0


def describe_bet(kind: str, value: int | None) -> str:
    if kind == "number":
        return f"Number {value}"
    return BET_LABELS[kind]
