from collections import Counter

from game import Card, Deck
import poker

JACKS_OR_BETTER = "jacks_or_better"
DEUCES_WILD = "deuces_wild"

WILD_RANK = "2"
_RANK_VALUES = {r: i for i, r in enumerate(
    ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"], start=2
)}

# 9/6 Jacks or Better paytable — multiplier applied to the bet, in descending priority order.
_JOB_PAYTABLE: list[tuple[str, int]] = [
    ("Royal Flush", 800),
    ("Straight Flush", 50),
    ("Four of a Kind", 25),
    ("Full House", 9),
    ("Flush", 6),
    ("Straight", 4),
    ("Three of a Kind", 3),
    ("Two Pair", 2),
    ("Jacks or Better", 1),
]

_JOB_PREDICATES = {
    "Royal Flush": lambda s: s[0] == 8 and s[1] == 14,
    "Straight Flush": lambda s: s[0] == 8,
    "Four of a Kind": lambda s: s[0] == 7,
    "Full House": lambda s: s[0] == 6,
    "Flush": lambda s: s[0] == 5,
    "Straight": lambda s: s[0] == 4,
    "Three of a Kind": lambda s: s[0] == 3,
    "Two Pair": lambda s: s[0] == 2,
    "Jacks or Better": lambda s: s[0] == 1 and s[1] >= 11,
}

# Full-pay Deuces Wild paytable — deuces are wild, minimum paying hand is Three of a Kind.
_DW_PAYTABLE: list[tuple[str, int]] = [
    ("Natural Royal Flush", 800),
    ("Four Deuces", 200),
    ("Wild Royal Flush", 25),
    ("Five of a Kind", 15),
    ("Straight Flush", 9),
    ("Four of a Kind", 5),
    ("Full House", 3),
    ("Flush", 2),
    ("Straight", 2),
    ("Three of a Kind", 1),
]

PAYTABLES = {
    JACKS_OR_BETTER: _JOB_PAYTABLE,
    DEUCES_WILD: _DW_PAYTABLE,
}

GAME_TITLES = {
    JACKS_OR_BETTER: "Video Poker",
    DEUCES_WILD: "Deuces Wild",
}


def deal(deck: Deck) -> list[Card]:
    return [deck.draw() for _ in range(5)]


def draw_replacements(deck: Deck, hand: list[Card], held: list[bool]) -> list[Card]:
    """Returns a new 5-card hand: kept cards stay in place, the rest are redrawn."""
    return [card if hold else deck.draw() for card, hold in zip(hand, held)]


def _evaluate_jacks_or_better(cards: list[Card]) -> tuple[str, int]:
    score = poker.score_5(cards)
    for label, multiplier in _JOB_PAYTABLE:
        if _JOB_PREDICATES[label](score):
            return label, multiplier
    return "Nothing", 0


def _straight_high(ranks: set[int], wilds: int) -> int:
    """Highest straight (ace-high through the wheel) completable by filling gaps in the
    distinct `ranks` with `wilds` extra cards. Returns 0 if none is reachable."""
    for high in range(14, 4, -1):
        window = {14, 2, 3, 4, 5} if high == 5 else set(range(high - 4, high + 1))
        missing = window - ranks
        extra = ranks - window
        if not extra and len(missing) <= wilds:
            return high
    return 0


def _full_house_possible(counts: list[int], wilds: int) -> bool:
    """Whether a 3+2 split is reachable: pick two distinct rank-groups (real or a fresh
    rank built entirely from wilds) to fill toward 3 and 2, minimizing wilds spent."""
    groups = counts + [0, 0]  # the two zero pads stand in for up to two fresh ranks
    best_cost = min(
        max(0, 3 - a) + max(0, 2 - b)
        for i, a in enumerate(groups)
        for j, b in enumerate(groups)
        if i != j
    )
    return best_cost <= wilds


def _evaluate_deuces_wild(cards: list[Card]) -> tuple[str, int]:
    wild_count = sum(1 for c in cards if c.rank == WILD_RANK)
    if wild_count == 4:
        return "Four Deuces", 200

    others = [c for c in cards if c.rank != WILD_RANK]
    rank_vals = [_RANK_VALUES[c.rank] for c in others]
    same_suit = len({c.suit for c in others}) <= 1
    has_dup_rank = len(set(rank_vals)) != len(rank_vals)
    straight_high = 0 if has_dup_rank else _straight_high(set(rank_vals), wild_count)

    if same_suit and straight_high == 14:
        return ("Natural Royal Flush", 800) if wild_count == 0 else ("Wild Royal Flush", 25)

    counts = sorted(Counter(rank_vals).values(), reverse=True)
    max_count = counts[0] if counts else 0

    if max_count + wild_count >= 5:
        return "Five of a Kind", 15
    if same_suit and straight_high:
        return "Straight Flush", 9
    if max_count + wild_count >= 4:
        return "Four of a Kind", 5
    if _full_house_possible(counts, wild_count):
        return "Full House", 3
    if same_suit:
        return "Flush", 2
    if straight_high:
        return "Straight", 2
    if max_count + wild_count >= 3:
        return "Three of a Kind", 1
    return "Nothing", 0


def evaluate(cards: list[Card], variant: str = JACKS_OR_BETTER) -> tuple[str, int]:
    """Returns (hand label, payout multiplier) for a 5-card hand; multiplier 0 = no win."""
    if variant == DEUCES_WILD:
        return _evaluate_deuces_wild(cards)
    return _evaluate_jacks_or_better(cards)
