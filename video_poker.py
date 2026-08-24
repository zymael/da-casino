from collections import Counter, defaultdict
from itertools import combinations

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

# Landing either variant's top-tier natural hand also claims the progressive jackpot.
JACKPOT_HANDS = {"Royal Flush", "Natural Royal Flush"}

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


# --- Auto-hold: a simplified published-strategy hold advisor ------------------------------
#
# This is the well-known "expert strategy" shape used by real-money video poker trainers: an
# ordered list of hand patterns from best to worst, first match wins. It is not a brute-force
# EV search (that would mean scoring up to ~2.6M possible draws per hand, far too slow for a
# live Discord interaction) — it's a heuristic approximation that gets every clear-cut case
# right and collapses a few of the rarer close calls (e.g. inside vs. outside straight draws)
# that a certified strategy trainer would split further.

ROYAL_VALUES = {10, 11, 12, 13, 14}


def _rank_groups(hand: list[Card], idxs: list[int]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for i in idxs:
        groups[hand[i].rank].append(i)
    return groups


def _suit_groups(hand: list[Card], idxs: list[int]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for i in idxs:
        groups[hand[i].suit].append(i)
    return groups


def _find_group(hand: list[Card], idxs: list[int], size: int) -> list[int] | None:
    """First rank-group of exactly `size` cards among idxs."""
    for group in _rank_groups(hand, idxs).values():
        if len(group) == size:
            return group
    return None


def _fits_straight_window(rank_values: set[int]) -> bool:
    """Whether every value in rank_values lies inside some single 5-consecutive-rank window
    (ace-high through the wheel) — i.e. this could still become a straight."""
    for high in range(14, 4, -1):
        window = {14, 2, 3, 4, 5} if high == 5 else set(range(high - 4, high + 1))
        if rank_values <= window:
            return True
    return False


def _find_flush_draw(hand: list[Card], idxs: list[int], n: int) -> list[int] | None:
    for group in _suit_groups(hand, idxs).values():
        if len(group) >= n:
            return group[:n]
    return None


def _find_straight_draw(hand: list[Card], idxs: list[int], n: int) -> list[int] | None:
    """n cards (any suits) whose distinct ranks fit a single straight window."""
    by_value: dict[int, int] = {}
    for i in idxs:
        by_value[_RANK_VALUES[hand[i].rank]] = i
    values = list(by_value.keys())
    if len(values) < n:
        return None
    for combo in combinations(values, n):
        if _fits_straight_window(set(combo)):
            return [by_value[v] for v in combo]
    return None


def _find_suited_straight(hand: list[Card], idxs: list[int], n: int) -> list[int] | None:
    """n suited cards whose ranks fit a single straight window (a straight-flush draw)."""
    for suit, group in _suit_groups(hand, idxs).items():
        if len(group) < n:
            continue
        for combo in combinations(group, n):
            values = {_RANK_VALUES[hand[i].rank] for i in combo}
            if _fits_straight_window(values):
                return list(combo)
    return None


def _find_royal_draw(hand: list[Card], idxs: list[int], n: int) -> list[int] | None:
    for group in _suit_groups(hand, idxs).values():
        royal = [i for i in group if _RANK_VALUES[hand[i].rank] in ROYAL_VALUES]
        if len(royal) >= n:
            return royal[:n]
    return None


def _find_suited_high(hand: list[Card], idxs: list[int], n: int) -> list[int] | None:
    for group in _suit_groups(hand, idxs).values():
        high = [i for i in group if _RANK_VALUES[hand[i].rank] >= 11]
        if len(high) >= n:
            return high[:n]
    return None


def _suggest_jacks_or_better(hand: list[Card]) -> list[bool]:
    idxs = list(range(5))
    label, _ = _evaluate_jacks_or_better(hand)

    def hold(indices) -> list[bool]:
        indices = set(indices)
        return [i in indices for i in idxs]

    if label in ("Royal Flush", "Straight Flush"):
        return [True] * 5
    if label == "Four of a Kind":
        return hold(_find_group(hand, idxs, 4))

    four_royal = _find_royal_draw(hand, idxs, 4)
    if four_royal:
        return hold(four_royal)

    if label in ("Full House", "Flush", "Straight"):
        return [True] * 5
    if label == "Three of a Kind":
        return hold(_find_group(hand, idxs, 3))

    four_sf = _find_suited_straight(hand, idxs, 4)
    if four_sf:
        return hold(four_sf)

    if label == "Two Pair":
        pairs = [i for group in _rank_groups(hand, idxs).values() if len(group) == 2 for i in group]
        return hold(pairs)
    if label == "Jacks or Better":
        return hold(_find_group(hand, idxs, 2))

    three_royal = _find_royal_draw(hand, idxs, 3)
    if three_royal:
        return hold(three_royal)

    four_flush = _find_flush_draw(hand, idxs, 4)
    if four_flush:
        return hold(four_flush)

    low_pair = _find_group(hand, idxs, 2)
    if low_pair:
        return hold(low_pair)

    four_straight = _find_straight_draw(hand, idxs, 4)
    if four_straight:
        return hold(four_straight)

    two_suited_high = _find_suited_high(hand, idxs, 2)
    if two_suited_high:
        return hold(two_suited_high)

    three_sf = _find_suited_straight(hand, idxs, 3)
    if three_sf:
        return hold(three_sf)

    high_cards = [i for i in idxs if _RANK_VALUES[hand[i].rank] >= 11]
    if high_cards:
        best = max(high_cards, key=lambda i: _RANK_VALUES[hand[i].rank])
        return hold([best])

    return [False] * 5


def _suggest_deuces_wild(hand: list[Card]) -> list[bool]:
    idxs = list(range(5))
    deuce_idxs = [i for i in idxs if hand[i].rank == WILD_RANK]
    other_idxs = [i for i in idxs if i not in deuce_idxs]
    wild_count = len(deuce_idxs)
    label, _ = _evaluate_deuces_wild(hand)

    def hold(indices) -> list[bool]:
        indices = set(indices)
        return [i in indices for i in idxs]

    if label in ("Natural Royal Flush", "Four Deuces", "Wild Royal Flush", "Five of a Kind", "Straight Flush"):
        return [True] * 5

    if wild_count == 3:
        # Every other split of 3 deuces + 2 more cards is already a pat hand caught above,
        # or a guaranteed-but-beatable Full House — discarding the 2 kickers is correct either way.
        return hold(deuce_idxs)

    if wild_count == 2:
        royal_draw = _find_royal_draw(hand, other_idxs, 2)
        if royal_draw:
            return hold(deuce_idxs + royal_draw)
        pair = _find_group(hand, other_idxs, 2)
        if pair:
            return hold(deuce_idxs + pair)
        return hold(deuce_idxs)

    if wild_count == 1:
        if label == "Full House":
            return [True] * 5
        trip = _find_group(hand, other_idxs, 3)
        if trip:
            return hold(deuce_idxs + trip)
        royal_draw = _find_royal_draw(hand, other_idxs, 3)
        if royal_draw:
            return hold(deuce_idxs + royal_draw)
        pair = _find_group(hand, other_idxs, 2)
        if pair:
            return hold(deuce_idxs + pair)
        flush_draw = _find_flush_draw(hand, other_idxs, 3)
        if flush_draw:
            return hold(deuce_idxs + flush_draw)
        straight_draw = _find_straight_draw(hand, other_idxs, 3)
        if straight_draw:
            return hold(deuce_idxs + straight_draw)
        royal_draw2 = _find_royal_draw(hand, other_idxs, 2)
        if royal_draw2:
            return hold(deuce_idxs + royal_draw2)
        return hold(deuce_idxs)

    # wild_count == 0
    if label == "Four of a Kind":
        return hold(_find_group(hand, other_idxs, 4))
    if label in ("Full House", "Flush", "Straight"):
        return [True] * 5
    if label == "Three of a Kind":
        return hold(_find_group(hand, other_idxs, 3))

    four_sf = _find_suited_straight(hand, other_idxs, 4)
    if four_sf:
        return hold(four_sf)

    three_royal = _find_royal_draw(hand, other_idxs, 3)
    if three_royal:
        return hold(three_royal)

    two_pair_groups = [g for g in _rank_groups(hand, other_idxs).values() if len(g) == 2]
    if len(two_pair_groups) == 2:
        return hold(two_pair_groups[0])

    four_flush = _find_flush_draw(hand, other_idxs, 4)
    if four_flush:
        return hold(four_flush)

    four_straight = _find_straight_draw(hand, other_idxs, 4)
    if four_straight:
        return hold(four_straight)

    three_sf = _find_suited_straight(hand, other_idxs, 3)
    if three_sf:
        return hold(three_sf)

    pair = _find_group(hand, other_idxs, 2)
    if pair:
        return hold(pair)

    return [False] * 5


def suggest_hold(hand: list[Card], variant: str = JACKS_OR_BETTER) -> list[bool]:
    """Returns a 5-element hold mask picking the best cards to keep, per a simplified
    published expert-strategy chart for the given variant."""
    if variant == DEUCES_WILD:
        return _suggest_deuces_wild(hand)
    return _suggest_jacks_or_better(hand)
