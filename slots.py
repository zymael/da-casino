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
CHERRY_PAIR_PAYOUT = 2  # any two (not three) cherries on a line

_POOL = list(SYMBOLS.keys())
_WEIGHTS = [SYMBOLS[symbol]["weight"] for symbol in _POOL]

GRID_ROWS = 3
GRID_COLS = 3

# Each payline is a row index per column, read left to right. Lines are activated
# in this order as a player bets on more of them (1 line = middle only, up to all 5).
PAYLINES: list[tuple[int, int, int]] = [
    (1, 1, 1),  # middle row
    (0, 0, 0),  # top row
    (2, 2, 2),  # bottom row
    (0, 1, 2),  # diagonal, top-left to bottom-right
    (2, 1, 0),  # diagonal, bottom-left to top-right
]
MAX_LINES = len(PAYLINES)


def spin_grid() -> list[list[str]]:
    """A GRID_ROWS x GRID_COLS grid of symbols, grid[row][col]."""
    return [random.choices(_POOL, weights=_WEIGHTS, k=GRID_COLS) for _ in range(GRID_ROWS)]


def line_symbols(grid: list[list[str]], line: tuple[int, int, int]) -> list[str]:
    return [grid[row][col] for col, row in enumerate(line)]


def payout_multiplier(line: list[str]) -> float:
    """Total-return multiplier for one line: 0 = lose the stake, 1 = push, >1 = profit."""
    if line[0] == line[1] == line[2]:
        return SYMBOLS[line[0]]["triple"]
    if line.count(CHERRY) == 2:
        return CHERRY_PAIR_PAYOUT
    return 0
