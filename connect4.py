"""Pure Connect 4 rules -- no Discord/view code, same split as mancala.py/mancala_view.py.

Board is a 2D list, board[row][col], row 0 at the top and row ROWS-1 at the bottom -- a dropped
disc falls to the lowest empty row in its column, same as gravity on a real board. Cells are 0
(empty), 1 (challenger), or 2 (opponent).
"""

ROWS = 6
COLS = 7
CHALLENGER = 1
OPPONENT = 2


def new_board() -> list[list[int]]:
    return [[0] * COLS for _ in range(ROWS)]


def legal_moves(board: list[list[int]]) -> list[int]:
    return [c for c in range(COLS) if board[0][c] == 0]


def is_full(board: list[list[int]]) -> bool:
    return all(board[0][c] != 0 for c in range(COLS))


_DIRECTIONS = [(0, 1), (1, 0), (1, 1), (1, -1)]


def _winning_line(board: list[list[int]], row: int, col: int) -> list[tuple[int, int]] | None:
    """Checks the 4 lines (horizontal/vertical/both diagonals) that pass through the
    just-dropped disc at (row, col) -- the only cell that could possibly complete a new
    four-in-a-row, so there's no need to scan the whole board after every move."""
    piece = board[row][col]
    for dr, dc in _DIRECTIONS:
        line = [(row, col)]
        for sign in (1, -1):
            r, c = row + dr * sign, col + dc * sign
            while 0 <= r < ROWS and 0 <= c < COLS and board[r][c] == piece:
                line.append((r, c))
                r, c = r + dr * sign, c + dc * sign
        if len(line) >= 4:
            return line[:4]
    return None


class MoveResult:
    def __init__(self, row: int, win_line: list[tuple[int, int]] | None, draw: bool):
        self.row = row
        self.win_line = win_line
        self.draw = draw


def apply_move(board: list[list[int]], col: int, is_challenger: bool) -> MoveResult:
    """Drops a disc into `col`, mutating `board` in place. Caller is responsible for having
    checked `col` is one of legal_moves(board)'s entries."""
    piece = CHALLENGER if is_challenger else OPPONENT
    row = ROWS - 1
    while board[row][col] != 0:
        row -= 1
    board[row][col] = piece
    win_line = _winning_line(board, row, col)
    draw = win_line is None and is_full(board)
    return MoveResult(row=row, win_line=win_line, draw=draw)
