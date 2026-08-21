# da-casino — architecture notes for Claude

This file captures conventions and hard-won design decisions that aren't obvious from reading the
code cold. Read this before making structural changes to the dungeon/quest/NPC/room systems or the
admin content editor — several of these were arrived at after getting it wrong once.

## Core philosophy: content is data, not code

Every piece of *content* (monsters, equipment, materials, consumables, recipes, skills, delves,
quest items, quests, NPCs) lives in a JSON file and is editable through the admin panel
(`admin_server.py`, password-gated, port 8787) without touching Python. Adding a new monster, a
new quest, a new NPC should never require a code change or a bot restart to author (a restart is
only needed to deploy *code* changes, not content edits — the admin panel hot-reloads JSON saves
into the live process immediately).

**The generic CRUD pattern** (`admin_schemas.py` + `admin_server.py`): one `CONTENT_TYPES` entry
per content type describes its JSON file, which module owns its in-memory registry, its real
loader function (reused as the save-time validator — write to a temp file, try loading it for
real, only replace the live file and hot-reload if that succeeds), and its field list. The
list/edit/save routes are entirely generic over this — a new content type is a schema entry, not
new routes or templates. Repeatable list fields (`effects`, `materials`, `tier_list`,
`quest_stages`) all use the same add/remove-row primitive (`_render_repeatable` + a `<template>`
with a `ROWIDX` placeholder the page script substitutes) and the same server-side "discover
whatever indices actually got submitted" parsing (regex over form keys, not a contiguous-from-0
assumption) — rows can be added/removed client-side in any order.

**One place each rule lives.** Cross-file references get validated at load time wherever the
validating module can safely import the thing being referenced without a circular import; where it
can't (e.g. `quests.py` importing `npcs.py` means `npcs.py` can't import `quests.py` back to
validate its own `visible_trigger`), the validation happens in the module that *can* see both,
immediately after both are loaded — see the bottom of `quests.py`. This is a recurring shape:
check whether the "obvious" direction would be circular before writing a validator.

## The trigger/flag system

`quests.TRIGGER_SCHEMAS` is a generic condition language — `turn_in_item`, `achievement`,
`kill_monster`, `craft_item`, `quest_complete`, `flag_at_least` — each a `{type, ...params}` dict
with `quests.trigger_satisfied()` as the one function that evaluates any of them. This is shared by
quest stage/start conditions *and* NPC presence conditions (`npcs.json`'s optional
`visible_trigger`, checked via `quests.npcs_present_in_room`). Adding a new trigger *type* is the
whole cost of a new condition kind (a `TRIGGER_SCHEMAS` entry + a case in `trigger_satisfied`); the
admin panel's trigger row UI is generic over whatever's in `TRIGGER_PARAM_NAMES`, so it needs no
changes for a new type.

State backing all of this is `db.py`'s generic `flags` table (`(guild_id, user_id, key) -> int`),
not a bespoke table per feature. A quest's stage lives at flag key `quest:<id>` (stored as
stage+1, since 0 must mean "not started" — see `quests._quest_flag_key`); a counted trigger's
progress lives at `quest:<id>:stage<N>:count`. Before inventing a new table for some new kind of
per-player state, check whether it's actually just a new flag key.

## Rooms/NPCs: the ethos that matters most

**NPCs are data** (`npcs.json` — id, name, room, sprite, greet message/achievement, optional
`visible_trigger`). Every hub's view loops over `quests.npcs_present_in_room(guild_id, user_id,
room_id)` uniformly — **no hub's Python may ever hardcode which NPC ids appear in it.** If you
catch yourself writing `TalkToNpcButton("kel", ...)` or similar with a literal npc id anywhere
outside `npcs.json` itself, that's the bug this section is warning about.

**Rooms are thin command wrappers — this is the rule that got violated once already, be careful
here.** A room's button for a command invokes that command with at most the arguments a *generic*
collector can gather:
- zero args (`hub_ui.NoArgButton`, optionally with `const_args` — a *fixed* value baked in at
  button-construction time, e.g. Ranch's Upgrade Facility button always passing `"buy"`)
- one player-typed integer via a modal (`hub_ui.AmountButton`/`AmountModal`, also `const_args`-capable)

That's it. **A room must never construct a bespoke picker (a `discord.ui.Select` sourced from
per-player DB data, or any other custom collection UI) as part of its own view.** If a command
needs a richer argument than "zero" or "one typed number" — e.g. `!train` picking from a dynamic
list of the player's own horses — the **command itself** (in `bot.py`) is what presents that UI,
by sending its own response with a view attached (`ctx.send(..., view=some_view)`), when called
with the argument missing. The room's button stays a plain `NoArgButton` regardless; it has no idea
`!train`'s response might contain a dropdown. See `ranch_view.build_train_horse_picker` +
`bot.py`'s `train_cmd`/`_train_horse` for the reference shape: the picker-building UI lives in the
`*_view.py` module (bot.py never constructs `discord.ui` components directly, ever — that
boundary is absolute), but it's *requested by* the command, not pre-built into the room.

Why this matters: a room's full button list (NPCs *and* commands) **is** JSON data —
`{"key": "train", "kind": "none"}` sitting in `rooms.json` next to Casino's game list (see below).
That only works because every room command is uniformly "invoke with 0 or 1 generically-collected
args." A bespoke per-room Select breaks that uniformity and can't be expressed as data without
inventing a whole second "kind" of room-command just for it — so it doesn't happen, full stop; the
command grows the smarts instead.

## Rooms are data too (`rooms.py`/`rooms.json`)

Every room — id, name, optional flavor `description`, `background_path`, `exits` (`[{room_id,
label}]`), `commands` (`[{key, kind, label, const_args?, modal_title?, input_label?,
closes_hub?}]`), optional `specialization` — is a `rooms.json` entry, editable through the admin
panel exactly like every other content type. `room_view.py`'s `RoomView`/`build_room_display` are
the *only* room-rendering code left — one generic implementation for every room, not one class per
hub. `RoomExitButton` handles all navigation uniformly (any room can reach any other room it has an
exit to); there's no more per-hub `_go_X`/`go_home`-closure threading anywhere.

`room_commands.py` holds `COMMANDS: dict = {}`, populated once by `bot.py` right after every
`@bot.command` is defined — `rooms.json`'s `commands[].key` references into this dict. It starts
out *empty* at `rooms.py`/`admin_schemas.py` import time (bot.py populates it much later in
execution order), which is why `rooms.py`'s own loader can't validate a command key itself —
`rooms.validate_command_keys()` is a deferred check bot.py runs once `COMMANDS` is real, and
`admin_server.py`'s save flow runs the same check (via `admin_schemas.py`'s `"rooms"` entry's
`extra_validators`) against every edit before committing it, so a typo'd key is rejected right there
instead of surfacing as a `KeyError` the moment a player clicks the resulting broken button. Same
two-step story for `specialization` against `room_view._SPECIALIZATIONS`.

**The `specialization` hook is the one documented exception** to "every room is 100% generic data."
A room's `specialization` (currently `casino`/`ranch`/`dungeon`) names a module in
`room_view._SPECIALIZATIONS` — a small, fixed, hardcoded dict, never dynamic — that may define
`extra_items(commands) -> list[discord.ui.Item]` and/or `async extra_embed_fields(guild_id,
user_id) -> list[(name, value, inline)]`, both optional. This exists because exactly one real case
(`casino_view.GameSelect`) can't be expressed as a `rooms.json` commands[] entry — it's one
`Select` standing in for 7 different commands, not "one button, zero-or-one collected args" — and
because some rooms need live per-player embed content (Ranch's horse roster, Dungeon's energy) a
generic room embed has no way to derive on its own. Neither hook ever builds bespoke
argument-collection UI; `GameSelect` still only ever invokes an existing command per option, same
as every plain room button. Don't add a third use of this hook without a similarly hard reason —
it's meant to stay rare.

## Operational

- The bot runs under systemd as `da-casino-bot.service`. Restart via `sudo systemctl restart
  da-casino-bot` to deploy *code* changes (content/JSON edits through the admin panel hot-reload
  without a restart). **Always ask before restarting, even mid-session** — a restart can interrupt
  a live game in progress, and this holds regardless of how the conversation has been going.
- Back up `casino.db` before any migration that touches its schema, and verify the migration
  against a copy before ever running it against the live file.
