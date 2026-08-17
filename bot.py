import asyncio
import os
import random

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import db
from holdem_view import (
    BIG_BLIND as HOLDEM_BIG_BLIND,
    active_tables as active_holdem_tables,
    busy_players as holdem_busy_players,
    start_holdem_table,
)
from roulette_view import RouletteView, active_rounds
from slots_view import SlotsView, play_spin
from views import BlackjackView

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

DAILY_AMOUNT = 100
CASINO_CHANNEL_NAME = "da-casino"
SLOTS_MAX_BET = 10

PIZZA_COST = 10
PIZZA_COOLDOWN_SECONDS = 10 * 60
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
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.check
async def in_casino_channel(ctx):
    return getattr(ctx.channel, "name", None) == CASINO_CHANNEL_NAME


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
    if not sync_champions_loop.is_running():
        sync_champions_loop.start()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return  # command used outside #da-casino — ignore silently to avoid spamming other channels
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("I couldn't find that user. Try mentioning them, e.g. `!transfer @Bob 50`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("That doesn't look like a valid bet. Use a whole number, e.g. `!slots 50`.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`")
    else:
        raise error


@bot.command(name="ping")
async def ping(ctx):
    """Check the bot's latency."""
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")


@bot.command(name="balance", aliases=["bal", "credits"])
async def balance(ctx):
    """Check your credit balance."""
    bal = await asyncio.to_thread(db.get_balance, ctx.author.id)
    await ctx.send(f"💰 {ctx.author.display_name} has **{bal}** credits.")


@bot.command(name="leaderboard", aliases=["lb", "top"])
async def leaderboard(ctx):
    """Show the top credit holders and top pizza buyers."""
    credit_rows = await asyncio.to_thread(db.get_leaderboard, 10)
    pizza_rows = await asyncio.to_thread(db.get_pizza_leaderboard, 10)
    if not credit_rows and not pizza_rows:
        await ctx.send("No one has any credits yet!")
        return

    medals = ["🥇", "🥈", "🥉"]

    def rank_line(i, user_id, value, suffix):
        member = ctx.guild.get_member(user_id) if ctx.guild else None
        name = member.display_name if member else f"<@{user_id}>"
        rank = medals[i] if i < len(medals) else f"`#{i + 1}`"
        return f"{rank} {name} — **{value}** {suffix}"

    embed = discord.Embed(title="🏆 Casino Leaderboard", color=discord.Color.gold())
    credit_lines = [
        rank_line(i, user_id, bal, "credits") for i, (user_id, bal, _pizzas) in enumerate(credit_rows)
    ]
    embed.add_field(name="Credits", value="\n".join(credit_lines) or "No one has any credits yet!", inline=False)

    pizza_lines = [rank_line(i, user_id, pizzas, "🍕") for i, (user_id, pizzas) in enumerate(pizza_rows)]
    embed.add_field(name="Pizza", value="\n".join(pizza_lines) or "No one has bought pizza yet!", inline=False)

    await ctx.send(embed=embed)


@bot.command(name="daily")
async def daily(ctx):
    """Claim your daily credits (once per day)."""
    claimed, bal = await asyncio.to_thread(db.claim_daily, ctx.author.id, DAILY_AMOUNT)
    if claimed:
        await ctx.send(
            f"✅ {ctx.author.display_name} claimed their daily **{DAILY_AMOUNT}** credits! Balance: **{bal}**"
        )
    else:
        await ctx.send(f"⏳ {ctx.author.display_name}, you already claimed your daily credits today. Come back tomorrow!")


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
    if member.id == ctx.author.id:
        await ctx.send("You can't transfer credits to yourself.")
        return
    if member.bot:
        await ctx.send("You can't transfer credits to a bot.")
        return

    success, from_bal, to_bal = await asyncio.to_thread(
        db.transfer_balance, ctx.author.id, member.id, amount
    )
    if not success:
        embed = discord.Embed(
            title="🪹 Can't send what you don't have",
            description=f"You only have **{from_bal}** credits, {ctx.author.display_name}.",
            color=discord.Color.orange(),
        )
        embed.set_image(url=random.choice(BROKE_GIFS))
        await ctx.send(embed=embed)
        return

    await ctx.send(
        f"💸 {ctx.author.display_name} sent **{amount}** credits to {member.display_name}! "
        f"({ctx.author.display_name}: **{from_bal}**, {member.display_name}: **{to_bal}**)"
    )


@bot.command(name="blackjack", aliases=["bj"])
async def blackjack_cmd(ctx, bet: int = None):
    """Play a hand of blackjack: !blackjack <bet>"""
    if await _reject_if_at_poker_table(ctx):
        return
    if bet is None or bet <= 0:
        await ctx.send("Usage: `!blackjack <bet>` — e.g. `!blackjack 50`")
        return

    balance = await asyncio.to_thread(db.get_balance, ctx.author.id)
    if bet > balance:
        await ctx.send(f"You only have **{balance}** credits, {ctx.author.display_name}.")
        return

    await asyncio.to_thread(db.update_balance, ctx.author.id, -bet)  # escrow the bet

    view = BlackjackView(ctx.author, bet)
    natural_display = await view.resolve_naturals()
    embeds, files = natural_display if natural_display else view.build_display()
    message = await ctx.send(embeds=embeds, files=files, view=view)
    view.message = message


@bot.command(name="slots", aliases=["slot"])
async def slots_cmd(ctx, bet: int = None):
    """Play the slot machine: !slots <bet>"""
    if await _reject_if_at_poker_table(ctx):
        return
    if bet is None or bet <= 0:
        await ctx.send("Usage: `!slots <bet>` — e.g. `!slots 10`")
        return
    if bet > SLOTS_MAX_BET:
        await ctx.send(f"Slots bets are capped at **{SLOTS_MAX_BET}** credits.")
        return

    balance = await asyncio.to_thread(db.get_balance, ctx.author.id)
    if bet > balance:
        await ctx.send(f"You only have **{balance}** credits, {ctx.author.display_name}.")
        return

    reels, payout, new_balance = await play_spin(ctx.author.id, bet)
    view = SlotsView(ctx.author, bet)
    embed = view.build_embed(reels, payout, new_balance)
    message = await ctx.send(embed=embed, view=view)
    view.message = message


@bot.command(name="roulette", aliases=["rl"])
async def roulette_cmd(ctx):
    """Open a roulette table others can join before it spins: !roulette"""
    if ctx.channel.id in active_rounds:
        await ctx.send("A roulette round is already open here — place your bets on that one!")
        return

    view = RouletteView(ctx.author, ctx.channel.id)
    active_rounds[ctx.channel.id] = view
    embed, file = view.build_display()
    message = await ctx.send(embed=embed, file=file, view=view)
    view.message = message


@bot.command(name="holdem", aliases=["poker"])
async def holdem_cmd(ctx, buy_in: int = None):
    """Open a persistent Texas Hold'em table — deals hands back to back until fewer
    than 2 players remain: !holdem [buy_in]"""
    if await _reject_if_at_poker_table(ctx):
        return
    if buy_in is not None and buy_in < HOLDEM_BIG_BLIND:
        await ctx.send(f"Buy-in must be at least the big blind ({HOLDEM_BIG_BLIND} credits).")
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
    rows = await asyncio.to_thread(db.get_pizza_leaderboard, 1)
    await _sync_champion(guild, "pizza", rows[0][0] if rows else None)


async def _update_money_champion(guild: discord.Guild | None):
    rows = await asyncio.to_thread(db.get_leaderboard, 1)
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
    status, value = await asyncio.to_thread(db.buy_pizza, ctx.author.id, PIZZA_COST, PIZZA_COOLDOWN_SECONDS)

    if status == "cooldown":
        minutes, seconds = divmod(value, 60)
        wait_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        await ctx.send(f"🍕 Still full from last time, {ctx.author.display_name} — try again in **{wait_str}**.")
        return

    if status == "broke":
        embed = discord.Embed(
            title="🍕 No pizza for you...",
            description=f"You need **{PIZZA_COST}** credits for a pizza, {ctx.author.display_name}.",
            color=discord.Color.orange(),
        )
        embed.set_image(url=random.choice(BROKE_GIFS))
        await ctx.send(embed=embed)
        return

    balance = value
    embed = discord.Embed(title="🍕 Pizza delivery for the casino!", color=discord.Color.orange())
    embed.set_image(url=random.choice(PIZZA_GIFS))
    embed.set_footer(text=f"Balance: {balance} credits")
    await ctx.send(embed=embed)
    await _update_pizza_champion(ctx.guild)


db.init_db()
bot.run(TOKEN)
