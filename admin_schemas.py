"""The content-editor's schema registry: one entry per editable content type, describing its JSON
file, which registry it backs, and its fields. admin_server.py's list/edit/save routes are all
generic over this -- adding a future content type (as long as it follows dungeon.py's existing
"JSON list of dicts, each with an id" convention) means adding an entry here, not writing new
routes or templates.

Every content type carries a "category" (groups the sidebar/dashboard -- currently "Dungeon
Content" for the dungeon.py-backed registries, "Story" for quests.py's) and an "icon" emoji shown
next to its label everywhere it's linked, so the nav reads as a game's content categories rather
than a flat alphabetical list of JSON files.

Any field entry can also carry:
  - "group" -- a short label admin_server.py renders as a heading whenever it differs from the
    previous field's, so a form reads as sections ("Identity", "Stats", "Loot", ...) instead of
    one flat stack of same-weight boxes. Only used on plain scalar fields (str/int/text/enum/
    color/image) -- compound field types (effects, materials, trigger, ...) already render inside
    their own labeled <fieldset>, which is grouping enough on its own.
  - "hint" -- a one-line explanation shown as small print under that field's input, for a box
    whose meaning isn't obvious from its name alone (e.g. "rarity" is flavor-only and never read
    by game logic; a monster's "tier" only scales its XP reward now). Trigger
    params get their own per-param hints instead (TRIGGER_PARAM_HINTS), since the same flattened
    param row is reused across every trigger type.

Field types the generic form-builder knows how to render:
  - "str"    -- a single-line text input
  - "text"   -- a multi-line textarea (flavor text and the like)
  - "int"    -- a number input
  - "color"  -- a text input rendered as an HTML5 color picker
  - "enum"   -- a <select> sourced from a "choices" list. Pass a plain list for choices that are
    truly fixed (e.g. equipment slots); pass a zero-arg callable instead when the choices come from
    a hot-reloadable registry (e.g. room ids), so the list is read fresh at render time rather than
    frozen at admin_schemas.py's own import time -- a plain list of a mutable registry's keys goes
    stale the moment content is added after import, same class of bug as the room dropdown once had.
  - "stat_bonuses" -- equipment's fixed {hp, atk, def} sub-dict (each an optional number input;
    only non-zero ones are written, since that's what "no bonus in that stat" means in practice)
  - "effects"   -- a repeatable list of {type, ...params}. Every effect type across
    dungeon.EFFECT_PARAM_SCHEMAS only ever uses one of 3 param names (value/reduction/multiplier),
    so each row shows all 3 as optional inputs rather than needing per-type dynamic fields --
    dungeon._validate_effects (run at save time via the real loader) is what actually enforces
    which params a given type needs.
  - "materials" -- a repeatable list of {material_id, qty}, material_id a <select> sourced live
    from dungeon.MATERIALS.
  - "monster_drops" -- a monster's own explicit loot table: a repeatable list of {kind, item_id,
    chance}. kind is a fixed equipment/material <select>; item_id is a cascaded_id-shaped select
    scoped to that row (same "not a top-level field" reasoning as "shop_items" below, wired to the
    row's own kind select) sourced from dungeon.EQUIPMENT (quest_only items excluded -- those are
    only ever granted through a quest turn-in) or dungeon.MATERIALS; chance is a single 0-1 number
    input, rolled independently per row at kill time (dungeon.roll_drops).
  - "delve_rooms" -- a delve's rooms: a repeatable list of rooms, in order, room count implied by
    the list length. Each room is itself a repeatable list of monster <select> rows (each option
    labeled "Tier N — Name" so a large roster stays scannable) -- the only nested repeatable in
    this schema, a repeatable list inside a repeatable list. See admin_server._render_delve_room_row
    and _dynamic_script's wireRepeatAdd for how "+ Add" wiring stays correct at that extra depth.
  - "image" -- a file upload with a live preview (monsters' sprite_path, delves'
    background_path). Needs a "subdir" key (e.g. "dungeon/monsters") saying where under assets/
    an upload for this field lands, saved as <subdir>/<entry id>.<ext>. Leaving the file input
    blank on an edit keeps whatever image was already set -- uploading is optional, not a
    re-required field every save. See admin_server.py's _save_uploaded_image.
  - "cascaded_id" -- a <select> of item ids whose valid options depend on a sibling field's
    current value (recipes' output_id on output_kind, npcs.json shop rows' item_id on kind) --
    the *only* place across this schema an id gets typed free-text against "whatever registry the
    real loader happens to check it against" is the shop_items row-builder below, since every
    other id field is either a fixed single-registry "materials"-style select or one of these. Needs "cascade" (which entry in admin_server._cascade_options() supplies the live
    id->label choices, one dict per possible sibling value) and "cascade_from" (the sibling
    field's name). The paired enum field needs "cascades_to" set to that same cascade name so its
    <select> gets a data-cascade attribute the page script's wireCascadingSelects hooks onto to
    repopulate this field's options live when the sibling changes -- see
    admin_server._render_cascaded_select and its CASCADE_OPTIONS script data.
  - "shop_items" -- npcs.json's optional "shop": a repeatable list of {kind, item_id, price}. kind
    is a fixed equipment/material/consumable/quest_item <select> (npcs.SHOP_KINDS); item_id is a
    cascaded_id-shaped select scoped to that row (not a top-level field, so it can't use the
    "cascaded_id" field type above -- admin_server._render_shop_row builds it directly the same
    way, just wired to the row's own kind select instead of a sibling top-level field).
  - "trigger" -- a single quests.TRIGGER_SCHEMAS condition ({type, ...params}), used for a
    quest's top-level "start_trigger". Every type's params are flattened into one row (same idea
    as "effects") -- quests._validate_trigger (run at save time via the real loader) is what
    actually enforces which params a given type needs. See TRIGGER_PARAM_KINDS for how each
    param renders (a number input, or a <select> sourced from the right registry).
  - "quest_stages" -- a repeatable list of {prompt, trigger, on_complete_message, reward,
    reward_item}; each row's trigger portion is the same flattened rendering as the "trigger"
    field type above.
  - "room_exits" -- a repeatable list of {room_id, label}, room_id a <select> sourced live from
    rooms.ROOMS.
  - "room_commands" -- a repeatable list of {key, kind, label, const_args?, modal_title?,
    input_label?, closes_hub?}. key is a <select> sourced live from room_commands.COMMANDS (the
    real command keys bot.py has registered); kind is a fixed none/amount <select>, matching
    rooms._COMMAND_KINDS. const_args is one comma-separated text input, split into a list at parse
    time (same "flatten to one text input" idea as "materials"' qty column). modal_title/
    input_label only matter when kind is "amount" -- hidden client-side otherwise, same
    show-only-what's-relevant idea as a trigger row's per-type param visibility. closes_hub is a
    checkbox.

An "enum" field with "required": False gets a blank leading option (selectable, and the default
when no value is set) -- required ones don't, since a real select never needs to represent "no
value" and always defaults to its first real choice.

"effects", "materials", "monster_drops", "delve_rooms", and "quest_stages" all render as an
add/remove-able list (a "+ Add row" button clones a <template>, each row gets its own "Remove"
button) rather than padding the form with a fixed number of blank rows -- see admin_server.py's
_render_repeatable and its per-type row-builder helpers (_render_effect_row, _render_material_row,
_render_drop_row, _render_delve_room_row, _render_stage_row).

Every content type reuses its actual owning-module loader (`loader`) as the save-time validator --
see admin_server.py's save handler, which dispatches on each entry's `module` key (`dungeon` for
everything content-registry-shaped, `quests` for quests/quest_items). That's deliberate: there is
exactly one place each content type's rules live, whether they're being checked at bot startup or
from this editor.
"""

import achievements
import dungeon
import horse_clothes
import npcs
import quests
import room_commands
import room_view
import rooms

# Every trigger type across quests.TRIGGER_SCHEMAS only ever uses these param names -- unlike
# "effects" (whose params are all numeric), triggers mix string ids and numbers, so each param
# needs to know whether to render a number input or a <select> (and which registry backs it).
TRIGGER_TYPES = list(quests.TRIGGER_SCHEMAS.keys())
TRIGGER_PARAM_NAMES = sorted({p for required, optional in quests.TRIGGER_SCHEMAS.values() for p in required | optional})
TRIGGER_PARAM_KINDS = {
    "item_id": "quest_item", "drop_monster": "monster", "monster_id": "monster",
    "recipe_id": "recipe", "kind": "achievement", "quest_id": "quest",
    "count": "int", "tier": "int", "value": "int", "key": "str",
}
# One-line explanations shown under each trigger param box -- the params themselves (tier vs
# monster_id, drop_monster vs item_id) are easy to mix up out of context, especially since every
# trigger row shows every param that *any* type uses (see admin_server._render_trigger_inputs),
# only a handful of which apply once you've actually picked a type.
TRIGGER_PARAM_HINTS = {
    "item_id": "the quest item that must be turned in to advance this stage",
    "drop_monster": "only this monster can drop the item (blank = any monster can)",
    "monster_id": "must kill this specific monster (blank = any monster counts -- or use tier instead)",
    "tier": "any monster at this dungeon tier counts (blank = any monster -- or use monster_id instead)",
    "count": "how many kills or crafts are needed",
    "recipe_id": "must craft this specific recipe (blank = any recipe counts)",
    "kind": "the achievement that must already be earned",
    "quest_id": "the quest that must be fully complete (all its stages turned in)",
    "key": "advanced/escape-hatch use -- prefer one of the other trigger types above if it fits. "
           "Start typing to see keys already in use (e.g. \"quest:<id>\"), or type a new one if "
           "something else in the game is going to set it",
    "value": "the flag's value must be at least this",
}

DUNGEON_CONTENT = "Dungeon Content"
STORY_CONTENT = "Story"
RANCH_CONTENT = "Ranch"
# Sidebar/dashboard order: categories in this order, content types within a category in
# CONTENT_TYPES' own definition order (below).
CATEGORIES = [DUNGEON_CONTENT, STORY_CONTENT, RANCH_CONTENT]

CONTENT_TYPES = {
    "monsters": {
        "label": "Monsters",
        "category": DUNGEON_CONTENT,
        "icon": "👹",
        "json_path": "dungeon_monsters.json",
        "module": dungeon,
        "registry_attr": "MONSTERS",
        "loader": dungeon._load_monsters,
        "list_columns": ["id", "name", "tier", "hp", "atk", "def"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "tier", "type": "int", "required": True, "min": 1, "group": "Stats",
                "hint": "difficulty rating used only to scale XP-per-kill (XP_PER_TIER) and as an "
                        "optional filter on the kill_monster quest trigger -- no longer gates which "
                        "delve rooms this monster can appear in or what it drops (see Drops below)",
            },
            {"name": "hp", "type": "int", "required": True, "min": 0, "group": "Stats"},
            {"name": "atk", "type": "int", "required": True, "min": 0, "group": "Stats"},
            {"name": "def", "type": "int", "required": True, "min": 0, "group": "Stats"},
            {
                "name": "shape", "type": "str", "required": True, "group": "Appearance",
                "hint": "procedural token shape: circle, triangle, pentagon, or hexagon (anything else "
                        "falls back to an octagon, not an error) -- only used if there's no sprite_path",
            },
            {"name": "color", "type": "color", "required": True, "group": "Appearance"},
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {
                "name": "loot_min", "type": "int", "required": True, "min": 0, "group": "Loot",
                "hint": "currency dropped on a kill is randint(loot_min, loot_max)",
            },
            {"name": "loot_max", "type": "int", "required": True, "min": 0, "group": "Loot"},
            {
                "name": "drops", "type": "monster_drops", "required": False,
                "hint": "this monster's own explicit loot table -- each row is one equipment or "
                        "material item plus its own drop chance (0-1), rolled independently, so a "
                        "single kill can land any number of them",
            },
            {"name": "sprite_path", "type": "image", "required": False, "subdir": "dungeon/monsters"},
        ],
    },
    "equipment": {
        "label": "Equipment",
        "category": DUNGEON_CONTENT,
        "icon": "⚔️",
        "json_path": "dungeon_equipment.json",
        "module": dungeon,
        "registry_attr": "EQUIPMENT",
        "loader": dungeon._load_equipment,
        "list_columns": ["id", "name", "slot", "rarity"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "slot", "type": "enum", "required": True, "choices": list(dungeon.EQUIPMENT_SLOTS), "group": "Identity"},
            {
                "name": "rarity", "type": "str", "required": True, "group": "Drop Info",
                "hint": "flavor label only (e.g. \"common\", \"legendary\") -- not read by any game logic",
            },
            {"name": "stat_bonuses", "type": "stat_bonuses", "required": True},
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
        ],
    },
    "materials": {
        "label": "Materials",
        "category": DUNGEON_CONTENT,
        "icon": "⛏️",
        "json_path": "dungeon_materials.json",
        "module": dungeon,
        "registry_attr": "MATERIALS",
        "loader": dungeon._load_materials,
        "list_columns": ["id", "name", "rarity"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "rarity", "type": "str", "required": True, "group": "Drop Info",
                "hint": "flavor label only (e.g. \"common\", \"rare\") -- not read by any game logic",
            },
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
        ],
    },
    "consumables": {
        "label": "Consumables",
        "category": DUNGEON_CONTENT,
        "icon": "🧪",
        "json_path": "dungeon_consumables.json",
        "module": dungeon,
        "registry_attr": "CONSUMABLES",
        "loader": dungeon._load_consumables,
        "list_columns": ["id", "name"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "kind", "type": "str", "required": True, "default": "consumable", "group": "Identity",
                "hint": "must be exactly \"consumable\" -- the loader rejects anything else. Kept as an "
                        "explicit field rather than hardcoded for when a second kind of item exists.",
            },
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {"name": "effects", "type": "effects", "required": True},
        ],
    },
    "recipes": {
        "label": "Recipes",
        "category": DUNGEON_CONTENT,
        "icon": "📜",
        "json_path": "dungeon_recipes.json",
        "module": dungeon,
        "registry_attr": "RECIPES",
        "loader": dungeon._load_recipes,
        "list_columns": ["id", "name", "output_kind", "output_id"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "output_kind", "type": "enum", "required": True, "choices": list(dungeon.RECIPE_OUTPUT_KINDS),
                "group": "Output", "hint": "which registry output_id below is looked up in",
                "cascades_to": "recipe_output",
            },
            {
                "name": "output_id", "type": "cascaded_id", "required": True, "group": "Output",
                "cascade": "recipe_output", "cascade_from": "output_kind",
                "hint": "the equipment or consumable this recipe produces -- options populate once "
                        "output_kind (above) is chosen",
            },
            {"name": "materials", "type": "materials", "required": True},
            {
                "name": "currency_cost", "type": "int", "required": False, "min": 0, "default": 0, "group": "Cost",
                "hint": "currency required in addition to the materials above (0 = materials only)",
            },
            {"name": "flavor", "type": "text", "required": False, "group": "Flavor Text"},
        ],
    },
    "skills": {
        "label": "Skills",
        "category": DUNGEON_CONTENT,
        "icon": "✨",
        "json_path": "dungeon_skills.json",
        "module": dungeon,
        "registry_attr": "SKILLS",
        "loader": dungeon._load_skills,
        "list_columns": ["id", "main_class", "subclass", "unlock_level", "name"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "main_class", "type": "str", "required": True, "group": "Class",
                "hint": "a character's broad class (e.g. \"warrior\") -- must match how characters are built elsewhere",
            },
            {
                "name": "subclass", "type": "str", "required": True, "group": "Class",
                "hint": "the specific build within main_class this skill belongs to",
            },
            {
                "name": "unlock_level", "type": "int", "required": True, "min": 1, "group": "Class",
                "hint": "character level this unlocks automatically at -- no player choice involved",
            },
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {"name": "effects", "type": "effects", "required": True},
        ],
    },
    "delves": {
        "label": "Delves",
        "category": DUNGEON_CONTENT,
        "icon": "🗺️",
        "json_path": "dungeon_delves.json",
        "module": dungeon,
        "registry_attr": "DELVES",
        "loader": dungeon._load_delves,
        "list_columns": ["id", "name", "rooms"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {
                "name": "rooms", "type": "delve_rooms", "required": True,
                "hint": "one row per room, in order -- check off whichever monsters can show up "
                        "there; the room's monster is picked uniformly at random from the checked "
                        "set each time it's entered",
            },
            {"name": "background_path", "type": "image", "required": False, "subdir": "dungeon/backgrounds"},
        ],
    },
    "rooms": {
        "label": "Rooms",
        "category": STORY_CONTENT,
        "icon": "🚪",
        "json_path": "rooms.json",
        "module": rooms,
        "registry_attr": "ROOMS",
        "loader": rooms._load_rooms,
        # The loader alone can't catch a typo'd command key or specialization (see rooms.py's own
        # docstring for why -- the same import-ordering problem bot.py's startup call to
        # validate_command_keys/validate_specializations works around) -- these run against the
        # freshly-loaded candidate registry as an extra save-time check instead, so a bad edit is
        # rejected right here rather than crashing a player's click later.
        "extra_validators": [
            lambda new_registry: rooms.validate_command_keys(room_commands.COMMANDS.keys(), new_registry),
            lambda new_registry: rooms.validate_specializations(room_view._SPECIALIZATIONS.keys(), new_registry),
        ],
        "list_columns": ["id", "name", "specialization"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "description", "type": "text", "required": False, "group": "Identity",
                "hint": "optional flavor text shown under the title -- blank for none",
            },
            {
                "name": "specialization", "type": "enum", "required": False, "group": "Identity",
                "choices": sorted(room_view._SPECIALIZATIONS.keys()),
                "hint": "optional -- hooks in a room's one-off extra embed fields and/or components "
                        "(e.g. Casino's game picker, Ranch's horse roster) that a plain room can't "
                        "express as data. Leave blank for a room with no such extras.",
            },
            {"name": "background_path", "type": "image", "required": True, "subdir": "rooms"},
            {"name": "exits", "type": "room_exits", "required": True},
            {"name": "commands", "type": "room_commands", "required": True},
        ],
    },
    "quest_items": {
        "label": "Quest Items",
        "category": STORY_CONTENT,
        "icon": "🎒",
        "json_path": "quest_items.json",
        "module": quests,
        "registry_attr": "QUEST_ITEMS",
        "loader": quests._load_quest_items,
        "list_columns": ["id", "name", "emoji"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "emoji", "type": "str", "required": True, "group": "Identity"},
            {"name": "description", "type": "text", "required": True, "group": "Flavor Text"},
        ],
    },
    "npcs": {
        "label": "NPCs",
        "category": STORY_CONTENT,
        "icon": "🗣️",
        "json_path": "npcs.json",
        "module": npcs,
        "registry_attr": "NPCS",
        "loader": npcs._load_npcs,
        "list_columns": ["id", "name", "room", "greet_achievement"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "room", "type": "enum", "required": True, "group": "Identity",
                "choices": lambda: sorted(rooms.ROOMS.keys()),
                "hint": "which room's view adds this NPC's talk button",
            },
            {
                "name": "greet_message", "type": "text", "required": True, "group": "Dialogue",
                "hint": "shown when talked to and no quest with them is currently active",
            },
            {
                "name": "greet_achievement", "type": "enum", "required": False, "group": "Dialogue",
                "choices": [a["kind"] for a in achievements.ACHIEVEMENTS],
                "hint": "granted (once, idempotently) the first time this NPC is talked to -- optional",
            },
            {
                "name": "visible_trigger", "type": "trigger", "required": False,
                "hint": "optional -- if set, this NPC is only present in their room once this "
                        "condition holds (e.g. quest_complete). Absent means always present.",
            },
            {
                "name": "sprite_path", "type": "image", "required": False, "subdir": "npcs",
                "hint": "a portrait composited onto whatever banner they're shown against -- optional; "
                        "without one, the NPC is just implied to be part of the banner photo itself",
            },
            {
                "name": "shop", "type": "shop_items", "required": False,
                "hint": "optional -- any items listed here show a Shop button for this NPC. "
                        "item_id is looked up in the registry named by kind (equipment/material/"
                        "consumable/quest_item).",
            },
        ],
    },
    "quests": {
        "label": "Quests",
        "category": STORY_CONTENT,
        "icon": "📖",
        "json_path": "quests.json",
        "module": quests,
        "registry_attr": "QUESTS_BY_ID",
        "loader": quests._load_quests,
        "list_columns": ["id", "npc"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "npc", "type": "enum", "required": True, "choices": list(npcs.NPCS.keys()), "group": "Identity",
                "hint": "who this quest belongs to",
            },
            {"name": "start_trigger", "type": "trigger", "required": True},
            {
                "name": "complete_message", "type": "text", "required": False, "group": "Flavor Text",
                "hint": "shown once every stage below is complete (blank = a generic default message)",
            },
            {"name": "stages", "type": "quest_stages", "required": True},
        ],
    },
    "horse_clothes": {
        "label": "Horse Clothes",
        "category": RANCH_CONTENT,
        "icon": "👒",
        "json_path": "horse_clothes.json",
        "module": horse_clothes,
        "registry_attr": "HORSE_CLOTHES",
        "loader": horse_clothes._load_horse_clothes,
        "list_columns": ["id", "name", "slot"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "slot", "type": "enum", "required": True, "choices": list(horse_clothes.CLOTHES_SLOTS), "group": "Identity"},
            {
                "name": "image_path", "type": "image", "required": True, "subdir": "horses/horse_clothes",
                "hint": "composited directly onto the horse's coat sprite in race photos -- draw it "
                        "on a transparent 50x37 canvas aligned to assets/horses/*.png so it lines up "
                        "with no extra positioning",
            },
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
        ],
    },
}

EFFECT_TYPES = list(dungeon.EFFECT_PARAM_SCHEMAS.keys())
EFFECT_PARAM_NAMES = sorted({p for required, optional, _ in dungeon.EFFECT_PARAM_SCHEMAS.values() for p in required | optional})
# type -> sorted list of every param name that type actually uses -- every "effects" row shows all
# of EFFECT_PARAM_NAMES (value/reduction/multiplier) so one row-builder works for every type (see
# _render_effect_row), but only ever 0 or 1 of them actually applies to a given type -- this is
# what the page script hides the rest against once a row's type is picked (same idea as
# TRIGGER_PARAMS_BY_TYPE below, just for effects).
EFFECT_PARAMS_BY_TYPE = {
    effect_type: sorted(required | optional)
    for effect_type, (required, optional, _fraction) in dungeon.EFFECT_PARAM_SCHEMAS.items()
}
# One-line, plain-English explanation of what a type's one relevant param actually means in combat
# (dungeon_view.EFFECT_HANDLERS is what interprets these) -- every type here uses at most one
# param, so a single hint per type is enough context. Shown under the row's type picker and
# swapped live as the type changes, same spot TRIGGER_PARAM_HINTS's per-param hints occupy for a
# trigger row, just keyed by type instead of param since here the param *name* alone (multiplier?
# reduction? value?) doesn't say what it does.
EFFECT_TYPE_HINTS = {
    "damage_multiplier": "value: your attack's damage this hit is multiplied by this (e.g. 1.5 = +50% damage)",
    "heal_fraction": "value: fraction of your max HP restored (0-1, e.g. 0.3 = 30%)",
    "guard": "reduction: the monster's next hit is multiplied by this (0-1, e.g. 0.5 = -50% damage taken)",
    "lifesteal_fraction": "value: fraction of the damage you deal that's restored as HP (0-1)",
    "def_shred": "value: flat amount the monster's DEF is lowered by, for the rest of this fight",
    "extra_attack": "multiplier: optional damage multiplier for the bonus attack (blank = 1.0, a full extra hit)",
    "atk_buff": "value: flat ATK bonus for the rest of this fight",
    "def_buff": "value: flat DEF bonus for the rest of this fight",
}

SHOP_KINDS = list(npcs.SHOP_KINDS.keys())
