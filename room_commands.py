"""Registry of every command a room can invoke -- a bare `{key: callback}` dict, populated once by
bot.py right after every @bot.command is defined (see the bottom of bot.py). Deliberately its own
tiny leaf module rather than living in bot.py or being read from there directly: rooms.py's loader
and admin_schemas.py's "commands" field both need to check/offer real command keys, but neither
can import bot.py (bot.py already imports admin_server.py -> admin_schemas.py; the reverse would
be circular).

COMMANDS is empty at import time and only becomes meaningful once bot.py finishes running its own
top-level code -- see rooms.validate_command_keys(), called right after bot.py populates this, for
why nothing validates against it any earlier than that.
"""

COMMANDS: dict = {}
