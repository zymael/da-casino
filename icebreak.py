"""Pure "Don't Break the Ice" rules -- no Discord/view code, same split as mancala.py/connect4.py.

The physical model: the sturdy outer frame (never breaks, not part of the grid) is the only real
anchor -- it's bolted to the table. A perimeter cell is pressed directly against that frame, so it
stays put on its own regardless of what happens elsewhere. Every other cell is only held up by
being pressed against its neighbors -- straight up/down/left/right, never diagonally, since these
are square cubes pushing against square cubes -- in an unbroken chain leading back to some
perimeter cell. That chain can run through a full row supporting a partial column, a full column
supporting a partial row, or any mix of both; only the shape of the remaining connected chain
matters, not which direction it runs.

Any cube that loses that chain falls immediately, for real -- not a cosmetic warning, it becomes
open water right along with the cube that got hammered, and losing it can just as immediately
strand its own neighbors, which fall too, cascading until everything left standing has a route
back to a wall again. The penguin stands on a fixed cell at dead center; the only fall that
actually matters is his -- whoever's break puts him in the water (directly or by starting a chain
reaction that reaches him) is the loser. No draws are possible.
"""

ROWS = 5
COLS = 5
CENTER = (ROWS // 2, COLS // 2)

_DIRECTIONS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def new_board() -> list[list[int]]:
    return [[1] * COLS for _ in range(ROWS)]


def legal_moves(board: list[list[int]]) -> list[tuple[int, int]]:
    return [(r, c) for r in range(ROWS) for c in range(COLS) if board[r][c] == 1]


def _is_perimeter(row: int, col: int) -> bool:
    return row == 0 or row == ROWS - 1 or col == 0 or col == COLS - 1


def supported_cells(board: list[list[int]]) -> set[tuple[int, int]]:
    """Every intact cell reachable from the (intact) perimeter through a chain of intact,
    4-directionally-adjacent neighbors -- a multi-source flood fill starting from every still-
    standing perimeter cell at once, since any of them is independently anchored to the frame."""
    supported: set[tuple[int, int]] = set()
    stack = []
    for r in range(ROWS):
        for c in range(COLS):
            if board[r][c] == 1 and _is_perimeter(r, c):
                supported.add((r, c))
                stack.append((r, c))
    while stack:
        r, c = stack.pop()
        for dr, dc in _DIRECTIONS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < ROWS and 0 <= nc < COLS and board[nr][nc] == 1 and (nr, nc) not in supported:
                supported.add((nr, nc))
                stack.append((nr, nc))
    return supported


class MoveResult:
    def __init__(self, collapsed: bool, fallen: set[tuple[int, int]]):
        self.collapsed = collapsed
        self.fallen = fallen


def apply_move(board: list[list[int]], row: int, col: int) -> MoveResult:
    """Breaks (row, col) and then cascades: any cube left standing with no remaining chain back to
    a wall falls too (mutating `board` in place, same as the direct break), which can strand
    further cubes in turn, repeating until everything left standing is genuinely supported. Caller
    is responsible for having checked (row, col) is one of legal_moves(board)'s entries.
    `fallen` is every cube that went into the water this turn, including (row, col) itself --
    `collapsed` is just whether CENTER (the penguin) ended up among them, by direct hit or by the
    cascade reaching him."""
    fallen = {(row, col)}
    board[row][col] = 0
    while True:
        supported = supported_cells(board)
        newly_fallen = {
            (r, c) for r in range(ROWS) for c in range(COLS) if board[r][c] == 1 and (r, c) not in supported
        }
        if not newly_fallen:
            break
        for r, c in newly_fallen:
            board[r][c] = 0
        fallen |= newly_fallen
    collapsed = CENTER in fallen
    return MoveResult(collapsed=collapsed, fallen=fallen)
