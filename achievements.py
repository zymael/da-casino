"""Registry of achievements, plus the shared logic for claiming and announcing them.

Two scopes:
  - "first"    -- claimed by exactly one user per guild, ever (e.g. first_horse). Never moves
                  once claimed, unlike the money/pizza crown badges which follow the current
                  leaderboard leader. Backed by db.award_first_achievement().
  - "personal" -- every user can claim it independently, once (e.g. winning a given game for
                  the first time). Backed by db.award_personal_achievement().

To add a new achievement: add an entry to ACHIEVEMENTS, then call `try_award_many()` (or have
`kinds_for_bet()` / `record_and_check()` pick it up automatically, for bet-outcome-based ones) at
the point it's earned.

Achievements sharing a "track" (e.g. every blackjack_wins_* tier) represent one progression --
callers that only want the highest tier a user has reached (like !achievements) should group by
`achievement.get("track", achievement["kind"])` and take the max by `achievement.get("tier", 0)`.
"""

import asyncio

import discord

import db

BIG_WIN_TIER_1 = 1_000
BIG_WIN_TIER_2 = 10_000
BIG_LOSS_TIER_1 = -1_000
BIG_LOSS_TIER_2 = -10_000

ACHIEVEMENTS = [
    {
        "kind": "first_horse",
        "scope": "first",
        "emoji": "🐴",
        "name": "First in the Gate",
        "description": "First person on the server to buy a horse.",
        "reward": 250,
    },
    {
        "kind": "win_blackjack",
        "scope": "personal",
        "emoji": "🃏",
        "name": "Blackjack!",
        "description": "Win a hand of blackjack.",
        "reward": 25,
    },
    {
        "kind": "win_slots",
        "scope": "personal",
        "emoji": "🎰",
        "name": "Jackpot",
        "description": "Win a spin on the slots.",
        "reward": 25,
    },
    {
        "kind": "win_roulette",
        "scope": "personal",
        "emoji": "🎡",
        "name": "Lucky Number",
        "description": "Win a bet on roulette.",
        "reward": 25,
    },
    {
        "kind": "win_horserace",
        "scope": "personal",
        "emoji": "🏁",
        "name": "Photo Finish",
        "description": "Win a bet at the horse track.",
        "reward": 25,
    },
    {
        "kind": "win_video_poker",
        "scope": "personal",
        "emoji": "🎴",
        "name": "Royal Flush Energy",
        "description": "Win a hand of video poker.",
        "reward": 25,
    },
    {
        "kind": "hit_jackpot",
        "scope": "personal",
        "emoji": "💰",
        "name": "Jackpot!",
        "description": "Hit the progressive jackpot on slots or video poker.",
        "reward": 200,
    },
    {
        "kind": "big_win_1",
        "scope": "personal",
        "emoji": "💰",
        "name": "High Roller",
        "description": f"Win {BIG_WIN_TIER_1}+ credits on a single bet.",
        "reward": 100,
    },
    {
        "kind": "big_win_2",
        "scope": "personal",
        "emoji": "🤑",
        "name": "Whale",
        "description": f"Win {BIG_WIN_TIER_2}+ credits on a single bet.",
        "reward": 500,
    },
    {
        "kind": "big_loss_1",
        "scope": "personal",
        "emoji": "💸",
        "name": "Ouch",
        "description": f"Lose {-BIG_LOSS_TIER_1}+ credits on a single bet.",
        "reward": 50,
    },
    {
        "kind": "big_loss_2",
        "scope": "personal",
        "emoji": "🩸",
        "name": "Rock Bottom",
        "description": f"Lose {-BIG_LOSS_TIER_2}+ credits on a single bet.",
        "reward": 250,
    },
    {
        "kind": "love_in_bloom",
        "scope": "personal",
        "emoji": "💐",
        "name": "Love in Bloom",
        "description": "You introduced yourself to the ranch hand, Kel!",
        "reward": 25,
    },
    {
        "kind": "morbed_by_kel",
        "scope": "personal",
        "emoji": "🐺",
        "name": "Morbed",
        "description": "Your Alpha called you \"kitten,\" then beat the ever loving shit out of you.",
        "reward": 25,
    },
    {
        "kind": "dared_by_mondor",
        "scope": "personal",
        "emoji": "🧙",
        "name": "Dare Accepted",
        "description": "You greeted Mondor at the dungeon entrance and accepted his challenge!",
        "reward": 25,
    },
    {
        "kind": "greeted_leto",
        "scope": "personal",
        "emoji": "🚐",
        "name": "New Neighbor",
        "description": "You greeted Leto at the Trailer Park.",
        "reward": 25,
    },
    {
        "kind": "bitten_by_princess",
        "scope": "personal",
        "emoji": "🐀",
        "name": "Bitten by a Princess",
        "description": "You greeted the \"Princess\". She was not appreciative.",
        "reward": 25,
    },
    {
        "kind": "stolen_from",
        "scope": "personal",
        "emoji": "💃",
        "name": "Sweet Talked",
        "description": "Get your roulette winnings stolen by a Lady of the evening (or a Classy Escort).",
        "reward": 50,
    },
    {
        "kind": "first_dream",
        "scope": "personal",
        "emoji": "💭",
        "name": "Sweet Dreams",
        "description": "Receive your first dream.",
        "reward": 25,
    },
    {
        "kind": "win_duel",
        "scope": "personal",
        "emoji": "⚔️",
        "name": "Duelist",
        "description": "Win a 1v1 duel against another player.",
        "reward": 25,
    },
    # Streak/comeback duel achievements -- a different axis from the generated duel_wins_{tier}
    # tiers below (cumulative wins), so they live here as their own standalone entries rather than
    # in that generator loop. Tracked via the generic `flags` table (key "duel_streak") and
    # PartyMember.was_low_hp, both in dungeon_view.py's _end_duel/_resolve_duel_turn.
    {
        "kind": "duel_streak_3",
        "scope": "personal",
        "emoji": "🔥",
        "name": "On a Roll",
        "description": "Win 3 duels in a row.",
        "reward": 50,
    },
    {
        "kind": "duel_streak_5",
        "scope": "personal",
        "emoji": "🔥",
        "name": "Hot Streak",
        "description": "Win 5 duels in a row.",
        "reward": 150,
    },
    {
        "kind": "duel_streak_10",
        "scope": "personal",
        "emoji": "🔥",
        "name": "Unstoppable",
        "description": "Win 10 duels in a row.",
        "reward": 500,
    },
    {
        "kind": "comeback_duel_win",
        "scope": "personal",
        "emoji": "🩸",
        "name": "Comeback Kid",
        "description": "Win a duel after dropping to 10% HP or below.",
        "reward": 100,
    },
    {
        "kind": "win_mancala",
        "scope": "personal",
        "emoji": "🟤",
        "name": "Pit Boss",
        "description": "Win a game of Mancala against another player.",
        "reward": 25,
    },
    {
        "kind": "win_connect4",
        "scope": "personal",
        "emoji": "🔴",
        "name": "Four in a Row",
        "description": "Win a game of Connect 4 against another player.",
        "reward": 25,
    },
    {
        "kind": "win_icebreak",
        "scope": "personal",
        "emoji": "🧊",
        "name": "Ice Cold",
        "description": "Win a game of Don't Break the Ice against another player.",
        "reward": 25,
    },
    {
        "kind": "win_uno",
        "scope": "personal",
        "emoji": "🔄",
        "name": "UNO Champion",
        "description": "Win a game of UNO against 1-3 other players.",
        "reward": 25,
    },
]

# Maps each game bucket to its emoji/title and the db.log_bet() `game` string(s) that feed it
# -- video poker's two variants share one bucket, matching win_video_poker above. Buckets not
# listed elsewhere (e.g. horserace_owner) don't get win/loss tracking or tier achievements.
GAMES = {
    "blackjack": {"emoji": "🃏", "title": "Blackjack", "log_keys": ["blackjack"], "first_win_kind": "win_blackjack"},
    "slots": {"emoji": "🎰", "title": "Slots", "log_keys": ["slots"], "first_win_kind": "win_slots"},
    "roulette": {"emoji": "🎡", "title": "Roulette", "log_keys": ["roulette"], "first_win_kind": "win_roulette"},
    "horserace": {"emoji": "🏁", "title": "Horse Racing", "log_keys": ["horserace"], "first_win_kind": "win_horserace"},
    "video_poker": {
        "emoji": "🎴", "title": "Video Poker", "log_keys": ["jacks_or_better", "deuces_wild"],
        "first_win_kind": "win_video_poker",
    },
    "duel": {"emoji": "⚔️", "title": "Duels", "log_keys": ["duel"], "first_win_kind": "win_duel"},
    "mancala": {"emoji": "🟤", "title": "Mancala", "log_keys": ["mancala"], "first_win_kind": "win_mancala"},
    "connect4": {"emoji": "🔴", "title": "Connect 4", "log_keys": ["connect4"], "first_win_kind": "win_connect4"},
    "icebreak": {"emoji": "🧊", "title": "Don't Break the Ice", "log_keys": ["icebreak"], "first_win_kind": "win_icebreak"},
    "uno": {"emoji": "🔄", "title": "UNO", "log_keys": ["uno"], "first_win_kind": "win_uno"},
}

# Win/loss count tiers -- reaching a tier claims that tier's kind ({bucket}_wins_{tier} /
# {bucket}_losses_{tier}), generated below for every game bucket. Rewards scale with the tier;
# losses earn less than a win at the same tier, same asymmetry as big_win_1/2 vs big_loss_1/2.
TIERS = [10, 25, 50, 100, 200, 500, 1000]
WIN_TIER_REWARDS = {10: 25, 25: 50, 50: 100, 100: 200, 200: 350, 500: 750, 1000: 1500}
LOSS_TIER_REWARDS = {10: 15, 25: 25, 50: 50, 100: 100, 200: 175, 500: 375, 1000: 750}

for _bucket, _info in GAMES.items():
    for _tier in TIERS:
        ACHIEVEMENTS.append({
            "kind": f"{_bucket}_wins_{_tier}",
            "scope": "personal",
            "emoji": _info["emoji"],
            "name": f"{_info['title']} — {_tier} Wins",
            "description": f"Win {_tier} bets of {_info['title']}.",
            "reward": WIN_TIER_REWARDS[_tier],
            "track": f"{_bucket}_wins",
            "tier": _tier,
        })
        ACHIEVEMENTS.append({
            "kind": f"{_bucket}_losses_{_tier}",
            "scope": "personal",
            "emoji": _info["emoji"],
            "name": f"{_info['title']} — {_tier} Losses",
            "description": f"Lose {_tier} bets of {_info['title']}.",
            "reward": LOSS_TIER_REWARDS[_tier],
            "track": f"{_bucket}_losses",
            "tier": _tier,
        })

BY_KIND = {achievement["kind"]: achievement for achievement in ACHIEVEMENTS}

# Maps a db.log_bet() `game` string to the personal achievement for winning it the first time,
# and to the game bucket its win/loss counts accumulate into.
WIN_GAME_KIND = {}
STAT_BUCKET = {}
for _bucket, _info in GAMES.items():
    for _log_key in _info["log_keys"]:
        WIN_GAME_KIND[_log_key] = _info["first_win_kind"]
        STAT_BUCKET[_log_key] = _bucket


def kinds_for_bet(game: str, net: int, is_win: bool | None = None) -> list[str]:
    """Every achievement kind a single resolved bet's net profit/loss newly qualifies for.
    Inclusive on the tiers -- a bet that wins 15,000 in one shot qualifies for both big_win_1
    and big_win_2, not just the higher one, since both thresholds are genuinely met.

    `is_win`, when given, overrides net's sign for deciding win vs. loss (but not the big_win/
    big_loss money-tier checks below, which always look at the real net) -- duels need this
    since a duel always has a winner/loser regardless of wager, unlike every other game here
    where net == 0 genuinely means "no result" (a push)."""
    kinds = []
    win = (net > 0) if is_win is None else is_win
    if win:
        win_kind = WIN_GAME_KIND.get(game)
        if win_kind:
            kinds.append(win_kind)
    if net >= BIG_WIN_TIER_1:
        kinds.append("big_win_1")
    if net >= BIG_WIN_TIER_2:
        kinds.append("big_win_2")
    if net <= BIG_LOSS_TIER_1:
        kinds.append("big_loss_1")
    if net <= BIG_LOSS_TIER_2:
        kinds.append("big_loss_2")
    return kinds


async def record_and_check(guild_id: int, user_id: int, game: str, net: int, is_win: bool | None = None) -> list[str]:
    """Records this bet's outcome in the user's per-game win/loss counts (a no-op for `game`
    buckets not in GAMES, and -- absent an `is_win` override -- for a push where net == 0) and
    returns every tier-achievement kind now satisfied by the updated count. Inclusive like
    kinds_for_bet -- a count that jumps past several tiers at once (or already-claimed tiers) is
    fine, since try_award_many is idempotent per kind.

    `is_win`, when given, overrides net's sign for win/loss (see kinds_for_bet) -- pass it for
    duels, which always have a winner/loser even at net == 0 (the default, wagerless case)."""
    win = (net > 0) if is_win is None else is_win
    if is_win is None and net == 0:
        return []
    bucket = STAT_BUCKET.get(game)
    if bucket is None:
        return []
    wins, losses = await asyncio.to_thread(db.record_game_outcome, guild_id, user_id, bucket, net, force_win=is_win)
    count, direction = (wins, "wins") if win else (losses, "losses")
    return [f"{bucket}_{direction}_{tier}" for tier in TIERS if count >= tier]


async def try_award_many(send, guild_id: int, user_id: int, display_name: str, kinds: list[str]):
    """Attempts to claim each kind in `kinds` for user_id (per its scope), grants the credit
    reward for every one actually won, and posts a single combined embed via `send` (an async
    callable like ctx.send / interaction.followup.send / message.channel.send) if at least one
    landed. Silently no-ops if none did (already claimed, or lost the race for a "first" one)."""
    unlocked = []
    for kind in kinds:
        achievement = BY_KIND[kind]
        if achievement["scope"] == "first":
            won = await asyncio.to_thread(db.award_first_achievement, guild_id, kind, user_id)
        else:
            won = await asyncio.to_thread(db.award_personal_achievement, guild_id, kind, user_id)
        if won:
            unlocked.append(achievement)

    if not unlocked:
        return

    total_reward = sum(achievement["reward"] for achievement in unlocked)
    if total_reward:
        await asyncio.to_thread(db.update_balance, guild_id, user_id, total_reward)

    if len(unlocked) == 1:
        achievement = unlocked[0]
        title = f"🏆 Achievement Unlocked: {achievement['emoji']} {achievement['name']}"
        description = f"**{display_name}** — {achievement['description']}"
    else:
        title = f"🏆 {len(unlocked)} Achievements Unlocked!"
        body = "\n".join(f"{achievement['emoji']} **{achievement['name']}** — {achievement['description']}" for achievement in unlocked)
        description = f"**{display_name}**\n{body}"

    embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
    if total_reward:
        embed.set_footer(text=f"+{total_reward} {db.get_currency_name(guild_id)}")
    await send(embed=embed)
