import sqlite3
from datetime import date, datetime, timezone

DB_PATH = "casino.db"
STARTING_BALANCE = 100


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 100,
            last_daily TEXT,
            pizzas_bought INTEGER NOT NULL DEFAULT 0,
            last_pizza TEXT
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "pizzas_bought" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN pizzas_bought INTEGER NOT NULL DEFAULT 0")
    if "last_pizza" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN last_pizza TEXT")
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


def _ensure_user(conn, user_id: int):
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
        (user_id, STARTING_BALANCE),
    )


def get_balance(user_id: int) -> int:
    conn = _connect()
    try:
        _ensure_user(conn, user_id)
        conn.commit()
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row[0]
    finally:
        conn.close()


def update_balance(user_id: int, delta: int) -> int:
    """Applies delta to the user's balance and returns the new balance."""
    conn = _connect()
    try:
        _ensure_user(conn, user_id)
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id))
        conn.commit()
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row[0]
    finally:
        conn.close()


def transfer_balance(from_id: int, to_id: int, amount: int) -> tuple[bool, int, int]:
    """Moves `amount` credits from one user to another. Returns (success, from_balance, to_balance)."""
    conn = _connect()
    try:
        _ensure_user(conn, from_id)
        _ensure_user(conn, to_id)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        from_balance = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (from_id,)
        ).fetchone()[0]
        if from_balance < amount:
            conn.rollback()
            to_balance = conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (to_id,)
            ).fetchone()[0]
            return False, from_balance, to_balance
        conn.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, from_id))
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, to_id))
        conn.commit()
        from_balance, to_balance = (
            conn.execute("SELECT balance FROM users WHERE user_id = ?", (from_id,)).fetchone()[0],
            conn.execute("SELECT balance FROM users WHERE user_id = ?", (to_id,)).fetchone()[0],
        )
        return True, from_balance, to_balance
    finally:
        conn.close()


def get_leaderboard(limit: int = 10) -> list[tuple[int, int, int]]:
    """Returns up to `limit` (user_id, balance, pizzas_bought) rows, highest balance first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, balance, pizzas_bought FROM users ORDER BY balance DESC LIMIT ?", (limit,)
        ).fetchall()
        return rows
    finally:
        conn.close()


def get_pizza_leaderboard(limit: int = 10) -> list[tuple[int, int]]:
    """Returns up to `limit` (user_id, pizzas_bought) rows, most pizza first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, pizzas_bought FROM users WHERE pizzas_bought > 0 "
            "ORDER BY pizzas_bought DESC LIMIT ?",
            (limit,),
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


def buy_pizza(user_id: int, cost: int, cooldown_seconds: int) -> tuple[str, int]:
    """Attempts to buy a pizza, checking cooldown then affordability.

    Returns (status, value):
      - ("cooldown", seconds_remaining) — still on cooldown
      - ("broke", balance) — off cooldown but can't afford it
      - ("ok", new_balance) — purchase succeeded
    """
    conn = _connect()
    try:
        _ensure_user(conn, user_id)
        conn.commit()
        balance, last_pizza = conn.execute(
            "SELECT balance, last_pizza FROM users WHERE user_id = ?", (user_id,)
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
            "WHERE user_id = ?",
            (cost, now.isoformat(), user_id),
        )
        conn.commit()
        new_balance = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
        return "ok", new_balance
    finally:
        conn.close()


def claim_daily(user_id: int, amount: int) -> tuple[bool, int]:
    """Grants `amount` credits once per calendar day. Returns (claimed, balance)."""
    conn = _connect()
    try:
        _ensure_user(conn, user_id)
        conn.commit()
        today = date.today().isoformat()
        last_daily, balance = conn.execute(
            "SELECT last_daily, balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if last_daily == today:
            return False, balance
        new_balance = balance + amount
        conn.execute(
            "UPDATE users SET balance = ?, last_daily = ? WHERE user_id = ?",
            (new_balance, today, user_id),
        )
        conn.commit()
        return True, new_balance
    finally:
        conn.close()
