"""Pure Mancala (Kalah) rules -- no Discord/view code, same split as dungeon.py (rules) vs.
dungeon_view.py (Discord session/UI) vs. dungeon_render.py (image).

Board is a flat 14-int list. Indices 0-5 are the challenger's own pits, 6 is the challenger's
store; 7-12 are the opponent's pits, 13 is the opponent's store. Sowing always advances by +1 mod
14 (the natural counterclockwise order — challenger's row left-to-right into their own store, then
continuing into the opponent's row, then into the opponent's store, then back around), skipping
only the *other* side's store.
"""

PITS_PER_SIDE = 6
STARTING_STONES = 4
BOARD_SIZE = 14
CHALLENGER_STORE = 6
OPPONENT_STORE = 13


def new_board() -> list[int]:
    board = [STARTING_STONES] * BOARD_SIZE
    board[CHALLENGER_STORE] = 0
    board[OPPONENT_STORE] = 0
    return board


def own_store(is_challenger: bool) -> int:
    return CHALLENGER_STORE if is_challenger else OPPONENT_STORE


def side_pits(is_challenger: bool) -> range:
    return range(0, 6) if is_challenger else range(7, 13)


def opposite_pit(pit: int) -> int:
    """The pit directly across the board -- 0<->12, 1<->11, ... 5<->7. Used by the capture rule."""
    return 12 - pit


def legal_moves(board: list[int], is_challenger: bool) -> list[int]:
    return [i for i in side_pits(is_challenger) if board[i] > 0]


class MoveResult:
    def __init__(self, extra_turn: bool, captured: int, game_over: bool):
        self.extra_turn = extra_turn
        self.captured = captured
        self.game_over = game_over


def apply_move(board: list[int], pit: int, is_challenger: bool) -> MoveResult:
    """Sows `pit`'s stones one-by-one into every subsequent pit (skipping the opponent's store),
    applies the extra-turn/capture rules, mutates `board` in place, and sweeps + ends the game if
    either side is left with no legal moves. Caller is responsible for having checked `pit` is one
    of `is_challenger`'s own non-empty pits."""
    stones = board[pit]
    board[pit] = 0
    skip_store = OPPONENT_STORE if is_challenger else CHALLENGER_STORE
    idx = pit
    while stones > 0:
        idx = (idx + 1) % BOARD_SIZE
        if idx == skip_store:
            continue
        board[idx] += 1
        stones -= 1

    landed_in_own_store = idx == own_store(is_challenger)
    captured = 0
    if not landed_in_own_store and idx in side_pits(is_challenger) and board[idx] == 1:
        # Last stone landed in a pit on our own side that was empty before it -- capture it plus
        # whatever's opposite, both going straight to our store (a "capture" of just our own 1
        # stone when the opposite pit happens to be empty is harmless -- same net effect as not
        # capturing at all -- so there's no need to special-case that).
        opp_idx = opposite_pit(idx)
        captured = board[idx] + board[opp_idx]
        board[own_store(is_challenger)] += captured
        board[idx] = 0
        board[opp_idx] = 0

    game_over = is_game_over(board)
    if game_over:
        sweep(board)
    return MoveResult(extra_turn=landed_in_own_store and not game_over, captured=captured, game_over=game_over)


def is_game_over(board: list[int]) -> bool:
    return all(board[i] == 0 for i in side_pits(True)) or all(board[i] == 0 for i in side_pits(False))


def sweep(board: list[int]) -> None:
    """Once one side has no legal moves left, the other side keeps whatever's still sitting in
    their own pits -- swept straight into their store."""
    for i in side_pits(True):
        board[CHALLENGER_STORE] += board[i]
        board[i] = 0
    for i in side_pits(False):
        board[OPPONENT_STORE] += board[i]
        board[i] = 0


def winner(board: list[int]) -> bool | None:
    """True if the challenger has more stones in their store, False if the opponent does, None on
    a tie. Only meaningful once is_game_over(board) is true."""
    if board[CHALLENGER_STORE] == board[OPPONENT_STORE]:
        return None
    return board[CHALLENGER_STORE] > board[OPPONENT_STORE]
