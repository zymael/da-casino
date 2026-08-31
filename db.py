import random
import sqlite3
from datetime import date, datetime, timedelta, timezone

DB_PATH = "casino.db"
STARTING_BALANCE = 100
ENERGY_CAP = 40  # energy carries over between rests -- this is the hard ceiling it accumulates to
ENERGY_REST_GAIN = 3  # base energy granted per !rest (added to current energy, capped at ENERGY_CAP)
# Shared cooldown for !rest, !rub, and !train -- a rolling window since each one's own last use,
# not a calendar-day reset (see _seconds_until_refresh). One knob for all three since they're
# meant to stay in lockstep; give a function its own constant instead if one of them ever needs to
# diverge from the others.
REFRESH_HOURS = 12


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
    # users predates last_mine_start/last_mine_claim/last_tip/last_rub/last_energy_item -- add them
    # for installs where CREATE TABLE IF NOT EXISTS above was a no-op against an older schema.
    # last_energy_item is the once-per-calendar-day gate on db.use_energy_item -- same shape as
    # last_tip (a bare date.today().isoformat(), not a full timestamp), shared across every
    # energy-restoring consumable a player owns, not tracked per item_id.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for column in ("last_mine_start", "last_mine_claim", "last_tip", "last_rub", "last_energy_item"):
        if column not in columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
    # Energy: a delve-gating resource (spent 1 per delve, gained ENERGY_REST_GAIN per !rest, capped
    # at ENERGY_CAP -- it carries over unspent, it's not a use-it-or-lose-it refill) -- separate
    # from the last_daily cooldown that gates *when* !rest can be claimed. DEFAULT backfills
    # existing rows on upgrade, same as any other ALTER ADD COLUMN here.
    if "energy" not in columns:
        conn.execute(f"ALTER TABLE users ADD COLUMN energy INTEGER NOT NULL DEFAULT {ENERGY_REST_GAIN}")
    # Luck: a purely cosmetic stat (nothing else in the game reads it) that only !rub touches --
    # permanently bumps the rubber's luck and permanently docks the target's (see apply_rub); no
    # restore-on-rest, stolen luck stays stolen. Every player gets a random starting value rather
    # than a shared baseline, which a single SQL DEFAULT can't express -- ADD COLUMN backfills
    # every existing row to the placeholder 50 first, then this block individually randomizes
    # each of them in Python (the same RNG call _ensure_user uses for brand-new rows below, just
    # run once here for pre-existing ones).
    if "luck" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN luck INTEGER NOT NULL DEFAULT 50")
        for g_id, u_id in conn.execute("SELECT guild_id, user_id FROM users WHERE luck = 50").fetchall():
            conn.execute(
                "UPDATE users SET luck = ? WHERE guild_id = ? AND user_id = ?",
                (random.randint(1, 100), g_id, u_id),
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
    # Discovery-based crafting (crafting.combine): a recipe a player has successfully combined
    # at least once, purely so !craft can show them a "Known Recipes" reference list instead of
    # forcing them to re-guess a combo they already found. Same shape as personal_achievements
    # (each user claims each recipe_id independently) -- presence in this table is the only
    # state; there's nothing else to store per row.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discovered_recipes (
            guild_id INTEGER NOT NULL,
            recipe_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            discovered_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, recipe_id, user_id)
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
    # A player's dungeon RPG character: a permanent one-time choice (main class x subclass),
    # created via create_character()'s INSERT OR IGNORE and never overwritten after that. Stats
    # are snapshotted at creation (dungeon.compute_stats) rather than recomputed live, so a
    # character's power stays stable even if the base numbers get rebalanced later.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS characters (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            main_class TEXT NOT NULL,
            subclass TEXT NOT NULL,
            hp INTEGER NOT NULL,
            atk INTEGER NOT NULL,
            def INTEGER NOT NULL,
            last_delve TEXT,
            level INTEGER NOT NULL DEFAULT 1,
            xp INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    # characters predates leveling -- added non-destructively for the same reason as every other
    # migration in this file (existing characters just start at level 1 / 0 xp, which is already
    # the column default).
    character_columns = {row[1] for row in conn.execute("PRAGMA table_info(characters)")}
    for column in ("level", "xp"):
        if column not in character_columns:
            conn.execute(f"ALTER TABLE characters ADD COLUMN {column} INTEGER NOT NULL DEFAULT {1 if column == 'level' else 0}")
    # current_hp persists a character's HP *across* delves -- unlike `hp` (the permanent max, only
    # ever raised by leveling/equipment), this is what's left after a delve's damage/heals, and it
    # carries into the next delve unhealed except by !rest (claim_rest below) or an in-combat
    # heal/item. Backfilled to each row's full `hp` since that's the only sane value for a
    # character that predates this column -- can't be a literal ALTER DEFAULT since it has to
    # match each row's own hp, not a constant.
    if "current_hp" not in character_columns:
        conn.execute("ALTER TABLE characters ADD COLUMN current_hp INTEGER")
        conn.execute("UPDATE characters SET current_hp = hp WHERE current_hp IS NULL")
    # spatk/spdef (Special Attack/Special Defense) -- a second permanent stat pair, parallel to
    # atk/def, used by skills flagged "special" (dungeon.py's EFFECT_PARAM_SCHEMAS/skill loading)
    # instead of the physical pair. Same "can't be a literal ALTER DEFAULT" story as current_hp:
    # a pre-existing character's only sane starting value is its own atk/def mirrored over, not a
    # constant, since spatk/spdef didn't exist yet when that character was created.
    if "spatk" not in character_columns:
        conn.execute("ALTER TABLE characters ADD COLUMN spatk INTEGER")
        conn.execute("UPDATE characters SET spatk = atk WHERE spatk IS NULL")
    if "spdef" not in character_columns:
        conn.execute("ALTER TABLE characters ADD COLUMN spdef INTEGER")
        conn.execute("UPDATE characters SET spdef = def WHERE spdef IS NULL")
    # speed -- drives dynamic turn order (dungeon.preview_next_turns), a sixth stat with no
    # existing column it naturally mirrors the way spatk/spdef mirror atk/def (there's no
    # "physical speed" this game already tracked), so a pre-existing character backfills to a flat
    # neutral value (10, the healer archetype's own base -- the most average of the four classes)
    # rather than a copied column. Same reasoning as monster/CLASSES speed values elsewhere in this
    # change: a first design pass, not a precise reconstruction.
    if "speed" not in character_columns:
        conn.execute("ALTER TABLE characters ADD COLUMN speed INTEGER")
        conn.execute("UPDATE characters SET speed = 10 WHERE speed IS NULL")
    # A character's currently equipped gear -- one row per filled slot, upserted on equip/replace.
    # No row for a slot means empty, same "absence = default state" idea as ranch_facilities.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS character_equipment (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            slot TEXT NOT NULL,
            item_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, slot)
        )
        """
    )
    # A player's housing grid -- one row per filled slot (0-8), same "absence = empty" idea as
    # character_equipment, but with an integer slot instead of a named one since all 9 grid
    # positions are mechanically identical (display-only).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS house_placements (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            slot INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, slot)
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
    # Same predates-the-table story as currency_name above -- delve_test_mode is 0/absent by
    # default, meaning "obey each delve's own active flag" (see dungeon.active_delves).
    if "delve_test_mode" not in columns:
        conn.execute("ALTER TABLE guild_settings ADD COLUMN delve_test_mode INTEGER NOT NULL DEFAULT 0")
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
            sex TEXT,
            coat TEXT,
            pending_boost_stat TEXT,
            PRIMARY KEY (guild_id, horse_index)
        )
        """
    )
    # horses predates places/shows (Place/Show bets) -- add them non-destructively, since
    # unlike the race_starts drop-and-reseed above, this table now holds real accumulated
    # race history that must survive the migration. Horses with real races from before this
    # existed are simply left at places = shows = 0 here -- no backfill needed, since
    # horserace.current_probabilities() no longer derives odds from this history at all (it's
    # a fresh Monte-Carlo simulation of current stats every call); wins/places/shows/races are
    # purely a career record now, shown in !horses but not read for pricing/payouts.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(horses)")}
    for column in ("places", "shows"):
        if column not in columns:
            conn.execute(f"ALTER TABLE horses ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
    # horses predates sex/coat too -- added non-destructively for the same reason. Left NULL
    # here for any horse that already existed; horserace.get_roster() detects sex IS NULL and
    # backfills it the next time that guild's stable is read (canonical values for legends,
    # random for foals), the same lazy-backfill-on-first-touch idea as places/shows above.
    for column in ("sex", "coat"):
        if column not in columns:
            conn.execute(f"ALTER TABLE horses ADD COLUMN {column} TEXT")
    # horses predates the !ranch training-boost items too -- same non-destructive add. NULL means
    # no boost queued, which is also what a freshly-created row gets by default.
    if "pending_boost_stat" not in columns:
        conn.execute("ALTER TABLE horses ADD COLUMN pending_boost_stat TEXT")
    # A horse's equipped cosmetics, one row per filled slot (saddle/hat) -- keyed by horse_index
    # rather than owner, since the horse (not the owner) is what's dressed up, and ownership can
    # change hands via buy_legend_horse while a cosmetic stays on. Unlike character_equipment,
    # equipping here never consumes anything from `inventory` -- horse clothes are a reusable
    # wardrobe an owner can put on any of their horses, not a single wearable instance.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS horse_clothes_equipped (
            guild_id INTEGER NOT NULL,
            horse_index INTEGER NOT NULL,
            slot TEXT NOT NULL,
            item_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, horse_index, slot)
        )
        """
    )
    # Per-owner, per-guild permanent training facility tier (0 = none). Absence of a row means
    # tier 0, same "no row yet = default state" idea as everything else lazily created on first
    # purchase (e.g. champion_base_nick).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ranch_facilities (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            tier INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    # Generic per-player key -> int store. Quest stages, quest counters, and (soon) NPC/room
    # presence conditions all read/write through here instead of each getting their own
    # purpose-built table -- one condition language ("flag X compared to Y"), one storage
    # mechanism. Absence means 0, same "absence = default state" idea as inventory. A quest's
    # stage lives at key "quest:<id>" (see quests.py's _quest_flag_key) -- stored as stage+1 so 0
    # unambiguously means "not started" rather than colliding with real stage 0; a counted
    # trigger's progress lives at "quest:<id>:stage<N>:count" (quests.py's _stage_counter_key).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flags (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, key)
        )
        """
    )
    # One-time migration: quest_progress/quest_counters folded into the generic flags table above.
    # Guarded by table existence so this only ever runs once per database, same idempotent
    # convention as every other migration block in this function -- a fresh install never creates
    # either old table, so this is a no-op there.
    existing_tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    if "quest_progress" in existing_tables:
        for guild_id, user_id, quest_id, stage in conn.execute(
            "SELECT guild_id, user_id, quest_id, stage FROM quest_progress"
        ).fetchall():
            conn.execute(
                "INSERT INTO flags (guild_id, user_id, key, value) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, user_id, f"quest:{quest_id}", stage + 1),
            )
        conn.execute("DROP TABLE quest_progress")
    if "quest_counters" in existing_tables:
        for guild_id, user_id, quest_id, stage, count in conn.execute(
            "SELECT guild_id, user_id, quest_id, stage, count FROM quest_counters"
        ).fetchall():
            conn.execute(
                "INSERT INTO flags (guild_id, user_id, key, value) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, key) DO UPDATE SET value = excluded.value",
                (guild_id, user_id, f"quest:{quest_id}:stage{stage}:count", count),
            )
        conn.execute("DROP TABLE quest_counters")
    # Generic item bag for stuff that isn't equippable dungeon gear (quest items, keepsakes) --
    # separate from character_equipment, which auto-equips by stat power rather than being held.
    # No row for an item_id means 0 of it, same "absence = default state" idea as ranch_facilities.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, item_id)
        )
        """
    )
    # One-time content-rename migration: the crafting redo (2026-08-21) fully replaced
    # dungeon_materials.json's old id set with a new one. inventory_view.py deliberately raises
    # if it sees an item id it doesn't recognize (a real content-bug tripwire), which broke
    # !inventory for anyone still holding an old material id. Rather than keep the old ids around
    # as inert legacy content, merge whatever a player still holds into the new material closest
    # to it (adding onto any of that new material they already have) and drop the old row.
    # Idempotent and safe to run every startup -- once converted, no old-id rows remain to match,
    # so this is a no-op on every run after the first.
    _LEGACY_MATERIAL_RENAMES = {
        "goblin_ear": "rat_tooth",
        "rusty_scrap": "nail",
        "slime_residue": "droppings",
        "iron_ore": "shiny_rock",
        "wolf_pelt": "dirty_cloth",
        "cursed_bone": "pointy_rock",
        "dragon_scale": "pan",
        "void_crystal": "shiny_rock",
        "ancient_rune": "comb",
    }
    for _old_id, _new_id in _LEGACY_MATERIAL_RENAMES.items():
        for _guild_id, _user_id, _qty in conn.execute(
            "SELECT guild_id, user_id, qty FROM inventory WHERE item_id = ?", (_old_id,)
        ).fetchall():
            conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
                (_guild_id, _user_id, _new_id, _qty),
            )
            conn.execute(
                "DELETE FROM inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                (_guild_id, _user_id, _old_id),
            )
    # Dungeon gear a player has found/been granted but isn't currently wearing -- character_equipment
    # holds at most one item per slot, this holds everything else so a non-upgrade drop is stored
    # instead of silently discarded, and !equipment can let a player swap back to it later.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS equipment_inventory (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, item_id)
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
    # OR IGNORE makes this a no-op for a returning player, so the random luck roll below only
    # ever happens once per (guild_id, user_id) -- the very first time this row is created.
    conn.execute(
        "INSERT OR IGNORE INTO users (guild_id, user_id, balance, luck) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, STARTING_BALANCE, random.randint(1, 100)),
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


def set_balance(guild_id: int, user_id: int, value: int) -> None:
    """Admin-panel direct override -- unlike update_balance (a delta applied by normal gameplay),
    this sets balance to an exact value. Clamped to >= 0."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.execute(
            "UPDATE users SET balance = ? WHERE guild_id = ? AND user_id = ?",
            (max(0, value), guild_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_known_users(guild_id: int) -> list[int]:
    """Every user_id this guild has an economy row for -- the admin panel's player-debug tool uses
    this to populate its player picker, since a user with no row here has nothing to debug anyway."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT user_id FROM users WHERE guild_id = ? ORDER BY user_id", (guild_id,)).fetchall()
        return [row[0] for row in rows]
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


def get_random_active_user(guild_id: int) -> int | None:
    """A random user_id who has actually logged a bet in this guild (bet_log) -- not just anyone
    who's ever touched their balance. get_balance/_ensure_user lazily create a `users` row for
    literally anyone who runs !balance once, so that table alone isn't proof someone's actually
    played. None if nobody in this guild has logged a bet yet."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT DISTINCT user_id FROM bet_log WHERE guild_id = ? ORDER BY RANDOM() LIMIT 1",
            (guild_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_luck_leaderboard(guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
    """Returns up to `limit` (user_id, luck) rows for this guild, luckiest first. Unlike
    get_pizza_leaderboard, no "> 0" floor -- every row has some luck value from the moment it's
    created (see _ensure_user), so filtering it the same way would just mean "everyone", not
    "everyone who's actually done something"."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, luck FROM users WHERE guild_id = ? ORDER BY luck DESC LIMIT ?",
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


def mark_recipe_discovered(guild_id: int, recipe_id: str, user_id: int) -> bool:
    """Records that user_id has successfully combined recipe_id in this guild, if they haven't
    already. Returns whether this call was the one that recorded it -- False if they'd already
    discovered it, which is how crafting.combine() knows to show a "first time" message only
    once."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO discovered_recipes (guild_id, recipe_id, user_id, discovered_at) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, recipe_id, user_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_discovered_recipes(guild_id: int, user_id: int) -> set[str]:
    """Returns every recipe_id this user has successfully combined at least once in this guild."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT recipe_id FROM discovered_recipes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def record_game_outcome(guild_id: int, user_id: int, game: str, net: int, force_win: bool | None = None) -> tuple[int, int]:
    """Increments this user's win or loss count for `game` in this guild based on the sign of
    `net` (net == 0, e.g. a blackjack push, is a caller error -- don't call this for it), unless
    `force_win` is given, in which case it overrides net's sign (duels always have a winner/loser
    even at net == 0, the default wagerless case). Returns (wins, losses) after the update."""
    column = "wins" if (net > 0 if force_win is None else force_win) else "losses"
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


def set_base_nick(guild_id: int, user_id: int, base_nick: str | None) -> str | None:
    """Records `base_nick` (the caller's already badge-stripped nickname) as this user's current
    badge-free nickname in this guild, overwriting whatever was stored before -- unlike the old
    insert-once behavior, this tracks nickname changes made while a badge is held instead of
    reverting to a stale snapshot from the first time they were ever crowned. Returns it back."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO champion_base_nick (guild_id, user_id, base_nick) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET base_nick = excluded.base_nick",
            (guild_id, user_id, base_nick),
        )
        conn.commit()
        return base_nick
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


def get_delve_test_mode(guild_id: int) -> bool:
    """Whether this guild sees every delve regardless of its own "active" flag (see
    dungeon.active_delves) AND pays no energy to start one (see dungeon_view._spend_delve_energy)
    -- lets a test server play WIP delves freely without exposing them, or the free plays, to
    anywhere else. Off (obey each delve's active flag and normal energy cost) unless a guild has
    explicitly turned it on."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT delve_test_mode FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return bool(row[0]) if row else False
    finally:
        conn.close()


def set_delve_test_mode(guild_id: int, enabled: bool):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, delve_test_mode) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET delve_test_mode = excluded.delve_test_mode",
            (guild_id, int(enabled)),
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
            "races, race_starts, last_trained, sex, coat, pending_boost_stat FROM horses WHERE guild_id = ?",
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
                "sex": sex,
                "coat": coat,
                "pending_boost_stat": pending_boost_stat,
            }
            for (
                horse_index, is_foal, name, owner_id, speed, endurance, spirit, age, wins, places, shows,
                races, race_starts, last_trained, sex, coat, pending_boost_stat,
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


def seed_legend(
    guild_id: int, horse_index: int, name: str, speed: float, endurance: float, spirit: float, age: int,
    sex: str, coat: str,
):
    """Creates a legend horse's row the first time it's touched in a guild. A no-op if it
    already exists (never overwrites a rename, training, or ownership already in place)."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO horses "
            "(guild_id, horse_index, is_foal, name, owner_id, speed, endurance, spirit, age, sex, coat) "
            "VALUES (?, ?, 0, ?, NULL, ?, ?, ?, ?, ?, ?)",
            (guild_id, horse_index, name, speed, endurance, spirit, age, sex, coat),
        )
        conn.commit()
    finally:
        conn.close()


def backfill_horse_traits(guild_id: int, horse_index: int, sex: str, coat: str):
    """One-time top-up for a horse that existed before sex/coat tracking did. Guarded by
    sex IS NULL, so it never overwrites a real (already-backfilled or freshly-created) value."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE horses SET sex = ?, coat = ? WHERE guild_id = ? AND horse_index = ? AND sex IS NULL",
            (sex, coat, guild_id, horse_index),
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


def get_facility_tier(guild_id: int, user_id: int) -> int:
    """Returns this owner's current ranch facility tier in this guild, 0 if they've never
    bought one."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT tier FROM ranch_facilities WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def upgrade_facility(guild_id: int, user_id: int, next_tier: int, cost: int, max_tier: int) -> tuple[str, int]:
    """Attempts to buy the next facility tier (must be exactly current_tier + 1 -- tiers can't be
    skipped -- and no higher than max_tier). Returns (status, balance): "ok", "wrong_tier"
    (next_tier isn't current+1 or exceeds max_tier, e.g. the caller's view of the current tier
    was stale), or "broke"."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT tier FROM ranch_facilities WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        current_tier = row[0] if row else 0
        if next_tier != current_tier + 1 or next_tier > max_tier:
            conn.rollback()
            balance = conn.execute(
                "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            ).fetchone()[0]
            return "wrong_tier", balance
        balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        if balance < cost:
            conn.rollback()
            return "broke", balance
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
            (cost, guild_id, user_id),
        )
        conn.execute(
            "INSERT INTO ranch_facilities (guild_id, user_id, tier) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET tier = excluded.tier",
            (guild_id, user_id, next_tier),
        )
        conn.commit()
        new_balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        return "ok", new_balance
    finally:
        conn.close()


def buy_horse_item(guild_id: int, user_id: int, horse_index: int, stat: str, cost: int) -> tuple[str, int]:
    """Buys a training-boost item and immediately queues it on `horse_index`'s next training.
    Returns (status, balance): "ok", "not_owner", "pending" (that horse already has a boost
    queued -- train it first), or "broke"."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner_id, pending_boost_stat FROM horses WHERE guild_id = ? AND horse_index = ?",
            (guild_id, horse_index),
        ).fetchone()
        if row is None or row[0] != user_id:
            conn.rollback()
            balance = conn.execute(
                "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            ).fetchone()[0]
            return "not_owner", balance
        if row[1] is not None:
            conn.rollback()
            balance = conn.execute(
                "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            ).fetchone()[0]
            return "pending", balance
        balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        if balance < cost:
            conn.rollback()
            return "broke", balance
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
            (cost, guild_id, user_id),
        )
        conn.execute(
            "UPDATE horses SET pending_boost_stat = ? WHERE guild_id = ? AND horse_index = ?",
            (stat, guild_id, horse_index),
        )
        conn.commit()
        new_balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        return "ok", new_balance
    finally:
        conn.close()


def get_ranch_horses(guild_id: int, user_id: int) -> list[dict]:
    """Full detail (stats, sex/coat, pending boost) for every horse this user owns in this
    guild, ordered by index -- the !ranch dashboard's listing, richer than get_horses_owned_by's
    compact !stats summary."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT horse_index, is_foal, name, age, speed, endurance, spirit, wins, places, shows, races, "
            "sex, coat, pending_boost_stat FROM horses WHERE guild_id = ? AND owner_id = ? ORDER BY horse_index",
            (guild_id, user_id),
        ).fetchall()
        return [
            {
                "horse_index": horse_index, "is_foal": bool(is_foal), "name": name, "age": age,
                "speed": speed, "endurance": endurance, "spirit": spirit,
                "wins": wins, "places": places, "shows": shows, "races": races,
                "sex": sex, "coat": coat, "pending_boost_stat": pending_boost_stat,
            }
            for (
                horse_index, is_foal, name, age, speed, endurance, spirit, wins, places, shows, races,
                sex, coat, pending_boost_stat,
            ) in rows
        ]
    finally:
        conn.close()


def get_guild_horse_clothes(guild_id: int) -> dict[int, dict[str, str]]:
    """Returns {horse_index: {slot: item_id}} for every horse in this guild with at least one
    cosmetic equipped -- a horse absent from the dict (or a slot absent from its sub-dict) has
    nothing equipped there, same "absence = default state" idea as get_equipped_items."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT horse_index, slot, item_id FROM horse_clothes_equipped WHERE guild_id = ?", (guild_id,)
        ).fetchall()
        by_horse: dict[int, dict[str, str]] = {}
        for horse_index, slot, item_id in rows:
            by_horse.setdefault(horse_index, {})[slot] = item_id
        return by_horse
    finally:
        conn.close()


def equip_horse_clothes(guild_id: int, user_id: int, horse_index: int, slot: str, item_id: str) -> str:
    """Equips item_id in `slot` on horse_index, replacing whatever cosmetic was there -- if
    user_id owns that horse. Returns "ok" or "not_owner"."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT owner_id FROM horses WHERE guild_id = ? AND horse_index = ?", (guild_id, horse_index),
        ).fetchone()
        if row is None or row[0] != user_id:
            return "not_owner"
        conn.execute(
            "INSERT INTO horse_clothes_equipped (guild_id, horse_index, slot, item_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, horse_index, slot) DO UPDATE SET item_id = excluded.item_id",
            (guild_id, horse_index, slot, item_id),
        )
        conn.commit()
        return "ok"
    finally:
        conn.close()


def unequip_horse_clothes(guild_id: int, user_id: int, horse_index: int, slot: str) -> str:
    """Empties `slot` on horse_index -- if user_id owns that horse. Returns "ok" or "not_owner"
    (a no-op "ok" if the slot was already empty)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT owner_id FROM horses WHERE guild_id = ? AND horse_index = ?", (guild_id, horse_index),
        ).fetchone()
        if row is None or row[0] != user_id:
            return "not_owner"
        conn.execute(
            "DELETE FROM horse_clothes_equipped WHERE guild_id = ? AND horse_index = ? AND slot = ?",
            (guild_id, horse_index, slot),
        )
        conn.commit()
        return "ok"
    finally:
        conn.close()


def buy_foal(
    guild_id: int, horse_index: int, user_id: int, name: str, price: int,
    speed: float, endurance: float, spirit: float, sex: str, coat: str,
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
            "INSERT INTO horses (guild_id, horse_index, is_foal, name, owner_id, speed, endurance, spirit, age, sex, coat) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?, 0, ?, ?)",
            (guild_id, horse_index, name, user_id, speed, endurance, spirit, sex, coat),
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
    """Trains a horse if user_id owns it and REFRESH_HOURS have passed since it was last trained.
    Returns (status, payload): "ok" with (new_speed, new_endurance, new_spirit, new_age),
    "not_owner", or ("cooldown", seconds_remaining)."""
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
        remaining = _seconds_until_refresh(last_trained)
        if remaining is not None:
            return "cooldown", remaining
        now = datetime.now().isoformat()
        new_speed = min(stat_cap, speed + speed_gain)
        new_endurance = min(stat_cap, endurance + endurance_gain)
        new_spirit = min(stat_cap, spirit + spirit_gain)
        new_age = age + 1
        # Any queued !boost item is consumed here regardless of whether the caller actually
        # folded its bonus into the gains passed in -- by the time train_horse runs, that
        # decision has already been made, so this just clears the flag either way.
        conn.execute(
            "UPDATE horses SET speed = ?, endurance = ?, spirit = ?, age = ?, last_trained = ?, "
            "pending_boost_stat = NULL WHERE guild_id = ? AND horse_index = ?",
            (new_speed, new_endurance, new_spirit, new_age, now, guild_id, horse_index),
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


def _seconds_until_refresh(last_timestamp: str | None) -> float | None:
    """REFRESH_HOURS-gated cooldown check for !rest/!rub/!train -- a rolling window since
    `last_timestamp` (a datetime.now().isoformat() string) rather than a calendar-day reset like
    _seconds_until_next_day above. Returns None once the window has passed (or it's never been
    used at all), otherwise the seconds still remaining. datetime.fromisoformat also accepts a
    bare date.today().isoformat() string, so rows written before this cooldown existed (when it
    was calendar-day-only) still parse fine, as that day's midnight."""
    if last_timestamp is None:
        return None
    elapsed = (datetime.now() - datetime.fromisoformat(last_timestamp)).total_seconds()
    remaining = REFRESH_HOURS * 3600 - elapsed
    return remaining if remaining > 0 else None


def claim_rest(
    guild_id: int, user_id: int, gold_amount: int, energy_bonus: int = 0, gold_bonus: int = 0,
    max_hp: int | None = None,
) -> tuple[str, float | int] | tuple[str, int, int]:
    """Grants `gold_amount` (+ `gold_bonus`) credits, adds ENERGY_REST_GAIN + `energy_bonus` energy
    (capped at ENERGY_CAP -- unspent energy carries over, this is not a use-it-or-lose-it refill),
    and (if this user has a dungeon character) heals it to full -- once every REFRESH_HOURS (still
    gated by last_daily -- renamed from claim_daily now that resting does multiple duties, the
    column itself wasn't worth an ALTER just for the name; it stores a full timestamp now rather
    than a bare date, despite the name). Healing here is deliberate: current_hp otherwise only
    rises from an in-combat heal skill/item, never automatically between delves -- see
    set_current_hp. `energy_bonus`/`gold_bonus` default to 0 -- the caller (bot.py's rest_cmd) is
    expected to look up any housing rest-bonus items and pass them in, the same way it already
    computes gold_amount itself before calling this. `max_hp` is likewise caller-computed (
    dungeon.compute_effective_stats, same "db.py doesn't own game-content formulas" split
    add_xp/set_character_progress already use) -- the row's own `hp` column excludes equipment/
    housing HP bonuses, so healing to it would shortchange a geared-up character. None (a user with
    no dungeon character, nothing to compute) falls back to the raw `hp` column, a no-op either way
    since the UPDATE simply matches no rows.

    Returns (status, value) or (status, value, new_energy):
      - ("cooldown", seconds_remaining) — still within REFRESH_HOURS of the last rest
      - ("claimed", new_balance, new_energy) — credits granted, energy gained, character healed
    """
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        last_daily, balance = conn.execute(
            "SELECT last_daily, balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        remaining = _seconds_until_refresh(last_daily)
        if remaining is not None:
            return "cooldown", remaining
        new_balance = balance + gold_amount + gold_bonus
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE users SET balance = ?, last_daily = ?, energy = MIN(?, energy + ?) "
            "WHERE guild_id = ? AND user_id = ?",
            (new_balance, now, ENERGY_CAP, ENERGY_REST_GAIN + energy_bonus, guild_id, user_id),
        )
        conn.execute(
            "UPDATE characters SET current_hp = COALESCE(?, hp) WHERE guild_id = ? AND user_id = ?",
            (max_hp, guild_id, user_id),
        )
        new_energy = conn.execute(
            "SELECT energy FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        conn.commit()
        return "claimed", new_balance, new_energy
    finally:
        conn.close()


def get_luck(guild_id: int, user_id: int) -> int:
    """See the "luck" column comment in init_db() for what touches this. Purely cosmetic;
    nothing reads this to affect any actual game odds."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        row = conn.execute(
            "SELECT luck FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def apply_rub(
    guild_id: int, author_id: int, target_id: int, author_gain: int, target_penalty: int
) -> tuple[str, tuple[int, int] | float]:
    """!rub's REFRESH_HOURS-gated effect: the author's luck permanently increases by
    `author_gain`, the target's luck permanently drops by `target_penalty` -- stolen luck stays
    stolen, no restore-on-rest. If author_id == target_id (rubbing yourself into your own bad
    luck, if the random draw lands that way), both updates just apply to the same row.

    Returns (status, value):
      - ("cooldown", seconds_remaining) -- still within REFRESH_HOURS of the author's last rub
      - ("ok", (author_luck, target_luck)) -- both players' luck, post-update
    """
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, author_id)
        _ensure_user(conn, guild_id, target_id)
        conn.commit()
        (last_rub,) = conn.execute(
            "SELECT last_rub FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, author_id)
        ).fetchone()
        remaining = _seconds_until_refresh(last_rub)
        if remaining is not None:
            return "cooldown", remaining

        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE users SET luck = luck + ?, last_rub = ? WHERE guild_id = ? AND user_id = ?",
            (author_gain, now, guild_id, author_id),
        )
        conn.execute(
            "UPDATE users SET luck = luck - ? WHERE guild_id = ? AND user_id = ?",
            (target_penalty, guild_id, target_id),
        )
        conn.commit()
        author_luck = conn.execute(
            "SELECT luck FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, author_id)
        ).fetchone()[0]
        target_luck = conn.execute(
            "SELECT luck FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, target_id)
        ).fetchone()[0]
        return "ok", (author_luck, target_luck)
    finally:
        conn.close()


def get_energy(guild_id: int, user_id: int) -> int:
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        row = conn.execute(
            "SELECT energy FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def spend_energy(guild_id: int, user_id: int, amount: int = 1) -> bool:
    """Spends `amount` energy (e.g. for a delve) if the player has enough. Returns whether it
    succeeded -- False leaves their energy untouched."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT energy FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        energy = row[0]
        if energy < amount:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE users SET energy = ? WHERE guild_id = ? AND user_id = ?",
            (energy - amount, guild_id, user_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def set_energy(guild_id: int, user_id: int, value: int) -> None:
    """Admin-panel direct override -- unlike spend_energy (gated, delta-based, for normal
    gameplay), this sets energy to an exact value. Clamped to [0, ENERGY_CAP]."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.execute(
            "UPDATE users SET energy = ? WHERE guild_id = ? AND user_id = ?",
            (max(0, min(ENERGY_CAP, value)), guild_id, user_id),
        )
        conn.commit()
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
    sender -- same once-a-day gating as claim_rest.

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


def create_character(
    guild_id: int, user_id: int, main_class: str, subclass: str,
    hp: int, atk: int, def_: int, spatk: int, spdef: int, speed: int,
) -> bool:
    """Creates this user's dungeon character if they don't already have one -- permanent and
    never overwritten once chosen, same idempotent INSERT OR IGNORE pattern as
    award_first_achievement/seed_legend. Returns whether this call was the one that created it."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO characters "
            "(guild_id, user_id, main_class, subclass, hp, atk, def, spatk, spdef, speed, current_hp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, main_class, subclass, hp, atk, def_, spatk, spdef, speed, hp),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_character(guild_id: int, user_id: int) -> dict | None:
    """Returns this user's dungeon character, or None if they haven't picked one yet. hp/atk/def/
    spatk/spdef/speed already include all permanent level growth (see add_xp) -- equipment bonuses
    are separate, see get_equipped_items/dungeon.compute_effective_stats. current_hp is what a
    fresh delve starts at (see set_current_hp) -- everything from this row except that one field is
    a permanent stat, never healed."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT main_class, subclass, hp, atk, def, spatk, spdef, speed, last_delve, level, xp, current_hp "
            "FROM characters WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if row is None:
            return None
        main_class, subclass, hp, atk, def_, spatk, spdef, speed, last_delve, level, xp, current_hp = row
        return {
            "main_class": main_class, "subclass": subclass,
            "hp": hp, "atk": atk, "def": def_, "spatk": spatk, "spdef": spdef, "speed": speed,
            "last_delve": last_delve, "level": level, "xp": xp, "current_hp": current_hp,
        }
    finally:
        conn.close()


def choose_subclass(
    guild_id: int, user_id: int, subclass: str,
    hp_delta: int, atk_delta: int, def_delta: int, spatk_delta: int, spdef_delta: int, speed_delta: int,
) -> bool:
    """Sets a character's subclass for the first time, applying the subclass's flat stat modifiers
    onto their already-leveled stats (same in-place-delta pattern as add_xp) since compute_stats
    only ever applied a subclass modifier once, at creation -- a base-class character (dungeon.
    NO_SUBCLASS) was created with a zero modifier instead. current_hp is bumped by hp_delta too,
    same as a level-up. Only succeeds while the stored subclass is still dungeon.NO_SUBCLASS --
    picking a subclass is permanent, same one-shot guard shape as create_character's INSERT OR
    IGNORE. Returns whether this call was the one that set it."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT subclass, hp, atk, def, spatk, spdef, speed, current_hp "
            "FROM characters WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if row is None or row[0] != "none":
            conn.rollback()
            return False
        _, hp, atk, def_, spatk, spdef, speed, current_hp = row
        conn.execute(
            "UPDATE characters SET subclass = ?, hp = ?, atk = ?, def = ?, spatk = ?, spdef = ?, speed = ?, "
            "current_hp = ? WHERE guild_id = ? AND user_id = ?",
            (
                subclass, hp + hp_delta, atk + atk_delta, def_ + def_delta, spatk + spatk_delta,
                spdef + spdef_delta, speed + speed_delta, current_hp + hp_delta, guild_id, user_id,
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def set_current_hp(guild_id: int, user_id: int, hp: int) -> None:
    """Persists a character's HP at the end of a delve (retreat, victory, death, or a party wipe)
    -- the value the *next* delve will start from, since HP no longer auto-refills between delves,
    only !rest (claim_rest) or an in-combat heal/item raises it. A no-op if this user has no
    character (defensive; every real caller already has one, but costs nothing to be safe)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE characters SET current_hp = ? WHERE guild_id = ? AND user_id = ?",
            (max(0, hp), guild_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_character_progress(
    guild_id: int, user_id: int, level: int, xp: int, current_hp: int,
    hp: int, atk: int, def_: int, spatk: int, spdef: int, speed: int,
) -> None:
    """Admin-panel direct override for a character's level/xp/current_hp AND permanent stats --
    hp/atk/def/spatk/spdef/speed are caller-supplied (dungeon.compute_stats_at_level, keyed off
    the level being set here) rather than derived in this function, same "db.py doesn't own
    game-content formulas" split add_xp already uses. Callers always pass stats matching the level
    they're setting, so an admin changing Level on the Player Debug page keeps attributes
    consistent with it automatically rather than leaving them stale. A no-op if this user has no
    character."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE characters SET level = ?, xp = ?, current_hp = ?, hp = ?, atk = ?, def = ?, "
            "spatk = ?, spdef = ?, speed = ? WHERE guild_id = ? AND user_id = ?",
            (
                max(1, level), max(0, xp), max(0, current_hp), hp, atk, def_, spatk, spdef, speed,
                guild_id, user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def add_xp(
    guild_id: int, user_id: int, xp_gain: int,
    hp_gain: int, atk_gain: int, def_gain: int, spatk_gain: int, spdef_gain: int, speed_gain: int,
    xp_per_level: int,
) -> dict:
    """Awards xp_gain, then loops applying level-ups (mutating the character's stored hp/atk/def/
    spatk/spdef/speed in place, same idea as train_horse growing a horse's stats) for as long as
    the accumulated xp clears the next threshold -- so one big award can cross several levels in
    one call, same inclusive-tiers idea used elsewhere in this codebase (e.g. achievement tiers).
    `hp_gain`/etc and `xp_per_level` are both caller-supplied (dungeon.CLASSES' per-class growth
    fields and dungeon.LEVELING's shared pacing number, respectively) rather than looked up here --
    db.py doesn't import game-content modules (dungeon.py already imports db.py; importing back
    would be circular), same reasoning as horserace.py owning its own constants that db.py's
    callers pass in.

    Returns {new_level, levels_gained, new_hp, new_atk, new_def, new_spatk, new_spdef, new_speed,
    new_xp} so the caller can apply the same deltas to a live delve session immediately rather than
    waiting for the next one."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        level, xp, hp, atk, def_, spatk, spdef, speed = conn.execute(
            "SELECT level, xp, hp, atk, def, spatk, spdef, speed FROM characters WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        xp += xp_gain
        levels_gained = 0
        while xp >= xp_per_level * level:
            xp -= xp_per_level * level
            level += 1
            levels_gained += 1
            hp += hp_gain
            atk += atk_gain
            def_ += def_gain
            spatk += spatk_gain
            spdef += spdef_gain
            speed += speed_gain
        conn.execute(
            "UPDATE characters SET level = ?, xp = ?, hp = ?, atk = ?, def = ?, spatk = ?, spdef = ?, speed = ? "
            "WHERE guild_id = ? AND user_id = ?",
            (level, xp, hp, atk, def_, spatk, spdef, speed, guild_id, user_id),
        )
        conn.commit()
        return {
            "new_level": level, "levels_gained": levels_gained, "new_xp": xp,
            "new_hp": hp, "new_atk": atk, "new_def": def_, "new_spatk": spatk, "new_spdef": spdef,
            "new_speed": speed,
        }
    finally:
        conn.close()


def get_equipped_items(guild_id: int, user_id: int) -> dict[str, str]:
    """Returns {slot: item_id} for whatever this character currently has equipped -- a slot
    with nothing equipped is simply absent from the dict."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT slot, item_id FROM character_equipment WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchall()
        return {slot: item_id for slot, item_id in rows}
    finally:
        conn.close()


def equip_item(guild_id: int, user_id: int, slot: str, item_id: str):
    """Unconditionally equips item_id in `slot`, replacing whatever was there. Pure storage --
    the decision of *whether* this item is worth equipping (empty slot, or better than what's
    already there) is made by the caller (dungeon_view.py), which has the item stat data via
    dungeon.EQUIPMENT; this function doesn't need to know anything about equipment content.
    Doesn't touch equipment_inventory -- see equip_item_smart for the version that does."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO character_equipment (guild_id, user_id, slot, item_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, slot) DO UPDATE SET item_id = excluded.item_id",
            (guild_id, user_id, slot, item_id),
        )
        conn.commit()
    finally:
        conn.close()


def _add_equipment_inventory(conn, guild_id: int, user_id: int, item_id: str, qty: int = 1):
    """Internal helper sharing one already-open connection/transaction -- see store_equipment_item
    for the standalone public version."""
    conn.execute(
        "INSERT INTO equipment_inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
        (guild_id, user_id, item_id, qty),
    )


def _remove_equipment_inventory(conn, guild_id: int, user_id: int, item_id: str, qty: int = 1) -> bool:
    """Internal helper sharing one already-open connection/transaction. Returns whether enough
    was held -- False leaves the row untouched."""
    row = conn.execute(
        "SELECT qty FROM equipment_inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
        (guild_id, user_id, item_id),
    ).fetchone()
    current = row[0] if row else 0
    if current < qty:
        return False
    if current == qty:
        conn.execute(
            "DELETE FROM equipment_inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
            (guild_id, user_id, item_id),
        )
    else:
        conn.execute(
            "UPDATE equipment_inventory SET qty = qty - ? WHERE guild_id = ? AND user_id = ? AND item_id = ?",
            (qty, guild_id, user_id, item_id),
        )
    return True


def store_equipment_item(guild_id: int, user_id: int, item_id: str, qty: int = 1):
    """Adds qty of item_id to equipment_inventory without equipping it -- used when a found/
    rewarded item isn't worth equipping over what's already in that slot, so it's kept (swappable
    later via !equipment) rather than discarded."""
    conn = _connect()
    try:
        _add_equipment_inventory(conn, guild_id, user_id, item_id, qty)
        conn.commit()
    finally:
        conn.close()


def sell_equipment_item(guild_id: int, user_id: int, item_id: str, qty: int = 1) -> bool:
    """Removes qty of item_id from equipment_inventory (never from character_equipment -- an
    equipped item was never in this table to begin with, see _stored_excluding_equipped, so this
    can never accidentally sell something worn). Returns whether enough was held -- False leaves
    storage untouched. Standalone public version of _remove_equipment_inventory, for sell.py; the
    currency credit is a separate call (db.update_balance), same "compose separately-atomic calls"
    shape shop.py/quests.turn_in already use rather than one all-encompassing transaction."""
    conn = _connect()
    try:
        removed = _remove_equipment_inventory(conn, guild_id, user_id, item_id, qty)
        conn.commit()
        return removed
    finally:
        conn.close()


def get_equipment_inventory(guild_id: int, user_id: int) -> dict[str, int]:
    """Returns {item_id: qty} for gear this character has found/been granted but isn't currently
    wearing -- an item not in the dict means 0, same "absence = default state" idea as
    get_equipped_items."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT item_id, qty FROM equipment_inventory WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchall()
        return {item_id: qty for item_id, qty in rows}
    finally:
        conn.close()


def equip_item_smart(guild_id: int, user_id: int, slot: str, item_id: str) -> str | None:
    """Equips item_id into `slot` (from a fresh find, a quest reward, or swapping in something
    from equipment_inventory), moving whatever was previously in that slot into
    equipment_inventory instead of overwriting it into oblivion, and removing item_id from
    equipment_inventory if it was stored there (it's worn now, not held). Returns the
    previously-equipped item_id that got bumped into inventory, or None if the slot was empty."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT item_id FROM character_equipment WHERE guild_id = ? AND user_id = ? AND slot = ?",
            (guild_id, user_id, slot),
        ).fetchone()
        previous = row[0] if row else None
        conn.execute(
            "INSERT INTO character_equipment (guild_id, user_id, slot, item_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, slot) DO UPDATE SET item_id = excluded.item_id",
            (guild_id, user_id, slot, item_id),
        )
        if previous and previous != item_id:
            _add_equipment_inventory(conn, guild_id, user_id, previous)
        _remove_equipment_inventory(conn, guild_id, user_id, item_id)
        conn.commit()
        return previous
    finally:
        conn.close()


def unequip_item(guild_id: int, user_id: int, slot: str) -> str | None:
    """Empties `slot` entirely, moving whatever was equipped there into equipment_inventory.
    Returns the item_id that was removed, or None if the slot was already empty."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT item_id FROM character_equipment WHERE guild_id = ? AND user_id = ? AND slot = ?",
            (guild_id, user_id, slot),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        item_id = row[0]
        conn.execute(
            "DELETE FROM character_equipment WHERE guild_id = ? AND user_id = ? AND slot = ?",
            (guild_id, user_id, slot),
        )
        _add_equipment_inventory(conn, guild_id, user_id, item_id)
        conn.commit()
        return item_id
    finally:
        conn.close()


def get_house_placements(guild_id: int, user_id: int) -> dict[int, str]:
    """Returns {slot: item_id} for whatever's currently placed in this player's house -- a slot
    with nothing placed is simply absent from the dict, same "absence = default state" idea as
    get_equipped_items."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT slot, item_id FROM house_placements WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchall()
        return {slot: item_id for slot, item_id in rows}
    finally:
        conn.close()


def place_house_item(guild_id: int, user_id: int, slot: int, item_id: str) -> str:
    """Places item_id into `slot`, moving whatever was previously there back into inventory (same
    swap behavior as equip_item_smart) and removing one item_id from inventory (it's on display
    now, not held). Returns "placed" on success, "no_item" (untouched) if the player doesn't hold
    a free copy of item_id, or "duplicate" (untouched) if item_id is already placed in a different
    slot -- housing items are one-per-house, not one-per-slot, so their passive effect (see
    housing.get_house_bonuses) can't be stacked by placing several copies."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT qty FROM inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
            (guild_id, user_id, item_id),
        ).fetchone()
        if not row or row[0] < 1:
            conn.rollback()
            return "no_item"
        duplicate = conn.execute(
            "SELECT 1 FROM house_placements WHERE guild_id = ? AND user_id = ? AND item_id = ? AND slot != ?",
            (guild_id, user_id, item_id, slot),
        ).fetchone()
        if duplicate:
            conn.rollback()
            return "duplicate"
        if row[0] == 1:
            conn.execute(
                "DELETE FROM inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                (guild_id, user_id, item_id),
            )
        else:
            conn.execute(
                "UPDATE inventory SET qty = qty - 1 WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                (guild_id, user_id, item_id),
            )
        previous = conn.execute(
            "SELECT item_id FROM house_placements WHERE guild_id = ? AND user_id = ? AND slot = ?",
            (guild_id, user_id, slot),
        ).fetchone()
        if previous:
            conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + 1",
                (guild_id, user_id, previous[0]),
            )
        conn.execute(
            "INSERT INTO house_placements (guild_id, user_id, slot, item_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, slot) DO UPDATE SET item_id = excluded.item_id",
            (guild_id, user_id, slot, item_id),
        )
        conn.commit()
        return "placed"
    finally:
        conn.close()


def remove_house_item(guild_id: int, user_id: int, slot: int) -> str | None:
    """Empties `slot` entirely, moving whatever was placed there back into inventory. Returns the
    item_id that was removed, or None if the slot was already empty."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT item_id FROM house_placements WHERE guild_id = ? AND user_id = ? AND slot = ?",
            (guild_id, user_id, slot),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        item_id = row[0]
        conn.execute(
            "DELETE FROM house_placements WHERE guild_id = ? AND user_id = ? AND slot = ?",
            (guild_id, user_id, slot),
        )
        conn.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + 1",
            (guild_id, user_id, item_id),
        )
        conn.commit()
        return item_id
    finally:
        conn.close()


def get_flag(guild_id: int, user_id: int, key: str) -> int:
    """Returns this user's value for `key`, or 0 if no row (nothing set yet), same "absence =
    default state" idea as get_inventory. The generic per-player state primitive -- quest stages,
    quest counters, and NPC/room presence conditions all read through here."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM flags WHERE guild_id = ? AND user_id = ? AND key = ?", (guild_id, user_id, key),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def get_distinct_flag_keys() -> list[str]:
    """Every distinct flag key currently in use, across every guild/user -- not itself a gameplay
    function, just a discoverability aid for admin_server.py's "flag_at_least" trigger editor
    (a raw text field otherwise has zero hints as to what keys already mean something)."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT DISTINCT key FROM flags ORDER BY key").fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def set_flag(guild_id: int, user_id: int, key: str, value: int):
    """Unconditionally sets `key` to `value`, creating the row if it's the first write."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO flags (guild_id, user_id, key, value) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, user_id, key, value),
        )
        conn.commit()
    finally:
        conn.close()


def set_flag_if_zero(guild_id: int, user_id: int, key: str, value: int) -> bool:
    """Sets `key` to `value` only if it's currently unset/0. Returns whether this call was the one
    that set it -- idempotent, same INSERT OR IGNORE shape as award_personal_achievement, so a
    quest's start trigger can fire repeatedly (e.g. an achievement re-checked on every claim
    attempt) without restarting progress."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO flags (guild_id, user_id, key, value) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, key) DO UPDATE SET value = excluded.value WHERE flags.value = 0",
            (guild_id, user_id, key, value),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def compare_and_set_flag(guild_id: int, user_id: int, key: str, expected: int, new: int) -> bool:
    """Sets `key` to `new` only if its current value is `expected`. Returns whether it actually
    moved -- False if the stored value no longer matches `expected` (e.g. a stale view double
    button-clicked), same stale-state guard as upgrade_facility's wrong_tier check. Unlike
    set_flag_if_zero, this works whether the row already exists at `expected` or is absent and
    `expected` is 0 (the INSERT branch handles the absent case, the UPDATE's WHERE handles the
    existing-row case)."""
    conn = _connect()
    try:
        if expected == 0:
            cursor = conn.execute(
                "INSERT INTO flags (guild_id, user_id, key, value) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, key) DO UPDATE SET value = excluded.value WHERE flags.value = 0",
                (guild_id, user_id, key, new),
            )
        else:
            cursor = conn.execute(
                "UPDATE flags SET value = ? WHERE guild_id = ? AND user_id = ? AND key = ? AND value = ?",
                (new, guild_id, user_id, key, expected),
            )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def increment_flag(guild_id: int, user_id: int, key: str, by: int = 1) -> int:
    """Bumps `key` by `by` (row auto-creates at `by` on first call, same upsert shape as
    add_inventory_item) and returns the new value."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO flags (guild_id, user_id, key, value) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, key) DO UPDATE SET value = value + excluded.value",
            (guild_id, user_id, key, by),
        )
        conn.commit()
        row = conn.execute(
            "SELECT value FROM flags WHERE guild_id = ? AND user_id = ? AND key = ?", (guild_id, user_id, key),
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def add_inventory_item(guild_id: int, user_id: int, item_id: str, qty: int = 1):
    """Adds qty of item_id to this user's inventory, creating the row if it's their first one."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
            (guild_id, user_id, item_id, qty),
        )
        conn.commit()
    finally:
        conn.close()


def get_inventory(guild_id: int, user_id: int) -> dict[str, int]:
    """Returns {item_id: qty} for everything this user is holding -- an item not in the dict
    means 0, same "absence = default state" idea as get_equipped_items."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT item_id, qty FROM inventory WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchall()
        return {item_id: qty for item_id, qty in rows}
    finally:
        conn.close()


def consume_inventory_item(guild_id: int, user_id: int, item_id: str, qty: int = 1) -> bool:
    """Removes qty of item_id from this user's inventory if they're holding at least that many.
    Returns whether it succeeded -- False leaves their inventory untouched. Deletes the row
    entirely once it hits 0 rather than leaving a 0-qty row around."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT qty FROM inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
            (guild_id, user_id, item_id),
        ).fetchone()
        current = row[0] if row else 0
        if current < qty:
            conn.rollback()
            return False
        if current == qty:
            conn.execute(
                "DELETE FROM inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                (guild_id, user_id, item_id),
            )
        else:
            conn.execute(
                "UPDATE inventory SET qty = qty - ? WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                (qty, guild_id, user_id, item_id),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def use_energy_item(guild_id: int, user_id: int, item_id: str, energy_amount: int) -> tuple[str, int | float]:
    """Consumes one of item_id (a dungeon.CONSUMABLES entry with an energy_restore effect --
    dungeon.usable_outside_combat/inventory_view.py's UseConsumableButton is what a player actually
    reaches this through) for energy_amount energy, capped at ENERGY_CAP -- once per calendar day,
    shared across every energy-restoring item a player owns, not tracked per item_id. Same
    once-a-day gate shape as tip's last_tip (a bare date, not a timestamp), a separate column and
    separate cooldown from !rest's own last_daily.

    Returns (status, value):
      - ("cooldown", seconds_remaining) -- already used an energy item today, item NOT consumed
      - ("no_item", 0) -- doesn't actually have one anymore (lost a race, e.g. a double-click)
      - ("used", new_energy) -- item consumed, energy granted
    """
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        today = date.today().isoformat()
        (last_energy_item,) = conn.execute(
            "SELECT last_energy_item FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
    finally:
        conn.close()
    if last_energy_item == today:
        return "cooldown", _seconds_until_next_day()

    if not consume_inventory_item(guild_id, user_id, item_id):
        return "no_item", 0

    conn = _connect()
    try:
        conn.execute(
            "UPDATE users SET last_energy_item = ?, energy = MIN(?, energy + ?) WHERE guild_id = ? AND user_id = ?",
            (today, ENERGY_CAP, energy_amount, guild_id, user_id),
        )
        conn.commit()
        new_energy = conn.execute(
            "SELECT energy FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        return "used", new_energy
    finally:
        conn.close()


def use_healing_item(guild_id: int, user_id: int, item_id: str, heal_fraction: float) -> tuple[str, int]:
    """Consumes one of item_id (a dungeon.CONSUMABLES entry with a heal_fraction effect, used
    outside combat via inventory_view.py's UseConsumableButton) for a heal_fraction-of-max-HP heal
    to current_hp, capped at max. No daily gate -- healing isn't the scarce economy resource energy
    is, so this is just a normal inventory consumption, same as it already is in combat.

    Returns (status, value):
      - ("no_character", 0) -- nothing to heal
      - ("full", current_hp) -- already at max HP, item NOT consumed
      - ("no_item", 0) -- doesn't actually have one anymore (lost a race)
      - ("used", new_current_hp) -- item consumed, healed
    """
    character = get_character(guild_id, user_id)
    if character is None:
        return "no_character", 0
    if character["current_hp"] >= character["hp"]:
        return "full", character["current_hp"]
    if not consume_inventory_item(guild_id, user_id, item_id):
        return "no_item", 0
    new_hp = min(character["hp"], character["current_hp"] + round(character["hp"] * heal_fraction))
    set_current_hp(guild_id, user_id, new_hp)
    return "used", new_hp


def spend_currency(guild_id: int, user_id: int, cost: int) -> tuple[str, int]:
    """Atomically checks and deducts `cost` from this user's balance -- the currency-only half of
    craft_item's check-then-consume shape, for callers (shop.buy) that have no materials to check
    alongside it. Returns (status, balance): "ok" (deducted, new balance) or "broke" (untouched,
    current balance)."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        if balance < cost:
            conn.rollback()
            return "broke", balance
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
            (cost, guild_id, user_id),
        )
        conn.commit()
        new_balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        return "ok", new_balance
    finally:
        conn.close()


def craft_item(guild_id: int, user_id: int, materials: dict[str, int], currency_cost: int) -> tuple[str, int]:
    """Atomically checks and consumes every required material qty plus currency_cost for a
    crafting recipe -- either all of it succeeds or none of it does. Returns (status, balance):
    "ok", "insufficient_materials" (rolled back, nothing touched), or "broke". Modeled on
    upgrade_facility/buy_horse_item's validate-with-rollback shape, and inlines the same
    check-then-decrement-or-delete logic as consume_inventory_item (rather than calling it) since
    that would need its own nested BEGIN IMMEDIATE, unsafe on a connection with one already open.

    Deliberately covers materials/currency consumption only, not the resulting item grant --
    crafting.craft composes this with a separate equip_item_smart/store_equipment_item/
    add_inventory_item call afterward, the same way quests.turn_in already composes
    consume_inventory_item with its own separately-atomic reward step rather than one
    all-encompassing transaction."""
    conn = _connect()
    try:
        _ensure_user(conn, guild_id, user_id)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for item_id, qty in materials.items():
            row = conn.execute(
                "SELECT qty FROM inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                (guild_id, user_id, item_id),
            ).fetchone()
            if (row[0] if row else 0) < qty:
                conn.rollback()
                balance = conn.execute(
                    "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
                ).fetchone()[0]
                return "insufficient_materials", balance

        balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        if balance < currency_cost:
            conn.rollback()
            return "broke", balance

        for item_id, qty in materials.items():
            current = conn.execute(
                "SELECT qty FROM inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                (guild_id, user_id, item_id),
            ).fetchone()[0]
            if current == qty:
                conn.execute(
                    "DELETE FROM inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                    (guild_id, user_id, item_id),
                )
            else:
                conn.execute(
                    "UPDATE inventory SET qty = qty - ? WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                    (qty, guild_id, user_id, item_id),
                )
        if currency_cost:
            conn.execute(
                "UPDATE users SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
                (currency_cost, guild_id, user_id),
            )
        conn.commit()
        new_balance = conn.execute(
            "SELECT balance FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]
        return "ok", new_balance
    finally:
        conn.close()


