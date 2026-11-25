"""Decision-making for NPC Hold'em bots (holdem_view.py seats them; npcs.json's poker_* fields give
each one a personality). Pure functions over cards and numbers -- no discord, no db -- so the
whole brain is testable on its own.

How a bot decides:
  1. Estimate its equity (chance to win the pot at showdown) by Monte Carlo: deal the opponents
     random hole cards and the rest of the board, many times over (`estimate_equity`).
  2. Discount that against a bet. Random hands are weaker than the hands people actually put
     money in with, so the bigger the bet it faces relative to the pot, the more it shades its
     equity down (`_adjusted_equity`).
  3. Turn that into an action through its personality (`decide`):
       tightness  -- how much extra equity over the pot odds it wants before calling
       aggression -- how readily it raises a good hand instead of just calling
       bluff      -- chance to bet a hand that would otherwise check or fold
       allin_rate -- chance a raise becomes an all-in shove
     Every personality still respects the pot odds within a few points, caps how much of its
     stack a bluff can cost (wilder bluffers risk more), and stops re-raising after two raises a
     street. Loose is fun; suicidal
     isn't.
"""

import random
from dataclasses import dataclass

import poker
from game import RANKS, SUITS, Card

DEFAULT_ITERATIONS = 300


@dataclass
class Personality:
    tightness: float = 0.5
    aggression: float = 0.5
    bluff: float = 0.08
    allin_rate: float = 0.05

    @classmethod
    def from_npc(cls, npc: dict) -> "Personality":
        return cls(
            tightness=npc.get("poker_tightness", cls.tightness),
            aggression=npc.get("poker_aggression", cls.aggression),
            bluff=npc.get("poker_bluff", cls.bluff),
            allin_rate=npc.get("poker_allin_rate", cls.allin_rate),
        )


@dataclass
class Situation:
    hole: list
    community: list
    n_opponents: int  # opponents still in the hand (not folded)
    to_call: int
    pot: int  # everything already in the middle, this street included
    current_bet: int  # the street's highest total bet
    street_contributed: int  # what this bot has already put in this street
    remaining: int  # this bot's stack behind
    big_blind: int
    raises_this_street: int = 0  # how many times this bot has already raised this street


def estimate_equity(
    hole: list, community: list, n_opponents: int, iterations: int = DEFAULT_ITERATIONS,
    rng: random.Random | None = None,
) -> float:
    """Share of the pot this hand wins on average against `n_opponents` random hands, ties split."""
    rng = rng or random
    if n_opponents <= 0:
        return 1.0
    known = {(c.rank, c.suit) for c in hole + community}
    stub = [Card(r, s) for s in SUITS for r in RANKS if (r, s) not in known]
    board_needed = 5 - len(community)
    deal = board_needed + 2 * n_opponents
    total = 0.0
    for _ in range(iterations):
        drawn = rng.sample(stub, deal)
        board = community + drawn[:board_needed]
        mine = poker.best_hand(hole + board)[0]
        others = [
            poker.best_hand(drawn[board_needed + 2 * i:board_needed + 2 * i + 2] + board)[0]
            for i in range(n_opponents)
        ]
        best_other = max(others)
        if mine > best_other:
            total += 1.0
        elif mine == best_other:
            total += 1.0 / (1 + others.count(mine))
    return total / iterations


def equity_vs_hands(
    hands: dict, community: list, iterations: int = 1500, rng: random.Random | None = None,
) -> dict:
    """True equity of each known hand ({key: [2 cards]}) against the others, running out the rest of
    the board -- what everyone's chances were once all the money was in. Ties split."""
    rng = rng or random
    known = {(c.rank, c.suit) for c in community} | {(c.rank, c.suit) for h in hands.values() for c in h}
    stub = [Card(r, s) for s in SUITS for r in RANKS if (r, s) not in known]
    needed = 5 - len(community)
    totals = dict.fromkeys(hands, 0.0)
    runs = iterations if needed else 1
    for _ in range(runs):
        board = community + (rng.sample(stub, needed) if needed else [])
        scores = {k: poker.best_hand(h + board)[0] for k, h in hands.items()}
        best = max(scores.values())
        winners = [k for k, s in scores.items() if s == best]
        for k in winners:
            totals[k] += 1.0 / len(winners)
    return {k: v / runs for k, v in totals.items()}


def _adjusted_equity(equity: float, sit: Situation) -> float:
    """Shades raw equity (measured against random hands) down when facing a bet, since whoever bet
    usually holds better than random. The fair share (1/players) is the floor it shrinks toward."""
    if sit.to_call <= sit.big_blind:
        return equity
    pressure = min(1.0, sit.to_call / max(1, sit.pot))
    fair = 1.0 / (sit.n_opponents + 1)
    if equity <= fair:
        return equity * (1 - 0.25 * pressure)
    return equity - (equity - fair) * 0.35 * pressure


def _raise_to(sit: Situation, strength: float, p: Personality, rng: random.Random) -> int:
    """A total-bet amount for a raise: 40% to 80% of the pot, a bit bigger for stronger hands and
    more aggressive bots. Kept modest so calling a bot down isn't ruinous when it has it."""
    frac = 0.4 + 0.15 * min(1.0, max(0.0, strength)) + 0.25 * p.aggression + rng.uniform(-0.1, 0.1)
    pot_after_call = sit.pot + sit.to_call
    size = max(sit.big_blind, int(pot_after_call * frac))
    return sit.current_bet + size


def _as_raise(sit: Situation, raise_to: int) -> tuple:
    max_total = sit.street_contributed + sit.remaining
    if raise_to >= max_total or raise_to <= sit.current_bet:
        return ("allin",)
    return ("raise", raise_to)


def _bluff_cap(p: Personality, base: float) -> float:
    """Most of its stack a bluff may cost: `base` plus its bluff rate, so a wild bot (bluff 0.45)
    bluffs up to ~70% of its stack and a careful one (0.1) about a third."""
    return min(0.75, base + p.bluff)


def decide(sit: Situation, p: Personality, equity: float, rng: random.Random | None = None) -> tuple:
    """One of ("fold",), ("check",), ("call",), ("raise", total), ("allin",) -- the same action
    tuples holdem_view.apply_action takes from a human's buttons."""
    rng = rng or random
    eq = _adjusted_equity(equity, sit)
    fair = 1.0 / (sit.n_opponents + 1)
    # How far above an even share this hand is: 0 = average, 1 = a lock.
    strength = (eq - fair) / (1 - fair) if fair < 1 else 1.0

    can_raise = sit.remaining > sit.to_call and sit.raises_this_street < 2
    call_commits_most = sit.to_call >= 0.6 * sit.remaining

    # Monsters: raise (or shove) almost always, aggression only decides how often it slowplays.
    raise_bar = 0.55 - 0.35 * p.aggression  # aggression 0 -> 0.55, 1 -> 0.20
    if can_raise and strength >= raise_bar and rng.random() < 0.45 + 0.55 * p.aggression:
        # A near-lock only shoves once the pot is already about as big as its stack.
        pot_committed = sit.remaining <= 1.5 * (sit.pot + sit.to_call)
        if rng.random() < p.allin_rate or call_commits_most or (strength > 0.85 and pot_committed):
            return ("allin",)
        return _as_raise(sit, _raise_to(sit, strength, p, rng))

    if sit.to_call <= 0:
        # Free to check. A bluff never risks a big share of the stack, though wilder bots risk more.
        if can_raise and rng.random() < p.bluff:
            raise_to = _raise_to(sit, 0.0, p, rng)
            if raise_to - sit.street_contributed <= _bluff_cap(p, 0.25) * sit.remaining:
                return _as_raise(sit, raise_to)
        return ("check",)

    pot_odds = sit.to_call / (sit.pot + sit.to_call)
    margin = -0.04 + 0.14 * p.tightness  # loosest bots call slightly light, tightest want a cushion
    if call_commits_most:
        margin += 0.05  # stacking off wants a real edge, whatever the personality
    if eq >= pot_odds + margin:
        if call_commits_most and eq >= fair * 1.4:
            return ("allin",)
        return ("call",)

    # Would fold. A cheap re-raise bluff is occasionally worth it against a small bet.
    if (
        can_raise and sit.to_call <= 3 * sit.big_blind and rng.random() < p.bluff * 0.5
    ):
        raise_to = _raise_to(sit, 0.0, p, rng)
        if raise_to - sit.street_contributed <= _bluff_cap(p, 0.2) * sit.remaining:
            return _as_raise(sit, raise_to)
    return ("fold",)
