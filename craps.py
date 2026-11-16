"""Craps rules: bet kinds, validation, and per-roll settlement. No Discord or rendering here --
craps_view.py owns the table/rounds, craps_render.py owns the picture.

Unlike roulette, a craps table has state that outlives a single roll: the point. Pass Line, Don't
Pass, Place and Hardway bets are multi-roll -- they stay on the felt across rounds until a roll
decides them (settle() returns None for "still riding"). Everything else is a one-roll prop bet.
Place and Hardway bets are always working (a come-out 7 takes them down too), which is simpler to
explain than the real-casino "off on the come-out" convention."""

import random

POINT_NUMBERS = (4, 5, 6, 8, 9, 10)
HARD_NUMBERS = (4, 6, 8, 10)

# Payouts as (numerator, denominator) of profit per unit staked -- place bets pay fractional odds.
PLACE_PAYOUT = {4: (9, 5), 10: (9, 5), 5: (7, 5), 9: (7, 5), 6: (7, 6), 8: (7, 6)}
HARD_PAYOUT = {4: (7, 1), 10: (7, 1), 6: (9, 1), 8: (9, 1)}
ONE_ROLL_PAYOUT = {
    "any7": (4, 1),
    "anycraps": (7, 1),
    "yo": (15, 1),
    "snake": (30, 1),
    "boxcars": (30, 1),
}
FIELD_NUMBERS = (2, 3, 4, 9, 10, 11, 12)

# Bets that only make sense with no point on (real tables don't take a fresh line bet mid-hand).
COME_OUT_ONLY = ("pass", "dontpass")
MULTI_ROLL = ("pass", "dontpass", "place", "hard")

LABELS = {
    "pass": "Pass Line",
    "dontpass": "Don't Pass",
    "field": "Field",
    "any7": "Any 7",
    "anycraps": "Any Craps",
    "yo": "Yo (11)",
    "snake": "Snake Eyes (2)",
    "boxcars": "Boxcars (12)",
}

DIE_FACES = "⚀⚁⚂⚃⚄⚅"


def roll() -> tuple[int, int]:
    return random.randint(1, 6), random.randint(1, 6)


def dice_text(d1: int, d2: int) -> str:
    return f"{DIE_FACES[d1 - 1]} {DIE_FACES[d2 - 1]} = {d1 + d2}"


def describe_bet(kind: str, value: int | None) -> str:
    if kind == "place":
        return f"Place {value}"
    if kind == "hard":
        return f"Hard {value}"
    return LABELS[kind]


def validate(kind: str, value: int | None, point: int | None) -> str | None:
    """A user-facing reason this bet can't be placed right now, or None if it's fine."""
    if kind in COME_OUT_ONLY and point is not None:
        return f"{describe_bet(kind, value)} only takes bets on the come-out roll. The point is {point} right now."
    if kind == "place" and value not in POINT_NUMBERS:
        return "Place bets go on 4, 5, 6, 8, 9 or 10."
    if kind == "hard" and value not in HARD_NUMBERS:
        return "Hardways are 4, 6, 8 or 10."
    if kind not in ("place", "hard") and kind not in LABELS:
        return "Unknown bet."
    return None


def _win(amount: int, ratio: tuple[int, int]) -> int:
    """Total returned (stake + profit), profit floored to a whole flake."""
    num, den = ratio
    return amount + amount * num // den


def settle(kind: str, value: int | None, amount: int, d1: int, d2: int, point: int | None) -> int | None:
    """Total returned to the bettor for this roll (0 = lost, amount = push), or None if a
    multi-roll bet is still undecided and stays on the table. `point` is the point *before* this
    roll."""
    total = d1 + d2
    if kind == "pass":
        if point is None:
            if total in (7, 11):
                return amount * 2
            if total in (2, 3, 12):
                return 0
            return None
        if total == point:
            return amount * 2
        return 0 if total == 7 else None
    if kind == "dontpass":
        if point is None:
            if total in (2, 3):
                return amount * 2
            if total == 12:
                return amount  # bar 12: push
            if total in (7, 11):
                return 0
            return None
        if total == 7:
            return amount * 2
        return 0 if total == point else None
    if kind == "place":
        if total == value:
            return _win(amount, PLACE_PAYOUT[value])
        return 0 if total == 7 else None
    if kind == "hard":
        if total == value:
            return _win(amount, HARD_PAYOUT[value]) if d1 == d2 else 0
        return 0 if total == 7 else None
    if kind == "field":
        if total == 2:
            return amount * 3
        if total == 12:
            return amount * 4
        return amount * 2 if total in FIELD_NUMBERS else 0
    if kind == "any7":
        return _win(amount, ONE_ROLL_PAYOUT["any7"]) if total == 7 else 0
    if kind == "anycraps":
        return _win(amount, ONE_ROLL_PAYOUT["anycraps"]) if total in (2, 3, 12) else 0
    if kind == "yo":
        return _win(amount, ONE_ROLL_PAYOUT["yo"]) if total == 11 else 0
    if kind == "snake":
        return _win(amount, ONE_ROLL_PAYOUT["snake"]) if total == 2 else 0
    if kind == "boxcars":
        return _win(amount, ONE_ROLL_PAYOUT["boxcars"]) if total == 12 else 0
    return 0


def next_point(point: int | None, d1: int, d2: int) -> int | None:
    total = d1 + d2
    if point is None:
        return total if total in POINT_NUMBERS else None
    return None if total in (point, 7) else point


def roll_headline(point: int | None, d1: int, d2: int) -> str:
    """Stickman call for this roll, given the point before it."""
    total = d1 + d2
    if point is None:
        if total in (7, 11):
            return "Winner on the come-out! Pass Line pays."
        if total in (2, 3, 12):
            return "Craps! Pass Line loses."
        return f"The point is {total}."
    if total == point:
        return f"Hit the point! {point} pays the Pass Line."
    if total == 7:
        return "Seven out! Line and place bets come down."
    return f"Point is still {point}."
