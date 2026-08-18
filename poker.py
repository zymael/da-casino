from collections import Counter
from itertools import combinations

from game import Card

RANK_VALUES = {r: i for i, r in enumerate(
    ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"], start=2
)}

RANK_SINGULAR = {
    2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "10",
    11: "Jack", 12: "Queen", 13: "King", 14: "Ace",
}
RANK_PLURAL = {
    2: "2s", 3: "3s", 4: "4s", 5: "5s", 6: "6s", 7: "7s", 8: "8s", 9: "9s", 10: "10s",
    11: "Jacks", 12: "Queens", 13: "Kings", 14: "Aces",
}


def score_5(cards: list[Card]) -> tuple:
    """Scores a single 5-card hand as a tuple that sorts correctly against other hands."""
    ranks = sorted((RANK_VALUES[c.rank] for c in cards), reverse=True)
    is_flush = len({c.suit for c in cards}) == 1

    unique_ranks = sorted(set(ranks), reverse=True)
    straight_high = None
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            straight_high = unique_ranks[0]
        elif unique_ranks == [14, 5, 4, 3, 2]:
            straight_high = 5

    counts = Counter(ranks)
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    group_ranks = [r for r, _ in groups]
    pattern = [c for _, c in groups]

    if straight_high and is_flush:
        return (8, straight_high)
    if pattern[0] == 4:
        return (7, group_ranks[0], group_ranks[1])
    if pattern[0] == 3 and pattern[1] == 2:
        return (6, group_ranks[0], group_ranks[1])
    if is_flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if pattern[0] == 3:
        return (3, group_ranks[0], *group_ranks[1:])
    if pattern[0] == 2 and pattern[1] == 2:
        return (2, group_ranks[0], group_ranks[1], group_ranks[2])
    if pattern[0] == 2:
        return (1, group_ranks[0], *group_ranks[1:])
    return (0, *ranks)


def best_hand(seven_cards: list[Card]) -> tuple[tuple, list[Card]]:
    """Returns (score, best_5_cards) — the best-scoring 5-card combo out of the given cards."""
    best_score = None
    best_combo = None
    for combo in combinations(seven_cards, 5):
        score = score_5(list(combo))
        if best_score is None or score > best_score:
            best_score = score
            best_combo = list(combo)
    return best_score, best_combo


def describe_score(score: tuple) -> str:
    category = score[0]
    if category == 8:
        high = score[1]
        return "Royal Flush" if high == 14 else f"Straight Flush, {RANK_SINGULAR[high]}-high"
    if category == 7:
        return f"Four of a Kind, {RANK_PLURAL[score[1]]}"
    if category == 6:
        return f"Full House, {RANK_PLURAL[score[1]]} over {RANK_PLURAL[score[2]]}"
    if category == 5:
        return f"Flush, {RANK_SINGULAR[score[1]]}-high"
    if category == 4:
        return f"Straight, {RANK_SINGULAR[score[1]]}-high"
    if category == 3:
        return f"Three of a Kind, {RANK_PLURAL[score[1]]}"
    if category == 2:
        return f"Two Pair, {RANK_PLURAL[score[1]]} and {RANK_PLURAL[score[2]]}"
    if category == 1:
        return f"Pair of {RANK_PLURAL[score[1]]}"
    return f"High Card, {RANK_SINGULAR[score[1]]}"


def build_pots(contributions: dict[int, int], folded: set[int]) -> list[tuple[int, list[int]]]:
    """Splits total chip contributions into main/side pots.

    `contributions` maps user_id -> total chips they put in this hand (winners and
    folders alike). Returns a list of (pot_amount, eligible_user_ids), smallest all-in
    layer first. A pot's eligible winners are whichever contributors at that layer
    haven't folded.
    """
    contributors = {uid: amt for uid, amt in contributions.items() if amt > 0}
    levels = sorted(set(contributors.values()))
    pots = []
    prev = 0
    for level in levels:
        layer = level - prev
        participants = [uid for uid, amt in contributors.items() if amt >= level]
        amount = layer * len(participants)
        if amount > 0:
            eligible = [uid for uid in participants if uid not in folded]
            pots.append((amount, eligible))
        prev = level
    return pots
