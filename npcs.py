"""Registry of NPCs -- id, display name, which room they're in, optional portrait sprite, static
greeting, and an optional achievement granted (idempotently) the first time they're talked to.
Mirrors quests.py's own loader shape (achievements.py's ACHIEVEMENTS, dungeon.py's EQUIPMENT
registry).

This module deliberately knows nothing about quests -- an NPC is just "someone with a face, a
room, and a default thing to say." Whatever quests.py quest is currently active with a given NPC
(if any) is what actually decides what dialogue shows on any given click (see
npc_view.TalkToNpcButton); greet_message is only ever the fallback for "no quest is active with
them right now."

"room" is what makes NPC placement data instead of a per-hub Python decision -- every hub's view
loops over quests.npcs_present_in_room(room_id) uniformly rather than hardcoding which NPC ids it
adds a button for. An NPC's optional "visible_trigger" (same condition vocabulary as a quest's own
triggers -- quests.TRIGGER_SCHEMAS) additionally gates whether they're currently present at all
(e.g. the Greasy Princess only after mondor_goblin_chieftain is complete) -- absent means always
present in their room. This module can't validate visible_trigger's contents itself (that needs
quests.TRIGGER_SCHEMAS/_validate_trigger, and quests.py already imports this module -- the reverse
would be circular), so quests.py cross-validates every NPC's visible_trigger right after loading
both registries; see the bottom of quests.py.
"""

import json
import os

import achievements

_NPCS_PATH = os.path.join(os.path.dirname(__file__), "npcs.json")
_REQUIRED_NPC_FIELDS = {"id", "name", "room", "greet_message"}


def _load_npcs(path: str = _NPCS_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    npcs_by_id: dict[str, dict] = {}
    achievement_kinds = {a["kind"] for a in achievements.ACHIEVEMENTS}
    for entry in raw:
        npc_id = entry.get("id", "?")
        missing = _REQUIRED_NPC_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"npcs.json: npc {npc_id!r} missing field(s): {sorted(missing)}")
        if npc_id in npcs_by_id:
            raise ValueError(f"npcs.json: duplicate npc id {npc_id!r}")
        greet_achievement = entry.get("greet_achievement")
        if greet_achievement and greet_achievement not in achievement_kinds:
            raise ValueError(f"npcs.json: npc {npc_id!r} has unknown greet_achievement {greet_achievement!r}")
        visible_trigger = entry.get("visible_trigger")
        if visible_trigger is not None and not (isinstance(visible_trigger, dict) and "type" in visible_trigger):
            raise ValueError(f"npcs.json: npc {npc_id!r} has a malformed visible_trigger")
        npcs_by_id[npc_id] = entry
    return npcs_by_id


NPCS = _load_npcs()
