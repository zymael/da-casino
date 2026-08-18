import sqlite3
from datetime import date, datetime, timezone

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
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            casino_channel_id INTEGER
        )
        """
    )
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
    return sqlite3.connect(DB_PATH)


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


def claim_daily(guild_id: int, user_id: int, amount: int) -> tuple[bool, int]:
    """Grants `amount` credits once per calendar day. Returns (claimed, balance)."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        today = date.today().isoformat()
        last_daily, balance = conn.execute(
            "SELECT last_daily, balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        if last_daily == today:
            return False, balance
        new_balance = balance + amount
        conn.execute(
            "UPDATE users SET balance = ?, last_daily = ? WHERE guild_id = ? AND user_id = ?",
            (new_balance, today, guild_id, user_id),
        )
        conn.commit()
        return True, new_balance
    finally:
        conn.close()
