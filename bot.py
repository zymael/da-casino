import asyncio
import os
import random
import time

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import db
from blackjack_view import active_tables as active_blackjack_tables, start_blackjack_table
from holdem_view import (
    BIG_BLIND as HOLDEM_BIG_BLIND,
    active_tables as active_holdem_tables,
    busy_players as holdem_busy_players,
    start_holdem_table,
)
import horserace
from horserace_view import HorseRaceView, active_races
from roulette_view import RouletteView, active_rounds
from slots_view import SlotsView

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

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

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

HELP_CATEGORIES = [
    ("💰 Economy", ["balance", "daily", "mine", "tip", "transfer", "pizza", "leaderboard"]),
    ("🎲 Casino Games", ["blackjack", "slots", "roulette", "holdem"]),
    ("🐎 Horse Racing", ["horserace", "horses", "buyhorse", "buyfoal", "renamehorse", "train"]),
    ("⚙️ Utility", ["ping", "setcasino", "setcurrency"]),
]


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


async def _reject_if_at_poker_table(ctx) -> bool:
    """True (and sends a message) if the author is mid-hand at a poker table — their
    tracked stack there would desync from their balance if they spent money elsewhere."""
    if ctx.author.id in holdem_busy_players:
        await ctx.send(f"{ctx.author.display_name}, finish your poker hand first!")
        return True
    return False


@bot.event
async def on_ready():
    print(f"{bot.user} has connected to Discord!", flush=True)
    print("------", flush=True)
    await asyncio.to_thread(db.migrate_legacy_users_into_guilds, [g.id for g in bot.guilds])
    for guild in bot.guilds:  # warm each guild's seed so it's not the first command paying for it
        await asyncio.to_thread(horserace.current_win_probabilities, guild.id)
        await asyncio.to_thread(db.load_currency_name_cache, guild.id)
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
        description="Run `!help <command>` for details on any command below.",
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


@bot.command(name="leaderboard", aliases=["lb", "top"])
async def leaderboard(ctx):
    """Show the top credit holders, top pizza buyers, and biggest single-bet win/loss."""
    credit_rows = await asyncio.to_thread(db.get_leaderboard, ctx.guild.id, 10)
    pizza_rows = await asyncio.to_thread(db.get_pizza_leaderboard, ctx.guild.id, 10)
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

    win_lines = [bet_rank_line(i, user_id, net, game) for i, (user_id, net, game) in enumerate(win_rows)]
    embed.add_field(
        name="Biggest Win (single bet)", value="\n".join(win_lines) or "No wins logged yet!", inline=False
    )

    loss_lines = [bet_rank_line(i, user_id, net, game) for i, (user_id, net, game) in enumerate(loss_rows)]
    embed.add_field(
        name="Biggest Loss (single bet)", value="\n".join(loss_lines) or "No losses logged yet!", inline=False
    )

    await ctx.send(embed=embed)


@bot.command(name="daily")
async def daily(ctx):
    """Claim your daily credits (once per day)."""
    status, value = await asyncio.to_thread(db.claim_daily, ctx.guild.id, ctx.author.id, DAILY_AMOUNT)
    if status == "cooldown":
        await ctx.send(
            f"⏳ {ctx.author.display_name}, you already claimed your daily credits today. "
            f"You can claim again {_in_seconds(value)}."
        )
        return

    currency = db.get_currency_name(ctx.guild.id)
    await ctx.send(
        f"✅ {ctx.author.display_name} claimed their daily **{DAILY_AMOUNT}** {currency}! Balance: **{value}**"
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

    view = SlotsView(ctx.author, balance, ctx.guild.id)
    message = await ctx.send(embed=view.build_bet_embed(), view=view, file=view.build_initial_file())
    view.message = message


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

    roster, eligible, probabilities = await asyncio.to_thread(horserace.current_win_probabilities, ctx.guild.id)
    race_field = horserace.select_race_field(eligible)
    view = HorseRaceView(ctx.author, ctx.channel.id, ctx.guild.id, roster, race_field, probabilities)
    active_races[ctx.channel.id] = view
    embed, file = view.build_display()
    message = await ctx.send(embed=embed, file=file, view=view)
    view.message = message


@bot.command(name="horses", aliases=["stable"])
async def horses_cmd(ctx):
    """List every horse in the stable, its odds/price/record, and who owns it: !horses"""
    roster, _eligible, probabilities = await asyncio.to_thread(horserace.current_win_probabilities, ctx.guild.id)
    currency = db.get_currency_name(ctx.guild.id)
    lines = []
    for i in sorted(roster):
        horse = roster[i]
        kind = "🐣 Foal" if horse["is_foal"] else "🏆 Legend"
        if horse["age"] < horserace.MIN_RACING_AGE:
            odds = "—"
            status = f"Growing (age {horse['age']}/{horserace.MIN_RACING_AGE}) — owned by <@{horse['owner_id']}>"
        else:
            odds = horserace.describe_odds(i, probabilities)
            if horse["owner_id"] is not None:
                status = f"Owned by <@{horse['owner_id']}>"
            else:
                status = f"💰 {horserace.price_of(i, probabilities)} {currency} — `!buyhorse {i + 1}`"
        record = f"{horse['wins']}W-{horse['races'] - horse['wins']}L" if horse["races"] else "unraced"
        stats = (
            f"SPD {horse['speed']:.0f} / END {horse['endurance']:.0f} / SPI {horse['spirit']:.0f} — {record}"
        )
        lines.append(f"**{i + 1}. {horse['name']}** ({odds}) — {kind} — {status}\n{stats}")

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


@bot.command(name="buyhorse")
async def buyhorse_cmd(ctx, number: int = None):
    """Buy an unowned legend: !buyhorse <number> — see !horses for numbers and prices"""
    if await _reject_if_at_poker_table(ctx):
        return
    if number is None or not 1 <= number <= horserace.LEGEND_COUNT:
        await ctx.send(f"Usage: `!buyhorse <1-{horserace.LEGEND_COUNT}>` — see `!horses` for the list.")
        return

    horse_index = number - 1
    _roster, _eligible, probabilities = await asyncio.to_thread(horserace.current_win_probabilities, ctx.guild.id)
    price = horserace.price_of(horse_index, probabilities)
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
    status, balance = await asyncio.to_thread(
        db.buy_foal, ctx.guild.id, horse_index, ctx.author.id, name, horserace.FOAL_PRICE, *horserace.FOAL_BASE_STATS.values()
    )
    if status == "broke":
        currency = db.get_currency_name(ctx.guild.id)
        await ctx.send(f"A foal costs **{horserace.FOAL_PRICE}** {currency} — you only have **{balance}**.")
        return

    currency = db.get_currency_name(ctx.guild.id)
    await ctx.send(
        f"🐣 {ctx.author.display_name} bought a foal named **{name}** for **{horserace.FOAL_PRICE}** {currency}! "
        f"Balance: **{balance}**. Train it with `!train {horse_index + 1}` once a day — it needs to reach "
        f"age {horserace.MIN_RACING_AGE} before it can race."
    )


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


@bot.command(name="train")
async def train_cmd(ctx, number: int = None):
    """Train a horse you own once per day, raising its stats and age: !train <number>"""
    if number is None or number < 1:
        await ctx.send("Usage: `!train <number>` — see `!horses` for numbers.")
        return

    horse_index = number - 1
    speed_gain = random.uniform(horserace.TRAIN_STAT_GAIN_MIN, horserace.TRAIN_STAT_GAIN_MAX)
    endurance_gain = random.uniform(horserace.TRAIN_STAT_GAIN_MIN, horserace.TRAIN_STAT_GAIN_MAX)
    spirit_gain = random.uniform(horserace.TRAIN_STAT_GAIN_MIN, horserace.TRAIN_STAT_GAIN_MAX)
    status, payload = await asyncio.to_thread(
        db.train_horse, ctx.guild.id, horse_index, ctx.author.id,
        speed_gain, endurance_gain, spirit_gain, horserace.STAT_CAP,
    )
    if status == "not_owner":
        await ctx.send("You don't own that horse — check `!horses` to see who does.")
        return
    if status == "cooldown":
        await ctx.send("That horse has already been trained today — try again tomorrow.")
        return

    new_speed, new_endurance, new_spirit, new_age = payload
    horses = await asyncio.to_thread(db.get_guild_horses, ctx.guild.id)
    name = horses[horse_index]["name"]
    lines = [f"🏋️ **{name}** trained! SPD {new_speed:.0f} / END {new_endurance:.0f} / SPI {new_spirit:.0f} — Age {new_age}"]
    if new_age < horserace.MIN_RACING_AGE:
        lines.append(f"Still growing — needs age {horserace.MIN_RACING_AGE} to enter a race.")
    elif new_age == horserace.MIN_RACING_AGE:
        lines.append("🎉 Old enough to race now!")
    await ctx.send("\n".join(lines))


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
BADGES = [("money", MONEY_CHAMPION_EMOJI), ("pizza", PIZZA_CHAMPION_EMOJI)]


async def _refresh_nick(member: discord.Member, guild_id: int) -> bool:
    """Rebuilds a member's nickname from every badge they currently hold, or restores
    their original nickname if they hold none. Returns False only if Discord rejected the edit."""
    if member.id == member.guild.owner_id:
        # Discord blocks nickname changes for the guild owner no matter the bot's role
        # hierarchy or permissions — there's no badge prefix to add/remove for them, so
        # treat this as a permanent no-op success rather than a retryable failure.
        return True

    held = await asyncio.to_thread(db.get_user_badges, guild_id, member.id)
    base = await asyncio.to_thread(db.ensure_base_nick, guild_id, member.id, member.nick)
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


@tasks.loop(seconds=60)
async def sync_champions_loop():
    for guild in bot.guilds:
        await _update_pizza_champion(guild)
        await _update_money_champion(guild)


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


db.init_db()
bot.run(TOKEN)
