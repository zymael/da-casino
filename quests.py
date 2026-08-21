"""Framework for multi-stage NPC storylines: a quest starts once its own "start_trigger" condition
is met, and each stage advances once its "trigger" condition is met -- turning in an item, killing
enough of a monster, crafting enough of a recipe, earning an achievement, another quest already
being complete, or an arbitrary flag reaching some value (see TRIGGER_SCHEMAS). Both starting and
completing a stage are always NPC-delivered: even a trigger with nothing to physically hand over
just makes that NPC's greeting/turn-in available, nothing here ever starts or advances a quest on
its own -- see talk_to_npc and turn_in, the only two places that write quest progress (through
db.py's generic per-player flags table -- a quest's stage lives at flag key "quest:<id>", see
_quest_flag_key/_get_stage/_start_quest/_advance_stage).

This module deliberately knows nothing about *why* an achievement was earned or a monster was
killed -- it only ever reads state other systems already own (personal_achievements, inventory,
flags), so e.g. achievements.py doesn't need to import this module or know quests exist.

New quests are added as data in quests.json (mirroring achievements.py's ACHIEVEMENTS and
dungeon.py's EQUIPMENT registry) -- no new plumbing needed per quest, and no new admin-panel
plumbing needed per trigger *param* (only a genuinely new trigger *type* needs a schema entry here
plus, if it's a counted type, a hook-point call to record_progress).

A stage with no "trigger" key is a dialogue-only endpoint (the quest's current end, until more
stages are appended).
"""

import asyncio
import json
import os
import random

import db
import dungeon
import npcs

_QUEST_ITEMS_PATH = os.path.join(os.path.dirname(__file__), "quest_items.json")
_QUESTS_PATH = os.path.join(os.path.dirname(__file__), "quests.json")
_REQUIRED_ITEM_FIELDS = {"id", "name", "emoji", "description"}
_REQUIRED_QUEST_FIELDS = {"id", "npc", "start_trigger", "stages"}
_REQUIRED_STAGE_FIELDS = {"prompt", "on_complete_message"}

# type -> (required param names, optional param names). Valid both as a quest's top-level
# "start_trigger" and as a stage's "trigger" -- starting and advancing use the exact same
# condition vocabulary, and (via the public trigger_satisfied) the same vocabulary NPC-presence
# and room-exit conditions use too (see rooms.py). turn_in_item is checked against the player's
# inventory (roll_item_drop is what actually gets the item into their hands), achievement against
# personal_achievements, quest_complete against another quest's own flag, and flag_at_least
# against an arbitrary flag key -- all four are live state this module can check on demand, no
# counter needed. kill_monster/craft_item are "counted" types (they have a required "count"):
# checked against a flag scoped to the specific quest stage asking, bumped by record_progress from
# whatever game event matches -- deliberately quest-stage-scoped rather than generic counters (see
# module docstring in the project plan for why). Adding a new type here is the whole cost of a new
# trigger *kind* -- the admin panel's trigger field flattens every type's params into one row
# generically (see admin_schemas.TRIGGER_PARAM_KINDS), so it needs no changes of its own.
TRIGGER_SCHEMAS = {
    "turn_in_item": ({"item_id"}, {"drop_monster"}),
    "achievement": ({"kind"}, set()),
    "kill_monster": ({"count"}, {"monster_id", "tier"}),
    "craft_item": ({"count"}, {"recipe_id"}),
    "quest_complete": ({"quest_id"}, set()),
    "flag_at_least": ({"key", "value"}, set()),
}

# event_type -> does this trigger match the event's data. Only the *counted* trigger types
# (turn_in_item and achievement are checked live, not event-sourced) need one of these --
# record_progress uses it to decide whether an incoming event bumps a given counter at all.
_TRIGGER_MATCHERS = {
    "kill_monster": lambda trigger, data: (
        (trigger.get("monster_id") is None or trigger["monster_id"] == data.get("monster_id"))
        and (trigger.get("tier") is None or trigger["tier"] == data.get("tier"))
    ),
    "craft_item": lambda trigger, data: (
        trigger.get("recipe_id") is None or trigger["recipe_id"] == data.get("recipe_id")
    ),
}

# Sentinel "stage" a counted trigger's flag key uses for progress toward a quest's start_trigger,
# before it has a real stage index (stage 0 is the first real stage) -- keeps start-progress and
# stage-progress addressed the same way rather than needing a separate scheme.
_START_STAGE = -1


def _quest_flag_key(quest_id: str) -> str:
    """A quest's own progress lives at this flag key -- stored as stage_index + 1 (see
    _get_stage/_start_quest/_advance_stage) so 0 unambiguously means "not started" without
    colliding with real stage 0, since db.get_flag's own "absence = 0" default can't distinguish
    "never written" from "written as 0" any other way."""
    return f"quest:{quest_id}"


def _stage_counter_key(quest_id: str, stage: int) -> str:
    """Where a counted trigger's (kill_monster/craft_item) progress toward *this* stage (or the
    quest's start_trigger, if stage is _START_STAGE) lives. Scoped to one quest's one stage --
    once that stage advances, its old counter key is simply never read again (no cleanup needed,
    same "stale flags are harmless" idea the old quest_counters table already relied on)."""
    if stage == _START_STAGE:
        return f"quest:{quest_id}:start:count"
    return f"quest:{quest_id}:stage{stage}:count"


async def _get_stage(guild_id: int, user_id: int, quest_id: str) -> int | None:
    """This user's current stage index in `quest_id`, or None if they haven't started it."""
    raw = await asyncio.to_thread(db.get_flag, guild_id, user_id, _quest_flag_key(quest_id))
    return None if raw == 0 else raw - 1


async def _start_quest(guild_id: int, user_id: int, quest_id: str) -> bool:
    """Starts `quest_id` at stage 0 if they haven't already started it. Returns whether this call
    was the one that started it -- idempotent, so a quest's start trigger can be re-checked (e.g.
    every time its NPC is talked to) without restarting progress."""
    return await asyncio.to_thread(db.set_flag_if_zero, guild_id, user_id, _quest_flag_key(quest_id), 1)


async def _advance_stage(guild_id: int, user_id: int, quest_id: str, from_stage: int) -> bool:
    """Advances `quest_id` from `from_stage` to `from_stage + 1`. Returns whether it actually
    moved -- False if the stored stage no longer matches `from_stage` (e.g. a stale view double
    button-clicked), same stale-state guard the old quest_progress table's UPDATE...WHERE relied
    on, now expressed as a compare-and-set against the flag's encoded (stage + 1) value."""
    return await asyncio.to_thread(
        db.compare_and_set_flag, guild_id, user_id, _quest_flag_key(quest_id), from_stage + 1, from_stage + 2,
    )


# Shown once every stage is turned in, for a quest with no quest-level "complete_message" of its
# own -- most quests won't bother writing one until/unless there's a reason to (a follow-up
# stage, a reveal, ...).
DEFAULT_COMPLETE_MESSAGE = "\"...\" (There's nothing more for now -- come back later.)"

QUEST_ITEM_DROP_CHANCE = 1.0  # guaranteed -- a quest stage's own monster/inventory gating (see
# roll_item_drop) already controls when an item is even eligible to drop; stacking a random
# chance on top of that just meant grinding the right monster repeatedly for no story reason


def _load_quest_items(path: str = _QUEST_ITEMS_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    items: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_ITEM_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"quest_items.json: item {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in items:
            raise ValueError(f"quest_items.json: duplicate item id {entry_id!r}")
        items[entry_id] = entry
    return items


QUEST_ITEMS = _load_quest_items()

# Quest items, crafting materials, and consumables all key the same generic `inventory` table by
# item_id with no type column -- dungeon.py can't check this itself (it can't import quests.py,
# which already imports dungeon.py). A collision here is a genuine, easy-to-hit content bug (two
# hand-edited JSON files independently picking the same id), so it's caught loudly at import time
# rather than silently letting one kind of item shadow another in someone's inventory.
_item_id_collisions = QUEST_ITEMS.keys() & (dungeon.MATERIALS.keys() | dungeon.CONSUMABLES.keys())
if _item_id_collisions:
    raise ValueError(f"quest_items.json ids collide with dungeon materials/consumables: {sorted(_item_id_collisions)}")


def _validate_trigger(trigger: dict, context: str):
    """Shared by every stage's trigger -- `context` is an f-string-ready label (e.g. "quests.json:
    quest 'foo' stage 0") prefixed onto every error, same convention as dungeon._validate_effects."""
    trigger_type = trigger.get("type")
    if trigger_type not in TRIGGER_SCHEMAS:
        raise ValueError(f"{context} has unknown trigger type {trigger_type!r}")
    required, optional = TRIGGER_SCHEMAS[trigger_type]
    params = trigger.keys() - {"type"}
    missing = required - params
    if missing:
        raise ValueError(f"{context} trigger {trigger_type!r} missing param(s): {sorted(missing)}")
    unknown = params - required - optional
    if unknown:
        raise ValueError(f"{context} trigger {trigger_type!r} has unknown param(s): {sorted(unknown)}")
    if "item_id" in params and trigger["item_id"] not in QUEST_ITEMS:
        raise ValueError(f"{context} trigger references unknown quest item {trigger['item_id']!r}")
    if "drop_monster" in params and trigger["drop_monster"] not in dungeon.MONSTERS:
        raise ValueError(f"{context} trigger references unknown monster {trigger['drop_monster']!r}")
    if "monster_id" in params and trigger["monster_id"] not in dungeon.MONSTERS:
        raise ValueError(f"{context} trigger references unknown monster {trigger['monster_id']!r}")
    if "recipe_id" in params and trigger["recipe_id"] not in dungeon.RECIPES:
        raise ValueError(f"{context} trigger references unknown recipe {trigger['recipe_id']!r}")
    if "count" in params and trigger["count"] <= 0:
        raise ValueError(f"{context} trigger {trigger_type!r} count must be > 0")
    if "tier" in params and trigger["tier"] <= 0:
        raise ValueError(f"{context} trigger {trigger_type!r} tier must be > 0")


def _load_quests(path: str = _QUESTS_PATH) -> dict[str, dict]:
    """A quest's "start_trigger" is validated exactly like a stage's "trigger" (see
    TRIGGER_SCHEMAS/_validate_trigger) -- both use the same condition vocabulary, just checked at
    a different moment (talk_to_npc vs turn_in). Note an "achievement" trigger's "kind" isn't
    cross-checked against achievements.ACHIEVEMENTS here -- quests.py can't import achievements.py
    (achievements.py has no reason to import quests.py either, now that starting is checked lazily
    rather than pushed from an achievement-unlock call site, but the reverse direction still would
    be circular since achievements.py predates this and might grow other reasons to import it) --
    so a typo there just means the quest never auto-starts rather than a load-time crash; the
    admin panel's enum field is what actually prevents that typo at entry time.

    Each stage: "prompt" (NPC dialogue shown while this stage is active), "trigger" (what advances
    it, or absent for a dialogue-only endpoint), "on_complete_message", and either a currency
    "reward" or an equipment "reward_item" (id into dungeon.EQUIPMENT -- equipped via the same
    is_upgrade rule as ordinary loot, see turn_in). Later stages are just appended to a quest's
    list."""
    with open(path) as f:
        raw = json.load(f)
    quests_by_id: dict[str, dict] = {}
    for entry in raw:
        quest_id = entry.get("id", "?")
        missing = _REQUIRED_QUEST_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"quests.json: quest {quest_id!r} missing field(s): {sorted(missing)}")
        if quest_id in quests_by_id:
            raise ValueError(f"quests.json: duplicate quest id {quest_id!r}")
        if entry["npc"] not in npcs.NPCS:
            raise ValueError(f"quests.json: quest {quest_id!r} has unknown npc {entry['npc']!r}")
        _validate_trigger(entry["start_trigger"], f"quests.json: quest {quest_id!r} start_trigger")
        stages = entry["stages"]
        if not stages:
            raise ValueError(f"quests.json: quest {quest_id!r} has no stages")
        for i, stage in enumerate(stages):
            context = f"quests.json: quest {quest_id!r} stage {i}"
            missing_stage = _REQUIRED_STAGE_FIELDS - stage.keys()
            if missing_stage:
                raise ValueError(f"{context} missing field(s): {sorted(missing_stage)}")
            trigger = stage.get("trigger")
            if trigger is not None:
                _validate_trigger(trigger, context)
            reward_item_id = stage.get("reward_item")
            if reward_item_id and reward_item_id not in dungeon.EQUIPMENT:
                raise ValueError(f"{context} reward_item {reward_item_id!r} not in dungeon.EQUIPMENT")
        quests_by_id[quest_id] = entry

    # Second pass: a quest_complete trigger references another quest by id, which might not have
    # been loaded yet at the point its own trigger was validated above (quests.json order doesn't
    # have to match reference order) -- checked here instead, once every quest id is known.
    for quest_id, entry in quests_by_id.items():
        triggers = [entry["start_trigger"]] + [s["trigger"] for s in entry["stages"] if s.get("trigger")]
        for trigger in triggers:
            if trigger["type"] == "quest_complete" and trigger["quest_id"] not in quests_by_id:
                raise ValueError(
                    f"quests.json: quest {quest_id!r} has a quest_complete trigger referencing "
                    f"unknown quest {trigger['quest_id']!r}"
                )
    return quests_by_id


QUESTS_BY_ID = _load_quests()

# npcs.py can't validate its own "visible_trigger" field (that needs TRIGGER_SCHEMAS/
# _validate_trigger, and this module already imports npcs -- the reverse would be circular), so
# it's cross-validated here instead, right after both registries are loaded. Same "one direction
# only" constraint as start_trigger's achievement kind not being checked against
# achievements.ACHIEVEMENTS -- except a malformed visible_trigger would crash mid-game the moment
# a room actually evaluates it (not just silently no-op), so this one really does need a load-time
# check rather than just leaning on the admin panel's form to prevent typos.
#
# Same story for a "quest_item"-kind shop entry's item_id -- npcs.py validates every other shop
# kind itself (dungeon.py is safely importable from there) but can't check this one without
# importing this module back, so it's checked here too, right alongside visible_trigger.
for _npc in npcs.NPCS.values():
    _npc_trigger = _npc.get("visible_trigger")
    if _npc_trigger is not None:
        _validate_trigger(_npc_trigger, f"npcs.json: npc {_npc['id']!r} visible_trigger")
    for _i, _shop_entry in enumerate(_npc.get("shop") or []):
        if _shop_entry["kind"] == "quest_item" and _shop_entry["item_id"] not in QUEST_ITEMS:
            raise ValueError(
                f"npcs.json: npc {_npc['id']!r} shop entry {_i} item_id {_shop_entry['item_id']!r} "
                f"not in QUEST_ITEMS"
            )


async def npcs_present_in_room(guild_id: int, user_id: int, room_id: str) -> list[str]:
    """Every npcs.json id whose "room" matches `room_id` and whose optional "visible_trigger" (if
    any) is currently satisfied for this player. This is the generic "which NPCs does this room's
    view actually add a TalkToNpcButton for" list every hub's view-building function loops over
    uniformly -- which NPCs live where (and when) is npcs.json data, not a per-hub Python
    decision."""
    present = []
    for npc_id, npc in npcs.NPCS.items():
        if npc.get("room") != room_id:
            continue
        trigger = npc.get("visible_trigger")
        if trigger is None or await trigger_satisfied(guild_id, user_id, trigger):
            present.append(npc_id)
    return present


async def quest_log(guild_id: int, user_id: int) -> list[dict]:
    """Every quest this player has started (in quests.json order), each as {"quest_id", "npc",
    "stage_index", "total_stages", "complete", "prompt"} -- prompt is the current stage's own
    text (or None once complete). Backs !quests; deliberately doesn't invent quest titles or any
    other copy -- npc id and each stage's existing prompt are the only text surfaced."""
    entries = []
    for quest in QUESTS_BY_ID.values():
        stage_index = await _get_stage(guild_id, user_id, quest["id"])
        if stage_index is None:
            continue
        complete = stage_index >= len(quest["stages"])
        if complete:
            prompt = quest.get("complete_message", DEFAULT_COMPLETE_MESSAGE)
        else:
            prompt = quest["stages"][stage_index]["prompt"]
        entries.append({
            "quest_id": quest["id"],
            "npc": quest["npc"],
            "stage_index": stage_index,
            "total_stages": len(quest["stages"]),
            "complete": complete,
            "prompt": prompt,
        })
    return entries


async def roll_item_drop(guild_id: int, user_id: int, room_index: int, monster_id: str) -> dict | None:
    """None most of the time. When it hits, adds one quest item to the player's inventory and
    returns it -- but only an item their *current* stage on some in-progress quest is actually
    waiting on, so players not on that quest never see unrelated flavor items. A stage whose
    trigger sets "drop_monster" only offers its item after killing that specific dungeon.MONSTERS
    id; room_index is accepted for symmetry with dungeon.roll_drops's call site (no quest item is
    room-gated, only monster-gated)."""
    candidates = []
    for quest in QUESTS_BY_ID.values():
        stage_index = await _get_stage(guild_id, user_id, quest["id"])
        if stage_index is None or stage_index >= len(quest["stages"]):
            continue
        stage = quest["stages"][stage_index]
        trigger = stage.get("trigger")
        if trigger is None or trigger["type"] != "turn_in_item":
            continue
        item_id = trigger["item_id"]
        required_monster = trigger.get("drop_monster")
        if required_monster is not None and required_monster != monster_id:
            continue
        held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
        if held.get(item_id, 0) > 0:
            continue  # already holding it, waiting on the turn-in itself
        candidates.append(item_id)

    if not candidates or random.random() > QUEST_ITEM_DROP_CHANCE:
        return None

    item_id = random.choice(candidates)
    await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, item_id)
    return QUEST_ITEMS[item_id]


def _quests_for_npc(npc_id: str) -> list[dict]:
    return [quest for quest in QUESTS_BY_ID.values() if quest["npc"] == npc_id]


async def trigger_satisfied(
    guild_id: int, user_id: int, trigger: dict, *, quest_id: str | None = None, stage: int | None = None,
) -> bool:
    """Whether a trigger condition currently holds. `quest_id`/`stage` are only needed for the two
    *counted* types (kill_monster/craft_item), whose progress is scoped to one quest stage's own
    counter -- every other type is fully self-contained and callable from anywhere, which is what
    lets NPC-presence and room-exit conditions (rooms.py) reuse this exact function and vocabulary
    instead of quests needing their own bespoke condition-checking. turn_in_item is checked
    against the player's inventory; achievement against personal_achievements; quest_complete
    against another quest's own flag; flag_at_least against an arbitrary flag key -- all four are
    live state this function can just read, no counter needed."""
    trigger_type = trigger["type"]
    if trigger_type == "turn_in_item":
        held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
        return held.get(trigger["item_id"], 0) > 0
    if trigger_type == "achievement":
        earned = await asyncio.to_thread(db.get_user_personal_achievements, guild_id, user_id)
        return trigger["kind"] in earned
    if trigger_type == "quest_complete":
        target = QUESTS_BY_ID[trigger["quest_id"]]
        target_stage = await _get_stage(guild_id, user_id, trigger["quest_id"])
        return target_stage is not None and target_stage >= len(target["stages"])
    if trigger_type == "flag_at_least":
        value = await asyncio.to_thread(db.get_flag, guild_id, user_id, trigger["key"])
        return value >= trigger["value"]
    # Counted types (kill_monster, craft_item) -- scoped to one quest stage's own counter flag.
    count = await asyncio.to_thread(db.get_flag, guild_id, user_id, _stage_counter_key(quest_id, stage))
    return count >= trigger["count"]


async def record_progress(guild_id: int, user_id: int, event_type: str, **event_data):
    """Called from wherever a countable game event happens (a monster kill, a successful craft) --
    bumps the flag counter for whichever checkpoint a player is currently waiting on (a not-yet-
    started quest's start_trigger, or an in-progress quest's current-stage trigger) if it's a
    counted type matching event_type. This never starts or advances anything itself -- it only
    ever moves a counter closer to satisfied; _start_quest and _advance_stage are only ever called
    from talk_to_npc / turn_in respectively, once the player actually visits the NPC. No-ops for
    event types nothing is listening for (achievement isn't event-sourced at all -- see
    trigger_satisfied)."""
    matcher = _TRIGGER_MATCHERS.get(event_type)
    if matcher is None:
        return
    for quest in QUESTS_BY_ID.values():
        stage_index = await _get_stage(guild_id, user_id, quest["id"])
        if stage_index is None:
            trigger, stage = quest["start_trigger"], _START_STAGE
        elif stage_index < len(quest["stages"]):
            trigger, stage = quest["stages"][stage_index].get("trigger"), stage_index
        else:
            continue
        if trigger is None or trigger["type"] != event_type:
            continue
        if not matcher(trigger, event_data):
            continue
        await asyncio.to_thread(db.increment_flag, guild_id, user_id, _stage_counter_key(quest["id"], stage))


async def talk_to_npc(guild_id: int, user_id: int, npc_id: str) -> list[dict]:
    """Returns one {"quest_id", "prompt", "can_turn_in", "item", "complete"} entry per quest this
    player has active (or just started) with this NPC -- an NPC can have more than one eligible
    quest at once, so this is a list rather than a single merged dict (an earlier version
    collapsed every quest into one result and silently let whichever quest came last in
    quests.json order clobber the rest, which also meant the displayed prompt and the thing
    turn_in would actually resolve could disagree once a second quest existed). A stage with no
    trigger (dialogue-only endpoint) reports can_turn_in False, same as one whose trigger isn't
    satisfied yet. "complete" marks a quest whose complete_message is now showing (all stages
    turned in) -- callers that want a one-off visual (e.g. a reveal sprite) for one specific quest
    check for its quest_id here rather than "any NPC quest is done".

    This is the only place a not-yet-started quest ever actually starts: whenever the player talks
    to its NPC and its start_trigger is satisfied (an achievement already earned, an item already
    held, or a counter record_progress has already brought up to target), it starts right here --
    same "nothing happens until you visit the NPC" rule every other quest beat follows."""
    results = []
    for quest in _quests_for_npc(npc_id):
        stage_index = await _get_stage(guild_id, user_id, quest["id"])
        if stage_index is None:
            if not await trigger_satisfied(
                guild_id, user_id, quest["start_trigger"], quest_id=quest["id"], stage=_START_STAGE,
            ):
                continue
            await _start_quest(guild_id, user_id, quest["id"])
            stage_index = 0
        if stage_index >= len(quest["stages"]):
            results.append({
                "quest_id": quest["id"], "prompt": quest.get("complete_message", DEFAULT_COMPLETE_MESSAGE),
                "can_turn_in": False, "item": None, "complete": True,
            })
            continue
        stage = quest["stages"][stage_index]
        trigger = stage.get("trigger")
        can_turn_in = trigger is not None and await trigger_satisfied(
            guild_id, user_id, trigger, quest_id=quest["id"], stage=stage_index,
        )
        item = QUEST_ITEMS[trigger["item_id"]] if can_turn_in and trigger["type"] == "turn_in_item" else None
        results.append({
            "quest_id": quest["id"], "prompt": stage["prompt"], "can_turn_in": can_turn_in, "item": item,
            "complete": False,
        })
    return results


async def turn_in(guild_id: int, user_id: int, quest_id: str) -> dict:
    """Resolves this specific quest's current stage and, if its trigger is satisfied, consumes
    its cost (if any) and advances the stage. Takes a quest_id rather than an npc_id -- an NPC can
    have more than one eligible quest active at once (see talk_to_npc), so "which quest this
    button turns in" has to be decided by whoever built that button, not re-resolved ambiguously
    here. Returns {"success", "message", "reward", "reward_item", "equipped", "quest_complete"} --
    success is False (everything else None/0/False) if there's nothing to turn in. reward_item is
    the dungeon.EQUIPMENT dict if this stage grants one (None otherwise); equipped says whether it
    actually got equipped (same is_upgrade rule as ordinary loot) -- if not, it's stored in
    equipment_inventory instead, swappable later via !equipment rather than lost."""
    failure = {
        "success": False, "message": None, "reward": 0, "reward_item": None, "equipped": False,
        "quest_complete": False,
    }
    quest = QUESTS_BY_ID[quest_id]
    stage_index = await _get_stage(guild_id, user_id, quest_id)
    if stage_index is None or stage_index >= len(quest["stages"]):
        return failure

    stage = quest["stages"][stage_index]
    trigger = stage.get("trigger")
    if trigger is None:
        return failure
    if trigger["type"] == "turn_in_item":
        if not await asyncio.to_thread(db.consume_inventory_item, guild_id, user_id, trigger["item_id"]):
            return failure
    elif not await trigger_satisfied(guild_id, user_id, trigger, quest_id=quest_id, stage=stage_index):
        return failure

    advanced = await _advance_stage(guild_id, user_id, quest_id, stage_index)
    if not advanced:
        # Lost a race (e.g. a double-clicked button) -- give the item back rather than eat it.
        if trigger["type"] == "turn_in_item":
            await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, trigger["item_id"])
        return failure

    reward = stage.get("reward", 0)
    if reward:
        await asyncio.to_thread(db.update_balance, guild_id, user_id, reward)

    reward_item, equipped = None, False
    reward_item_id = stage.get("reward_item")
    if reward_item_id:
        reward_item = dungeon.EQUIPMENT[reward_item_id]
        equipped_items = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
        slot = reward_item["slot"]
        if dungeon.is_upgrade(equipped_items.get(slot), reward_item):
            await asyncio.to_thread(db.equip_item_smart, guild_id, user_id, slot, reward_item_id)
            equipped = True
        else:
            await asyncio.to_thread(db.store_equipment_item, guild_id, user_id, reward_item_id)

    return {
        "success": True, "message": stage.get("on_complete_message"), "reward": reward,
        "reward_item": reward_item, "equipped": equipped,
        "quest_complete": stage_index + 1 >= len(quest["stages"]),
    }
