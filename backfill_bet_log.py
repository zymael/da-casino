"""One-off script: reconstructs historical bet_log rows for blackjack and roulette by
scanning each guild's casino channel history and parsing the bot's own result embeds.
Slots is skipped — its result embeds carry no user attribution, so a spin can only be
guessed at by proximity to a `!slots` command message, which is too unreliable to trust.

Player names are resolved back to user ids via targeted gateway member lookups
(Guild.query_members), which — unlike a bulk member fetch — doesn't require the
privileged "Server Members" intent to be enabled for the bot application.

Safe to re-run: db.log_bet() dedupes backfilled rows by (message_id, user_id, game), so
already-imported bets are skipped rather than double-counted. Run once with:
    python backfill_bet_log.py
"""

import asyncio
import os
import re

import discord
from dotenv import load_dotenv

import db

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CASINO_CHANNEL_NAME = "da-casino"

BLACKJACK_RESULT_RE = re.compile(r"\(([+-]?\d+) credits\)")
BLACKJACK_BET_RE = re.compile(r"(\d+)")
ROULETTE_LINE_RE = re.compile(
    r"\*\*(?P<name>.+?)\*\* — .+? \((?P<bet>\d+)\) — (?:🎉 WIN|❌ LOSE) \((?P<net>[+-]?\d+)\) — Balance: \d+"
)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def resolve_name(guild: discord.Guild, cache: dict[str, int | None], name: str) -> int | None:
    """Looks up a display/username via a targeted gateway search, memoized in `cache`."""
    if name in cache:
        return cache[name]
    matches = await guild.query_members(query=name, limit=5, cache=False)
    exact = {m.id for m in matches if m.display_name.lower() == name.lower() or m.name.lower() == name.lower()}
    resolved = next(iter(exact)) if len(exact) == 1 else None
    cache[name] = resolved
    return resolved


async def find_casino_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel_id = await asyncio.to_thread(db.get_casino_channel_id, guild.id)
    if channel_id is not None:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
    for channel in guild.text_channels:
        if channel.name == CASINO_CHANNEL_NAME:
            return channel
    return None


def parse_blackjack(message: discord.Message) -> tuple[str, int, int] | None:
    """Returns (display_name, bet, net) if this message is a resolved blackjack hand."""
    if len(message.embeds) != 2:
        return None
    dealer_embed, player_embed = message.embeds
    if not (dealer_embed.title or "").startswith("🃏 Blackjack — Dealer"):
        return None

    bet_value = None
    result_value = None
    for field in player_embed.fields:
        if field.name == "Bet":
            bet_value = field.value
        elif field.name == "Result":
            result_value = field.value
    if bet_value is None or result_value is None:
        return None

    net_match = BLACKJACK_RESULT_RE.search(result_value)
    bet_match = BLACKJACK_BET_RE.search(bet_value)
    if not net_match or not bet_match:
        return None  # e.g. "Game timed out — bet refunded", not a real bet outcome

    display_name = player_embed.title
    if not display_name:
        return None
    return display_name, int(bet_match.group(1)), int(net_match.group(1))


def parse_roulette(message: discord.Message) -> list[tuple[str, int, int]]:
    """Returns [(display_name, bet, net), ...] for every bettor in a resolved roulette round."""
    if not message.embeds:
        return []
    result_embed = message.embeds[0]
    if not (result_embed.title or "").startswith("🎡 Roulette Result:"):
        return []
    description = result_embed.description or ""
    return [
        (m.group("name"), int(m.group("bet")), int(m.group("net")))
        for m in ROULETTE_LINE_RE.finditer(description)
    ]


@client.event
async def on_ready():
    print(f"Logged in as {client.user}. Scanning guilds...", flush=True)
    totals = {"blackjack": 0, "roulette": 0, "unresolved": 0}

    for guild in client.guilds:
        channel = await find_casino_channel(guild)
        if channel is None:
            print(f"[{guild.name}] no casino channel found, skipping", flush=True)
            continue

        print(f"[{guild.name}] scanning #{channel.name} history...", flush=True)
        name_cache: dict[str, int | None] = {}
        scanned = 0
        async for message in channel.history(limit=None, oldest_first=True):
            scanned += 1
            if message.author.id != client.user.id or not message.embeds:
                continue

            bj = parse_blackjack(message)
            if bj is not None:
                display_name, bet, net = bj
                user_id = await resolve_name(guild, name_cache, display_name)
                if user_id is None:
                    totals["unresolved"] += 1
                    continue
                await asyncio.to_thread(
                    db.log_bet,
                    guild.id,
                    user_id,
                    "blackjack",
                    bet,
                    net,
                    message.id,
                    message.created_at.isoformat(),
                )
                totals["blackjack"] += 1
                continue

            for display_name, bet, net in parse_roulette(message):
                user_id = await resolve_name(guild, name_cache, display_name)
                if user_id is None:
                    totals["unresolved"] += 1
                    continue
                await asyncio.to_thread(
                    db.log_bet,
                    guild.id,
                    user_id,
                    "roulette",
                    bet,
                    net,
                    message.id,
                    message.created_at.isoformat(),
                )
                totals["roulette"] += 1

        print(f"[{guild.name}] scanned {scanned} messages", flush=True)

    print(
        f"Done. Imported {totals['blackjack']} blackjack bets, {totals['roulette']} roulette bets, "
        f"skipped {totals['unresolved']} with unresolved/ambiguous player names.",
        flush=True,
    )
    await client.close()


db.init_db()
client.run(TOKEN)
