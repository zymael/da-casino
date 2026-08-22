import db

# Guild-wide progressive jackpots for slots and video poker. State lives in db's generic
# per-player flags table under a sentinel user_id (0 -- no real Discord user ever has this id),
# same "it's just a new flag key" idea flags.py's own docstring points at, so this needed no new
# table. One pot per game key; every bet feeds it a slice, and landing the top-tier hand claims
# the whole pot and resets it back to the seed.
_POT_USER = 0

SEED = 25000
CONTRIBUTION_RATE = 0.05  # fraction of every bet that feeds the pot


def _key(game: str) -> str:
    return f"jackpot:{game}"


def get_pot(guild_id: int, game: str) -> int:
    value = db.get_flag(guild_id, _POT_USER, _key(game))
    return value if value else SEED


def contribute(guild_id: int, game: str, bet: int) -> int:
    """Feeds a slice of `bet` into the pot (seeding it first on its very first touch). Returns
    the pot's new size."""
    key = _key(game)
    if db.get_flag(guild_id, _POT_USER, key) == 0:
        db.set_flag(guild_id, _POT_USER, key, SEED)
    added = max(1, int(bet * CONTRIBUTION_RATE))
    return db.increment_flag(guild_id, _POT_USER, key, added)


def claim(guild_id: int, game: str) -> int:
    """Pays out the pot and resets it to the seed value. Returns the amount won."""
    won = get_pot(guild_id, game)
    db.set_flag(guild_id, _POT_USER, _key(game), SEED)
    return won
