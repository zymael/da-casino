import random

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


class Card:
    def __init__(self, rank: str, suit: str):
        self.rank = rank
        self.suit = suit

    def __str__(self):
        return f"{self.rank}{self.suit}"

    @property
    def value(self) -> int:
        if self.rank in ("J", "Q", "K"):
            return 10
        if self.rank == "A":
            return 11
        return int(self.rank)


class Deck:
    def __init__(self):
        self.cards = [Card(rank, suit) for suit in SUITS for rank in RANKS]
        random.shuffle(self.cards)

    def draw(self) -> Card:
        return self.cards.pop()


def hand_value(cards: list[Card]) -> int:
    total = sum(card.value for card in cards)
    aces = sum(1 for card in cards if card.rank == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(cards: list[Card]) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21


def format_hand(cards: list[Card], hide_first: bool = False) -> str:
    if hide_first:
        return "🂠 " + " ".join(str(card) for card in cards[1:])
    return " ".join(str(card) for card in cards)
