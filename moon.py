from datetime import datetime, timezone

# The moon's actual phase, computed offline from the date -- no API needed, since phase is a
# deterministic function of time (a synodic-month calculation), and this sits in a code path that
# affects real betting odds where a network dependency would be an unacceptable failure point.
SYNODIC_MONTH_DAYS = 29.530588861
# 2000-01-06 18:14 UTC is a well-known reference new moon; any correctly-dated new moon works as
# the epoch, since we only ever use it modulo the synodic month.
_REFERENCE_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

# (key, emoji, label, favored_game, direction). Deliberately *not* a uniform "full moon = lucky"
# axis -- each game gets exactly one player-favor night and one house-favor night per ~29.5-day
# cycle, with the two Dungeon nights bookending the Full Moon (its natural theme) as the
# strongest swing. Only the emoji/label half of this are ever shown to players (see bot.py's
# rest_cmd) -- favored_game/direction are never displayed or documented anywhere in-bot.
PHASES = [
    ("new_moon", "🌑", "New Moon", "roulette", "house"),
    ("waxing_crescent", "🌒", "Waxing Crescent", "slots", "house"),
    ("first_quarter", "🌓", "First Quarter", "dungeon", "house"),
    ("waxing_gibbous", "🌔", "Waxing Gibbous", "blackjack", "player"),
    ("full_moon", "🌕", "Full Moon", "dungeon", "player"),
    ("waning_gibbous", "🌖", "Waning Gibbous", "slots", "player"),
    ("last_quarter", "🌗", "Last Quarter", "roulette", "player"),
    ("waning_crescent", "🌘", "Waning Crescent", "blackjack", "house"),
]


def current_phase() -> tuple[str, str, str, str, str]:
    """(key, emoji, label, favored_game, direction) for right now."""
    age_days = (datetime.now(timezone.utc) - _REFERENCE_NEW_MOON).total_seconds() / 86400
    age_days %= SYNODIC_MONTH_DAYS
    index = round(age_days / (SYNODIC_MONTH_DAYS / 8)) % 8
    return PHASES[index]


def effect_for(game: str) -> str | None:
    """'player', 'house', or None if tonight's phase isn't this game's night."""
    _, _, _, favored_game, direction = current_phase()
    return direction if favored_game == game else None
