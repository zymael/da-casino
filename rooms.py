"""Registry of rooms -- id, display name, background image, which commands live there, and which
other rooms it connects to. Mirrors quests.py's/npcs.py's own loader shape.

"description" is optional flavor text shown under the room's title (room_view.py falls back to no
description if absent, e.g. Ranch's) -- not in _REQUIRED_ROOM_FIELDS, no further validation beyond
being present-or-not, same treatment as "specialization" below.

Which NPCs appear in a room is *not* stored here -- an NPC's own "room" field (npcs.py) is the
only source of truth for that (see quests.npcs_present_in_room), so there's exactly one place
placement lives rather than two that could disagree.

Two cross-references can't be validated from inside this module's own loader, both for the same
reason -- the thing they'd check against isn't fully built yet at the point _load_rooms() itself
runs:
  - `commands[].key` must be a real key in room_commands.COMMANDS, but that dict is deliberately
    still empty at the moment this module gets imported (it's populated by bot.py only after
    every @bot.command has been defined, which happens well after bot.py's own `import
    admin_server -> admin_schemas -> rooms` chain already ran this module's top-level code).
  - `specialization` must be a real key in room_view._SPECIALIZATIONS, but room_view.py needs to
    import *this* module (for ROOMS) -- the reverse import would be circular.
Both are instead checked by validate_command_keys()/validate_specializations() below, called from
whichever module can see the *other* side once it's actually ready (bot.py, room_view.py) -- same
"one direction only" idea as quests.py cross-validating npcs.json's visible_trigger.
"""

import json
import os

_ROOMS_PATH = os.path.join(os.path.dirname(__file__), "rooms.json")
_REQUIRED_ROOM_FIELDS = {"id", "name", "background_path", "exits", "commands"}
_REQUIRED_EXIT_FIELDS = {"room_id", "label"}
_REQUIRED_COMMAND_FIELDS = {"key", "kind", "label"}
_COMMAND_KINDS = {"none", "amount"}


def _validate_command(command: dict, context: str):
    missing = _REQUIRED_COMMAND_FIELDS - command.keys()
    if missing:
        raise ValueError(f"{context} command missing field(s): {sorted(missing)}")
    # Present-but-blank (an admin-panel row with e.g. key set but label left empty) is just as
    # invalid as genuinely missing -- catches it as loudly as the check above, rather than saving
    # a command with an empty key/kind/label that would only surface as a broken button later.
    blank = [f for f in ("key", "kind", "label") if not command[f]]
    if blank:
        raise ValueError(f"{context} command has blank required field(s): {sorted(blank)}")
    kind = command["kind"]
    if kind not in _COMMAND_KINDS:
        raise ValueError(f"{context} command {command['key']!r} has unknown kind {kind!r}")
    if kind == "amount":
        if not command.get("modal_title") or not command.get("input_label"):
            raise ValueError(
                f"{context} command {command['key']!r} is kind \"amount\" but missing "
                f"modal_title/input_label"
            )
    const_args = command.get("const_args", [])
    if not isinstance(const_args, list) or not all(isinstance(a, str) for a in const_args):
        raise ValueError(f"{context} command {command['key']!r} const_args must be a list of strings")
    if "closes_hub" in command and not isinstance(command["closes_hub"], bool):
        raise ValueError(f"{context} command {command['key']!r} closes_hub must be true/false")


def _load_rooms(path: str = _ROOMS_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    rooms_by_id: dict[str, dict] = {}
    for entry in raw:
        room_id = entry.get("id", "?")
        missing = _REQUIRED_ROOM_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"rooms.json: room {room_id!r} missing field(s): {sorted(missing)}")
        if room_id in rooms_by_id:
            raise ValueError(f"rooms.json: duplicate room id {room_id!r}")
        for exit_entry in entry["exits"]:
            missing_exit = _REQUIRED_EXIT_FIELDS - exit_entry.keys()
            if missing_exit:
                raise ValueError(f"rooms.json: room {room_id!r} exit missing field(s): {sorted(missing_exit)}")
        for command in entry["commands"]:
            _validate_command(command, f"rooms.json: room {room_id!r}")
        rooms_by_id[room_id] = entry

    # Second pass: an exit's room_id might reference a room defined later in the file.
    for room_id, entry in rooms_by_id.items():
        for exit_entry in entry["exits"]:
            if exit_entry["room_id"] not in rooms_by_id:
                raise ValueError(
                    f"rooms.json: room {room_id!r} has an exit to unknown room {exit_entry['room_id']!r}"
                )
    return rooms_by_id


ROOMS = _load_rooms()


def validate_command_keys(known_command_keys, rooms_by_id: dict[str, dict] | None = None):
    """Called from bot.py once room_commands.COMMANDS is fully populated -- see module docstring
    for why this can't happen inside _load_rooms() itself. Raises loudly on a typo'd command key
    at startup, rather than a KeyError the moment some player clicks the broken button.

    `rooms_by_id` defaults to the module's own ROOMS, but admin_server.py's save flow passes in the
    freshly-loaded (not-yet-committed) candidate registry instead -- the loader alone can't catch a
    typo'd command key (the same ordering problem as above), but the admin panel's save can, by
    running this as an extra check before committing an edit, not just once at bot startup."""
    for room in (ROOMS if rooms_by_id is None else rooms_by_id).values():
        for command in room["commands"]:
            if command["key"] not in known_command_keys:
                raise ValueError(
                    f"rooms.json: room {room['id']!r} references unknown command {command['key']!r}"
                )


def validate_specializations(known_specializations, rooms_by_id: dict[str, dict] | None = None):
    """Called from room_view.py once _SPECIALIZATIONS is fully defined -- see module docstring for
    why this can't happen inside _load_rooms() itself. `rooms_by_id` -- see validate_command_keys
    above."""
    for room in (ROOMS if rooms_by_id is None else rooms_by_id).values():
        specialization = room.get("specialization")
        if specialization is not None and specialization not in known_specializations:
            raise ValueError(
                f"rooms.json: room {room['id']!r} references unknown specialization {specialization!r}"
            )
