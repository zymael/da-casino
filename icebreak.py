"""Pure "Don't Break the Ice" rules -- no Discord/view code, same split as mancala.py/connect4.py.

The physical model: the sturdy outer frame (never breaks, not part of the grid) is the only real
anchor -- it's bolted to the table. A perimeter cell is pressed directly against that frame, so it
stays put on its own regardless of what happens elsewhere. Every other cell is only held up by
being pressed against its neighbors in an unbroken chain leading back to some perimeter cell --
that's the "tension" propagating inward from the walls. The penguin stands on a fixed cell at dead
center; the moment that cell is broken, or loses every remaining chain back to a wall, there's
nothing left holding his footing and he falls -- whoever's break caused that is the loser. No
draws are possible.
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
    def __init__(self, collapsed: bool, unsupported: set[tuple[int, int]]):
        self.collapsed = collapsed
        self.unsupported = unsupported


def apply_move(board: list[list[int]], row: int, col: int) -> MoveResult:
    """Breaks (row, col), mutating `board` in place. Caller is responsible for having checked
    (row, col) is one of legal_moves(board)'s entries. `unsupported` is every remaining intact
    cell with no path back to a wall -- always includes CENTER when `collapsed` is true, and is
    otherwise just flavor for rendering which (if any) unrelated pockets have quietly come loose
    without threatening the penguin himself."""
    board[row][col] = 0
    supported = supported_cells(board)
    unsupported = {
        (r, c) for r in range(ROWS) for c in range(COLS) if board[r][c] == 1 and (r, c) not in supported
    }
    collapsed = board[CENTER[0]][CENTER[1]] == 0 or CENTER in unsupported
    return MoveResult(collapsed=collapsed, unsupported=unsupported)
