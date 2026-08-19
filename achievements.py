"""Registry of achievements, plus the shared logic for claiming and announcing them.

Two scopes:
  - "first"    -- claimed by exactly one user per guild, ever (e.g. first_horse). Never moves
                  once claimed, unlike the money/pizza crown badges which follow the current
                  leaderboard leader. Backed by db.award_first_achievement().
  - "personal" -- every user can claim it independently, once (e.g. winning a given game for
                  the first time). Backed by db.award_personal_achievement().

To add a new achievement: add an entry to ACHIEVEMENTS, then call `try_award_many()` (or have
`kinds_for_bet()` pick it up automatically, for bet-outcome-based ones) at the point it's earned.
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
]

BY_KIND = {achievement["kind"]: achievement for achievement in ACHIEVEMENTS}

# Maps a db.log_bet() `game` string to the personal achievement for winning it the first
# time. Games not listed here (e.g. horserace_owner) don't have a "first win" achievement.
WIN_GAME_KIND = {
    "blackjack": "win_blackjack",
    "slots": "win_slots",
    "roulette": "win_roulette",
    "horserace": "win_horserace",
    "jacks_or_better": "win_video_poker",
    "deuces_wild": "win_video_poker",
}


def kinds_for_bet(game: str, net: int) -> list[str]:
    """Every achievement kind a single resolved bet's net profit/loss newly qualifies for.
    Inclusive on the tiers -- a bet that wins 15,000 in one shot qualifies for both big_win_1
    and big_win_2, not just the higher one, since both thresholds are genuinely met."""
    kinds = []
    if net > 0:
        win_kind = WIN_GAME_KIND.get(game)
        if win_kind:
            kinds.append(win_kind)
        if net >= BIG_WIN_TIER_1:
            kinds.append("big_win_1")
        if net >= BIG_WIN_TIER_2:
            kinds.append("big_win_2")
    elif net < 0:
        if net <= BIG_LOSS_TIER_1:
            kinds.append("big_loss_1")
        if net <= BIG_LOSS_TIER_2:
            kinds.append("big_loss_2")
    return kinds


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
