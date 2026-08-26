"""Pure "Don't Break the Ice" rules -- no Discord/view code, same split as mancala.py/connect4.py.

The board is a single sheet of ice pressed together inside a sturdy frame that never breaks --
structurally, a cell only matters in relation to its neighbors: as long as every remaining
(unbroken) cell is still reachable from every other remaining cell through a chain of adjacent
unbroken cells, the whole sheet is one rigid piece and holds its shape. The moment a break
fractures the remaining ice into two or more separate pieces (or clears the board entirely), that
piece has nothing left holding it up and the platform on top sags through -- whoever's break
caused that failure loses, no draws are possible.
"""

ROWS = 5
COLS = 5


def new_board() -> list[list[int]]:
    return [[1] * COLS for _ in range(ROWS)]


def legal_moves(board: list[list[int]]) -> list[tuple[int, int]]:
    return [(r, c) for r in range(ROWS) for c in range(COLS) if board[r][c] == 1]


def connected_components(board: list[list[int]]) -> list[list[tuple[int, int]]]:
    """Every maximal group of still-intact cells reachable from each other via 4-directional
    adjacency. The ice sheet is structurally sound exactly when this returns a single component
    covering every remaining cell -- see apply_move."""
    seen: set[tuple[int, int]] = set()
    components: list[list[tuple[int, int]]] = []
    for start_r in range(ROWS):
        for start_c in range(COLS):
            if board[start_r][start_c] != 1 or (start_r, start_c) in seen:
                continue
            stack = [(start_r, start_c)]
            seen.add((start_r, start_c))
            component = []
            while stack:
                r, c = stack.pop()
                component.append((r, c))
                for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < ROWS and 0 <= nc < COLS and board[nr][nc] == 1 and (nr, nc) not in seen:
                        seen.add((nr, nc))
                        stack.append((nr, nc))
            components.append(component)
    return components


class MoveResult:
    def __init__(self, collapsed: bool, components: list[list[tuple[int, int]]]):
        self.collapsed = collapsed
        self.components = components


def apply_move(board: list[list[int]], row: int, col: int) -> MoveResult:
    """Breaks (row, col), mutating `board` in place. Caller is responsible for having checked
    (row, col) is one of legal_moves(board)'s entries. `collapsed` is true the instant the
    remaining ice is no longer exactly one connected piece -- including the degenerate case of
    zero cells left, which structurally fails just as much as a split does."""
    board[row][col] = 0
    components = connected_components(board)
    collapsed = len(components) != 1
    return MoveResult(collapsed=collapsed, components=components)
