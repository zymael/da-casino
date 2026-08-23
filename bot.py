import asyncio
import os
import random
import time
from datetime import datetime

import discord
from aiohttp import web as aiohttp_web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import achievements
import admin_server
import crafting_view
import db
from blackjack_view import active_tables as active_blackjack_tables, start_blackjack_table
import dreams
import dungeon
from dungeon_view import (
    ClassPickerView,
    DuelChallengeView,
    active_delves,
    build_delve_picker_display,
    build_duel_challenge_embed,
    build_duel_target_picker,
    build_mode_choice_display,
    start_duel,
)
import horse_clothes_view
from holdem_view import (
    BIG_BLIND as HOLDEM_BIG_BLIND,
    active_tables as active_holdem_tables,
    busy_players as holdem_busy_players,
    start_holdem_table,
)
import horserace
from horserace_view import HorseRaceView, active_races
import hub_ui
import inventory_view
import jackpot
import moon
import quests
import ranch_view
import room_commands
import room_view
import rooms
from roulette_view import RouletteView, active_rounds
import slots_view
from slots_view import SlotsView
import video_poker
import video_poker_view
from video_poker_view import VideoPokerView

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ACTIVITY_SERVER_PORT = int(os.getenv("ACTIVITY_SERVER_PORT", "8787"))

DAILY_AMOUNT = 100
CASINO_CHANNEL_NAME = "da-casino"

PIZZA_COST = 10
PIZZA_COOLDOWN_SECONDS = 10 * 60

MINE_REWARD = 20
MINE_MATURE_SECONDS = 10 * 60
MINE_COOLDOWN_SECONDS = 60 * 60

TIP_AMOUNT = 25
BROKE_GIFS = [
    "https://media.giphy.com/media/3orifdO6eKr9YBdOBq/giphy.gif",
    "https://i.makeagif.com/media/10-04-2020/GEbQDx.gif",
]
PIZZA_CHAMPION_EMOJI = "🍕"
MONEY_CHAMPION_EMOJI = "👑"
LUCK_CHAMPION_EMOJI = "🍀"

PIZZA_GIFS = [
    "https://media.giphy.com/media/10kxE34bJPaUO4/giphy.gif",
    "https://media.giphy.com/media/kN8P8JcB64fja/giphy.gif",
    "https://media.giphy.com/media/2q9NijsetHvT1mr1Pj/giphy.gif",
    "https://media.giphy.com/media/pbuHOqN3N6swg/giphy.gif",
    "https://media.giphy.com/media/sTUWqCKtxd01W/giphy.gif",
    "https://media.giphy.com/media/bguSbMBFt3JHa/giphy.gif",
    "https://media.giphy.com/media/VbU6X60pTQxUY/giphy.gif",
    "https://media.giphy.com/media/3d1vYpj10Fcgo/giphy.gif",
]

ROY_GIF = "https://media.giphy.com/media/ywGp4PMJdeLyuRq7vJ/giphy.gif"

RUB_GIF = "https://images-ext-1.discordapp.net/external/1P77ZqLgK4rKSPC0sX4o7VjLSbabwB22RU3dP_2OEPU/https/static.klipy.com/ii/35ccce3d852f7995dd2da910f2abd795/62/ec/kAMWSWzV.mp4"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

HELP_CATEGORIES = [
    ("💰 Economy", ["balance", "stats", "rest", "mine", "tip", "transfer", "pizza", "leaderboard"]),
    ("🎲 Casino Games", ["blackjack", "slots", "roulette", "holdem", "videopoker", "deuceswild"]),
    ("🐎 Horse Racing", ["horserace", "horses", "buyhorse", "buyfoal", "renamehorse", "train", "facility", "boost", "horseequip"]),
    ("🗡️ Dungeon", ["class", "delve", "inventory", "equipment", "craft", "quests"]),
    ("🏆 Achievements", ["achievements"]),
    ("⚙️ Utility", ["ping", "setcasino", "setcurrency", "setdelvetest", "rub", "roy"]),
]

_SYNCED_GUILD_IDS: set[int] = set()


@bot.check
async def in_casino_channel(ctx):
    if ctx.command is not None and ctx.command.name == "setcasino":
        return True  # must be runnable from anywhere, including before a channel is configured
    if ctx.guild is None:
        return False
    channel_id = await asyncio.to_thread(db.get_casino_channel_id, ctx.guild.id)
    if channel_id is not None:
        return ctx.channel.id == channel_id
    return getattr(ctx.channel, "name", None) == CASINO_CHANNEL_NAME


def _in_seconds(seconds: float) -> str:
    """Discord timestamp markup for a point `seconds` from now, e.g. 'in 6 minutes (11:32 PM)'."""
    epoch = int(time.time() + seconds)
    return f"<t:{epoch}:R> (<t:{epoch}:t>)"


def _gear_breakdown_lines(equipped: dict[str, str]) -> list[str]:
    """One line per equipment slot (Weapon/Armor/Trinket): rarity dot, item name, and its constant
    stat bonuses (inventory_view.stat_bonus_text -- the same formatter !inventory/!equipment use,
    so a build's gear reads identically everywhere), or *none* if that slot is empty. Any on_use/
    on_hit effects (inventory_view.dynamic_effect_lines -- previously only shown in !inventory/
    !equipment, invisible everywhere else a player might check their own gear) are listed
    underneath, indented, so a "ring of fireball"-style item's actual behavior is visible from a
    character sheet too, not just the dedicated equipment screen. Shared by !stats and !class so
    gear reads the same way in both."""
    lines = []
    for slot in dungeon.EQUIPMENT_SLOTS:
        item_id = equipped.get(slot)
        if item_id is None:
            lines.append(f"{slot.title()}: *none*")
            continue
        item = dungeon.EQUIPMENT[item_id]
        stat_text = inventory_view.stat_bonus_text(item)
        stat_suffix = f" ({stat_text})" if stat_text else ""
        lines.append(f"{slot.title()}: {dungeon.RARITY_EMOJI[item['rarity']]} {item['name']}{stat_suffix}")
        lines.extend(f"> {effect_line}" for effect_line in inventory_view.dynamic_effect_lines(item))
    return lines


def _character_sheet_stats(character: dict, effective: dict, max_chips: int) -> str:
    """The character-sheet stat block shared by !stats and !class -- one consistent emoji per
    stat (matching the existing 🪙 Chips convention) instead of some stats having an icon and
    others being bare text, grouped into four lines by what kind of number each one is: HP and
    Chips together (both a resource pool that refills -- HP between rests, Chips at the start of
    every fight -- unlike a flat combat stat), Physical (ATK/DEF) and Special (SpAtk/SpDef) each
    on their own line, then the two derived dodge/resist chances last."""
    current_hp = min(character["current_hp"], effective["hp"])
    dodge_pct = round(dungeon.dodge_chance(effective["def"]) * 100)
    resist_pct = round(dungeon.dodge_chance(effective["spdef"]) * 100)
    return (
        f"❤️ HP {current_hp}/{effective['hp']} — 🪙 Chips {max_chips}\n"
        f"⚔️ ATK {effective['atk']} — 🛡️ DEF {effective['def']}\n"
        f"✨ SpAtk {effective['spatk']} — 🔮 SpDef {effective['spdef']}\n"
        f"🏃 Speed {effective['speed']}\n"
        f"💨 Dodge {dodge_pct}% — 🌀 Resist {resist_pct}%"
    )


async def _reject_if_at_poker_table(ctx) -> bool:
    """True (and sends a message) if the author is mid-hand at a poker table — their
    tracked stack there would desync from their balance if they spent money elsewhere."""
    if ctx.author.id in holdem_busy_players:
        await ctx.send(f"{ctx.author.display_name}, finish your poker hand first!")
        return True
    return False


@bot.event
async def setup_hook():
    """Starts the content-editor's aiohttp server (admin_server.py) on this same event loop --
    discord.py calls this once, after login but before the gateway connects, specifically as the
    place to bring up extra background services. admin_server.py's own `web.run_app()` (used only
    for standalone dev) calls asyncio.run() and owns its own loop, incompatible with running
    alongside bot.run() -- AppRunner/TCPSite are the awaitable equivalent, safe to start from
    here. Wrapped in try/except so a bug in the web server can't take the Discord connection down
    with it (Restart=on-failure on the systemd unit would otherwise restart the whole bot over a
    web-only failure)."""
    try:
        runner = aiohttp_web.AppRunner(admin_server.build_app(bot))
        await runner.setup()
        site = aiohttp_web.TCPSite(runner, "0.0.0.0", ACTIVITY_SERVER_PORT)
        await site.start()
        print(f"Content editor listening on :{ACTIVITY_SERVER_PORT}", flush=True)
    except Exception:
        import traceback
        traceback.print_exc()


@bot.event
async def on_ready():
    print(f"{bot.user} has connected to Discord!", flush=True)
    print("------", flush=True)
    await asyncio.to_thread(db.migrate_legacy_users_into_guilds, [g.id for g in bot.guilds])
    for guild in bot.guilds:  # warm each guild's seed so it's not the first command paying for it
        await asyncio.to_thread(horserace.current_probabilities, guild.id)
        await asyncio.to_thread(db.load_currency_name_cache, guild.id)
        if guild.id not in _SYNCED_GUILD_IDS:
            # Guild-scoped sync (vs a bare global sync) so /play shows up immediately rather than
            # waiting up to an hour for global command propagation. Guarded by _SYNCED_GUILD_IDS
            # so a reconnect-triggered on_ready refire doesn't resync every time.
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            _SYNCED_GUILD_IDS.add(guild.id)
    if not sync_champions_loop.is_running():
        sync_champions_loop.start()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need the **Manage Server** permission to do that.")
        return
    if isinstance(error, commands.NotOwner):
        await ctx.send("Only the bot owner can do that.")
        return
    if isinstance(error, commands.CheckFailure):
        return  # command used outside the casino channel — ignore silently to avoid spamming other channels
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("I couldn't find that user. Try mentioning them, e.g. `!transfer @Bob 50`.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("I couldn't find that channel.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("That doesn't look like a valid bet. Use a whole number, e.g. `!slots 50`.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`")
    else:
        raise error


@bot.command(name="setcasino")
@commands.guild_only()
@commands.is_owner()
async def setcasino_cmd(ctx, channel: discord.TextChannel = None):
    """Set which channel casino commands work in: !setcasino [#channel]"""
    channel = channel or ctx.channel
    await asyncio.to_thread(db.set_casino_channel_id, ctx.guild.id, channel.id)
    await ctx.send(f"🎰 Casino commands are now restricted to {channel.mention}.")


@bot.command(name="setcurrency")
@commands.guild_only()
@commands.is_owner()
async def setcurrency_cmd(ctx, *, name: str = None):
    """Rename this server's currency, e.g. !setcurrency gold"""
    if not name or not name.strip():
        await ctx.send("Usage: `!setcurrency <name>` — e.g. `!setcurrency gold`")
        return
    name = name.strip()
    if len(name) > 20:
        await ctx.send("Currency names must be 20 characters or fewer.")
        return

    await asyncio.to_thread(db.set_currency_name, ctx.guild.id, name)
    await ctx.send(f"💱 This server's currency is now called **{name}**.")


@bot.command(name="setdelvetest")
@commands.guild_only()
@commands.is_owner()
async def setdelvetest_cmd(ctx, state: str = None):
    """Toggle whether this server can play delves marked inactive in the admin panel, for free:
    !setdelvetest on/off -- lets a test server play WIP dungeons without exposing them anywhere
    else (a delve's "active" flag otherwise applies everywhere at once), and without burning real
    energy while doing it."""
    if state not in ("on", "off"):
        await ctx.send("Usage: `!setdelvetest on` or `!setdelvetest off`")
        return
    enabled = state == "on"
    await asyncio.to_thread(db.set_delve_test_mode, ctx.guild.id, enabled)
    if enabled:
        await ctx.send(
            "🧪 Delve test mode is **on** for this server — inactive delves are now playable here, "
            "and delves no longer cost energy."
        )
    else:
        await ctx.send("Delve test mode is **off** for this server — only active delves are playable here.")


@bot.command(name="ping")
@commands.is_owner()
async def ping(ctx):
    """Check the bot's latency."""
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")


@bot.command(name="help")
async def help_cmd(ctx, *, command_name: str = None):
    """List every command by category, or !help <command> for details on one."""
    if command_name:
        cmd = bot.get_command(command_name.strip().lstrip("!"))
        if cmd is None:
            await ctx.send(f"No command called `{command_name}`.")
            return
        embed = discord.Embed(
            title=f"!{cmd.name}", description=cmd.help or "No description.", color=discord.Color.blurple()
        )
        if cmd.aliases:
            embed.add_field(name="Aliases", value=", ".join(f"!{a}" for a in cmd.aliases), inline=False)
        await ctx.send(embed=embed)
        return

    embed = discord.Embed(
        title="🎰 Da Casino — Commands",
        description=(
            "Run `!help <command>` for details on any command below.\n"
            "🏘️ Run `/play` for a private menu to the Casino, Ranch, and Dungeon."
        ),
        color=discord.Color.gold(),
    )
    for category, names in HELP_CATEGORIES:
        lines = []
        for name in names:
            cmd = bot.get_command(name)
            if cmd is None or cmd.hidden:
                continue
            try:
                if not await cmd.can_run(ctx):
                    continue
            except commands.CommandError:
                continue
            lines.append(f"**!{cmd.name}** — {cmd.short_doc or 'No description.'}")
        if lines:
            embed.add_field(name=category, value="\n".join(lines), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="balance", aliases=["bal", "credits"])
async def balance(ctx):
    """Check your credit balance."""
    bal = await asyncio.to_thread(db.get_balance, ctx.guild.id, ctx.author.id)
    currency = db.get_currency_name(ctx.guild.id)
    await ctx.send(f"💰 {ctx.author.display_name} has **{bal}** {currency}.")


MAX_STATS_HORSES = 10


@bot.command(name="stats")
async def stats_cmd(ctx):
    """Show your personal stats: wins/losses per game, horses owned, and lifetime credits."""
    guild_id = ctx.guild.id
    user_id = ctx.author.id
    currency = db.get_currency_name(guild_id)

    balance, pizzas_bought = await asyncio.to_thread(db.get_user_economy, guild_id, user_id)
    character = await asyncio.to_thread(db.get_character, guild_id, user_id)
    game_stats = await asyncio.to_thread(db.get_user_game_stats, guild_id, user_id)
    bet_count, total_won, total_lost, best_win, worst_loss = await asyncio.to_thread(
        db.get_user_bet_summary, guild_id, user_id
    )
    owned_horses = await asyncio.to_thread(db.get_horses_owned_by, guild_id, user_id)
    personal_earned = await asyncio.to_thread(db.get_user_personal_achievements, guild_id, user_id)
    first_claimed = await asyncio.to_thread(db.get_guild_achievements, guild_id)
    achievement_count = len(personal_earned) + sum(1 for holder, _ in first_claimed.values() if holder == user_id)
    energy = await asyncio.to_thread(db.get_energy, guild_id, user_id)
    luck = await asyncio.to_thread(db.get_luck, guild_id, user_id)

    embed = discord.Embed(title=f"📊 {ctx.author.display_name}'s Stats", color=discord.Color.blurple())
    embed.add_field(name="Balance", value=f"{balance} {currency}", inline=True)
    embed.add_field(name="⚡ Energy", value=f"{energy}/{db.ENERGY_MAX}", inline=True)
    embed.add_field(name="🍀 Luck", value=str(luck), inline=True)
    embed.add_field(name="🍕 Pizzas Bought", value=str(pizzas_bought), inline=True)
    embed.add_field(name="🏆 Achievements", value=f"{achievement_count} unlocked", inline=True)

    if character is not None:
        name = dungeon.display_name(character["main_class"], character["subclass"])
        rank = dungeon.CLASSES[character["main_class"]]["rank"]
        suit_symbol = dungeon.SUIT_SYMBOLS[character["subclass"]]
        equipped = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
        effective = dungeon.compute_effective_stats(character, equipped)
        max_chips = dungeon.compute_stats(character["main_class"], character["subclass"])["chips"]
        embed.add_field(name="🗡️ Class", value=f"{name} {rank}{suit_symbol}\nLevel {character['level']}", inline=True)
        embed.add_field(name="📊 Stats", value=_character_sheet_stats(character, effective, max_chips), inline=True)
        embed.add_field(name="⚔️ Gear", value="\n".join(_gear_breakdown_lines(equipped)), inline=False)
    else:
        embed.add_field(name="🗡️ Class", value="None yet — try `!class`.", inline=True)

    if game_stats:
        lines = []
        for bucket, (wins, losses) in sorted(game_stats.items()):
            info = achievements.GAMES.get(bucket)
            emoji = info["emoji"] if info else "🎮"
            title = info["title"] if info else bucket.replace("_", " ").title()
            lines.append(f"{emoji} **{title}** — {wins}W / {losses}L")
        embed.add_field(name="Wins & Losses", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Wins & Losses", value="No games played yet.", inline=False)

    money_lines = [
        f"Bets placed: **{bet_count}**",
        f"Total won: **+{total_won}** {currency}",
        f"Total lost: **-{total_lost}** {currency}",
    ]
    if best_win is not None:
        money_lines.append(f"Best single win: **+{best_win}** {currency}")
    if worst_loss is not None:
        money_lines.append(f"Worst single loss: **{worst_loss}** {currency}")
    embed.add_field(name="Lifetime Winnings", value="\n".join(money_lines), inline=False)

    if owned_horses:
        horse_lines = [
            f"{'🐣' if is_foal else '🏆'} **{name}** (#{horse_index + 1}) — Age {age} — "
            f"{f'{wins}W-{places}P ({races} starts)' if races else 'unraced'}"
            for horse_index, is_foal, name, age, wins, places, races in owned_horses[:MAX_STATS_HORSES]
        ]
        if len(owned_horses) > MAX_STATS_HORSES:
            horse_lines.append(f"...and {len(owned_horses) - MAX_STATS_HORSES} more — see `!horses`.")
        embed.add_field(name=f"🐴 Horses Owned ({len(owned_horses)})", value="\n".join(horse_lines), inline=False)
    else:
        embed.add_field(name="🐴 Horses Owned", value="None yet — try `!buyhorse` or `!buyfoal`.", inline=False)

    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb", "top"])
async def leaderboard(ctx):
    """Show the top credit holders, top pizza buyers, and biggest single-bet win/loss."""
    credit_rows = await asyncio.to_thread(db.get_leaderboard, ctx.guild.id, 10)
    pizza_rows = await asyncio.to_thread(db.get_pizza_leaderboard, ctx.guild.id, 10)
    luck_rows = await asyncio.to_thread(db.get_luck_leaderboard, ctx.guild.id, 10)
    win_rows = await asyncio.to_thread(db.get_biggest_win, ctx.guild.id, 5)
    loss_rows = await asyncio.to_thread(db.get_biggest_loss, ctx.guild.id, 5)
    currency = db.get_currency_name(ctx.guild.id)
    if not credit_rows and not pizza_rows:
        await ctx.send(f"No one has any {currency} yet!")
        return

    medals = ["🥇", "🥈", "🥉"]

    def rank_line(i, user_id, value, suffix):
        member = ctx.guild.get_member(user_id) if ctx.guild else None
        name = member.display_name if member else f"<@{user_id}>"
        rank = medals[i] if i < len(medals) else f"`#{i + 1}`"
        return f"{rank} {name} — **{value}** {suffix}"

    def bet_rank_line(i, user_id, net, game):
        member = ctx.guild.get_member(user_id) if ctx.guild else None
        name = member.display_name if member else f"<@{user_id}>"
        rank = medals[i] if i < len(medals) else f"`#{i + 1}`"
        sign = "+" if net >= 0 else ""
        return f"{rank} {name} — **{sign}{net}** {currency} ({game})"

    embed = discord.Embed(title="🏆 Casino Leaderboard", color=discord.Color.gold())
    credit_lines = [
        rank_line(i, user_id, bal, currency) for i, (user_id, bal, _pizzas) in enumerate(credit_rows)
    ]
    embed.add_field(
        name=currency.capitalize(), value="\n".join(credit_lines) or f"No one has any {currency} yet!", inline=False
    )

    pizza_lines = [rank_line(i, user_id, pizzas, "🍕") for i, (user_id, pizzas) in enumerate(pizza_rows)]
    embed.add_field(name="Pizza", value="\n".join(pizza_lines) or "No one has bought pizza yet!", inline=False)

    luck_lines = [rank_line(i, user_id, luck, "🍀") for i, (user_id, luck) in enumerate(luck_rows)]
    embed.add_field(name="Luckiest", value="\n".join(luck_lines) or "No luck stats yet!", inline=False)

    win_lines = [bet_rank_line(i, user_id, net, game) for i, (user_id, net, game) in enumerate(win_rows)]
    embed.add_field(
        name="Biggest Win (single bet)", value="\n".join(win_lines) or "No wins logged yet!", inline=False
    )

    loss_lines = [bet_rank_line(i, user_id, net, game) for i, (user_id, net, game) in enumerate(loss_rows)]
    embed.add_field(
        name="Biggest Loss (single bet)", value="\n".join(loss_lines) or "No losses logged yet!", inline=False
    )

    await ctx.send(embed=embed)


@bot.command(name="rest")
async def rest_cmd(ctx):
    """Claim your credits and refill your energy (once every 12 hours)."""
    status, value = await asyncio.to_thread(db.claim_rest, ctx.guild.id, ctx.author.id, DAILY_AMOUNT)
    if status == "cooldown":
        await ctx.send(
            f"⏳ {ctx.author.display_name}, you've rested recently. "
            f"You can rest again {_in_seconds(value)}."
        )
        return

    currency = db.get_currency_name(ctx.guild.id)
    _, moon_emoji, moon_label, _, _ = moon.current_phase()
    await ctx.send(
        f"✅ {ctx.author.display_name} rested up! Claimed **{DAILY_AMOUNT}** {currency} and refilled to "
        f"**{db.ENERGY_MAX}** ⚡ energy. Balance: **{value}**\n"
        f"{moon_emoji} Tonight's moon: **{moon_label}**"
    )

    # Dream delivery: whatever dream is currently active (admin panel), DM'd once ever per
    # (guild, dream) -- a no-op if no dream is active or this player already claimed the active
    # one. See dreams.try_deliver_dream for the claim-then-send-then-rollback-on-Forbidden shape.
    delivered = await dreams.try_deliver_dream(ctx.author.send, ctx.guild.id, ctx.author.id)
    if delivered:
        await achievements.try_award_many(
            ctx.send, ctx.guild.id, ctx.author.id, ctx.author.display_name, ["first_dream"]
        )


@bot.command(name="mine")
async def mine(ctx):
    """Dig for credits: !mine starts a dig, then !mine again 10 minutes later collects 20 credits (1h cooldown after collecting)."""
    if await _reject_if_at_poker_table(ctx):
        return

    status, value = await asyncio.to_thread(
        db.claim_mine, ctx.guild.id, ctx.author.id, MINE_REWARD, MINE_MATURE_SECONDS, MINE_COOLDOWN_SECONDS
    )

    if status == "started":
        await ctx.send(
            f"⛏️ {ctx.author.display_name} starts digging... come back {_in_seconds(MINE_MATURE_SECONDS)} "
            f"and run `!mine` again to collect."
        )
        return

    if status == "pending":
        await ctx.send(f"⛏️ Still digging, {ctx.author.display_name} — ready to collect {_in_seconds(value)}.")
        return

    if status == "cooldown":
        await ctx.send(
            f"⛏️ {ctx.author.display_name}, you're worn out from your last dig — "
            f"you can start a new one {_in_seconds(value)}."
        )
        return

    currency = db.get_currency_name(ctx.guild.id)
    await ctx.send(f"⛏️ {ctx.author.display_name} digs up **{MINE_REWARD}** {currency}! Balance: **{value}**")


@bot.command(name="tip")
async def tip_cmd(ctx, member: discord.Member = None):
    """Tip another user 25 freshly generated credits (not taken from your own balance): !tip @user — once per day."""
    if await _reject_if_at_poker_table(ctx):
        return
    if member is None:
        await ctx.send("Usage: `!tip @user` — e.g. `!tip @Bob`")
        return
    if member.id == ctx.author.id:
        await ctx.send("You can't tip yourself.")
        return
    if member.bot:
        await ctx.send("You can't tip a bot.")
        return

    status, value = await asyncio.to_thread(db.tip, ctx.guild.id, ctx.author.id, member.id, TIP_AMOUNT)
    if status == "cooldown":
        await ctx.send(
            f"⏳ {ctx.author.display_name}, you already tipped someone today. "
            f"You can tip again {_in_seconds(value)}."
        )
        return

    currency = db.get_currency_name(ctx.guild.id)
    await ctx.send(
        f"🎁 {ctx.author.display_name} tipped {member.display_name} **{TIP_AMOUNT}** {currency}! "
        f"{member.display_name}'s balance: **{value}**"
    )


RUB_LUCKY_TARGET_ID = 272816170749526027  # fallback only, if no active user is found
RUB_AUTHOR_LUCK_GAIN = 3  # permanent -- the rubber's own belly-rub payoff
RUB_TARGET_LUCK_PENALTY = 8  # also permanent -- stolen luck stays stolen


@bot.command(name="rub")
async def rub_cmd(ctx):
    """Rub your belly for good luck (once every 12 hours) -- permanently makes you luckier, and someone else less lucky."""
    target_id = await asyncio.to_thread(db.get_random_active_user, ctx.guild.id)
    if target_id is None:
        target_id = RUB_LUCKY_TARGET_ID  # nobody's logged a bet in this guild yet -- still land the joke

    status, value = await asyncio.to_thread(
        db.apply_rub, ctx.guild.id, ctx.author.id, target_id, RUB_AUTHOR_LUCK_GAIN, RUB_TARGET_LUCK_PENALTY
    )
    if status == "cooldown":
        await ctx.send(
            f"⏳ {ctx.author.display_name}, you've already rubbed your belly recently. "
            f"You can rub again {_in_seconds(value)}."
        )
        return

    member = await _fetch_member(ctx.guild, target_id)
    mention = member.mention if member else f"<@{target_id}>"
    await ctx.send(
        f"{ctx.author.display_name} rubs their belly for good luck 🍀... {mention} feels less lucky.\n{RUB_GIF}"
    )
    await _update_luck_champion(ctx.guild)


@bot.command(name="roy")
async def roy_cmd(ctx):
    """Posts a gif of Roy: !roy"""
    embed = discord.Embed(color=discord.Color.gold())
    embed.set_image(url=ROY_GIF)
    await ctx.send(embed=embed)


@bot.command(name="transfer", aliases=["give", "pay"])
async def transfer(ctx, member: discord.Member = None, amount: int = None):
    """Transfer credits to another user: !transfer @user <amount>"""
    if await _reject_if_at_poker_table(ctx):
        return
    if member is None or amount is None:
        await ctx.send("Usage: `!transfer @user <amount>` — e.g. `!transfer @Bob 50`")
        return
    if amount <= 0:
        await ctx.send("Transfer amount must be a positive number.")
        return
    currency = db.get_currency_name(ctx.guild.id)
    if member.id == ctx.author.id:
        await ctx.send(f"You can't transfer {currency} to yourself.")
        return
    if member.bot:
        await ctx.send(f"You can't transfer {currency} to a bot.")
        return

    success, from_bal, to_bal = await asyncio.to_thread(
        db.transfer_balance, ctx.guild.id, ctx.author.id, member.id, amount
    )
    if not success:
        embed = discord.Embed(
            title="🪹 Can't send what you don't have",
            description=f"You only have **{from_bal}** {currency}, {ctx.author.display_name}.",
            color=discord.Color.orange(),
        )
        embed.set_image(url=random.choice(BROKE_GIFS))
        await ctx.send(embed=embed)
        return

    await ctx.send(
        f"💸 {ctx.author.display_name} sent **{amount}** {currency} to {member.display_name}! "
        f"({ctx.author.display_name}: **{from_bal}**, {member.display_name}: **{to_bal}**)"
    )


@bot.command(name="blackjack", aliases=["bj"])
async def blackjack_cmd(ctx, bet: int = None):
    """Open a persistent blackjack table: !blackjack <bet> — deals round after round from a shared shoe that's only reshuffled once it runs out."""
    if await _reject_if_at_poker_table(ctx):
        return
    if bet is None or bet <= 0:
        await ctx.send("Usage: `!blackjack <bet>` — e.g. `!blackjack 50`")
        return
    if ctx.channel.id in active_blackjack_tables:
        await ctx.send("A blackjack table is already open here — click **Join / Set Bet** on it to sit down!")
        return

    balance = await asyncio.to_thread(db.get_balance, ctx.guild.id, ctx.author.id)
    if bet > balance:
        currency = db.get_currency_name(ctx.guild.id)
        await ctx.send(f"You only have **{balance}** {currency}, {ctx.author.display_name}.")
        return

    await start_blackjack_table(ctx, bet)


@bot.command(name="slots", aliases=["slot"])
async def slots_cmd(ctx):
    """Play the slot machine: !slots — choose paylines and a multiplier, then spin"""
    if await _reject_if_at_poker_table(ctx):
        return

    balance = await asyncio.to_thread(db.get_balance, ctx.guild.id, ctx.author.id)
    if balance < 1:
        currency = db.get_currency_name(ctx.guild.id)
        await ctx.send(f"You only have **{balance}** {currency}, {ctx.author.display_name} — not enough to play.")
        return

    jackpot_pot = await asyncio.to_thread(jackpot.get_pot, ctx.guild.id, slots_view.JACKPOT_GAME)
    view = SlotsView(ctx.author, balance, ctx.guild.id, jackpot_pot)
    message = await ctx.send(embed=view.build_bet_embed(), view=view, file=view.build_initial_file())
    view.message = message


async def _start_video_poker(ctx, bet: int, command_name: str, variant: str):
    if await _reject_if_at_poker_table(ctx):
        return
    if bet is None or bet <= 0:
        await ctx.send(f"Usage: `!{command_name} <bet>` — e.g. `!{command_name} 50`")
        return

    balance = await asyncio.to_thread(db.get_balance, ctx.guild.id, ctx.author.id)
    if bet > balance:
        currency = db.get_currency_name(ctx.guild.id)
        await ctx.send(f"You only have **{balance}** {currency}, {ctx.author.display_name}.")
        return

    balance = await asyncio.to_thread(db.update_balance, ctx.guild.id, ctx.author.id, -bet)
    jackpot_pot = await asyncio.to_thread(jackpot.contribute, ctx.guild.id, video_poker_view.JACKPOT_GAME, bet)
    view = VideoPokerView(ctx.author, ctx.guild.id, bet, balance, jackpot_pot, variant=variant)
    message = await ctx.send(embed=view.build_deal_embed(), file=view.build_hand_file(), view=view)
    view.message = message


@bot.command(name="videopoker", aliases=["vp"])
async def video_poker_cmd(ctx, bet: int = None):
    """Play 5-card draw video poker (Jacks or Better): !videopoker <bet> — hold your cards, then draw"""
    await _start_video_poker(ctx, bet, "videopoker", video_poker.JACKS_OR_BETTER)


@bot.command(name="deuceswild", aliases=["dw"])
async def deuces_wild_cmd(ctx, bet: int = None):
    """Play 5-card draw video poker with deuces wild: !deuceswild <bet> — 2s are wild, minimum paying hand is Three of a Kind"""
    await _start_video_poker(ctx, bet, "deuceswild", video_poker.DEUCES_WILD)


@bot.command(name="roulette", aliases=["rl"])
async def roulette_cmd(ctx):
    """Open a roulette table others can join before it spins: !roulette"""
    if ctx.channel.id in active_rounds:
        await ctx.send("A roulette round is already open here — place your bets on that one!")
        return

    view = RouletteView(ctx.author, ctx.channel.id, ctx.guild.id)
    active_rounds[ctx.channel.id] = view
    embed, file = view.build_display()
    message = await ctx.send(embed=embed, file=file, view=view)
    view.message = message


@bot.command(name="horserace", aliases=["horse", "race"])
async def horserace_cmd(ctx):
    """Open a horse race others can bet on before it runs: !horserace"""
    if ctx.channel.id in active_races:
        await ctx.send("A horse race is already open here — place your bets on that one!")
        return

    roster, eligible, probabilities = await asyncio.to_thread(horserace.current_probabilities, ctx.guild.id)
    race_field = horserace.select_race_field(eligible)
    equipped_clothes = await asyncio.to_thread(db.get_guild_horse_clothes, ctx.guild.id)
    view = HorseRaceView(ctx.author, ctx.channel.id, ctx.guild.id, roster, race_field, probabilities, equipped_clothes)
    active_races[ctx.channel.id] = view
    embed, file = view.build_display()
    message = await ctx.send(embed=embed, file=file, view=view)
    view.message = message


@bot.command(name="horses", aliases=["stable"])
async def horses_cmd(ctx):
    """List every horse in the stable, its odds/price/record, and who owns it: !horses"""
    roster, _eligible, probabilities = await asyncio.to_thread(horserace.current_probabilities, ctx.guild.id)
    currency = db.get_currency_name(ctx.guild.id)
    lines = []
    for i in sorted(roster):
        horse = roster[i]
        kind = "🐣 Foal" if horse["is_foal"] else "🏆 Legend"
        if horse["age"] < horserace.MIN_RACING_AGE:
            odds = "—"
            status = f"Growing (age {horse['age']}/{horserace.MIN_RACING_AGE}) — owned by <@{horse['owner_id']}>"
        else:
            odds = horserace.describe_odds(i, probabilities["win"])
            if horse["owner_id"] is not None:
                status = f"Owned by <@{horse['owner_id']}>"
            else:
                status = f"💰 {horserace.price_of(i, probabilities['win'])} {currency} — `!buyhorse {i + 1}`"
        record = f"{horse['wins']}W-{horse['places']}P-{horse['shows']}S ({horse['races']} starts)" if horse["races"] else "unraced"
        stats = (
            f"SPD {horse['speed']:.0f} / END {horse['endurance']:.0f} / SPI {horse['spirit']:.0f} — {record}"
        )
        sex_symbol = horserace.SEX_SYMBOLS.get(horse["sex"], "")
        lines.append(f"**{i + 1}. {horse['name']}** {sex_symbol} {horse['coat']} ({odds}) — {kind} — {status}\n{stats}")

    embed = discord.Embed(
        title="🐴 The Stable",
        description="\n".join(lines),
        color=discord.Color.dark_gold(),
    )
    embed.set_footer(
        text=f"Owners earn {int(horserace.OWNER_CUT_FRACTION * 100)}% of the pot whenever their horse wins. "
        f"`!buyfoal <name>` for {horserace.FOAL_PRICE} {currency} to start your own — `!train <number>` once "
        f"a day to raise its stats and age until it can race at age {horserace.MIN_RACING_AGE}."
    )
    await ctx.send(embed=embed)


@bot.command(name="achievements", aliases=["achievement"])
async def achievements_cmd(ctx):
    """Show the achievements you've earned -- only your highest tier in each tiered track."""
    first_claimed = await asyncio.to_thread(db.get_guild_achievements, ctx.guild.id)
    personal_earned = await asyncio.to_thread(db.get_user_personal_achievements, ctx.guild.id, ctx.author.id)
    currency = db.get_currency_name(ctx.guild.id)

    tracks: dict[str, list[dict]] = {}
    for achievement in achievements.ACHIEVEMENTS:
        track = achievement.get("track", achievement["kind"])
        tracks.setdefault(track, []).append(achievement)

    lines = []
    for members in tracks.values():
        members.sort(key=lambda a: a.get("tier", 0), reverse=True)
        for achievement in members:
            kind = achievement["kind"]
            if achievement["scope"] == "first":
                row = first_claimed.get(kind)
                if row is None or row[0] != ctx.author.id:
                    continue
                date_text = f" — {datetime.fromisoformat(row[1]).strftime('%Y-%m-%d')}"
            else:
                if kind not in personal_earned:
                    continue
                date_text = ""
            reward = achievement["reward"]
            reward_text = f" (+{reward} {currency})" if reward else ""
            lines.append(
                f"{achievement['emoji']} **{achievement['name']}**{reward_text}{date_text}\n{achievement['description']}"
            )
            break  # highest tier already found for this track

    if not lines:
        await ctx.send(f"{ctx.author.display_name} hasn't unlocked any achievements yet — go play!")
        return

    embed = discord.Embed(
        title=f"🏆 {ctx.author.display_name}'s Achievements", description="\n\n".join(lines), color=discord.Color.gold()
    )
    await ctx.send(embed=embed)


@bot.command(name="class")
async def class_cmd(ctx):
    """Pick your permanent dungeon class/subclass (one-time), or check your current one: !class"""
    character = await asyncio.to_thread(db.get_character, ctx.guild.id, ctx.author.id)
    if character is not None:
        name = dungeon.display_name(character["main_class"], character["subclass"])
        rank = dungeon.CLASSES[character["main_class"]]["rank"]
        suit_symbol = dungeon.SUIT_SYMBOLS[character["subclass"]]
        equipped = await asyncio.to_thread(db.get_equipped_items, ctx.guild.id, ctx.author.id)
        effective = dungeon.compute_effective_stats(character, equipped)
        max_chips = dungeon.compute_stats(character["main_class"], character["subclass"])["chips"]
        xp_needed = dungeon.xp_to_next_level(character["level"])

        embed = discord.Embed(title=f"{name} {rank}{suit_symbol}", color=discord.Color.blurple())
        embed.add_field(name="Level", value=f"{character['level']} ({character['xp']}/{xp_needed} XP)", inline=True)
        embed.add_field(name="Stats", value=_character_sheet_stats(character, effective, max_chips), inline=True)
        embed.add_field(name="⚔️ Equipment", value="\n".join(_gear_breakdown_lines(equipped)), inline=False)
        embed.set_footer(text="Class/subclass is permanent — gear and levels grow from delving.")
        await ctx.send(embed=embed)
        return

    view = ClassPickerView(ctx.guild.id, ctx.author.id)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command(name="delve")
async def delve_cmd(ctx, delve_id: str = None):
    """Delve the dungeon for a class-biased, push-your-luck payout -- costs 1 ⚡ energy: !delve, or
    !delve <delve_id> to jump straight into one specific dungeon -- what a room's own Delve button
    pins via const_args (rooms.json) when that room hosts just one dungeon, skipping the picker.
    Either way, once the delve is resolved you choose to go solo or start a free-to-join party."""
    if await _reject_if_at_poker_table(ctx):
        return
    character = await asyncio.to_thread(db.get_character, ctx.guild.id, ctx.author.id)
    if character is None:
        await ctx.send(f"You don't have a character yet, {ctx.author.display_name} — run `!class` to pick one first.")
        return
    if character["current_hp"] <= 0:
        await ctx.send(f"{ctx.author.display_name}, you're too beat up to delve — run `!rest` to heal first.")
        return
    if ctx.author.id in active_delves:
        await ctx.send("You're already mid-delve — finish that one first!")
        return

    test_mode = await asyncio.to_thread(db.get_delve_test_mode, ctx.guild.id)
    if delve_id is not None:
        delve = dungeon.DELVES.get(delve_id)
        if delve is None or not (delve.get("active", True) or test_mode):
            await ctx.send(f"No such delve `{delve_id}`.")
            return
    else:
        available = dungeon.active_delves(include_inactive=test_mode)
        if not available:
            await ctx.send("No delves are available to play right now.")
            return
        if len(available) > 1:
            # No specific delve pinned and more than one active dungeon defined -- let the player
            # pick which dungeon first; its own confirm button leads into the same Solo/Party
            # choice below.
            embed, view = await build_delve_picker_display(ctx.guild.id, ctx.author.id, character, test_mode)
            await ctx.send(embed=embed, view=view)
            return
        delve = next(iter(available.values()))

    # Energy is never spent just to see this choice -- only Solo Delve or a party leader's Start
    # Delve (both inside DelveModeChoiceView/PartyLobbyView) actually spends the charge, so
    # backing out never costs one.
    embed, view = await build_mode_choice_display(ctx.guild.id, ctx.author.id, character, delve)
    await ctx.send(embed=embed, view=view)


async def _duel_challenge(ctx, member: discord.Member, wager: int) -> None:
    """The actual challenge-creation logic for a resolved (member, wager) pair -- shared by
    duel_cmd's typed invocation and the duel picker's Select->Modal flow (dungeon_view.
    build_duel_target_picker), so there's exactly one place this logic lives regardless of how the
    target/wager got collected (same shape as _train_horse above)."""
    if member.id == ctx.author.id:
        await ctx.send("You can't duel yourself.")
        return
    if member.bot:
        await ctx.send("You can't duel a bot.")
        return
    if wager < 0:
        await ctx.send("Wager can't be negative.")
        return

    challenger_character = await asyncio.to_thread(db.get_character, ctx.guild.id, ctx.author.id)
    if challenger_character is None:
        await ctx.send(f"You don't have a character yet, {ctx.author.display_name} — run `!class` to pick one first.")
        return
    target_character = await asyncio.to_thread(db.get_character, ctx.guild.id, member.id)
    if target_character is None:
        await ctx.send(f"{member.display_name} doesn't have a character yet — they need to run `!class` first.")
        return
    if ctx.author.id in active_delves or ctx.author.id in holdem_busy_players:
        await ctx.send("Finish up whatever you're already doing first.")
        return
    if member.id in active_delves or member.id in holdem_busy_players:
        await ctx.send(f"{member.display_name} is already tied up in something else right now.")
        return

    currency = db.get_currency_name(ctx.guild.id)
    if wager:
        balance = await asyncio.to_thread(db.get_balance, ctx.guild.id, ctx.author.id)
        if balance < wager:
            await ctx.send(f"You only have **{balance}** {currency} — you can't wager **{wager}**.")
            return

    challenge = await start_duel(ctx.guild.id, ctx.author.id, ctx.author.display_name, member.id, member.display_name, wager)
    view = DuelChallengeView(challenge)
    challenge.message = await ctx.send(content=member.mention, embed=build_duel_challenge_embed(challenge), view=view)


@bot.command(name="duel")
async def duel_cmd(ctx, member: discord.Member = None, wager: int = 0):
    """Challenge another player to a 1v1 duel (same combat system as the dungeon, no dungeon HP/XP
    at stake -- both start at full HP): !duel @user [wager], or plain !duel to pick a target from a
    dropdown instead (an Arena room's Duel button always resolves this way, same as !train's own
    horse-picker fallback). They have to Accept before it starts."""
    if await _reject_if_at_poker_table(ctx):
        return
    if member is None:
        view = build_duel_target_picker(_duel_challenge)
        await ctx.send("Pick who you want to duel:", view=view)
        return
    await _duel_challenge(ctx, member, wager)


@bot.command(name="inventory")
async def inventory_cmd(ctx):
    """See your quest items and dungeon gear (equipped + stored): !inventory"""
    embed = await inventory_view.build_inventory_embed(ctx.guild.id, ctx.author.id)
    await ctx.send(embed=embed)


@bot.command(name="equipment")
async def equipment_cmd(ctx):
    """Equip, unequip, or swap in stored dungeon gear per slot: !equipment"""
    embed, view = await inventory_view.build_equipment_display(ctx.guild.id, ctx.author.id)
    await ctx.send(embed=embed, view=view)


@bot.command(name="craft")
async def craft_cmd(ctx):
    """Craft gear or consumables from materials you've found: !craft"""
    embed, view = await crafting_view.build_craft_display(ctx.guild.id, ctx.author.id)
    await ctx.send(embed=embed, view=view)


@bot.command(name="quests")
async def quests_cmd(ctx):
    """See your active and completed quests: !quests"""
    log = await quests.quest_log(ctx.guild.id, ctx.author.id)
    embed = discord.Embed(title=f"🗺️ {ctx.author.display_name}'s Quest Log", color=discord.Color.blurple())
    if not log:
        embed.description = "No quests started yet."
    else:
        for entry in log:
            status = "✅ Complete" if entry["complete"] else f"Stage {entry['stage_index'] + 1}/{entry['total_stages']}"
            value = entry["prompt"]
            embed.add_field(name=f"{entry['npc'].title()} — {status}", value=value, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="buyhorse")
async def buyhorse_cmd(ctx, number: int = None):
    """Buy an unowned legend: !buyhorse <number> — see !horses for numbers and prices"""
    if await _reject_if_at_poker_table(ctx):
        return
    if number is None or not 1 <= number <= horserace.LEGEND_COUNT:
        await ctx.send(f"Usage: `!buyhorse <1-{horserace.LEGEND_COUNT}>` — see `!horses` for the list.")
        return

    horse_index = number - 1
    _roster, _eligible, probabilities = await asyncio.to_thread(horserace.current_probabilities, ctx.guild.id)
    price = horserace.price_of(horse_index, probabilities["win"])
    status, balance = await asyncio.to_thread(db.buy_legend_horse, ctx.guild.id, horse_index, ctx.author.id, price)

    if status == "owned":
        await ctx.send("That horse is already owned — check `!horses` for what's still available.")
        return
    horse_name = horserace.HORSES[horse_index]["name"]
    currency = db.get_currency_name(ctx.guild.id)
    if status == "broke":
        await ctx.send(f"{horse_name} costs **{price}** {currency} — you only have **{balance}**.")
        return

    await ctx.send(
        f"🐴 {ctx.author.display_name} bought **{horse_name}** for **{price}** {currency}! "
        f"Balance: **{balance}**. Rename it with `!renamehorse {number} <name>`."
    )
    await achievements.try_award_many(ctx.send, ctx.guild.id, ctx.author.id, ctx.author.display_name, ["first_horse"])


@bot.command(name="buyfoal")
async def buyfoal_cmd(ctx, *, name: str = None):
    """Buy a brand-new foal and name it: !buyfoal <name> — cheap, but weaker than a legend until trained up."""
    if await _reject_if_at_poker_table(ctx):
        return
    if not name or not name.strip():
        await ctx.send("Usage: `!buyfoal <name>` — e.g. `!buyfoal Lightning`")
        return
    name = name.strip()
    if len(name) > horserace.MAX_HORSE_NAME_LEN:
        await ctx.send(f"Names must be {horserace.MAX_HORSE_NAME_LEN} characters or fewer.")
        return

    horse_index = await asyncio.to_thread(db.next_horse_index, ctx.guild.id, horserace.LEGEND_COUNT)
    sex, coat = horserace.random_sex(), horserace.random_coat()
    status, balance = await asyncio.to_thread(
        db.buy_foal, ctx.guild.id, horse_index, ctx.author.id, name, horserace.FOAL_PRICE,
        *horserace.FOAL_BASE_STATS.values(), sex, coat,
    )
    if status == "broke":
        currency = db.get_currency_name(ctx.guild.id)
        await ctx.send(f"A foal costs **{horserace.FOAL_PRICE}** {currency} — you only have **{balance}**.")
        return

    currency = db.get_currency_name(ctx.guild.id)
    await ctx.send(
        f"🐣 {ctx.author.display_name} bought a {coat} {sex} foal named **{name}** for **{horserace.FOAL_PRICE}** "
        f"{currency}! Balance: **{balance}**. Train it with `!train {horse_index + 1}` every 12 hours — it needs to "
        f"reach age {horserace.MIN_RACING_AGE} before it can race."
    )
    await achievements.try_award_many(ctx.send, ctx.guild.id, ctx.author.id, ctx.author.display_name, ["first_horse"])


@bot.command(name="renamehorse")
async def renamehorse_cmd(ctx, number: int = None, *, name: str = None):
    """Rename a horse you own: !renamehorse <number> <new name>"""
    if number is None or number < 1 or not name:
        await ctx.send("Usage: `!renamehorse <number> <new name>` — see `!horses` for numbers.")
        return
    name = name.strip()
    if not name:
        await ctx.send("The name can't be blank.")
        return
    if len(name) > horserace.MAX_HORSE_NAME_LEN:
        await ctx.send(f"Names must be {horserace.MAX_HORSE_NAME_LEN} characters or fewer.")
        return

    horse_index = number - 1
    renamed = await asyncio.to_thread(db.rename_horse, ctx.guild.id, horse_index, ctx.author.id, name)
    if not renamed:
        await ctx.send("You don't own that horse — check `!horses` to see who does.")
        return
    await ctx.send(f"🐴 Horse #{number} is now named **{name}**!")


async def _train_horse(ctx, horse_index: int):
    """The actual training logic for one specific horse -- shared by train_cmd's numbered
    invocation and the horse-picker Select's callback (ranch_view.build_train_horse_picker), so
    there's exactly one place this logic lives regardless of how the horse index got resolved."""
    # Fetched up front (rather than after training) so the facility bonus and any queued !boost
    # item can be folded into the gains *before* calling db.train_horse -- it applies whatever
    # gains it's given and unconditionally clears pending_boost_stat, so the bonus has to already
    # be baked in by this point.
    horses = await asyncio.to_thread(db.get_guild_horses, ctx.guild.id)
    horse = horses.get(horse_index)
    pending_stat = horse["pending_boost_stat"] if horse else None

    tier = await asyncio.to_thread(db.get_facility_tier, ctx.guild.id, ctx.author.id)
    facility_bonus = horserace.facility_bonus_for_tier(tier)
    speed_gain, endurance_gain, spirit_gain = horserace.compute_training_gains(facility_bonus, pending_stat)

    status, payload = await asyncio.to_thread(
        db.train_horse, ctx.guild.id, horse_index, ctx.author.id,
        speed_gain, endurance_gain, spirit_gain, horserace.STAT_CAP,
    )
    if status == "not_owner":
        await ctx.send("You don't own that horse — check `!horses` to see who does.")
        return
    if status == "cooldown":
        await ctx.send(f"That horse was trained recently — you can train it again {_in_seconds(payload)}.")
        return

    new_speed, new_endurance, new_spirit, new_age = payload
    name = horse["name"]
    lines = [f"🏋️ **{name}** trained! SPD {new_speed:.0f} / END {new_endurance:.0f} / SPI {new_spirit:.0f} — Age {new_age}"]
    extras = []
    if facility_bonus:
        extras.append(f"+{int(facility_bonus * 100)}% from your ranch facility")
    if pending_stat:
        extras.append(f"🧪 used your queued {pending_stat} boost")
    if extras:
        lines.append(" — ".join(extras))
    if new_age < horserace.MIN_RACING_AGE:
        lines.append(f"Still growing — needs age {horserace.MIN_RACING_AGE} to enter a race.")
    elif new_age == horserace.MIN_RACING_AGE:
        lines.append("🎉 Old enough to race now!")
    await ctx.send("\n".join(lines))


@bot.command(name="train")
async def train_cmd(ctx, number: int = None):
    """Train a horse you own once every 12 hours, raising its stats and age: !train <number>, or
    plain !train to pick from a dropdown of your horses instead of needing to already know its number."""
    if number is None:
        owned = await asyncio.to_thread(db.get_ranch_horses, ctx.guild.id, ctx.author.id)
        if not owned:
            await ctx.send("You don't own any horses yet — try `!buyhorse` or `!buyfoal`.")
            return
        view = ranch_view.build_horse_picker(owned, _train_horse, placeholder="Choose a horse to train...")
        await ctx.send("Pick a horse to train:", view=view)
        return
    if number < 1:
        await ctx.send("Usage: `!train <number>` — see `!horses` for numbers.")
        return
    await _train_horse(ctx, number - 1)


@bot.command(name="facility")
async def facility_cmd(ctx, action: str = None):
    """Check or upgrade your ranch's permanent training facility: !facility or !facility buy"""
    guild_id, user_id = ctx.guild.id, ctx.author.id
    currency = db.get_currency_name(guild_id)
    tier = await asyncio.to_thread(db.get_facility_tier, guild_id, user_id)

    if action is None or action.lower() != "buy":
        if tier > 0:
            current = horserace.FACILITY_TIERS[tier - 1]
            status_text = f"You have **{current['name']}** (Tier {tier}) — +{int(current['bonus'] * 100)}% training gains."
        else:
            status_text = "You don't have a training facility yet."
        if tier < len(horserace.FACILITY_TIERS):
            next_facility = horserace.FACILITY_TIERS[tier]
            status_text += (
                f"\nNext: **{next_facility['name']}** (Tier {next_facility['tier']}) — "
                f"+{int(next_facility['bonus'] * 100)}% training gains for **{next_facility['cost']}** {currency}. "
                f"Run `!facility buy` to purchase it."
            )
        else:
            status_text += "\nYou're already at the highest tier!"
        await ctx.send(status_text)
        return

    if tier >= len(horserace.FACILITY_TIERS):
        await ctx.send("You're already at the highest facility tier!")
        return

    next_facility = horserace.FACILITY_TIERS[tier]
    status, balance = await asyncio.to_thread(
        db.upgrade_facility, guild_id, user_id, next_facility["tier"], next_facility["cost"],
        len(horserace.FACILITY_TIERS),
    )
    if status == "broke":
        await ctx.send(f"**{next_facility['name']}** costs **{next_facility['cost']}** {currency} — you only have **{balance}**.")
        return
    if status == "wrong_tier":
        await ctx.send("Your facility tier changed since you last checked — run `!facility` again.")
        return

    await ctx.send(
        f"🏗️ {ctx.author.display_name} built **{next_facility['name']}**! All your horses now train "
        f"+{int(next_facility['bonus'] * 100)}% faster. Balance: **{balance}** {currency}."
    )


async def _boost_horse(ctx, horse_index: int, stat: str):
    """The actual boost-buying logic for one specific horse -- shared by boost_cmd's numbered
    invocation and the horse-picker Select's callback, same shape as _train_horse above."""
    currency = db.get_currency_name(ctx.guild.id)
    status, balance = await asyncio.to_thread(
        db.buy_horse_item, ctx.guild.id, ctx.author.id, horse_index, stat, horserace.ITEM_COST
    )
    if status == "not_owner":
        await ctx.send("You don't own that horse — check `!ranch` to see your own.")
        return
    if status == "pending":
        await ctx.send("That horse already has a boost queued — train it first to use it up.")
        return
    if status == "broke":
        await ctx.send(f"A training-boost item costs **{horserace.ITEM_COST}** {currency} — you only have **{balance}**.")
        return

    await ctx.send(
        f"🧪 {ctx.author.display_name} queued a **{stat}** boost on horse #{horse_index + 1} — "
        f"it'll apply on its next `!train`. Balance: **{balance}** {currency}."
    )


@bot.command(name="boost")
async def boost_cmd(ctx, stat: str = None, number: int = None):
    """Buy a training-boost item for a horse you own, queued for its next training: !boost
    <speed|endurance|spirit> <number>, or omit the number to pick from a dropdown of your horses
    instead of needing to already know its number."""
    if stat is None or stat.lower() not in horserace.ITEM_STATS:
        currency = db.get_currency_name(ctx.guild.id)
        await ctx.send(
            f"Usage: `!boost <speed|endurance|spirit> <number>` — costs {horserace.ITEM_COST} {currency}, "
            f"see `!ranch` for your horses' numbers."
        )
        return
    stat = stat.lower()

    if number is None:
        owned = await asyncio.to_thread(db.get_ranch_horses, ctx.guild.id, ctx.author.id)
        if not owned:
            await ctx.send("You don't own any horses yet — try `!buyhorse` or `!buyfoal`.")
            return
        view = ranch_view.build_horse_picker(
            owned, lambda c, i: _boost_horse(c, i, stat), placeholder=f"Choose a horse to boost {stat}..."
        )
        await ctx.send(f"Pick a horse to boost **{stat}**:", view=view)
        return
    if number < 1:
        await ctx.send(f"Usage: `!boost {stat} <number>` — see `!ranch` for your horses' numbers.")
        return
    await _boost_horse(ctx, number - 1, stat)


async def _show_horse_clothes(ctx, horse_index: int):
    """The actual !horseequip logic for one specific horse -- shared by horseequip_cmd's numbered
    invocation and the horse-picker Select's callback (ranch_view.build_horse_picker), same shape
    as _train_horse above."""
    horses = await asyncio.to_thread(db.get_guild_horses, ctx.guild.id)
    horse = horses.get(horse_index)
    if horse is None or horse["owner_id"] != ctx.author.id:
        await ctx.send("You don't own that horse — check `!horses` to see who does.")
        return
    embed, view = await horse_clothes_view.build_horse_equip_display(ctx.guild.id, ctx.author.id, horse_index)
    await ctx.send(embed=embed, view=view)


@bot.command(name="horseequip")
async def horseequip_cmd(ctx, number: int = None):
    """Dress a horse you own up in owned cosmetic clothing -- purely visual, no stat effect:
    !horseequip <number>, or plain !horseequip to pick from a dropdown of your horses instead."""
    if number is None:
        owned = await asyncio.to_thread(db.get_ranch_horses, ctx.guild.id, ctx.author.id)
        if not owned:
            await ctx.send("You don't own any horses yet — try `!buyhorse` or `!buyfoal`.")
            return
        view = ranch_view.build_horse_picker(owned, _show_horse_clothes, placeholder="Choose a horse to dress up...")
        await ctx.send("Pick a horse to dress up:", view=view)
        return
    if number < 1:
        await ctx.send("Usage: `!horseequip <number>` — see `!horses` for numbers.")
        return
    await _show_horse_clothes(ctx, number - 1)


@bot.command(name="holdem", aliases=["poker"])
async def holdem_cmd(ctx, buy_in: int = None):
    """Open a persistent Texas Hold'em table: !holdem [buy_in] — deals hands back to back until fewer than 2 players remain."""
    if await _reject_if_at_poker_table(ctx):
        return
    if buy_in is not None and buy_in < HOLDEM_BIG_BLIND:
        currency = db.get_currency_name(ctx.guild.id)
        await ctx.send(f"Buy-in must be at least the big blind ({HOLDEM_BIG_BLIND} {currency}).")
        return
    if ctx.channel.id in active_holdem_tables:
        await ctx.send("A Hold'em table is already open here — click **Buy In** on it to sit down!")
        return

    await start_holdem_table(ctx, buy_in)


async def _fetch_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


# Badge prefix order when a member holds more than one crown at once, e.g. "💰 🍕 Alice".
BADGES = [("money", MONEY_CHAMPION_EMOJI), ("pizza", PIZZA_CHAMPION_EMOJI), ("luck", LUCK_CHAMPION_EMOJI)]


def _strip_badge_prefix(nick: str | None) -> str | None:
    """Peels any badge emoji this bot could have prepended off the front of `nick`, so a
    manual rename made while a badge is held (e.g. "💰 Alice" -> "💰 Bob") is picked up as the
    new base name instead of being masked by it."""
    if nick is None:
        return None
    stripped = nick
    peeled = True
    while peeled:
        peeled = False
        for _, emoji in BADGES:
            prefix = f"{emoji} "
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                peeled = True
    return stripped or None


async def _refresh_nick(member: discord.Member, guild_id: int) -> bool:
    """Rebuilds a member's nickname from every badge they currently hold, or restores
    their original nickname if they hold none. Returns False only if Discord rejected the edit."""
    if member.id == member.guild.owner_id:
        # Discord blocks nickname changes for the guild owner no matter the bot's role
        # hierarchy or permissions — there's no badge prefix to add/remove for them, so
        # treat this as a permanent no-op success rather than a retryable failure.
        return True

    held = await asyncio.to_thread(db.get_user_badges, guild_id, member.id)
    # Always derive the base from the member's *current* nickname (badge prefix stripped off),
    # not a value frozen the first time they were ever crowned -- otherwise a nickname change
    # made while badged gets silently discarded the next time their badges change.
    base = await asyncio.to_thread(db.set_base_nick, guild_id, member.id, _strip_badge_prefix(member.nick))
    prefix = "".join(f"{emoji} " for kind, emoji in BADGES if kind in held)
    target_nick = f"{prefix}{base or member.name}"[:32] if held else base

    if target_nick == member.nick:
        return True
    try:
        await member.edit(nick=target_nick)
        return True
    except discord.Forbidden:
        bot_top_role = member.guild.me.top_role
        print(
            f"[champion] Forbidden renaming {member.id} in guild {guild_id}. "
            f"is_owner={member.id == member.guild.owner_id} "
            f"bot_top_role='{bot_top_role.name}'(pos={bot_top_role.position}) "
            f"target_top_role='{member.top_role.name}'(pos={member.top_role.position})",
            flush=True,
        )
        return False


async def _sync_champion(guild: discord.Guild | None, kind: str, top_user_id: int | None):
    """Moves the `kind` crown badge to `top_user_id` if it isn't already theirs."""
    if guild is None or top_user_id is None:
        return

    current_holder = await asyncio.to_thread(db.get_champion, guild.id, kind)
    if current_holder == top_user_id:
        return

    new_member = await _fetch_member(guild, top_user_id)
    if new_member is None:
        print(f"[champion] could not find member {top_user_id} in guild {guild.id}", flush=True)
        return

    # Tentatively record the new holder so _refresh_nick includes this badge in their prefix,
    # then roll back if Discord actually rejects the rename — otherwise a failed edit would get
    # marked "done" and never be retried.
    await asyncio.to_thread(db.set_champion, guild.id, kind, top_user_id)
    if not await _refresh_nick(new_member, guild.id):
        # Can't badge the true leader (role hierarchy blocks the rename) — don't leave the
        # crown stuck on whoever held it before, since they're no longer actually in the lead.
        # Strip it from everyone until the badge can land on its rightful owner.
        await asyncio.to_thread(db.clear_champion, guild.id, kind)
        if current_holder is not None:
            old_member = await _fetch_member(guild, current_holder)
            if old_member is not None:
                await _refresh_nick(old_member, guild.id)
        return

    if current_holder is not None:
        old_member = await _fetch_member(guild, current_holder)
        if old_member is not None:
            await _refresh_nick(old_member, guild.id)


async def _update_pizza_champion(guild: discord.Guild | None):
    if guild is None:
        return
    rows = await asyncio.to_thread(db.get_pizza_leaderboard, guild.id, 1)
    await _sync_champion(guild, "pizza", rows[0][0] if rows else None)


async def _update_money_champion(guild: discord.Guild | None):
    if guild is None:
        return
    rows = await asyncio.to_thread(db.get_leaderboard, guild.id, 1)
    await _sync_champion(guild, "money", rows[0][0] if rows else None)


async def _update_luck_champion(guild: discord.Guild | None):
    if guild is None:
        return
    rows = await asyncio.to_thread(db.get_luck_leaderboard, guild.id, 1)
    await _sync_champion(guild, "luck", rows[0][0] if rows else None)


@tasks.loop(seconds=60)
async def sync_champions_loop():
    for guild in bot.guilds:
        await _update_pizza_champion(guild)
        await _update_money_champion(guild)
        await _update_luck_champion(guild)


@bot.command(name="pizza")
async def pizza(ctx):
    """Deliver an authentic pizza to the casino: costs 10 credits, 10 minute cooldown."""
    if await _reject_if_at_poker_table(ctx):
        return
    status, value = await asyncio.to_thread(
        db.buy_pizza, ctx.guild.id, ctx.author.id, PIZZA_COST, PIZZA_COOLDOWN_SECONDS
    )

    if status == "cooldown":
        await ctx.send(
            f"🍕 Still full from last time, {ctx.author.display_name} — "
            f"you can grab another slice {_in_seconds(value)}."
        )
        return

    currency = db.get_currency_name(ctx.guild.id)
    if status == "broke":
        embed = discord.Embed(
            title="🍕 No pizza for you...",
            description=f"You need **{PIZZA_COST}** {currency} for a pizza, {ctx.author.display_name}.",
            color=discord.Color.orange(),
        )
        embed.set_image(url=random.choice(BROKE_GIFS))
        await ctx.send(embed=embed)
        return

    balance = value
    embed = discord.Embed(title="🍕 Pizza delivery for the casino!", color=discord.Color.orange())
    embed.set_image(url=random.choice(PIZZA_GIFS))
    embed.set_footer(text=f"Balance: {balance} {currency}")
    await ctx.send(embed=embed)
    await _update_pizza_champion(ctx.guild)


# Every command a /play room can invoke, keyed exactly as rooms.json's commands[].key references
# it. Populated once, here (not rebuilt per /play call the way it used to be -- it's static) --
# see room_commands.py's own docstring for why this dict lives in its own tiny module rather than
# directly in this one or being read from here: rooms.py's loader and admin_schemas.py's "commands"
# field both need to check/offer these same keys, and neither can import bot.py without a cycle.
room_commands.COMMANDS.update({
    "blackjack": blackjack_cmd.callback,
    "slots": slots_cmd.callback,
    "roulette": roulette_cmd.callback,
    "holdem": holdem_cmd.callback,
    "videopoker": video_poker_cmd.callback,
    "deuceswild": deuces_wild_cmd.callback,
    "horserace": horserace_cmd.callback,
    "balance": balance.callback,
    "rest": rest_cmd.callback,
    "mine": mine.callback,
    "pizza": pizza.callback,
    "leaderboard": leaderboard.callback,
    "stats": stats_cmd.callback,
    "achievements": achievements_cmd.callback,
    "class": class_cmd.callback,
    "delve": delve_cmd.callback,
    "duel": duel_cmd.callback,
    "craft": craft_cmd.callback,
    "train": train_cmd.callback,
    "boost": boost_cmd.callback,
    "facility": facility_cmd.callback,
    "horseequip": horseequip_cmd.callback,
})
# Catches a typo'd rooms.json command key loudly at startup instead of a KeyError the moment some
# player clicks the broken button -- see rooms.py's own docstring for why this can't run any
# earlier (room_commands.COMMANDS is empty until the update() above runs).
rooms.validate_command_keys(room_commands.COMMANDS.keys())


async def in_casino_channel_slash(interaction: discord.Interaction) -> bool:
    """app_commands equivalent of in_casino_channel above -- @bot.check only wires up for prefix
    commands, so /play needs its own copy of the same channel-lookup logic."""
    if interaction.guild is None:
        return False
    channel_id = await asyncio.to_thread(db.get_casino_channel_id, interaction.guild.id)
    if channel_id is not None:
        return interaction.channel.id == channel_id
    return getattr(interaction.channel, "name", None) == CASINO_CHANNEL_NAME


@bot.tree.command(name="play", description="Open a private menu to the Casino, Ranch, and Dungeon")
@app_commands.check(in_casino_channel_slash)
async def play_slash(interaction: discord.Interaction):
    session = hub_ui.HubSession(interaction)
    embed, view, file = await room_view.build_room_display(
        interaction.guild.id, interaction.user.id, "town_square", session,
    )
    await interaction.response.send_message(embed=embed, file=file, view=view, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message("Use this in the casino channel.", ephemeral=True)
        return
    raise error


db.init_db()
bot.run(TOKEN)
