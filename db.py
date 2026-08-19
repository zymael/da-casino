import sqlite3
from datetime import date, datetime, timedelta, timezone

DB_PATH = "casino.db"
STARTING_BALANCE = 100


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    # users used to be keyed by user_id alone (one global balance shared across every
    # server the bot is in). Move it aside so migrate_legacy_users_into_guilds() can
    # seed each guild the bot is actually in once bot.guilds is known (on_ready) --
    # init_db() itself runs before login and has no guild list to migrate into yet.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "users" in tables:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "guild_id" not in columns:
            conn.execute("ALTER TABLE users RENAME TO users_legacy")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            balance INTEGER NOT NULL DEFAULT 100,
            last_daily TEXT,
            pizzas_bought INTEGER NOT NULL DEFAULT 0,
            last_pizza TEXT,
            last_mine_start TEXT,
            last_mine_claim TEXT,
            last_tip TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    # users predates last_mine_start/last_mine_claim/last_tip -- add them for installs
    # where CREATE TABLE IF NOT EXISTS above was a no-op against an older schema.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for column in ("last_mine_start", "last_mine_claim", "last_tip"):
        if column not in columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS champions (
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, kind)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS champion_base_nick (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            base_nick TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    # One row per achievement kind per guild -- whoever's INSERT lands first claims it, and it
    # never moves afterward (unlike the champions crowns above, which follow the current leader).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS achievements (
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            achieved_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, kind)
        )
        """
    )
    # Unlike `achievements` above, every user can claim each kind independently (e.g. everyone
    # can eventually earn "won a blackjack hand"), so the primary key includes user_id.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS personal_achievements (
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            achieved_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, kind, user_id)
        )
        """
    )
    # Per-user, per-game win/loss counts, started fresh (not backfilled from bet_log) --
    # feeds the achievements.py tier achievements (10/25/50/... wins or losses of a game).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS game_stats (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, game)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            casino_channel_id INTEGER,
            currency_name TEXT
        )
        """
    )
    # guild_settings predates currency_name -- add it for installs where CREATE TABLE
    # IF NOT EXISTS above was a no-op against an older schema.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(guild_settings)")}
    if "currency_name" not in columns:
        conn.execute("ALTER TABLE guild_settings ADD COLUMN currency_name TEXT")
    # horse_ownership and horse_record (both briefly shipped, holding nothing but seed/empty
    # data so far — no real purchases or races existed against either) are superseded by the
    # single `horses` table below, which now also owns stats/age. Safe to drop and reseed.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for legacy_table in ("horse_ownership", "horse_record"):
        if legacy_table in tables:
            conn.execute(f"DROP TABLE {legacy_table}")
    # `horses` briefly shipped without race_starts; still holding nothing but seed data (no
    # horse had actually raced yet), so it's safe to drop and reseed rather than migrate.
    if "horses" in tables:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(horses)")}
        if "race_starts" not in columns:
            conn.execute("DROP TABLE horses")
    # One row per horse per guild: horse_index 0..LEGEND_COUNT-1 are the fixed legend roster
    # (lazily seeded from horserace.HORSES on first touch in a guild), horse_index
    # LEGEND_COUNT+ are foals bought into that guild's stable. Stats/age are mutable via
    # training; wins/races accumulate from real races (seeded once a horse first becomes
    # race-eligible, same idea as the old horse_record seeding). race_starts is separate from
    # races: races starts pre-loaded with a virtual seed count for odds purposes, but
    # race_starts only ever counts real starts, since that's what slowly ages a horse too.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS horses (
            guild_id INTEGER NOT NULL,
            horse_index INTEGER NOT NULL,
            is_foal INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL,
            owner_id INTEGER,
            speed REAL NOT NULL,
            endurance REAL NOT NULL,
            spirit REAL NOT NULL,
            age INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            places INTEGER NOT NULL DEFAULT 0,
            shows INTEGER NOT NULL DEFAULT 0,
            races INTEGER NOT NULL DEFAULT 0,
            race_starts INTEGER NOT NULL DEFAULT 0,
            last_trained TEXT,
            PRIMARY KEY (guild_id, horse_index)
        )
        """
    )
    # horses predates places/shows (Place/Show bets) -- add them non-destructively, since
    # unlike the race_starts drop-and-reseed above, this table now holds real accumulated
    # race history that must survive the migration. Horses with real races from before this
    # existed are left at places = shows = 0 here; horserace.current_probabilities() detects
    # that (races > 0 but places = shows = 0) and backfills a stat-simulated estimate the next
    # time it's called for their guild, the same way a brand-new horse gets seeded.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(horses)")}
    for column in ("places", "shows"):
        if column not in columns:
            conn.execute(f"ALTER TABLE horses ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bet_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            bet_amount INTEGER NOT NULL,
            net INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            message_id INTEGER
        )
        """
    )
    # message_id is only set for backfilled rows (one per historical Discord message) and lets
    # a re-run of the backfill script skip rows it already inserted instead of double-counting.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bet_log_message "
        "ON bet_log (message_id, user_id, game) WHERE message_id IS NOT NULL"
    )
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "pizza_champion" in tables:
        for guild_id, user_id, previous_nick in conn.execute(
            "SELECT guild_id, user_id, previous_nick FROM pizza_champion"
        ).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO champions (guild_id, kind, user_id) VALUES (?, 'pizza', ?)",
                (guild_id, user_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO champion_base_nick (guild_id, user_id, base_nick) VALUES (?, ?, ?)",
                (guild_id, user_id, previous_nick),
            )
        conn.execute("DROP TABLE pizza_champion")
    conn.commit()
    conn.close()


def _connect():
    conn = sqlite3.connect(DB_PATH)
    # journal_size_limit isn't persisted in the database file like journal_mode is -- it has to
    # be set on every connection. Caps how large the WAL file is allowed to stay on disk after a
    # checkpoint (8MB), as a defensive bound independent of the default auto-checkpoint threshold.
    conn.execute("PRAGMA journal_size_limit = 8388608")
    return conn


def migrate_legacy_users_into_guilds(guild_ids: list[int]):
    """One-time migration for installs that predate per-guild balances: seeds every
    guild the bot is currently in with the old global balances, so nobody's credits
    vanish when a shared pool becomes per-server pools. No-ops once already migrated."""
    conn = _connect()
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "users_legacy" not in tables:
            return
        legacy_rows = conn.execute(
            "SELECT user_id, balance, last_daily, pizzas_bought, last_pizza FROM users_legacy"
        ).fetchall()
        for guild_id in guild_ids:
            for user_id, balance, last_daily, pizzas_bought, last_pizza in legacy_rows:
                conn.execute(
                    "INSERT OR IGNORE INTO users "
                    "(guild_id, user_id, balance, last_daily, pizzas_bought, last_pizza) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (guild_id, user_id, balance, last_daily, pizzas_bought, last_pizza),
                )
        conn.execute("DROP TABLE users_legacy")
        conn.commit()
    finally:
        conn.close()


def _ensure_user(conn, guild_id: int, user_id: int):
    conn.execute(
        "INSERT OR IGNORE INTO users (guild_id, user_id, balance) VALUES (?, ?, ?)",
        (guild_id, user_id, STARTING_BALANCE),
    )


def get_balance(guild_id: int, user_id: int) -> int:
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        row = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def get_user_economy(guild_id: int, user_id: int) -> tuple[int, int]:
    """Returns (balance, pizzas_bought) for this user in this guild."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        row = conn.execute(
            "SELECT balance, pizzas_bought FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row
    finally:
        conn.close()


def update_balance(guild_id: int, user_id: int, delta: int) -> int:
    """Applies delta to the user's balance and returns the new balance."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
            (delta, guild_id, user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def transfer_balance(guild_id: int, from_id: int, to_id: int, amount: int) -> tuple[bool, int, int]:
    """Moves `amount` credits from one user to another within a guild. Returns (success, from_balance, to_balance)."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, from_id)
        _ensure_user(conn, guild_id, to_id)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        from_balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, from_id)
        ).fetchone()[0]
        if from_balance < amount:
            conn.rollback()
            to_balance = conn.execute(
                "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, to_id)
            ).fetchone()[0]
            return False, from_balance, to_balance
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, from_id),
        )
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, to_id),
        )
        conn.commit()
        from_balance, to_balance = (
            conn.execute(
                "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, from_id)
            ).fetchone()[0],
            conn.execute(
                "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, to_id)
            ).fetchone()[0],
        )
        return True, from_balance, to_balance
    finally:
        conn.close()


def get_leaderboard(guild_id: int, limit: int = 10) -> list[tuple[int, int, int]]:
    """Returns up to `limit` (user_id, balance, pizzas_bought) rows for this guild, highest balance first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, balance, pizzas_bought FROM users WHERE guild_id = ? "
            "ORDER BY balance DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
        return rows
    finally:
        conn.close()


def get_pizza_leaderboard(guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
    """Returns up to `limit` (user_id, pizzas_bought) rows for this guild, most pizza first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, pizzas_bought FROM users WHERE guild_id = ? AND pizzas_bought > 0 "
            "ORDER BY pizzas_bought DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
        return rows
    finally:
        conn.close()


def log_bet(
    guild_id: int,
    user_id: int,
    game: str,
    bet_amount: int,
    net: int,
    message_id: int | None = None,
    created_at: str | None = None,
):
    """Records the outcome of a single resolved bet (blackjack hand, slots spin, roulette
    bet). `message_id` is only passed by the history backfill script, so re-running it
    doesn't double-insert the same historical bet."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO bet_log (guild_id, user_id, game, bet_amount, net, created_at, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                user_id,
                game,
                bet_amount,
                net,
                created_at or datetime.now(timezone.utc).isoformat(),
                message_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_biggest_win(guild_id: int, limit: int = 10) -> list[tuple[int, int, str]]:
    """Returns up to `limit` (user_id, net, game) rows, one per user (their single best win),
    highest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT user_id, net, game FROM (
                SELECT user_id, net, game,
                       ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY net DESC, id ASC) AS rn
                FROM bet_log
                WHERE guild_id = ? AND net > 0
            ) WHERE rn = 1
            ORDER BY net DESC LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
        return rows
    finally:
        conn.close()


def get_biggest_loss(guild_id: int, limit: int = 10) -> list[tuple[int, int, str]]:
    """Returns up to `limit` (user_id, net, game) rows, one per user (their single worst loss),
    lowest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT user_id, net, game FROM (
                SELECT user_id, net, game,
                       ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY net ASC, id ASC) AS rn
                FROM bet_log
                WHERE guild_id = ? AND net < 0
            ) WHERE rn = 1
            ORDER BY net ASC LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
        return rows
    finally:
        conn.close()


def get_user_bet_summary(guild_id: int, user_id: int) -> tuple[int, int, int, int | None, int | None]:
    """Returns (bet_count, total_won, total_lost, best_win, worst_loss) across every logged bet
    for this user in this guild. total_won/total_lost are both non-negative sums; best_win and
    worst_loss are None if the user has never won (or never lost) a bet."""
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN net > 0 THEN net ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN net < 0 THEN -net ELSE 0 END), 0),
                MAX(CASE WHEN net > 0 THEN net END),
                MIN(CASE WHEN net < 0 THEN net END)
            FROM bet_log WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()
        return row
    finally:
        conn.close()


def get_champion(guild_id: int, kind: str) -> int | None:
    """Returns the user_id currently holding the given badge `kind` in this guild, or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT user_id FROM champions WHERE guild_id = ? AND kind = ?", (guild_id, kind)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def clear_champion(guild_id: int, kind: str):
    conn = _connect()
    try:
        conn.execute("DELETE FROM champions WHERE guild_id = ? AND kind = ?", (guild_id, kind))
        conn.commit()
    finally:
        conn.close()


def set_champion(guild_id: int, kind: str, user_id: int):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO champions (guild_id, kind, user_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, kind) DO UPDATE SET user_id = excluded.user_id",
            (guild_id, kind, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_badges(guild_id: int, user_id: int) -> set[str]:
    """Returns the set of badge kinds a user currently holds in this guild."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT kind FROM champions WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def award_first_achievement(guild_id: int, kind: str, user_id: int) -> bool:
    """Claims `kind` for `user_id` in this guild if nobody has claimed it yet. Returns whether
    this call was the one that claimed it -- False if someone (possibly this same user, on a
    repeat trigger) already holds it."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO achievements (guild_id, kind, user_id, achieved_at) VALUES (?, ?, ?, ?)",
            (guild_id, kind, user_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_guild_achievements(guild_id: int) -> dict[str, tuple[int, str]]:
    """Returns {kind: (user_id, achieved_at)} for every "first"-scoped achievement already
    claimed in this guild."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT kind, user_id, achieved_at FROM achievements WHERE guild_id = ?", (guild_id,)
        ).fetchall()
        return {kind: (user_id, achieved_at) for kind, user_id, achieved_at in rows}
    finally:
        conn.close()


def award_personal_achievement(guild_id: int, kind: str, user_id: int) -> bool:
    """Claims `kind` for `user_id` in this guild if they haven't already earned it. Returns
    whether this call was the one that claimed it -- False if they already had it."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO personal_achievements (guild_id, kind, user_id, achieved_at) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, kind, user_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_user_personal_achievements(guild_id: int, user_id: int) -> set[str]:
    """Returns every personal achievement kind this user has earned in this guild."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT kind FROM personal_achievements WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def record_game_outcome(guild_id: int, user_id: int, game: str, net: int) -> tuple[int, int]:
    """Increments this user's win or loss count for `game` in this guild based on the sign of
    `net` (net == 0, e.g. a blackjack push, is a caller error -- don't call this for it). Returns
    (wins, losses) after the update."""
    column = "wins" if net > 0 else "losses"
    conn = _connect()
    try:
        conn.execute(
            f"INSERT INTO game_stats (guild_id, user_id, game, {column}) VALUES (?, ?, ?, 1) "
            f"ON CONFLICT(guild_id, user_id, game) DO UPDATE SET {column} = {column} + 1",
            (guild_id, user_id, game),
        )
        conn.commit()
        row = conn.execute(
            "SELECT wins, losses FROM game_stats WHERE guild_id = ? AND user_id = ? AND game = ?",
            (guild_id, user_id, game),
        ).fetchone()
        return row
    finally:
        conn.close()


def get_user_game_stats(guild_id: int, user_id: int) -> dict[str, tuple[int, int]]:
    """Returns {game: (wins, losses)} for every game bucket this user has played in this guild."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT game, wins, losses FROM game_stats WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchall()
        return {game: (wins, losses) for game, wins, losses in rows}
    finally:
        conn.close()


def ensure_base_nick(guild_id: int, user_id: int, current_nick: str | None) -> str | None:
    """Records `current_nick` as the user's badge-free nickname the first time they earn any
    badge in this guild, and returns it (or the previously recorded one on later calls)."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO champion_base_nick (guild_id, user_id, base_nick) VALUES (?, ?, ?)",
            (guild_id, user_id, current_nick),
        )
        conn.commit()
        row = conn.execute(
            "SELECT base_nick FROM champion_base_nick WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def buy_pizza(guild_id: int, user_id: int, cost: int, cooldown_seconds: int) -> tuple[str, int]:
    """Attempts to buy a pizza, checking cooldown then affordability.

    Returns (status, value):
      - ("cooldown", seconds_remaining) — still on cooldown
      - ("broke", balance) — off cooldown but can't afford it
      - ("ok", new_balance) — purchase succeeded
    """
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        balance, last_pizza = conn.execute(
            "SELECT balance, last_pizza FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()

        now = datetime.now(timezone.utc)
        if last_pizza:
            elapsed = (now - datetime.fromisoformat(last_pizza)).total_seconds()
            if elapsed < cooldown_seconds:
                return "cooldown", int(cooldown_seconds - elapsed)

        if balance < cost:
            return "broke", balance

        conn.execute(
            "UPDATE users SET balance = balance - ?, pizzas_bought = pizzas_bought + 1, last_pizza = ? "
            "WHERE guild_id = ? AND user_id = ?",
            (cost, now.isoformat(), guild_id, user_id),
        )
        conn.commit()
        new_balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        return "ok", new_balance
    finally:
        conn.close()


def get_casino_channel_id(guild_id: int) -> int | None:
    """Returns the configured casino channel id for this guild, or None if unset."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT casino_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_casino_channel_id(guild_id: int, channel_id: int):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, casino_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET casino_channel_id = excluded.casino_channel_id",
            (guild_id, channel_id),
        )
        conn.commit()
    finally:
        conn.close()


DEFAULT_CURRENCY_NAME = "credits"

# Rendering code (embeds, view classes) runs synchronously and can't await a DB call per
# message, so the per-guild currency name lives in this in-memory cache once loaded --
# see load_currency_name_cache (called once per guild on startup) and set_currency_name.
_currency_name_cache: dict[int, str] = {}


def get_currency_name(guild_id: int) -> str:
    """Synchronous, cache-only lookup -- safe to call from any rendering code path."""
    return _currency_name_cache.get(guild_id, DEFAULT_CURRENCY_NAME)


def load_currency_name_cache(guild_id: int) -> str:
    """Reads the persisted currency name for `guild_id` into the in-memory cache and
    returns it. Call once per guild on startup."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT currency_name FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        name = row[0] if row and row[0] else DEFAULT_CURRENCY_NAME
        _currency_name_cache[guild_id] = name
        return name
    finally:
        conn.close()


def set_currency_name(guild_id: int, name: str) -> str:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, currency_name) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET currency_name = excluded.currency_name",
            (guild_id, name),
        )
        conn.commit()
    finally:
        conn.close()
    _currency_name_cache[guild_id] = name
    return name


def get_guild_horses(guild_id: int) -> dict[int, dict]:
    """Returns {horse_index: {...}} for every horse that exists in this guild's stable —
    legends already touched here, plus any foals bought into it."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT horse_index, is_foal, name, owner_id, speed, endurance, spirit, age, wins, places, shows, "
            "races, race_starts, last_trained FROM horses WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        return {
            horse_index: {
                "is_foal": bool(is_foal),
                "name": name,
                "owner_id": owner_id,
                "speed": speed,
                "endurance": endurance,
                "spirit": spirit,
                "age": age,
                "wins": wins,
                "places": places,
                "shows": shows,
                "races": races,
                "race_starts": race_starts,
                "last_trained": last_trained,
            }
            for (
                horse_index, is_foal, name, owner_id, speed, endurance, spirit, age, wins, places, shows,
                races, race_starts, last_trained,
            ) in rows
        }
    finally:
        conn.close()


def get_horses_owned_by(guild_id: int, user_id: int) -> list[tuple[int, bool, str, int, int, int, int]]:
    """Returns (horse_index, is_foal, name, age, wins, places, races) for every horse this
    user owns in this guild, ordered by index."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT horse_index, is_foal, name, age, wins, places, races FROM horses "
            "WHERE guild_id = ? AND owner_id = ? ORDER BY horse_index",
            (guild_id, user_id),
        ).fetchall()
        return rows
    finally:
        conn.close()


def seed_legend(guild_id: int, horse_index: int, name: str, speed: float, endurance: float, spirit: float, age: int):
    """Creates a legend horse's row the first time it's touched in a guild. A no-op if it
    already exists (never overwrites a rename, training, or ownership already in place)."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO horses "
            "(guild_id, horse_index, is_foal, name, owner_id, speed, endurance, spirit, age) "
            "VALUES (?, ?, 0, ?, NULL, ?, ?, ?, ?)",
            (guild_id, horse_index, name, speed, endurance, spirit, age),
        )
        conn.commit()
    finally:
        conn.close()


def next_horse_index(guild_id: int, legend_count: int) -> int:
    """The horse_index a newly bought foal should get: right after the highest index this
    guild has used so far (starting at legend_count if no foals exist yet)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MAX(horse_index) FROM horses WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        current_max = row[0]
        return max(legend_count - 1, current_max if current_max is not None else -1) + 1
    finally:
        conn.close()


def buy_legend_horse(guild_id: int, horse_index: int, user_id: int, price: int) -> tuple[str, int]:
    """Attempts to buy an unowned legend (its row must already exist via seed_legend).
    Returns (status, balance): "ok", "owned", or "broke"."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner_id FROM horses WHERE guild_id = ? AND horse_index = ?", (guild_id, horse_index)
        ).fetchone()
        if row is None or row[0] is not None:
            conn.rollback()
            balance = conn.execute(
                "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            ).fetchone()[0]
            return "owned", balance
        balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        if balance < price:
            conn.rollback()
            return "broke", balance
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
            (price, guild_id, user_id),
        )
        conn.execute(
            "UPDATE horses SET owner_id = ? WHERE guild_id = ? AND horse_index = ?",
            (user_id, guild_id, horse_index),
        )
        conn.commit()
        new_balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        return "ok", new_balance
    finally:
        conn.close()


def buy_foal(
    guild_id: int, horse_index: int, user_id: int, name: str, price: int,
    speed: float, endurance: float, spirit: float,
) -> tuple[str, int]:
    """Creates and buys a brand-new foal in one step. Returns (status, balance): "ok" or "broke"."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        if balance < price:
            conn.rollback()
            return "broke", balance
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
            (price, guild_id, user_id),
        )
        conn.execute(
            "INSERT INTO horses (guild_id, horse_index, is_foal, name, owner_id, speed, endurance, spirit, age) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?, 0)",
            (guild_id, horse_index, name, user_id, speed, endurance, spirit),
        )
        conn.commit()
        new_balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        return "ok", new_balance
    finally:
        conn.close()


def rename_horse(guild_id: int, horse_index: int, user_id: int, new_name: str) -> bool:
    """Renames a horse if user_id owns it. Returns whether the rename happened."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT owner_id FROM horses WHERE guild_id = ? AND horse_index = ?",
            (guild_id, horse_index),
        ).fetchone()
        if not row or row[0] != user_id:
            return False
        conn.execute(
            "UPDATE horses SET name = ? WHERE guild_id = ? AND horse_index = ?",
            (new_name, guild_id, horse_index),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def seed_race_history(guild_id: int, horse_index: int, wins: int, places: int, shows: int, races: int):
    """Gives a horse with no real races yet in this guild a starting (wins, places, shows,
    races) record. Only applies while races is still 0, so it never overwrites real
    accumulated results."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE horses SET wins = ?, places = ?, shows = ?, races = ? "
            "WHERE guild_id = ? AND horse_index = ? AND races = 0",
            (wins, places, shows, races, guild_id, horse_index),
        )
        conn.commit()
    finally:
        conn.close()


def backfill_place_show(guild_id: int, horse_index: int, places: int, shows: int):
    """One-time top-up for a horse with real races from before place/show tracking existed.
    Guarded by places = 0 AND shows = 0, so it never overwrites real accumulated results."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE horses SET places = ?, shows = ? "
            "WHERE guild_id = ? AND horse_index = ? AND places = 0 AND shows = 0",
            (places, shows, guild_id, horse_index),
        )
        conn.commit()
    finally:
        conn.close()


def record_race_result(guild_id: int, finish_order: list[int], race_age_interval: int):
    """finish_order is every horse that ran, ranked 1st-first. Increments races/race_starts
    for all of them, wins/places/shows for whichever finished top-1/top-2/top-3, and ages a
    horse by 1 every `race_age_interval` real starts (computed from the post-increment
    race_starts, so the horse that crosses the 10th/20th/... start this call gets its age
    bump in the same update)."""
    conn = _connect()
    try:
        for rank, horse_index in enumerate(finish_order, start=1):
            won = 1 if rank == 1 else 0
            placed = 1 if rank <= 2 else 0
            showed = 1 if rank <= 3 else 0
            conn.execute(
                "UPDATE horses SET races = races + 1, wins = wins + ?, places = places + ?, "
                "shows = shows + ?, race_starts = race_starts + 1, "
                "age = age + CASE WHEN (race_starts + 1) % ? = 0 THEN 1 ELSE 0 END "
                "WHERE guild_id = ? AND horse_index = ?",
                (won, placed, showed, race_age_interval, guild_id, horse_index),
            )
        conn.commit()
    finally:
        conn.close()


def train_horse(
    guild_id: int, horse_index: int, user_id: int, speed_gain: float, endurance_gain: float,
    spirit_gain: float, stat_cap: float,
) -> tuple[str, tuple | None]:
    """Trains a horse if user_id owns it and it hasn't been trained yet today. Returns
    (status, payload): "ok" with (new_speed, new_endurance, new_spirit, new_age), "not_owner",
    or "cooldown" (already trained today)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT owner_id, speed, endurance, spirit, age, last_trained FROM horses "
            "WHERE guild_id = ? AND horse_index = ?",
            (guild_id, horse_index),
        ).fetchone()
        if row is None or row[0] != user_id:
            return "not_owner", None
        _owner_id, speed, endurance, spirit, age, last_trained = row
        today = date.today().isoformat()
        if last_trained == today:
            return "cooldown", None
        new_speed = min(stat_cap, speed + speed_gain)
        new_endurance = min(stat_cap, endurance + endurance_gain)
        new_spirit = min(stat_cap, spirit + spirit_gain)
        new_age = age + 1
        conn.execute(
            "UPDATE horses SET speed = ?, endurance = ?, spirit = ?, age = ?, last_trained = ? "
            "WHERE guild_id = ? AND horse_index = ?",
            (new_speed, new_endurance, new_spirit, new_age, today, guild_id, horse_index),
        )
        conn.commit()
        return "ok", (new_speed, new_endurance, new_spirit, new_age)
    finally:
        conn.close()


def _seconds_until_next_day() -> float:
    """Seconds remaining until the calendar day (per date.today()) rolls over."""
    tomorrow = date.today() + timedelta(days=1)
    next_reset = datetime.combine(tomorrow, datetime.min.time())
    return max(0.0, (next_reset - datetime.now()).total_seconds())


def claim_daily(guild_id: int, user_id: int, amount: int) -> tuple[str, float | int]:
    """Grants `amount` credits once per calendar day.

    Returns (status, value):
      - ("cooldown", seconds_remaining) — already claimed today
      - ("claimed", new_balance) — credits granted
    """
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        today = date.today().isoformat()
        last_daily, balance = conn.execute(
            "SELECT last_daily, balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        if last_daily == today:
            return "cooldown", _seconds_until_next_day()
        new_balance = balance + amount
        conn.execute(
            "UPDATE users SET balance = ?, last_daily = ? WHERE guild_id = ? AND user_id = ?",
            (new_balance, today, guild_id, user_id),
        )
        conn.commit()
        return "claimed", new_balance
    finally:
        conn.close()


def claim_mine(
    guild_id: int, user_id: int, reward: int, mature_seconds: int, cooldown_seconds: int
) -> tuple[str, float | int | None]:
    """Two-step mining: the first call starts a dig; a second call made at least
    `mature_seconds` later collects `reward` credits and starts a `cooldown_seconds`
    cooldown before the next dig can be started.

    Returns (status, value):
      - ("started", None) — no dig was pending; one was just started
      - ("pending", seconds_remaining) — dig in progress, not ready to collect yet
      - ("cooldown", seconds_remaining) — last dig already collected, still on cooldown
      - ("claimed", new_balance) — dig was ready; credits collected
    """
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        last_mine_start, last_mine_claim, balance = conn.execute(
            "SELECT last_mine_start, last_mine_claim, balance FROM users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()

        now = datetime.now(timezone.utc)

        if last_mine_start is not None:
            elapsed = (now - datetime.fromisoformat(last_mine_start)).total_seconds()
            if elapsed < mature_seconds:
                return "pending", mature_seconds - elapsed
            new_balance = balance + reward
            conn.execute(
                "UPDATE users SET balance = ?, last_mine_start = NULL, last_mine_claim = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (new_balance, now.isoformat(), guild_id, user_id),
            )
            conn.commit()
            return "claimed", new_balance

        if last_mine_claim is not None:
            elapsed = (now - datetime.fromisoformat(last_mine_claim)).total_seconds()
            if elapsed < cooldown_seconds:
                return "cooldown", cooldown_seconds - elapsed

        conn.execute(
            "UPDATE users SET last_mine_start = ? WHERE guild_id = ? AND user_id = ?",
            (now.isoformat(), guild_id, user_id),
        )
        conn.commit()
        return "started", None
    finally:
        conn.close()


def tip(guild_id: int, from_id: int, to_id: int, amount: int) -> tuple[str, float | int]:
    """Grants `amount` newly generated credits to `to_id`, once per calendar day per
    sender -- same once-a-day gating as claim_daily.

    Returns (status, value):
      - ("cooldown", seconds_remaining) — sender already tipped today
      - ("ok", to_balance) — credits granted
    """
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, from_id)
        _ensure_user(conn, guild_id, to_id)
        conn.commit()
        today = date.today().isoformat()
        (last_tip,) = conn.execute(
            "SELECT last_tip FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, from_id)
        ).fetchone()
        if last_tip == today:
            return "cooldown", _seconds_until_next_day()

        conn.execute(
            "UPDATE users SET last_tip = ? WHERE guild_id = ? AND user_id = ?",
            (today, guild_id, from_id),
        )
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, to_id),
        )
        conn.commit()
        to_balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, to_id)
        ).fetchone()[0]
        return "ok", to_balance
    finally:
        conn.close()
