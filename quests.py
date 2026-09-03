"""Framework for multi-stage NPC storylines: a quest is a stage GRAPH, not a flat sequence -- each
stage has one or more "paths" out of it (mirroring a delve choice room's actions), each path gated
by its own "trigger" condition and pointing at whichever stage comes "next" (or nothing, ending the
quest). A quest starts once its own "start_trigger" condition is met; a stage advances once one of
its paths' triggers is met (see TRIGGER_SCHEMAS). Nothing here ever starts or advances a quest on
its own -- starting only ever happens through _try_start_quest, and advancing only ever happens
through turn_in.

Two identities per stage, doing different jobs: "id" (a string) is what a path's "next" points at --
editable, drag-connectable in the admin flowchart editor, exactly like a delve room's own id.
"ordinal" (an integer, assigned once and never reused, never shown in the editor) is what a
player's actual progress is stored as, in db.py's generic per-player flags table (a quest's stage
lives at flag key "quest:<id>", encoded as ordinal + 1 so 0 unambiguously means "not started" --
see _quest_flag_key/_get_stage_ordinal/_start_quest/_advance_via_path). The split exists because
delve rooms track progress in an ephemeral in-memory session that never outlives one delve run, so
a room can freely be addressed by its own editable id -- a quest's progress has to survive across
sessions in an integer-only flags table, so its *durable* identity (ordinal) has to stay stable even
while its *authoring* identity (id) can be freely renamed or the stage list reordered/extended.

_try_start_quest has two callers: npc_greet (npc-scoped, the original "visit the NPC and it offers
itself" flow) and check_new_quests (every quest at once, journal_view.py's !journal entry point for
starting a quest without visiting anyone) -- both are equally real, a quest doesn't care which one
triggered it. turn_in has exactly one caller: npc_view.py's NpcTurnInButton, nested inside a
topic's detail screen -- deliberately no !journal shortcut (an earlier version of journal_view.py
had one; removed so turning in always means actually talking to the NPC). An optional path_index
lets a caller resolve one specific path explicitly (for a stage with more than one path open at
once); omitted, turn_in auto-picks the first currently-satisfied path -- every quest today has
exactly one path per stage, so this is the only behavior any existing content ever exercises.

Which NPCs currently have anything to say about a stage is its own concern, orthogonal to the graph
above: each stage carries a "discuss_with" list of npc ids (validated against npcs.NPCS, same as
the quest's own top-level "npc"). npc_greet (an NPC's conversation entry point, replacing the old
talk_to_npc) offers a "topic" button for a quest's current stage on every NPC listed there -- not
just the quest's own giver -- plus a permanent giver-only wrap-up topic once the quest is complete
(matching the pre-existing "complete_message shows forever" behavior, deliberately not gated by
discuss_with since a completed stage no longer exists to carry the list). quest_topic_state is the
one place "what does this quest currently look like to show" is computed, reused both by
npc_greet's topic-list building and by npc_view.py's click-time refresh of an already-open topic.

This module deliberately knows nothing about *why* an achievement was earned or a monster was
killed -- it only ever reads state other systems already own (personal_achievements, inventory,
flags), so e.g. achievements.py doesn't need to import this module or know quests exist.

New quests are authored through the admin panel's flowchart editor (mirroring the dungeon delve
editor -- see admin_schemas.py's "quest_flowchart" field type), autosaved to quest_drafts.json with
no validation gate mid-edit (see check_quest_problems) and only checked for real (including full
reachability from start_stage) at Publish time -- the same two-tier draft/publish flow delves
already have.

A stage with an empty "paths" list is a dialogue-only endpoint (the quest's current end, until more
paths/stages are appended).
"""

import asyncio
import json
import os
import random

import db
import dungeon
import horse_clothes
import moon
import npcs
import rooms

_QUEST_ITEMS_PATH = os.path.join(os.path.dirname(__file__), "quest_items.json")
_QUESTS_PATH = os.path.join(os.path.dirname(__file__), "quests.json")
_QUEST_DRAFTS_PATH = os.path.join(os.path.dirname(__file__), "quest_drafts.json")
_REQUIRED_ITEM_FIELDS = {"id", "name", "emoji", "description"}
_REQUIRED_QUEST_FIELDS = {"id", "name", "npc", "start_trigger", "start_stage", "stages"}
_REQUIRED_STAGE_FIELDS = {"id", "ordinal", "prompt", "journal_text", "paths", "discuss_with"}
_REQUIRED_PATH_FIELDS = {"trigger", "on_complete_message"}

# type -> (required param names, optional param names). Valid both as a quest's top-level
# "start_trigger" and as a path's "trigger" -- starting and advancing use the exact same condition
# vocabulary, and (via the public trigger_satisfied) the same vocabulary NPC-presence and room-exit
# conditions use too (see rooms.py). turn_in_item is checked against the player's inventory
# (roll_item_drop is what actually gets the item into their hands), achievement against
# personal_achievements, quest_complete against another quest's own flag, and flag_at_least against
# an arbitrary flag key -- all four are live state this module can check on demand, no counter
# needed. kill_monster/craft_item are "counted" types (they have a required "count"): checked
# against a flag scoped to the specific quest stage asking, bumped by record_progress from whatever
# game event matches -- deliberately quest-stage-scoped rather than generic counters. Adding a new
# type here is the whole cost of a new trigger *kind* -- the admin panel's trigger field flattens
# every type's params into one row generically (see admin_schemas.TRIGGER_PARAM_KINDS), so it needs
# no changes of its own.
TRIGGER_SCHEMAS = {
    "turn_in_item": ({"item_id"}, {"drop_monster"}),
    "achievement": ({"kind"}, set()),
    "kill_monster": ({"count"}, {"monster_id"}),
    "craft_item": ({"count"}, {"recipe_id"}),
    "quest_complete": ({"quest_id"}, set()),
    "flag_at_least": ({"key", "value"}, set()),
    # Checks a *character's* class/subclass rather than any player-state db.py already tracks --
    # the one trigger type trigger_satisfied needs an explicit `character` dict for (every other
    # type is self-contained from guild_id/user_id alone). Added for dungeon.py's choice-room
    # action "requires" gates (e.g. "must be a mage" to attempt a magical unlock) -- see
    # trigger_satisfied's own "class" branch.
    "class": ({"main_class"}, {"subclass"}),
    # A currency cost rather than an item one -- same "consuming, not just checkable" shape as
    # turn_in_item (trigger_satisfied's own case below is a non-consuming "can they afford it"
    # check; the actual deduction is special-cased in turn_in(), same split turn_in_item has with
    # db.consume_inventory_item).
    "pay_currency": ({"amount"}, set()),
    # Real-world lunar phase (moon.py, already backing a secret odds nudge elsewhere) rather than
    # any player state -- self-contained like every type above it, just reading moon.current_phase()
    # instead of the db. not_moon_phase is the only trigger type with a negated counterpart, added
    # alongside moon_phase (rather than a generic "not" wrapper around any trigger) so an NPC's
    # presence can be made mutually exclusive with another's on the same condition without inventing
    # a whole second condition-combinator concept for one use.
    "moon_phase": ({"phase"}, set()),
    "not_moon_phase": ({"phase"}, set()),
}

# event_type -> does this trigger match the event's data. Only the *counted* trigger types
# (turn_in_item and achievement are checked live, not event-sourced) need one of these --
# record_progress uses it to decide whether an incoming event bumps a given counter at all.
_TRIGGER_MATCHERS = {
    "kill_monster": lambda trigger, data: (
        trigger.get("monster_id") is None or trigger["monster_id"] == data.get("monster_id")
    ),
    "craft_item": lambda trigger, data: (
        trigger.get("recipe_id") is None or trigger["recipe_id"] == data.get("recipe_id")
    ),
}

# Sentinel "stage" a counted trigger's flag key uses for progress toward a quest's start_trigger,
# before it has a real stage ordinal (real ordinals are always >= 0) -- keeps start-progress and
# stage-progress addressed the same way rather than needing a separate scheme.
_START_STAGE = -1

# Sentinel ordinal _advance_via_path writes when a path has no "next" (ending the quest). Must be
# -2, not -1: the flag actually stored is `ordinal + 1` (see _quest_flag_key), and 0 is already
# reserved to mean "not started" -- if this were -1, a just-completed quest would encode as
# `-1 + 1 == 0`, indistinguishable from never having started at all (a real bug caught by this
# module's own test suite: a "completed" player would vanish from !journal and _try_start_quest
# would happily restart them). -2 encodes as -1, which collides with neither 0 (not started) nor
# any real ordinal's own `+1` encoding (real ordinals are always >= 0, so their encoded values are
# always >= 1). Unrelated to _START_STAGE above despite the near-identical name and numeral --
# _START_STAGE only ever names a counter-key *string*, never a raw flag value; _COMPLETE_ORDINAL is
# only ever a raw flag value (after its own +1 encoding), never a counter-key name -- the two are
# never compared to each other or stored under the same key.
_COMPLETE_ORDINAL = -2


def _quest_flag_key(quest_id: str) -> str:
    """A quest's own progress lives at this flag key -- stored as ordinal + 1 (see
    _get_stage_ordinal/_start_quest/_advance_via_path) so 0 unambiguously means "not started"
    without colliding with a real ordinal of 0, since db.get_flag's own "absence = 0" default can't
    distinguish "never written" from "written as 0" any other way."""
    return f"quest:{quest_id}"


def _stage_counter_key(quest_id: str, ordinal: int) -> str:
    """Where a counted trigger's (kill_monster/craft_item) progress toward the stage at `ordinal`
    (or the quest's start_trigger, if ordinal is _START_STAGE) lives. Scoped to one quest's one
    stage -- once that stage advances, its old counter key is simply never read again (no cleanup
    needed, same "stale flags are harmless" idea the old quest_counters table already relied on).
    If a future branching stage ever has two or more counted-type paths at once, they'd collide on
    this one key (scoped to (quest, stage), not (quest, stage, path)) -- no existing content does
    this; deferred until it's actually needed, since a path has no stable identity of its own to
    scope a finer key to (mirroring a delve action, which has the same limitation)."""
    if ordinal == _START_STAGE:
        return f"quest:{quest_id}:start:count"
    return f"quest:{quest_id}:stage{ordinal}:count"


async def _get_stage_ordinal(guild_id: int, user_id: int, quest_id: str) -> int | None:
    """This user's current stage ordinal in `quest_id`, or None if they haven't started it."""
    raw = await asyncio.to_thread(db.get_flag, guild_id, user_id, _quest_flag_key(quest_id))
    return None if raw == 0 else raw - 1


async def _start_quest(guild_id: int, user_id: int, quest_id: str, ordinal: int) -> bool:
    """Starts `quest_id` at the stage whose ordinal is `ordinal` (its start_stage) if they haven't
    already started it. Returns whether this call was the one that started it -- idempotent, so a
    quest's start trigger can be re-checked (e.g. every time its NPC is talked to) without
    restarting progress."""
    return await asyncio.to_thread(db.set_flag_if_zero, guild_id, user_id, _quest_flag_key(quest_id), ordinal + 1)


async def _advance_via_path(guild_id: int, user_id: int, quest: dict, from_ordinal: int, path: dict) -> bool:
    """Advances `quest` from `from_ordinal` to whichever stage `path["next"]` names, or to the
    reserved _COMPLETE_ORDINAL sentinel if `path` has no "next" (ending the quest). Returns whether
    it actually moved -- False if the stored ordinal no longer matches `from_ordinal` (e.g. a stale
    view double button-clicked), same stale-state CAS guard as before, now resolving its target
    ordinal from the stage graph instead of a hardcoded +1."""
    next_id = path.get("next")
    new_ordinal = _COMPLETE_ORDINAL if next_id is None else _stages_by_id(quest)[next_id]["ordinal"]
    return await asyncio.to_thread(
        db.compare_and_set_flag, guild_id, user_id, _quest_flag_key(quest["id"]), from_ordinal + 1, new_ordinal + 1,
    )


def _stages_by_id(quest: dict) -> dict[str, dict]:
    return {stage["id"]: stage for stage in quest["stages"]}


def _stage_by_ordinal(quest: dict) -> dict[int, dict]:
    return {stage["ordinal"]: stage for stage in quest["stages"]}


def _is_complete(quest: dict, ordinal: int) -> bool:
    """Whether `ordinal` (a player's current decoded stage position, from _get_stage_ordinal)
    corresponds to a real stage of `quest` -- False means still in progress, True means done. One
    rule that transparently covers both the legacy "one past the last array index" encoding every
    already-completed player's flag already carries from before this module tracked stages by
    ordinal (see the module docstring), and the explicit _COMPLETE_ORDINAL sentinel
    _advance_via_path writes going forward -- neither is ever a real stage's ordinal, so "not
    found" is definitionally "complete" either way."""
    return ordinal not in _stage_by_ordinal(quest)


def _stage_chain(quest: dict) -> list[str]:
    """Walks `quest` from start_stage via each visited stage's own *first* path's "next",
    collecting the ordered chain of stage ids -- used only to compute !journal's "Stage X/Y"
    display (see quest_log). Well-defined for any currently-linear quest (every stage has at most
    one path); a stage only reachable via a second/later path, or a cycle, simply won't extend the
    chain past the point it diverges from a straight line -- an explicitly accepted limitation once
    real branching content exists (see the module docstring), not something this function tries to
    solve in general."""
    stages_by_id = _stages_by_id(quest)
    chain: list[str] = []
    seen: set[str] = set()
    stage_id = quest["start_stage"]
    while stage_id is not None and stage_id not in seen and stage_id in stages_by_id:
        seen.add(stage_id)
        chain.append(stage_id)
        stage = stages_by_id[stage_id]
        stage_id = stage["paths"][0].get("next") if stage["paths"] else None
    return chain


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

# kind -> item registry a path's "reward_item" can be drawn from -- same shape and grant-logic
# split (equipment is always stored, never auto-equipped -- see turn_in below; everything else
# gets add_inventory_item) as shop.py's and dreams.py's own REGISTRIES. housing.py deliberately is
# NOT imported here to add a
# "housing_item" kind: housing.py already imports this module (for its own item-id collision check
# against QUEST_ITEMS, mirroring the one directly above), so the reverse import would be circular.
# housing.py is the module that ends up able to see both, so -- same "module that can see both does
# the deferred wiring" shape as room_commands.COMMANDS (populated by bot.py well after rooms.py
# itself is loaded) -- it registers "housing_item" into this dict itself, at the bottom of its own
# module. validate_reward_item_kinds() below is what actually catches a bad reward_item_kind/
# reward_item pairing; _load_quests() above can't fully validate a "housing_item" reward inline,
# since this dict won't have that kind yet at quests.py's own import time.
# Values are zero-arg getters, not the registries themselves -- see npcs.SHOP_KINDS' own comment
# for why a captured `dungeon.MATERIALS` etc (this module's own QUEST_ITEMS included -- it's just
# as hot-reloadable as any other content registry) would silently go stale the moment a content
# edit landed through the admin panel with no restart.
REWARD_REGISTRIES = {
    "equipment": lambda: dungeon.EQUIPMENT,
    "material": lambda: dungeon.MATERIALS,
    "consumable": lambda: dungeon.CONSUMABLES,
    "quest_item": lambda: QUEST_ITEMS,
    "horse_clothes": lambda: horse_clothes.HORSE_CLOTHES,
}


def _validate_trigger(trigger: dict, context: str):
    """Shared by every quest's start_trigger and every path's trigger -- `context` is an
    f-string-ready label (e.g. "quests.json: quest 'foo' stage 'bar' path 0") prefixed onto every
    error, same convention as dungeon._validate_effects."""
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
    if "amount" in params and trigger["amount"] <= 0:
        raise ValueError(f"{context} trigger {trigger_type!r} amount must be > 0")
    if "main_class" in params and trigger["main_class"] not in dungeon.CLASSES:
        raise ValueError(f"{context} trigger references unknown class {trigger['main_class']!r}")
    if "subclass" in params and trigger["subclass"] not in dungeon.SUBCLASSES and trigger["subclass"] != dungeon.NO_SUBCLASS:
        raise ValueError(f"{context} trigger references unknown subclass {trigger['subclass']!r}")
    if "phase" in params and trigger["phase"] not in {p[0] for p in moon.PHASES}:
        raise ValueError(f"{context} trigger references unknown moon phase {trigger['phase']!r}")


def _load_quests(path: str = _QUESTS_PATH) -> dict[str, dict]:
    """A quest's "start_trigger" is validated exactly like a path's "trigger" (see
    TRIGGER_SCHEMAS/_validate_trigger) -- both use the same condition vocabulary, just checked at
    a different moment (npc_greet vs turn_in). Note an "achievement" trigger's "kind" isn't
    cross-checked against achievements.ACHIEVEMENTS here -- quests.py can't import achievements.py
    (achievements.py has no reason to import quests.py either, now that starting is checked lazily
    rather than pushed from an achievement-unlock call site, but the reverse direction still would
    be circular since achievements.py predates this and might grow other reasons to import it) --
    so a typo there just means the quest never auto-starts rather than a load-time crash; the
    admin panel's enum field is what actually prevents that typo at entry time.

    Each stage: "id" (referenced by a path's "next"), "ordinal" (permanent, never reused, never
    editable -- backs the durable progress flag), "prompt" (NPC dialogue shown while this stage is
    active), "journal_text" (the !journal objective line), "topic_label" (optional), "discuss_with"
    (a list of npc ids -- every one of them offers a topic button for this stage while it's the
    player's current one, not just this quest's own giver; possibly empty), and "paths" (a list,
    possibly empty for a dialogue-only terminal). Each path: "trigger" (what advances it),
    "on_complete_message", "next" (a stage id, or absent to end the quest), and either a currency
    "reward" or a "reward_item" (any REWARD_REGISTRIES kind, see turn_in -- an equipment one is
    always stored, never auto-equipped).

    Beyond field presence, this also enforces the stage GRAPH is well-formed: every stage id/
    ordinal is unique within the quest, start_stage names a real stage, next_ordinal exceeds every
    assigned ordinal (the admin editor's "never reuse an ordinal" invariant), every path's "next"
    names a real stage (no dangling edges), and -- unless the quest is explicitly inactive
    (quest.get("active", True) is False, same "still being authored" exemption dungeon._load_delves
    gives an inactive delve) -- every stage is reachable from start_stage (a plain BFS over the
    "next" edges, mirroring dungeon._load_delves' own reachability check)."""
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

        stage_ids: set[str] = set()
        ordinals: set[int] = set()
        for i, stage in enumerate(stages):
            context = f"quests.json: quest {quest_id!r} stage {i}"
            missing_stage = _REQUIRED_STAGE_FIELDS - stage.keys()
            if missing_stage:
                raise ValueError(f"{context} missing field(s): {sorted(missing_stage)}")
            stage_id = stage["id"]
            if not stage_id:
                raise ValueError(f"{context} has a blank id")
            if stage_id in stage_ids:
                raise ValueError(f"quests.json: quest {quest_id!r} has duplicate stage id {stage_id!r}")
            stage_ids.add(stage_id)
            ordinal = stage["ordinal"]
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
                raise ValueError(f"{context} (id {stage_id!r}) ordinal must be a non-negative integer")
            if ordinal in ordinals:
                raise ValueError(f"quests.json: quest {quest_id!r} has duplicate stage ordinal {ordinal!r}")
            ordinals.add(ordinal)
            for npc_id in stage["discuss_with"]:
                if npc_id not in npcs.NPCS:
                    raise ValueError(f"{context} (id {stage_id!r}) discuss_with references unknown npc {npc_id!r}")
            for j, path in enumerate(stage["paths"]):
                path_context = f"{context} (id {stage_id!r}) path {j}"
                missing_path = _REQUIRED_PATH_FIELDS - path.keys()
                if missing_path:
                    raise ValueError(f"{path_context} missing field(s): {sorted(missing_path)}")
                _validate_trigger(path["trigger"], path_context)
                reward_item_id = path.get("reward_item")
                reward_item_kind = path.get("reward_item_kind", "equipment")
                # Only the "equipment" kind (the original, and only, reward kind before
                # housing_item existed) is checked here -- REWARD_REGISTRIES doesn't have
                # "housing_item" registered yet at this point in module load order (see its own
                # comment above), so a housing_item reward is checked later instead, by
                # validate_reward_item_kinds().
                if reward_item_id and reward_item_kind == "equipment" and reward_item_id not in dungeon.EQUIPMENT:
                    raise ValueError(f"{path_context} reward_item {reward_item_id!r} not in dungeon.EQUIPMENT")

        if entry["start_stage"] not in stage_ids:
            raise ValueError(
                f"quests.json: quest {quest_id!r} start_stage {entry['start_stage']!r} is not a stage here"
            )

        next_ordinal = entry.get("next_ordinal")
        if next_ordinal is None:
            # Missing entirely (e.g. a hand-edited file) -- self-heal to one past the highest
            # assigned ordinal, same value the backfill migration / admin editor would have written.
            next_ordinal = (max(ordinals) if ordinals else -1) + 1
            entry["next_ordinal"] = next_ordinal
        if not isinstance(next_ordinal, int) or next_ordinal <= (max(ordinals) if ordinals else -1):
            raise ValueError(
                f"quests.json: quest {quest_id!r} next_ordinal must be greater than every stage ordinal"
            )

        # Dangling `next` check -- every path's target, if set, must be a real stage here.
        for i, stage in enumerate(stages):
            for j, path in enumerate(stage["paths"]):
                target = path.get("next")
                if target is not None and target not in stage_ids:
                    raise ValueError(
                        f"quests.json: quest {quest_id!r} stage {stage['id']!r} path {j} "
                        f"next {target!r} is not a stage here"
                    )

        # Reachability -- plain BFS from start_stage over every path's "next" edge, exempted for an
        # explicitly inactive (still being authored) quest, same exemption dungeon._load_delves
        # gives an inactive delve.
        if entry.get("active", True):
            stages_by_id_local = {s["id"]: s for s in stages}
            reachable = {entry["start_stage"]}
            frontier = [entry["start_stage"]]
            while frontier:
                current = frontier.pop()
                for path in stages_by_id_local[current]["paths"]:
                    target = path.get("next")
                    if target is not None and target not in reachable:
                        reachable.add(target)
                        frontier.append(target)
            unreachable = stage_ids - reachable
            if unreachable:
                raise ValueError(
                    f"quests.json: quest {quest_id!r} has unreachable stage(s): {sorted(unreachable)}"
                )

        quests_by_id[quest_id] = entry

    # Second pass: a quest_complete trigger references another quest by id, which might not have
    # been loaded yet at the point its own trigger was validated above (quests.json order doesn't
    # have to match reference order) -- checked here instead, once every quest id is known.
    for quest_id, entry in quests_by_id.items():
        triggers = [entry["start_trigger"]] + [
            path["trigger"] for stage in entry["stages"] for path in stage["paths"]
        ]
        for trigger in triggers:
            if trigger["type"] == "quest_complete" and trigger["quest_id"] not in quests_by_id:
                raise ValueError(
                    f"quests.json: quest {quest_id!r} has a quest_complete trigger referencing "
                    f"unknown quest {trigger['quest_id']!r}"
                )
    return quests_by_id


QUESTS_BY_ID = _load_quests()


def check_quest_problems(entry: dict, other_ids: set[str]) -> list[dict]:
    """Non-raising sibling of _load_quests' per-entry validation -- same rules (minus the
    reachability check, see below), but collects every problem found (instead of raising on the
    first) as {"stage_id", "path_index", "message"} dicts so the flowchart editor can highlight
    every broken stage/path at once rather than forcing one-fix-at-a-time. stage_id/path_index are
    None for a quest-level problem (e.g. a duplicate id against `other_ids` -- the currently-
    published quests this entry isn't itself). Deliberately omits the reachability check
    _load_quests enforces -- a draft is allowed to be arbitrarily broken mid-edit (see
    admin_server.py's quest_autosave_view); only Publish (which runs the real loader) enforces full
    reachability. Used for a draft; unlike _load_quests this never raises -- an empty return means
    "would pass Publish"."""
    problems: list[dict] = []

    def add(message: str, stage_id: str | None = None, path_index: int | None = None):
        problems.append({"stage_id": stage_id, "path_index": path_index, "message": message})

    entry_id = entry.get("id", "")
    if entry_id and entry_id in other_ids:
        add(f"id {entry_id!r} is already used by another quest")
    missing = _REQUIRED_QUEST_FIELDS - entry.keys()
    if missing:
        add(f"missing field(s): {sorted(missing)}")
    if entry.get("npc") and entry["npc"] not in npcs.NPCS:
        add(f"unknown npc {entry['npc']!r}")
    start_trigger = entry.get("start_trigger")
    if start_trigger is not None:
        try:
            _validate_trigger(start_trigger, "start_trigger")
        except ValueError as e:
            add(str(e))

    stages = entry.get("stages") or []
    if not stages:
        add("has no stages")
        return problems

    stage_ids: set[str] = set()
    ordinals: set[int] = set()
    for stage in stages:
        stage_id = stage.get("id")
        if not stage_id:
            add("a stage has no id")
            continue
        if stage_id in stage_ids:
            add(f"duplicate stage id {stage_id!r}", stage_id=stage_id)
        stage_ids.add(stage_id)
        ordinal = stage.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            add("ordinal must be a non-negative integer", stage_id=stage_id)
        elif ordinal in ordinals:
            add(f"duplicate stage ordinal {ordinal!r}", stage_id=stage_id)
        else:
            ordinals.add(ordinal)
        missing_stage = _REQUIRED_STAGE_FIELDS - stage.keys()
        if missing_stage:
            add(f"missing field(s): {sorted(missing_stage)}", stage_id=stage_id)
        for npc_id in stage.get("discuss_with") or []:
            if npc_id not in npcs.NPCS:
                add(f"discuss_with references unknown npc {npc_id!r}", stage_id=stage_id)
        for i, path in enumerate(stage.get("paths") or []):
            missing_path = _REQUIRED_PATH_FIELDS - path.keys()
            if missing_path:
                add(f"missing field(s): {sorted(missing_path)}", stage_id=stage_id, path_index=i)
                continue
            try:
                _validate_trigger(path["trigger"], "path trigger")
            except ValueError as e:
                add(str(e), stage_id=stage_id, path_index=i)
            reward_item_id = path.get("reward_item")
            reward_item_kind = path.get("reward_item_kind", "equipment")
            registry = REWARD_REGISTRIES.get(reward_item_kind)
            if reward_item_id and registry is not None and reward_item_id not in registry():
                add(f"reward_item {reward_item_id!r} not in {reward_item_kind}", stage_id=stage_id, path_index=i)
            elif reward_item_id and registry is None:
                add(f"unknown reward_item_kind {reward_item_kind!r}", stage_id=stage_id, path_index=i)

    start_stage = entry.get("start_stage")
    if start_stage and start_stage not in stage_ids:
        add(f"start_stage {start_stage!r} is not a stage here")

    for stage in stages:
        stage_id = stage.get("id")
        if not stage_id:
            continue
        for i, path in enumerate(stage.get("paths") or []):
            next_id = path.get("next")
            if next_id is not None and next_id not in stage_ids:
                add(f"next {next_id!r} is not a stage here", stage_id=stage_id, path_index=i)

    return problems


def load_quest_drafts() -> dict[str, dict]:
    if not os.path.exists(_QUEST_DRAFTS_PATH):
        return {}
    with open(_QUEST_DRAFTS_PATH) as f:
        return json.load(f)


def _write_quest_drafts(drafts: dict[str, dict]) -> None:
    with open(_QUEST_DRAFTS_PATH, "w") as f:
        json.dump(drafts, f, indent=2)


def save_quest_draft(entry: dict) -> None:
    drafts = load_quest_drafts()
    drafts[entry["id"]] = entry
    _write_quest_drafts(drafts)


def delete_quest_draft(item_id: str) -> None:
    """No-op if item_id isn't actually a draft -- callers (admin_server.py's quest_autosave_view/
    quest_publish_view) call this speculatively during id-rename/publish cleanup without checking
    existence first."""
    drafts = load_quest_drafts()
    if item_id in drafts:
        del drafts[item_id]
        _write_quest_drafts(drafts)


def validate_reward_item_kinds(quests_by_id: dict[str, dict] | None = None):
    """Called from bot.py once every REWARD_REGISTRIES-contributing module (including housing.py)
    is fully loaded -- see the REWARD_REGISTRIES comment above for why this can't happen inside
    _load_quests() itself. Raises loudly on a typo'd/unknown reward_item_kind or a reward_item id
    that doesn't exist in that kind's registry, rather than a KeyError the moment a player turns in
    the stage. `quests_by_id` -- see rooms.validate_command_keys for the same "defaults to the
    module's own registry, but admin_server.py's save flow passes in the freshly-loaded candidate
    instead" shape."""
    for quest in (QUESTS_BY_ID if quests_by_id is None else quests_by_id).values():
        for stage in quest["stages"]:
            for path in stage["paths"]:
                reward_item_id = path.get("reward_item")
                if not reward_item_id:
                    continue
                reward_item_kind = path.get("reward_item_kind", "equipment")
                registry = REWARD_REGISTRIES.get(reward_item_kind)
                if registry is None:
                    raise ValueError(
                        f"quests.json: quest {quest['id']!r} references unknown reward_item_kind {reward_item_kind!r}"
                    )
                if reward_item_id not in registry():
                    raise ValueError(
                        f"quests.json: quest {quest['id']!r} reward_item {reward_item_id!r} not in {reward_item_kind}"
                    )


def validate_shop_housing_items(npcs_by_id: dict[str, dict] | None = None):
    """Same deferred story as validate_reward_item_kinds, for an npc's "housing_item"-kind shop
    entries -- npcs.py's own SHOP_KINDS maps "housing_item" to None (same treatment as
    "quest_item") since it can't import housing.py either (housing.py already imports this module,
    which already imports npcs.py -- the reverse would be circular through this module). Unlike the
    existing quest_item shop-entry check a few lines below (which runs eagerly, right here at this
    module's own import time, since QUEST_ITEMS is already loaded by then), this one can't run
    eagerly -- housing.py hasn't loaded yet at that point -- so it's called later instead, from
    bot.py once housing.py has, and wired as a save-time extra_validator for the "npcs" content type
    (admin_schemas.py)."""
    housing_items = REWARD_REGISTRIES["housing_item"]()
    for npc in (npcs.NPCS if npcs_by_id is None else npcs_by_id).values():
        for i, shop_entry in enumerate(npc.get("shop") or []):
            if shop_entry["kind"] == "housing_item" and shop_entry["item_id"] not in housing_items:
                raise ValueError(
                    f"npcs.json: npc {npc['id']!r} shop entry {i} item_id {shop_entry['item_id']!r} "
                    f"not in housing_items"
                )


def validate_recipe_housing_items(recipes: dict[str, dict] | None = None):
    """Same deferred story as validate_shop_housing_items, for a "housing_item"-output recipe --
    dungeon._load_recipes can't check its output_id against HOUSING_ITEMS (dungeon.py can't import
    housing.py -- housing.py already imports dungeon.py, the reverse would be circular). Unlike
    validate_recipe_quest_items (called eagerly right below, against the live dungeon.RECIPES, since
    QUEST_ITEMS is already loaded by then), this can't run eagerly -- housing.py hasn't loaded yet
    at that point -- so it's called later instead, from bot.py once housing.py has, and wired as an
    additional save-time extra_validator for the "recipes" content type (admin_schemas.py).
    `recipes` defaults to the live dungeon.RECIPES, same "candidate override for admin save"
    shape as validate_recipe_quest_items."""
    housing_items = REWARD_REGISTRIES["housing_item"]()
    for recipe_id, entry in (dungeon.RECIPES if recipes is None else recipes).items():
        if entry["output_kind"] == "housing_item" and entry["output_id"] not in housing_items:
            raise ValueError(
                f"dungeon_recipes.json: recipe {recipe_id!r} output_id {entry['output_id']!r} not found in "
                f"HOUSING_ITEMS"
            )


def validate_monster_drop_housing_items(monsters: dict[str, dict] | None = None):
    """Same deferred story as validate_recipe_housing_items, for a monster's "housing_item"-kind
    drop -- dungeon._validate_monster_drops can't check that drop's item_id against HOUSING_ITEMS
    (dungeon.py can't import housing.py either, same circular-import story). Called later instead,
    from bot.py once housing.py has loaded, and wired as an additional save-time extra_validator
    for the "monsters" content type (admin_schemas.py). `monsters` defaults to the live
    dungeon.MONSTERS, same "candidate override for admin save" shape as the other two."""
    housing_items = REWARD_REGISTRIES["housing_item"]()
    for monster_id, entry in (dungeon.MONSTERS if monsters is None else monsters).items():
        for i, drop in enumerate(entry.get("drops") or []):
            if drop["kind"] == "housing_item" and drop["item_id"] not in housing_items:
                raise ValueError(
                    f"dungeon_monsters.json: monster {monster_id!r} drop {i} item_id {drop['item_id']!r} "
                    f"not found in HOUSING_ITEMS"
                )


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

# Same story again for a delve choice room's action "requires" (any trigger type) and its
# quest_item-kind cost's item_id -- dungeon.py's own loader treats both as opaque (it can't import
# this module either, for the same reason npcs.py can't), so they're cross-validated here too,
# right alongside the NPC checks above. kill_monster/craft_item are explicitly rejected as a delve
# gate -- both are *counted* trigger types whose progress is scoped to one quest stage's own
# counter flag, and a delve action has no quest/stage to scope that counter to.
for _delve in dungeon.DELVES.values():
    for _room in _delve["rooms"]:
        if _room["type"] != "choice":
            continue
        for _i, _action in enumerate(_room.get("actions", [])):
            _context = f"dungeon_delves.json: delve {_delve['id']!r} room {_room['id']!r} action {_i}"
            _requires = _action.get("requires")
            if _requires is not None:
                _validate_trigger(_requires, f"{_context} requires")
                if _requires["type"] in ("kill_monster", "craft_item"):
                    raise ValueError(
                        f"{_context} requires: {_requires['type']!r} can't be used as a delve gate "
                        f"(no quest to scope its counter to)"
                    )
            _cost = _action.get("cost") or {}
            if _cost.get("item_kind") == "quest_item" and _cost.get("item_id") not in QUEST_ITEMS:
                raise ValueError(f"{_context} cost references unknown quest item {_cost.get('item_id')!r}")
            for _outcome_key in ("on_success", "on_fail"):
                _outcome = _action.get(_outcome_key) or {}
                if _outcome.get("item_kind") == "quest_item" and _outcome.get("item_id") not in QUEST_ITEMS:
                    raise ValueError(
                        f"{_context} {_outcome_key} references unknown quest item {_outcome.get('item_id')!r}"
                    )

# Same story again for a monster drop's own optional "requires" (any trigger type) -- dungeon.py's
# _validate_monster_drops only checks it's a dict (same circular-import reason as everything else in
# this section), so the real shape check happens here, right alongside the delve-choice block above.
# Unlike housing_item drops (validate_monster_drop_housing_items), this needs nothing from housing.py
# -- TRIGGER_SCHEMAS/_validate_trigger are already available at this module's own import time -- so
# it's a plain inline check here rather than a separately-called deferred function.
for _monster in dungeon.MONSTERS.values():
    for _i, _drop in enumerate(_monster.get("drops") or []):
        _drop_requires = _drop.get("requires")
        if _drop_requires is not None:
            _validate_trigger(_drop_requires, f"dungeon_monsters.json: monster {_monster['id']!r} drop {_i} requires")

# A delve's own optional "unlock_trigger" (dungeon.py can't validate it -- same circular-import
# story as the "requires" block above) -- checked by delve_cmd (bot.py) so a hidden_until_discovered
# delve can't just be typed directly by id before its condition holds, not just hidden from the
# !delve list (dungeon.active_delves' own discovered_ids param handles that half).
for _delve in dungeon.DELVES.values():
    _unlock_trigger = _delve.get("unlock_trigger")
    if _unlock_trigger is not None:
        _validate_trigger(_unlock_trigger, f"dungeon_delves.json: delve {_delve['id']!r} unlock_trigger")

# rooms.py can't validate its own exits' optional "visible_trigger" either (same story again --
# rooms.py has no reason to import this module, and the reverse would be circular if it did), so
# it's cross-validated here too. This is the room-exit half of the same idea npcs.json's own
# visible_trigger already covers for NPC presence -- room_view.build_room_display calls
# trigger_satisfied the same way for both.
for _room in rooms.ROOMS.values():
    for _i, _exit in enumerate(_room["exits"]):
        _exit_trigger = _exit.get("visible_trigger")
        if _exit_trigger is not None:
            _validate_trigger(_exit_trigger, f"rooms.json: room {_room['id']!r} exit {_i} visible_trigger")


def validate_recipe_quest_items(recipes: dict[str, dict]):
    """dungeon._load_recipes can't check a "quest_item"-output recipe's output_id against
    QUEST_ITEMS itself (dungeon.py can't import this module -- circular), so it's deferred here,
    same story as visible_trigger/shop above. Run once at import time (right below, against the
    live dungeon.RECIPES) and again as a "recipes" save-time extra_validator (admin_schemas.py) so
    a bad edit through the admin panel is rejected before it ever reaches a player."""
    for recipe_id, entry in recipes.items():
        if entry["output_kind"] == "quest_item" and entry["output_id"] not in QUEST_ITEMS:
            raise ValueError(
                f"dungeon_recipes.json: recipe {recipe_id!r} output_id {entry['output_id']!r} not found in QUEST_ITEMS"
            )


validate_recipe_quest_items(dungeon.RECIPES)


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


async def _current_stage_matched_path(guild_id: int, user_id: int, quest: dict, stage: dict) -> dict | None:
    """The first path (in list order) on `stage` whose trigger is currently satisfied, or None --
    shared by quest_topic_state/quest_log and turn_in's own auto-pick resolution, so "is this stage
    ready to turn in, and via which path" is computed exactly one way. For every quest today (one path
    per stage) this is equivalent to the old single-trigger check; a future branching stage with
    several simultaneously-satisfiable paths picks whichever comes first in quests.json order."""
    for path in stage["paths"]:
        if await trigger_satisfied(guild_id, user_id, path["trigger"], quest_id=quest["id"], stage=stage["ordinal"]):
            return path
    return None


async def quest_log(guild_id: int, user_id: int) -> list[dict]:
    """Every quest this player has started (in quests.json order), each as {"quest_id", "name",
    "npc", "stage_index", "total_stages", "complete", "journal_text", "can_turn_in"}.
    "journal_text" is the objective-style line !journal shows (or the quest's complete_message
    once done) -- distinct from a stage's "prompt" (only ever what the NPC itself says, see
    quest_topic_state) because a journal entry reads like a task ("Give Kel a wooden horse
    carving") rather than dialogue. Backs !journal (journal_view.py, aliased as !quests); "name"
    (quests.json's own authored title) is what identifies each entry -- "npc" is only the giver,
    and multiple quests can share one NPC (e.g. the_goo), so npc alone can't tell two entries
    apart. can_turn_in is a hint only ("go talk to X") -- journal_view.py deliberately builds no
    turn-in button of its own; actually resolving which path and consuming its cost only ever
    happens via quest_topic_state/turn_in, triggered by talking to the NPC (npc_view.py).
    stage_index/total_stages are computed by walking the quest's first-path chain from start_stage
    (_stage_chain) -- well-defined for any currently-linear quest; see that helper's own docstring
    for the accepted limitation once real branching content exists."""
    entries = []
    for quest in QUESTS_BY_ID.values():
        ordinal = await _get_stage_ordinal(guild_id, user_id, quest["id"])
        if ordinal is None:
            continue
        stage = _stage_by_ordinal(quest).get(ordinal)
        complete = stage is None
        chain = _stage_chain(quest)
        total_stages = len(chain)
        if complete:
            journal_text = quest.get("complete_message", DEFAULT_COMPLETE_MESSAGE)
            can_turn_in = False
            stage_index = total_stages
        else:
            journal_text = stage["journal_text"]
            can_turn_in = await _current_stage_matched_path(guild_id, user_id, quest, stage) is not None
            stage_index = chain.index(stage["id"]) if stage["id"] in chain else 0
        entries.append({
            "quest_id": quest["id"],
            "name": quest["name"],
            "npc": quest["npc"],
            "stage_index": stage_index,
            "total_stages": total_stages,
            "complete": complete,
            "journal_text": journal_text,
            "can_turn_in": can_turn_in,
        })
    return entries


async def roll_item_drop(guild_id: int, user_id: int, room_id: str, monster_id: str) -> dict | None:
    """None most of the time. When it hits, adds one quest item to the player's inventory and
    returns it -- but only an item their *current* stage on some in-progress quest is actually
    waiting on (any turn_in_item path on that stage, not just a single one -- a stage can have more
    than one path today), so players not on that quest never see unrelated flavor items. A path
    whose trigger sets "drop_monster" only offers its item after killing that specific
    dungeon.MONSTERS id; room_id (a dungeon delve room id, unrelated to the casino-hub "room"
    concept elsewhere in this file) is accepted for symmetry with dungeon.roll_drops's call site
    (no quest item is room-gated, only monster-gated)."""
    candidates = []
    for quest in QUESTS_BY_ID.values():
        ordinal = await _get_stage_ordinal(guild_id, user_id, quest["id"])
        if ordinal is None:
            continue
        stage = _stage_by_ordinal(quest).get(ordinal)
        if stage is None:
            continue
        for path in stage["paths"]:
            trigger = path["trigger"]
            if trigger["type"] != "turn_in_item":
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
    character: dict | None = None,
) -> bool:
    """Whether a trigger condition currently holds. `quest_id`/`stage` are only needed for the two
    *counted* types (kill_monster/craft_item), whose progress is scoped to one quest stage's own
    counter (`stage` is that stage's *ordinal*, not any position in a list) -- every other type is
    fully self-contained and callable from anywhere, which is what lets NPC-presence and room-exit
    conditions (rooms.py) reuse this exact function and vocabulary instead of quests needing their
    own bespoke condition-checking. turn_in_item is checked against the player's inventory;
    achievement against personal_achievements; quest_complete against another quest's own flag
    (via _is_complete, so it means the same thing here as it does everywhere else in this module);
    flag_at_least against an arbitrary flag key -- all four are live state this function can just
    read, no counter needed. `character` is only needed for "class" (a dungeon character dict, e.g.
    from db.get_character or a live DelveSession/PartyMember) -- the one type that checks something
    about the caller's own build rather than persistent per-player state this function can fetch
    itself; every other type ignores it. pay_currency is a non-consuming "can they afford it"
    check, same relationship to its own actual deduction (turn_in()'s special-cased
    db.spend_currency call) as turn_in_item has to db.consume_inventory_item."""
    trigger_type = trigger["type"]
    if trigger_type == "turn_in_item":
        held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
        return held.get(trigger["item_id"], 0) > 0
    if trigger_type == "pay_currency":
        balance = await asyncio.to_thread(db.get_balance, guild_id, user_id)
        return balance >= trigger["amount"]
    if trigger_type == "achievement":
        earned = await asyncio.to_thread(db.get_user_personal_achievements, guild_id, user_id)
        return trigger["kind"] in earned
    if trigger_type == "quest_complete":
        target = QUESTS_BY_ID[trigger["quest_id"]]
        target_ordinal = await _get_stage_ordinal(guild_id, user_id, trigger["quest_id"])
        return target_ordinal is not None and _is_complete(target, target_ordinal)
    if trigger_type == "flag_at_least":
        value = await asyncio.to_thread(db.get_flag, guild_id, user_id, trigger["key"])
        return value >= trigger["value"]
    if trigger_type == "class":
        return (
            character is not None
            and character["main_class"] == trigger["main_class"]
            and (trigger.get("subclass") is None or character["subclass"] == trigger["subclass"])
        )
    if trigger_type == "moon_phase":
        return moon.current_phase()[0] == trigger["phase"]
    if trigger_type == "not_moon_phase":
        return moon.current_phase()[0] != trigger["phase"]
    # Counted types (kill_monster, craft_item) -- scoped to one quest stage's own counter flag.
    count = await asyncio.to_thread(db.get_flag, guild_id, user_id, _stage_counter_key(quest_id, stage))
    return count >= trigger["count"]


async def record_progress(guild_id: int, user_id: int, event_type: str, **event_data):
    """Called from wherever a countable game event happens (a monster kill, a successful craft) --
    bumps the flag counter for whichever checkpoint(s) a player is currently waiting on (a not-yet-
    started quest's start_trigger, or every one of an in-progress quest's current-stage paths) if
    they're a counted type matching event_type. This never starts or advances anything itself -- it
    only ever moves a counter closer to satisfied; _start_quest and _advance_via_path are only ever
    called from npc_greet / turn_in respectively, once the player actually visits the NPC (or
    !journal). No-ops for event types nothing is listening for (achievement isn't event-sourced at
    all -- see trigger_satisfied)."""
    matcher = _TRIGGER_MATCHERS.get(event_type)
    if matcher is None:
        return
    for quest in QUESTS_BY_ID.values():
        ordinal = await _get_stage_ordinal(guild_id, user_id, quest["id"])
        if ordinal is None:
            candidates = [(quest["start_trigger"], _START_STAGE)]
        else:
            stage = _stage_by_ordinal(quest).get(ordinal)
            if stage is None:
                continue  # already complete
            candidates = [(path["trigger"], ordinal) for path in stage["paths"]]
        for trigger, counter_ordinal in candidates:
            if trigger["type"] != event_type or not matcher(trigger, event_data):
                continue
            await asyncio.to_thread(
                db.increment_flag, guild_id, user_id, _stage_counter_key(quest["id"], counter_ordinal)
            )


async def _try_start_quest(guild_id: int, user_id: int, quest: dict) -> bool:
    """True (and actually starts it) if `quest` is active, isn't started yet, and its start_trigger
    is now satisfied. The one place "how does a quest start" lives -- see the module docstring for
    its two callers (npc_greet, npc-scoped, and check_new_quests, every quest at once). The
    active gate here (not anywhere else) is deliberate: it only ever blocks a *new* start, never an
    already-started player from continuing/turning in -- unlike a delve (whose active flag also
    gates entirely-ephemeral in-run state), a quest carries durable cross-session progress, and
    flipping a content flag should never strand someone already partway through."""
    if not quest.get("active", True):
        return False
    if await _get_stage_ordinal(guild_id, user_id, quest["id"]) is not None:
        return False
    if not await trigger_satisfied(
        guild_id, user_id, quest["start_trigger"], quest_id=quest["id"], stage=_START_STAGE,
    ):
        return False
    start_ordinal = _stages_by_id(quest)[quest["start_stage"]]["ordinal"]
    await _start_quest(guild_id, user_id, quest["id"], start_ordinal)
    return True


async def check_new_quests(guild_id: int, user_id: int) -> list[str]:
    """Starts every not-yet-started quest with "journal_startable" set whose start_trigger is now
    satisfied, regardless of which NPC it belongs to -- journal_view.py's !journal entry point for
    starting a quest without visiting anyone. journal_startable defaults False -- a quest only
    starts this way if it's explicitly opted in (quests.json/admin panel); every other quest still
    only ever starts by talking to its NPC (npc_greet, unaffected by this flag -- it always starts
    whatever it's scoped to, same as before). Returns the ids of whatever quests this call actually
    started, in quests.json order."""
    started = []
    for quest in QUESTS_BY_ID.values():
        if not quest.get("journal_startable", False):
            continue
        if await _try_start_quest(guild_id, user_id, quest):
            started.append(quest["id"])
    return started


def _topic_label(quest: dict, stage: dict | None) -> str:
    """A topic button's own text -- the current stage's own "topic_label" if the author set one
    (e.g. "🏕️ Ask about a place to stay"), else a generic computed default. `stage` is None for a
    completed quest's wrap-up topic (no current stage to draw a topic_label from)."""
    if stage is not None and stage.get("topic_label"):
        return stage["topic_label"]
    return f"💬 Ask about {quest['name']}" if stage is not None else f"💬 {quest['name']}"


async def quest_topic_state(guild_id: int, user_id: int, quest_id: str) -> dict | None:
    """None if this player hasn't started `quest_id` yet. Otherwise {"quest_id", "name", "label",
    "prompt", "can_turn_in", "item", "turn_in_label", "complete"} -- the one place "what does this
    quest currently look like to show" is computed, reused both by npc_greet's topic-list building
    and by npc_view.py's click-time refresh of an already-open topic (so a topic's displayed state
    is never resolved two different ways). "prompt" is the current stage's own dialogue, or the
    quest's complete_message once done; "label" is this topic's own button text (_topic_label) --
    included here too so a post-turn-in re-render of a still-open topic doesn't need a second
    lookup to relabel it."""
    quest = QUESTS_BY_ID[quest_id]
    ordinal = await _get_stage_ordinal(guild_id, user_id, quest_id)
    if ordinal is None:
        return None
    stage = _stage_by_ordinal(quest).get(ordinal)
    if stage is None:
        return {
            "quest_id": quest_id, "name": quest["name"], "label": _topic_label(quest, None),
            "prompt": quest.get("complete_message", DEFAULT_COMPLETE_MESSAGE),
            "can_turn_in": False, "item": None, "turn_in_label": None, "complete": True,
        }
    matched = await _current_stage_matched_path(guild_id, user_id, quest, stage)
    item = (
        QUEST_ITEMS[matched["trigger"]["item_id"]]
        if matched and matched["trigger"]["type"] == "turn_in_item" else None
    )
    return {
        "quest_id": quest_id, "name": quest["name"], "label": _topic_label(quest, stage),
        "prompt": stage["prompt"], "can_turn_in": matched is not None, "item": item,
        "turn_in_label": matched.get("turn_in_label") if matched else None, "complete": False,
    }


async def npc_greet(guild_id: int, user_id: int, npc_id: str) -> dict:
    """The entry point for opening a conversation with an NPC -- returns {"just_started":
    [quest_id, ...], "topics": [quest_topic_state(...) dict, ...]}.

    Starts a not-yet-started quest exactly like check_new_quests does (via the same
    _try_start_quest), just scoped to this one NPC's own quests (_quests_for_npc) instead of every
    quest -- talking to the giving NPC and opening !journal are equally valid ways to pick up a new
    quest. "just_started" lists whatever this call actually started, for a caller that wants to
    notify the player a new quest showed up.

    "topics" is every quest ANYWHERE (not just ones this npc gives) currently relevant to a
    conversation with npc_id: an in-progress quest whose current stage's "discuss_with" includes
    npc_id, or a quest THIS npc gives that's now complete (a wrap-up topic -- deliberately never
    discuss_with-gated, since a completed stage no longer exists to carry that list; matches the
    pre-existing "complete_message shows forever via the giving NPC" behavior)."""
    just_started = [
        quest["id"] for quest in _quests_for_npc(npc_id) if await _try_start_quest(guild_id, user_id, quest)
    ]
    topics = []
    for quest in QUESTS_BY_ID.values():
        ordinal = await _get_stage_ordinal(guild_id, user_id, quest["id"])
        if ordinal is None:
            continue
        stage = _stage_by_ordinal(quest).get(ordinal)
        if stage is not None:
            if npc_id not in stage["discuss_with"]:
                continue
        elif quest["npc"] != npc_id:
            continue
        topics.append(await quest_topic_state(guild_id, user_id, quest["id"]))
    return {"just_started": just_started, "topics": topics}


async def turn_in(guild_id: int, user_id: int, quest_id: str, path_index: int | None = None) -> dict:
    """Resolves this specific quest's current stage and, if one of its paths' triggers is
    satisfied, consumes its cost (if any) and advances via that path. Takes a quest_id rather than
    an npc_id -- an NPC can have more than one eligible quest active at once (see npc_greet), so
    "which quest this button turns in" has to be decided by whoever built that button, not
    re-resolved ambiguously here. `path_index`, if given, resolves one specific path explicitly
    (for a stage with more than one path open at once); omitted (every caller today omits it --
    every quest so far has exactly one path per stage), the first currently-satisfied path is
    picked automatically. Returns {"success", "message", "reward", "reward_item",
    "reward_item_kind", "quest_complete"} -- success is False (everything else None/0/False) if
    there's nothing to turn in. reward_item is the REWARD_REGISTRIES[reward_item_kind] dict if the
    resolved path grants one (None otherwise, kind defaults to "equipment" for backward
    compatibility) -- reward_item_kind is always reported alongside it so a caller can tell what
    it's showing. An "equipment" kind reward is always stored in equipment_inventory, never
    auto-equipped -- the player equips manually via !equipment. Every other kind is simply added to
    inventory. quest_complete is True exactly when the resolved path has no "next" (this transition
    ends the quest) -- kept in lockstep with _is_complete's own "resting state" definition of
    complete by construction, since _advance_via_path writes _COMPLETE_ORDINAL in exactly that
    case."""
    failure = {
        "success": False, "message": None, "reward": 0, "reward_item": None,
        "reward_item_kind": None, "quest_complete": False,
    }
    quest = QUESTS_BY_ID[quest_id]
    ordinal = await _get_stage_ordinal(guild_id, user_id, quest_id)
    if ordinal is None:
        return failure
    stage = _stage_by_ordinal(quest).get(ordinal)
    if stage is None:
        return failure  # already complete

    if path_index is not None:
        if not (0 <= path_index < len(stage["paths"])):
            return failure
        path = stage["paths"][path_index]
    else:
        path = await _current_stage_matched_path(guild_id, user_id, quest, stage)
        if path is None:
            return failure

    trigger = path["trigger"]
    if trigger["type"] == "turn_in_item":
        if not await asyncio.to_thread(db.consume_inventory_item, guild_id, user_id, trigger["item_id"]):
            return failure
    elif trigger["type"] == "pay_currency":
        status, _ = await asyncio.to_thread(db.spend_currency, guild_id, user_id, trigger["amount"])
        if status != "ok":
            return failure
    elif not await trigger_satisfied(guild_id, user_id, trigger, quest_id=quest_id, stage=ordinal):
        return failure

    advanced = await _advance_via_path(guild_id, user_id, quest, ordinal, path)
    if not advanced:
        # Lost a race (e.g. a double-clicked button) -- give back whatever was just consumed
        # rather than eat it.
        if trigger["type"] == "turn_in_item":
            await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, trigger["item_id"])
        elif trigger["type"] == "pay_currency":
            await asyncio.to_thread(db.update_balance, guild_id, user_id, trigger["amount"])
        return failure

    reward = path.get("reward", 0)
    if reward:
        await asyncio.to_thread(db.update_balance, guild_id, user_id, reward)

    reward_item, reward_item_kind = None, None
    reward_item_id = path.get("reward_item")
    if reward_item_id:
        reward_item_kind = path.get("reward_item_kind", "equipment")
        reward_item = REWARD_REGISTRIES[reward_item_kind]()[reward_item_id]
        if reward_item_kind == "equipment":
            await asyncio.to_thread(db.store_equipment_item, guild_id, user_id, reward_item_id)
        else:
            await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, reward_item_id, 1)

    return {
        "success": True, "message": path.get("on_complete_message"), "reward": reward,
        "reward_item": reward_item, "reward_item_kind": reward_item_kind,
        "quest_complete": path.get("next") is None,
    }
