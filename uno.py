"""Pure UNO rules -- no Discord/PIL code, same split as mancala.py/connect4.py/icebreak.py.

Official rules only: no stacking +2s/+4s, no jump-in, no 7-0 rule. On a seat's turn the caller
(uno_view.py) is responsible for offering exactly one of: play a card from `legal_plays`, or (only
when `legal_plays` is empty) `draw_card` followed by either `apply_play` on the freshly-drawn card
if it's now playable, or `pass_turn` if not/if the player declines. Every function here trusts the
caller to have already checked legality -- same "caller checked legal_moves first" contract as
mancala.apply_move/connect4.apply_move.
"""

import random

COLORS = ["red", "yellow", "green", "blue"]
ACTIONS = ["skip", "reverse", "draw_two"]
WILDS = ["wild", "wild_draw_four"]

MIN_PLAYERS = 2
MAX_PLAYERS = 4
HAND_SIZE = 7

_COLOR_ORDER = {"red": 0, "yellow": 1, "green": 2, "blue": 3, "wild": 4}
_ACTION_ORDER = {"skip": 0, "reverse": 1, "draw_two": 2}
_WILD_ORDER = {"wild": 0, "wild_draw_four": 1}


class Card:
    __slots__ = ("color", "kind")

    def __init__(self, color: str, kind: str):
        self.color = color  # one of COLORS, or "wild" for both WILDS kinds
        self.kind = kind  # "0"-"9", one of ACTIONS, or one of WILDS

    def is_wild(self) -> bool:
        return self.color == "wild"

    def __repr__(self) -> str:
        return f"Card({self.color}, {self.kind})"


def build_deck() -> list[Card]:
    """The real 108-card distribution: per color, one 0, two each of 1-9, and two each of
    Skip/Reverse/Draw Two (1 + 18 + 6 = 25 per color x 4 = 100), plus 4 Wild + 4 Wild Draw Four."""
    cards = []
    for color in COLORS:
        cards.append(Card(color, "0"))
        for n in range(1, 10):
            cards.append(Card(color, str(n)))
            cards.append(Card(color, str(n)))
        for action in ACTIONS:
            cards.append(Card(color, action))
            cards.append(Card(color, action))
    for _ in range(4):
        cards.append(Card("wild", "wild"))
        cards.append(Card("wild", "wild_draw_four"))
    random.shuffle(cards)
    return cards


def _sort_key(card: Card) -> tuple:
    if card.kind.isdigit():
        return (_COLOR_ORDER[card.color], 0, int(card.kind))
    if card.kind in _ACTION_ORDER:
        return (_COLOR_ORDER[card.color], 1, _ACTION_ORDER[card.kind])
    return (_COLOR_ORDER[card.color], 2, _WILD_ORDER[card.kind])


def sorted_hand(hand: list[Card]) -> list[Card]:
    """Canonical display/interaction order -- color, then number, then action cards within a
    color, wilds last of all. uno_render.render_hand and the ephemeral hand view's button layout
    both consume this so the image and the buttons always agree on ordering."""
    return sorted(hand, key=_sort_key)


class Seat:
    def __init__(self, user_id: int, name: str):
        self.user_id = user_id
        self.name = name
        self.hand: list[Card] = []


class UnoGame:
    def __init__(self, seats: list[tuple[int, str]]):
        self.seats = [Seat(uid, name) for uid, name in seats]
        self.draw_pile: list[Card] = build_deck()
        self.discard: list[Card] = []
        self.direction = 1
        self.current_index = 0
        self.current_color: str | None = None

    def current_seat(self) -> Seat:
        return self.seats[self.current_index]

    def top_card(self) -> Card:
        return self.discard[-1]


def deal_initial_hands(game: UnoGame, hand_size: int = HAND_SIZE) -> None:
    """Deals hand_size cards to each seat round-robin, then flips a starting card -- kept simple
    by re-drawing (reshuffling the rejected card back in) until a plain number card comes up, so
    the very first turn never has to handle a starting Skip/Reverse/Draw Two/Wild's special
    effects (a common simplification; the official rule for handling those on the very first flip
    is unusually fiddly for the value it adds here)."""
    for _ in range(hand_size):
        for seat in game.seats:
            seat.hand.append(game.draw_pile.pop())
    starter = game.draw_pile.pop()
    while not starter.kind.isdigit():
        game.draw_pile.insert(0, starter)
        random.shuffle(game.draw_pile)
        starter = game.draw_pile.pop()
    game.discard.append(starter)
    game.current_color = starter.color


def card_matches(card: Card, top: Card, current_color: str) -> bool:
    if card.is_wild():
        return True
    if card.color == current_color:
        return True
    return card.kind == top.kind


def legal_plays(hand: list[Card], top: Card, current_color: str) -> list[Card]:
    return [c for c in hand if card_matches(c, top, current_color)]


def _draw_one(game: UnoGame) -> Card:
    """Pops from the draw pile, reshuffling the discard pile (everything but the current top
    card) back into the draw pile first if it's run dry -- the real deck can and does run out."""
    if not game.draw_pile:
        top = game.discard[-1]
        rest = game.discard[:-1]
        random.shuffle(rest)
        game.draw_pile = rest
        game.discard = [top]
    return game.draw_pile.pop()


def draw_card(game: UnoGame, seat_index: int) -> Card:
    card = _draw_one(game)
    game.seats[seat_index].hand.append(card)
    return card


def advance_turn(game: UnoGame, steps: int = 1) -> None:
    game.current_index = (game.current_index + game.direction * steps) % len(game.seats)


def pass_turn(game: UnoGame) -> None:
    """The drawn card wasn't playable, or the player chose not to play it -- turn just moves on."""
    advance_turn(game, 1)


class MoveResult:
    def __init__(self, winner: bool, announce_uno: bool):
        self.winner = winner
        self.announce_uno = announce_uno


def apply_play(game: UnoGame, seat_index: int, card: Card, chosen_color: str | None = None) -> MoveResult:
    """Plays `card` out of seat_index's hand (removed by identity, not equality -- a hand can hold
    two otherwise-identical cards, e.g. two Red 7s, and the player picked a specific one via its
    own button), applies its effect, and advances the turn. Caller is responsible for having
    already checked this seat is game.current_index and the card is in legal_plays(...). Winning
    is checked before any turn-advancement/card-effect logic runs -- emptying your hand ends the
    round immediately regardless of what the card would otherwise have done."""
    seat = game.seats[seat_index]
    seat.hand.remove(card)
    game.discard.append(card)
    game.current_color = chosen_color if card.is_wild() else card.color

    if not seat.hand:
        return MoveResult(winner=True, announce_uno=False)

    if card.kind == "skip":
        advance_turn(game, 2)
    elif card.kind == "reverse":
        if len(game.seats) == 2:
            advance_turn(game, 2)  # reverse acts exactly like skip in a 2-player game
        else:
            game.direction *= -1
            advance_turn(game, 1)
    elif card.kind == "draw_two":
        advance_turn(game, 1)
        for _ in range(2):
            draw_card(game, game.current_index)
        advance_turn(game, 1)
    elif card.kind == "wild_draw_four":
        advance_turn(game, 1)
        for _ in range(4):
            draw_card(game, game.current_index)
        advance_turn(game, 1)
    else:
        advance_turn(game, 1)

    return MoveResult(winner=False, announce_uno=len(seat.hand) == 1)
