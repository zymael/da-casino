"""Pure "Don't Break the Ice" rules -- no Discord/view code, same split as mancala.py/connect4.py.

The physical model: picture each square cube as pushed on by 4 springs, one per side (up/down/
left/right, never diagonal). A spring is backed -- pushes back rather than just extending into
empty space -- when whatever's on that side is either the sturdy outer frame (a wall; it only ever
squeezes inward, a pressure point rather than something to rest on, but it's still a real
backstop) or another still-intact cube. A single backed spring isn't enough to hold a cube in
place -- it'd just get shoved sideways into the open space opposite it -- but a full OPPOSITE PAIR
(both up and down backed, or both left and right backed) pins it in place, since the two pushes
cancel out. The moment a cube has no backed opposite pair left, it falls, which can immediately
cost its own neighbors their half of *their* pair, cascading until everything left standing is
genuinely pinned again. The penguin stands on a fixed cell at dead center; the only fall that
actually matters is his -- whoever's break puts him in the water, directly or by starting a chain
reaction that reaches him, is the loser. No draws are possible.
"""

ROWS = 5
COLS = 5
CENTER = (ROWS // 2, COLS // 2)


def new_board() -> list[list[int]]:
    return [[1] * COLS for _ in range(ROWS)]


def legal_moves(board: list[list[int]]) -> list[tuple[int, int]]:
    return [(r, c) for r in range(ROWS) for c in range(COLS) if board[r][c] == 1]


def _backed(board: list[list[int]], row: int, col: int, dr: int, dc: int) -> bool:
    """True if the spring pointing (dr, dc) from (row, col) pushes against something real -- the
    wall (off the grid entirely) or a still-intact neighboring cube."""
    nr, nc = row + dr, col + dc
    if not (0 <= nr < ROWS and 0 <= nc < COLS):
        return True
    return board[nr][nc] == 1


def _is_pinned(board: list[list[int]], row: int, col: int) -> bool:
    vertical = _backed(board, row, col, -1, 0) and _backed(board, row, col, 1, 0)
    horizontal = _backed(board, row, col, 0, -1) and _backed(board, row, col, 0, 1)
    return vertical or horizontal


def _cells_no_longer_pinned(board: list[list[int]]) -> set[tuple[int, int]]:
    return {(r, c) for r in range(ROWS) for c in range(COLS) if board[r][c] == 1 and not _is_pinned(board, r, c)}


class MoveResult:
    def __init__(self, collapsed: bool, fallen: set[tuple[int, int]]):
        self.collapsed = collapsed
        self.fallen = fallen


def apply_move(board: list[list[int]], row: int, col: int) -> MoveResult:
    """Breaks (row, col) and then cascades: any cube left standing with no backed opposite pair of
    springs falls too, which can immediately unpin further cubes in turn, repeating until
    everything left standing is genuinely pinned again. Caller is responsible for having checked
    (row, col) is one of legal_moves(board)'s entries. `fallen` is every cube that went into the
    water this turn, including (row, col) itself -- `collapsed` is just whether CENTER (the
    penguin) ended up among them, by direct hit or by the cascade reaching him."""
    fallen = {(row, col)}
    board[row][col] = 0
    while True:
        newly_fallen = _cells_no_longer_pinned(board)
        if not newly_fallen:
            break
        for r, c in newly_fallen:
            board[r][c] = 0
        fallen |= newly_fallen
    collapsed = CENTER in fallen
    return MoveResult(collapsed=collapsed, fallen=fallen)
