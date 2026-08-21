import random

import moon

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
BLACK_NUMBERS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}

OUTSIDE_MULTIPLIER = 2  # red/black/odd/even/low/high pay 1:1 (total return 2x)
STRAIGHT_MULTIPLIER = 36  # single number pays 35:1 (total return 36x)
COLUMN_MULTIPLIER = 3  # column pays 2:1 (total return 3x)
DOZEN_MULTIPLIER = 3  # dozen pays 2:1 (total return 3x)

BET_LABELS = {
    "red": "Red",
    "black": "Black",
    "odd": "Odd",
    "even": "Even",
    "low": "1-18",
    "high": "19-36",
}

COMBO_LABELS = {2: "Split", 3: "Street", 4: "Corner", 6: "Six-Line"}


def column_of(n: int) -> int:
    """1, 2, or 3 — the vertical column a number (1-36) sits in on the table."""
    return ((n - 1) % 3) + 1


def dozen_of(n: int) -> int:
    """1, 2, or 3 — which dozen (1-12/13-24/25-36) a number (1-36) falls in."""
    return ((n - 1) // 12) + 1


def street_numbers(anchor: int) -> set[int]:
    """The row of 3 numbers containing anchor (1-36)."""
    c = (anchor - 1) // 3
    return {c * 3 + 1, c * 3 + 2, c * 3 + 3}


def corner_numbers(anchor: int) -> set[int] | None:
    """The 2x2 block whose bottom-left number is anchor, or None if anchor can't anchor a corner."""
    if (anchor - 1) % 3 == 2 or (anchor - 1) // 3 > 10:
        return None
    return {anchor, anchor + 1, anchor + 3, anchor + 4}


def sixline_numbers(anchor: int) -> set[int] | None:
    """The 6 numbers of two adjacent streets starting at anchor, or None if anchor is invalid."""
    if anchor % 3 != 1 or anchor > 31:
        return None
    return set(range(anchor, anchor + 6))


def are_split_adjacent(a: int, b: int) -> bool:
    """True if a and b share an edge on the table layout (a valid split bet)."""
    if a == b:
        return False
    if 0 in (a, b):
        other = b if a == 0 else a
        return other in (1, 2, 3)
    if a > b:
        a, b = b, a
    col_a, row_a = (a - 1) // 3, (a - 1) % 3
    col_b, row_b = (b - 1) // 3, (b - 1) % 3
    if col_a == col_b:
        return abs(row_a - row_b) == 1
    if row_a == row_b:
        return abs(col_a - col_b) == 1
    return False


def combo_label(numbers) -> str:
    return COMBO_LABELS.get(len(numbers), "Combo")


# 0 is what loses every outside bet (red/black/odd/even/low/high) -- see moon.py; never surfaced
# to players. On a house night it's relatively more likely; on a player night, less.
MOON_ZERO_SHIFT = 0.5


def spin() -> int:
    effect = moon.effect_for("roulette")
    if effect is None:
        return random.randint(0, 36)
    zero_weight = 1 + MOON_ZERO_SHIFT if effect == "house" else 1 - MOON_ZERO_SHIFT
    return random.choices(range(37), weights=[zero_weight] + [1] * 36, k=1)[0]


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
    if kind == "column":
        return COLUMN_MULTIPLIER if column_of(result) == value else 0
    if kind == "dozen":
        return DOZEN_MULTIPLIER if dozen_of(result) == value else 0
    if kind == "combo":
        return 36 // len(value) if result in value else 0
    return 0


def describe_bet(kind: str, value) -> str:
    if kind == "number":
        return f"Number {value}"
    if kind == "column":
        return f"Column {value}"
    if kind == "dozen":
        return f"Dozen {value} ({(value - 1) * 12 + 1}-{value * 12})"
    if kind == "combo":
        return f"{combo_label(value)} {'-'.join(str(n) for n in value)}"
    return BET_LABELS[kind]
