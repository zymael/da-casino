"""Minimal aiohttp server for the Discord Activity POC: serves activity_client/ as static files,
exchanges the OAuth2 authorization code the embedded-app-sdk hands back for an access_token
(using this Application's client secret, kept server-side only, mirroring the official starter's
server/src/app.ts token-exchange endpoint -- same Discord token URL, same form params, no
redirect_uri needed for the Activity flow), and relays basic multiplayer position state over a
WebSocket (see ws_handler below).

Deliberately its own process, separate from bot.py / the da-casino-bot systemd unit -- this is
throwaway POC infrastructure and shouldn't carry any risk to the live casino bot.
"""

import asyncio
import hashlib
import json
import logging
import os

import discord
from aiohttp import ClientSession, web
from dotenv import load_dotenv

import blackjack_view

load_dotenv()
logging.basicConfig(level=logging.INFO)  # aiohttp's access logger is silent without a handler

CLIENT_ID = os.getenv("DISCORD_ACTIVITY_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_ACTIVITY_CLIENT_SECRET")
PORT = int(os.getenv("ACTIVITY_SERVER_PORT", "8787"))

STATIC_DIR = os.path.join(os.path.dirname(__file__), "activity_client")


@web.middleware
async def no_cache_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Discord's discordsays.com proxy in front of the Activity iframe caches static assets
    aggressively by default. During active POC iteration that means a stale/truncated copy of
    main.js can get served even after the origin file changes -- disable caching entirely rather
    than debug cache invalidation on infrastructure we don't control."""
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    return response


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def token_exchange(request: web.Request) -> web.Response:
    body = await request.json()
    code = body.get("code")
    if not code:
        return web.json_response({"error": "missing code"}, status=400)

    async with ClientSession() as session, session.post(
        "https://discord.com/api/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
        },
    ) as resp:
        payload = await resp.json()
        if resp.status != 200:
            return web.json_response(payload, status=resp.status)

    return web.json_response({"access_token": payload["access_token"]})


# --- Multiplayer relay ---------------------------------------------------------------------
# Deliberately "basic": in-memory only (a restart drops everyone), one full player-list snapshot
# broadcast per instance on every join/move/leave (no delta compression, no persistence). An
# "instance" is Discord's own instance_id (everyone who launched this same Activity together),
# or a fixed "standalone" room when testing outside Discord -- see main.js.

PLAYER_COLORS = [
    "#e8813a", "#4fb0e8", "#7ed957", "#e857b0",
    "#f0d43a", "#a35ce8", "#e85c5c", "#5ce8c7",
]


def color_for_user(user_id: str) -> str:
    """Deterministic (not Python's randomized-per-process hash()) so a player's color stays the
    same across server restarts, not just within one connection."""
    digest = hashlib.md5(user_id.encode()).hexdigest()
    return PLAYER_COLORS[int(digest, 16) % len(PLAYER_COLORS)]


def sex_for_user(user_id: str) -> str:
    """Which of the two sprite sheets (activity_client/assets/sprite_{male,female}.png) this
    player renders as -- deterministic for the same reason as color_for_user."""
    digest = hashlib.md5((user_id + "sex").encode()).hexdigest()  # different salt than color
    return "male" if int(digest, 16) % 2 == 0 else "female"


# instance_id -> {websocket: player_state_dict}. Keyed by the live ws connection itself (not a
# separate connection id) since aiohttp WebSocketResponse objects are hashable and this is the
# only handle a connection's cleanup code has on itself.
INSTANCES: dict[str, dict[web.WebSocketResponse, dict]] = {}


async def broadcast_state(instance_id: str):
    room = INSTANCES.get(instance_id)
    if not room:
        return
    payload = json.dumps({"type": "state", "players": list(room.values())})
    dead = []
    for peer_ws in room:
        try:
            await peer_ws.send_str(payload)
        except ConnectionResetError:
            dead.append(peer_ws)
    for peer_ws in dead:
        room.pop(peer_ws, None)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    instance_id = None

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            msg_type = data.get("type")

            if msg_type == "join":
                instance_id = data["instance_id"]
                user_id = data["user_id"]
                INSTANCES.setdefault(instance_id, {})[ws] = {
                    "user_id": user_id,
                    "username": data["username"],
                    "tile_x": data["tile_x"],
                    "tile_y": data["tile_y"],
                    "color": color_for_user(user_id),
                    "sex": sex_for_user(user_id),
                }
                await broadcast_state(instance_id)
            elif msg_type == "move" and instance_id and ws in INSTANCES.get(instance_id, {}):
                INSTANCES[instance_id][ws]["tile_x"] = data["tile_x"]
                INSTANCES[instance_id][ws]["tile_y"] = data["tile_y"]
                await broadcast_state(instance_id)
    finally:
        if instance_id and instance_id in INSTANCES:
            INSTANCES[instance_id].pop(ws, None)
            if INSTANCES[instance_id]:
                await broadcast_state(instance_id)
            else:
                del INSTANCES[instance_id]

    return ws


# --- Blackjack sync ------------------------------------------------------------------------
# Separate WS route/schema from the movement game's /ws (different lifecycle, different message
# shape) -- lets a web client watch and play whichever blackjack table is currently running in a
# given Discord channel, in sync with the classic channel UI. Reads/mutates blackjack_view's
# live in-memory tables directly (same process since bot.py's setup_hook merged this server in),
# through the exact same table.join/quit_seat/set_bet/start/apply_hit/apply_double_down methods
# the Discord buttons call -- one mutation path, two frontends.

# channel_id -> set of websockets currently watching that table.
BLACKJACK_ROOMS: dict[int, set[web.WebSocketResponse]] = {}


async def resolve_member(bot, channel_id: int, user_id: int) -> discord.Member | None:
    """A web action only has a bare Discord user id -- table.join/BlackjackHand etc. all expect
    a real discord.Member (for .mention/.display_name, used in pings and embeds), so this
    resolves one via the channel's guild, same identity the Discord-side buttons already use.
    Checks the cache first (get_member), falls back to a live fetch for a member who hasn't
    interacted with the bot recently enough to be cached."""
    if bot is None:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None or channel.guild is None:
        return None
    member = channel.guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await channel.guild.fetch_member(user_id)
    except discord.NotFound:
        return None


async def broadcast_blackjack_state(channel_id: int):
    room = BLACKJACK_ROOMS.get(channel_id)
    if not room:
        return
    table = blackjack_view.active_tables.get(channel_id)
    model = blackjack_view.table_view_model(table) if table is not None else None
    payload = json.dumps({"type": "state", "model": model})
    dead = []
    for peer_ws in room:
        try:
            await peer_ws.send_str(payload)
        except ConnectionResetError:
            dead.append(peer_ws)
    for peer_ws in dead:
        room.discard(peer_ws)


class _ActivityCtx:
    """Minimal duck-typed stand-in for discord.ext.commands.Context -- blackjack_view.py's
    run_new_table/run_table/play_round internals only ever call ctx.send(...) (confirmed by
    reading every ctx.-prefixed reference in that file), so this is the entire surface needed to
    let a web-triggered table run through the exact same code the Discord !blackjack command
    already uses, rather than a parallel implementation."""

    def __init__(self, channel):
        self.channel = channel

    async def send(self, *args, **kwargs):
        return await self.channel.send(*args, **kwargs)


async def handle_blackjack_action(bot, channel_id: int, data: dict):
    action = data.get("action")
    try:
        user_id = int(data["user_id"])
    except (KeyError, ValueError, TypeError):
        return
    member = await resolve_member(bot, channel_id, user_id)
    if member is None:
        return  # can't act as a Discord user we can't resolve in this table's guild

    if action == "create_table":
        if channel_id in blackjack_view.active_tables:
            return  # one's already running -- nothing to do, the client's state already reflects it
        bet = int(data.get("bet", 0))
        if bet <= 0:
            return
        channel = bot.get_channel(channel_id) if bot is not None else None
        if channel is None or channel.guild is None:
            return
        # create_table is fully synchronous -- active_tables is updated before this line
        # returns, so the broadcast below is guaranteed to see the new table, no race.
        table = blackjack_view.create_table(channel, channel_id, channel.guild.id, member, bet)
        # run_new_table posts the lobby message, waits out the join window, then runs the whole
        # table until it closes -- must not be awaited inline here, or this WS message handler
        # (and this connection's ability to process further messages) would block for the
        # table's entire lifetime. The Discord !blackjack command gets this same "runs in the
        # background" property for free, since discord.py dispatches command invocations as
        # their own task.
        asyncio.create_task(blackjack_view.run_new_table(_ActivityCtx(channel), table))
        await broadcast_blackjack_state(channel_id)
        return

    table = blackjack_view.active_tables.get(channel_id)
    if table is None:
        return

    # If a between-hands decision (keep bet / change bet / quit) is currently waiting on this
    # user, resolving it needs to go through the live BetweenHandsView's own mark_decided --
    # mutating table state alone (e.g. table.quit_seat) wouldn't clear them from its pending set,
    # so the prompt (and the table) would keep waiting on them until BETWEEN_HANDS_SECONDS times
    # out, same as a real AFK player.
    round_ = table.round
    between_hands_view = round_.between_hands_view if round_ is not None else None
    awaiting_decision = between_hands_view is not None and user_id in between_hands_view.pending

    if action == "join":
        bet = int(data.get("bet", 0))
        if bet > 0 and table.seat_for(user_id) is None:
            table.join(member, bet)
            await blackjack_view.update_control_message(table)
    elif action == "keep_bet":
        if awaiting_decision:
            await between_hands_view.mark_decided(user_id)
    elif action == "quit":
        if table.quit_seat(user_id):
            if awaiting_decision:
                await between_hands_view.mark_decided(user_id)
            else:
                await blackjack_view.update_control_message(table)
    elif action == "start":
        if table.seat_for(user_id) is not None:
            table.start()
    elif action == "set_bet":
        bet = int(data.get("bet", 0))
        if bet > 0 and table.set_bet(user_id, bet):
            if awaiting_decision:
                await between_hands_view.mark_decided(user_id)
            else:
                await blackjack_view.update_control_message(table)
    elif action in ("hit", "stand", "double"):
        round_ = table.round
        view = round_.active_view if round_ is not None else None
        if view is None or view.hand.member.id != user_id or view.done:
            return  # not this user's turn (or no turn in progress) -- silently ignore
        async with round_.turn_lock:
            if view.done:
                return
            if action == "hit":
                busted = blackjack_view.apply_hit(table, view.hand)
                if len(view.hand.cards) > 2:
                    view.double_down.disabled = True
                if busted:
                    await view._finish("💥 Bust!")
                else:
                    await view.refresh_message()
            elif action == "stand":
                await view._finish("✋ Stand")
            elif action == "double":
                busted, error = await blackjack_view.apply_double_down(table, view.hand)
                if error is None:
                    await view._finish("💥 Bust!" if busted else "✋ Doubled down")

    await broadcast_blackjack_state(channel_id)


async def blackjack_ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    channel_id = None
    bot = request.app.get("bot")

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            msg_type = data.get("type")

            if msg_type == "join_table":
                try:
                    channel_id = int(data["channel_id"])
                except (KeyError, ValueError, TypeError):
                    continue
                BLACKJACK_ROOMS.setdefault(channel_id, set()).add(ws)
                table = blackjack_view.active_tables.get(channel_id)
                model = blackjack_view.table_view_model(table) if table is not None else None
                await ws.send_str(json.dumps({"type": "state", "model": model}))
            elif msg_type == "action" and channel_id is not None:
                await handle_blackjack_action(bot, channel_id, data)
    finally:
        if channel_id is not None:
            room = BLACKJACK_ROOMS.get(channel_id)
            if room is not None:
                room.discard(ws)
                if not room:
                    del BLACKJACK_ROOMS[channel_id]

    return ws


def build_app(bot=None) -> web.Application:
    app = web.Application(middlewares=[no_cache_middleware])
    app["bot"] = bot  # injected rather than imported, to avoid a circular import with bot.py
    # Lets blackjack_view.py push a fresh state to web watchers the instant something changes on
    # the Discord side (a round dealing, another player's turn, settlement, ...) instead of only
    # when a web client's own action happens to trigger a broadcast as a side effect.
    blackjack_view.on_table_changed = broadcast_blackjack_state
    app.router.add_post("/api/token", token_exchange)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/ws/blackjack", blackjack_ws_handler)
    app.router.add_get("/", index)
    app.router.add_static("/", STATIC_DIR)  # serves /main.js, /style.css, etc. by filename
    return app


if __name__ == "__main__":
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit(
            "Set DISCORD_ACTIVITY_CLIENT_ID and DISCORD_ACTIVITY_CLIENT_SECRET in .env before starting."
        )
    web.run_app(build_app(), port=PORT)
