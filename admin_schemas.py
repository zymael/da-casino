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
    whose meaning isn't obvious from its name alone (e.g. materials' "rarity" is flavor-only and
    never read by game logic, unlike equipment's own "rarity", which is a fixed tier that scales
    stat generation -- see dungeon.EQUIPMENT_RARITIES). Trigger params get their own per-param
    hints instead (TRIGGER_PARAM_HINTS), since the same flattened param row is reused across every
    trigger type.

Field types the generic form-builder knows how to render:
  - "str"    -- a single-line text input
  - "text"   -- a multi-line textarea (flavor text and the like)
  - "int"    -- a number input
  - "float"  -- a number input allowing decimals (step="any"), for a genuine fractional scalar like
    a subclass's loot_mult -- "int" fields elsewhere stay ints on purpose (chip_cost, unlock_level,
    ...); this is only for a field that's actually meant to hold a fraction.
  - "color"  -- a text input rendered as an HTML5 color picker
  - "bool"   -- a checkbox. Unchecked is simply absent from the submitted form (never a "false"
    string), so _parse_field's "bool" case always writes a real True/False -- the one field type
    here that's never conditionally omitted the way a blank optional field normally would be.
  - "enum"   -- a <select> sourced from a "choices" list. Pass a plain list for choices that are
    truly fixed (e.g. equipment slots); pass a zero-arg callable instead when the choices come from
    a hot-reloadable registry (e.g. room ids), so the list is read fresh at render time rather than
    frozen at admin_schemas.py's own import time -- a plain list of a mutable registry's keys goes
    stale the moment content is added after import, same class of bug as the room dropdown once had.
    Each entry in `choices` is normally a bare string (the stored value doubles as its own displayed
    label); pass a `(value, label)` pair instead where the raw stored value alone wouldn't mean
    anything to whoever's editing (a skill's main_class showing "The Enforcer (Ace)" while still
    storing "fighter" -- see admin_server._render_field's "enum" case).
  - "effects"   -- a repeatable list of {type, ...params}. Every effect type across
    dungeon.EFFECT_PARAM_SCHEMAS only ever uses one of a handful of param names
    (value/reduction/multiplier/duration), so each row shows all of them as optional inputs rather
    than needing per-type dynamic fields -- dungeon._validate_effects (run at save time via the
    real loader) is what actually enforces which params a given type needs. Each row also carries
    an independent "chance" (0-1, blank = always fires) -- dungeon.resolve_cast_effects rolls it
    separately per effect at cast time, so two effects each with their own chance can both land,
    either one, or neither.
  - "effect_groups" -- skills/consumables' alternative to plain "effects", for choosing between
    MUTUALLY EXCLUSIVE alternatives ("50% this OR 50% that") rather than independent per-effect
    rolls. A repeatable list of groups, each a "chance" (a relative WEIGHT against sibling groups --
    NOT the same 0-1 probability an individual effect's own "chance" means, see
    dungeon._validate_effect_groups) plus its own nested "effects" repeatable. Exactly one group is
    picked (weighted) each cast (dungeon.resolve_cast_effects); a skill/consumable authors EITHER
    "effects" OR "effect_groups", never both -- dungeon._validate_effects_or_groups enforces this at
    save time. Shares the exact same live-odds-percentage display each group's weight already gets
    for a monster's own skills / a combat room's monster_groups (admin_server._render_effect_group_
    row reuses the generic updateGroupOdds, scoped to this field's own [data-group-odds] fieldset).
  - "equipment_effects" -- equipment's own effects list, one level richer than plain "effects":
    each row also carries a `trigger` (constant/on_use/on_hit) and, only when trigger is on_hit, a
    `chance` (0-1) -- see admin_server.py's _render_effect_row(include_trigger=True) and
    dungeon.py's module comment above _validate_equipment_effects for what each trigger means and
    which effect types each one allows. A `constant` effect is what a flat stat_bonuses dict used
    to be (dungeon.constant_stat_bonuses folds these back into {hp,atk,def,spatk,spdef} everywhere
    that used to read stat_bonuses directly); trigger/type-restriction validation happens at save
    time via dungeon._validate_equipment_effects, same "let the type dropdown show everything,
    catch a bad combination loudly at Save" choice this admin panel already makes for free-typed
    room/action references elsewhere.
  - "materials" -- a repeatable list of {material_id, qty}, material_id a <select> sourced live
    from dungeon.MATERIALS.
  - "monster_drops" -- a monster's own explicit loot table: a repeatable list of {kind, item_id,
    chance}. kind is a fixed equipment/material <select>; item_id is a cascaded_id-shaped select
    scoped to that row (same "not a top-level field" reasoning as "shop_items" below, wired to the
    row's own kind select) sourced from dungeon.EQUIPMENT (quest_only items excluded -- those are
    only ever granted through a quest turn-in) or dungeon.MATERIALS; chance is a single 0-1 number
    input, rolled independently per row at kill time (dungeon.roll_drops).
  - "delve_flowchart" -- a delve's rooms, authored as a visual flowchart rather than a stack of
    text-box rows: each room is a draggable box on an SVG-backed canvas (admin_server.py's
    _render_delve_flowchart/_render_room_box, plus the flowchart script in _dynamic_script), and a
    room-to-room connection (a combat room's own "next", or a choice action's on_success/on_fail)
    is made by dragging that connection's own handle on the source room's box onto the target
    room -- never typed, so a typo'd/dangling reference simply can't happen client-side (though
    dungeon._load_delves's reachability pass still re-checks everything at save time, since a
    hand-edited JSON file bypasses this UI entirely). Each room box also carries a flag icon that
    sets the delve's start_room, which is why that's no longer its own top-level field in this
    schema -- it's sourced from a single page-level hidden input the flowchart script owns.
    A room's own id/type and type-specific fields (combat: a nested monster-select repeatable;
    choice: prompt + a nested actions repeatable) live in a per-room detail panel that's shown for
    at most one room at a time (click a box to open it) -- this, not any change to what data a
    room can hold, is what actually replaced the old wall of always-visible text boxes. A choice
    room's actions (see "requires"/"cost"/"check"/"on_success"/"on_fail") reuse
    _render_trigger_inputs for requires and a real cascading item_kind->item_id select for cost,
    same as before; each action's own connector handle is rendered on its room's box (one "success"
    handle always, a second dashed "fail" handle only once that action has a check configured) --
    this is deliberately what makes a choice room's multi-action, no-check-required forking
    visible as multiple arrows, since that's the primary way a delve is meant to branch (see
    dungeon.py's module docstring), not a side effect of a skill check. Either room type can also
    carry one optional background image, saved as "<subdir>/<entry id>_room_<room id><ext>" (needs
    a "subdir" key, same as "image") -- a room with none falls back to the delve's own top-level
    background_path at render time (dungeon_view._room_background_path). A room's canvas position
    is written into a new top-level "layout" field on the delve (dict of room id -> {x, y}, see
    dungeon.py's module docstring) -- purely presentational, never read by game logic. Since this
    field mixes repeatables, drag state, and image uploads, its render and parse sides are all
    special-cased outside the usual _render_field/_parse_field dispatch (see
    admin_server._render_delve_flowchart/_render_room_box/_render_room_detail_panel/
    _render_action_row and _parse_delve_flowchart/_parse_actions) and _dynamic_script's
    wireRepeatAdd for how "+ Add Room"/"+ Add action" wiring stays correct at the nested depth.
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
  - "quest_flowchart" -- a quest's stages, authored as a visual flowchart, the same shape as
    "delve_flowchart" above: each stage is a draggable box, and a path's "next" (the graph edge to
    whichever stage it leads to, or nothing to end the quest) is made by dragging that path's own
    connector handle onto the target stage -- never typed, so a typo'd/dangling reference can't
    happen client-side (though quests._load_quests's reachability pass still re-checks everything
    at Publish time, since a hand-edited JSON file bypasses this UI entirely). Each stage box also
    carries a flag icon that sets the quest's start_stage, which is why that's no longer its own
    top-level field in this schema -- same "sourced from a single page-level hidden input the
    flowchart script owns" story as start_room. A stage's own id/prompt/journal_text/button_label
    and its nested "paths" repeatable live in a per-stage detail panel shown for at most one stage
    at a time (click a box to open it). Every stage is structurally the delve-choice-room shape --
    there's no combat/choice split for quests -- so a path's own connector handle is always exactly
    one (never a second dashed one; a path has no check/fail concept, only a trigger that either
    holds or doesn't). A path's own fields (in the stage's detail panel) reuse _render_trigger_inputs
    for its trigger (required here, unlike a delve action's optional "requires" -- a path with no
    trigger could never actually be taken) and the same "quest_reward" cascade
    _render_cascaded_select already uses for reward_item. journal_text is the objective line shown
    in !journal (quests.quest_log) -- distinct from prompt, which is only ever the NPC's own spoken
    dialogue, and kept at the stage level (not per-path) since it describes what's currently going
    on with this stage regardless of which path eventually resolves it. reward_item_kind picks
    which of quests.REWARD_REGISTRIES' kinds reward_item is looked up in (defaults to "equipment"
    if blank, for every quest authored before reward_item_kind existed). button_label is optional
    -- it's this stage's own "topic" button text inside an NPC's conversation flow (see
    npc_view.NpcConversationView/quests._topic_label), shown once this stage is discussable there;
    blank keeps a generic computed default ("💬 Ask about {quest name}"). turn_in_label is the same
    idea but for the *turn-in confirm* button one screen deeper, and lives on the path (not the
    stage) since different paths out of one stage can each want their own label -- blank keeps its
    own generic default ("Give X the Y" for a turn_in_item trigger, else "Turn in to X"). Which
    NPCs offer this stage as a topic at all is a separate nested repeatable, "discuss_with" (a list
    of npc ids, each row just one <select> sourced from npcs.NPCS -- see
    admin_server._render_discuss_with_row) -- not gated to this quest's own giver, so a stage can be
    made discussable with any NPC the story calls for; migrated content defaults every stage to
    just the giver, matching pre-existing behavior. A stage's canvas position is written into a new top-level
    "layout" field on the quest (dict of stage id -> {x, y}, exact analog of a delve's own
    "layout"); a path's own canvas position is stored directly on the path (it has no stable id of
    its own to key an external layout dict by, exactly like a delve action). "ordinal" (also on
    each stage) is never shown here at all -- it's an opaque, system-assigned integer the durable
    per-player progress flag is encoded against (see quests.py's own module docstring for why a
    stage needs this second identity alongside its editable "id"), carried through the form as a
    hidden input and never touched by hand. Since this field mixes repeatables and drag state
    (though, unlike delve_flowchart, never image uploads -- quest stages carry no images), its
    render and parse sides are all special-cased outside the usual _render_field/_parse_field
    dispatch (see admin_server._render_quest_flowchart/_render_stage_box/
    _render_stage_detail_panel/_render_path_row/_render_discuss_with_row and
    _parse_quest_flowchart/_parse_paths) and
    _dynamic_script's wireRepeatAdd for how "+ Add Stage"/"+ Add path" wiring stays correct at the
    nested depth.
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

"effects", "materials", "monster_drops", "delve_flowchart", and "quest_flowchart" all render as an
add/remove-able list (a "+ Add row" button clones a <template>, each row gets its own "Remove"
button) rather than padding the form with a fixed number of blank rows -- see admin_server.py's
_render_repeatable and its per-type row-builder helpers (_render_effect_row, _render_material_row,
_render_drop_row, _render_room_node, _render_stage_node).

Every content type reuses its actual owning-module loader (`loader`) as the save-time validator --
see admin_server.py's save handler, which dispatches on each entry's `module` key (`dungeon` for
everything content-registry-shaped, `quests` for quests/quest_items). That's deliberate: there is
exactly one place each content type's rules live, whether they're being checked at bot startup or
from this editor.
"""

import achievements
import dreams
import dungeon
import horse_clothes
import housing
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
    "count": "int", "value": "int", "key": "str", "amount": "int",
    "main_class": "main_class", "subclass": "subclass", "phase": "moon_phase",
}
# One-line explanations shown under each trigger param box -- the params themselves (tier vs
# monster_id, drop_monster vs item_id) are easy to mix up out of context, especially since every
# trigger row shows every param that *any* type uses (see admin_server._render_trigger_inputs),
# only a handful of which apply once you've actually picked a type.
TRIGGER_PARAM_HINTS = {
    "item_id": "the quest item that must be turned in to advance this stage",
    "drop_monster": "only this monster can drop the item (blank = any monster can)",
    "monster_id": "must kill this specific monster (blank = any monster counts)",
    "count": "how many kills or crafts are needed",
    "recipe_id": "must craft this specific recipe (blank = any recipe counts)",
    "kind": "the achievement that must already be earned",
    "quest_id": "the quest that must be fully complete (all its stages turned in)",
    "key": "advanced/escape-hatch use -- prefer one of the other trigger types above if it fits. "
           "Start typing to see keys already in use (e.g. \"quest:<id>\"), or type a new one if "
           "something else in the game is going to set it",
    "value": "the flag's value must be at least this",
    "amount": "how much currency must be paid to advance this stage",
    "main_class": "the player's dungeon class (e.g. mage) -- required for the \"class\" trigger",
    "subclass": "optional -- narrows to one specific subclass; blank means any subclass of main_class",
    "phase": "which of the 8 lunar phases (moon.py) must (moon_phase) or must not (not_moon_phase) "
              "currently be showing",
}

DUNGEON_CONTENT = "Dungeon Content"
STORY_CONTENT = "Story"
RANCH_CONTENT = "Ranch"
HOUSING_CONTENT = "Housing"
# Sidebar/dashboard order: categories in this order, content types within a category in
# CONTENT_TYPES' own definition order (below).
CATEGORIES = [DUNGEON_CONTENT, STORY_CONTENT, RANCH_CONTENT, HOUSING_CONTENT]

CONTENT_TYPES = {
    "monsters": {
        "label": "Monsters",
        "category": DUNGEON_CONTENT,
        "icon": "👹",
        "json_path": "dungeon_monsters.json",
        "module": dungeon,
        "registry_attr": "MONSTERS",
        "loader": dungeon._load_monsters,
        # dungeon._load_monsters can't check a "housing_item"-kind drop's item_id itself (see
        # dungeon.DROP_KINDS' own comment for why) -- deferred here as an extra save-time check,
        # same "rooms"/"recipes"/"npcs" pattern above. Renaming/removing a monster id that some
        # equipment's vs_monster_debuff still points at is caught the same way, in the opposite
        # direction from "equipment" above.
        "extra_validators": [
            lambda new_registry: quests.validate_monster_drop_housing_items(new_registry),
            lambda new_registry: dungeon.validate_vs_monster_debuffs(dungeon.EQUIPMENT, new_registry),
        ],
        "list_columns": ["id", "name", "intended_level", "hp", "atk", "def", "spatk", "spdef", "spd"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "hp", "type": "int", "required": True, "min": 0, "group": "Stats"},
            {"name": "atk", "type": "int", "required": True, "min": 0, "group": "Stats"},
            {"name": "def", "type": "int", "required": True, "min": 0, "group": "Stats"},
            {
                "name": "spatk", "type": "int", "required": True, "min": 0, "group": "Stats",
                "hint": "Special Attack -- used instead of atk when this monster's own skill is flagged special",
            },
            {
                "name": "spdef", "type": "int", "required": True, "min": 0, "group": "Stats",
                "hint": "Special Defense -- used instead of def against an attacker's special skills",
            },
            {"name": "spd", "type": "int", "required": True, "min": 0, "group": "Stats"},
            {
                "name": "intended_level", "type": "int", "required": False, "min": 1, "group": "Stats",
                "hint": "optional -- the player level this monster is meant to be a fair fight for. "
                        "Use \"Generate stats for level\" above to pre-fill hp/atk/def from a level, or "
                        "just label a monster you've already hand-tuned.",
            },
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
                "hint": "this monster's own explicit loot table -- each row is one equipment, "
                        "material, consumable, or housing item plus its own drop chance (0-1), "
                        "rolled independently, so a single kill can land any number of them",
            },
            {
                "name": "attack_chance", "type": "int", "required": False, "min": 0, "group": "Skills",
                "hint": "optional -- this monster's plain-attack WEIGHT against its own skills' "
                        "chances below (default 1, same units as those, not a 0-1 probability). Set "
                        "to 0 to make a monster that only ever uses its skills, never a plain attack, "
                        "once it has any.",
            },
            {
                "name": "skills", "type": "monster_skills", "required": False,
                "hint": "optional abilities this monster can use instead of a plain attack -- each "
                        "time it acts, one option (its plain attack, weighted by attack_chance above, "
                        "or one of these skills, weighted by its own chance) is picked at random. No "
                        "mana/cooldown -- a monster can reuse the same skill as often as it randomly "
                        "comes up.",
            },
            {"name": "sprite_path", "type": "image", "required": False, "subdir": "dungeon/monsters"},
            {
                "name": "sprite_scale", "type": "float", "required": False, "group": "Appearance",
                "hint": "optional multiplier (default 1.0) on this monster's own on-screen height. "
                        "Every sprite is normalized to the same height regardless of source "
                        "resolution, so a slender/narrow sprite (e.g. a humanoid standing straight) "
                        "reads as much smaller than a wide/blobby one at the same height -- bump "
                        "this on a sprite that looks too small next to its cohort instead of "
                        "re-exporting the art.",
            },
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
        # dungeon._load_equipment can't check a vs_monster_debuff's monster_id against MONSTERS
        # itself (EQUIPMENT loads before MONSTERS -- see dungeon.validate_vs_monster_debuffs' own
        # docstring), so it's deferred here as an extra save-time check, same
        # "rooms"/"recipes"/"npcs"/"classes" pattern elsewhere in this file.
        "extra_validators": [
            lambda new_registry: dungeon.validate_vs_monster_debuffs(new_registry, dungeon.MONSTERS),
        ],
        "list_columns": ["id", "name", "slot", "rarity", "effects", "base_value"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "slot", "type": "enum", "required": True, "choices": list(dungeon.EQUIPMENT_SLOTS), "group": "Identity"},
            {
                "name": "rarity", "type": "enum", "required": True, "choices": list(dungeon.EQUIPMENT_RARITIES),
                "group": "Drop Info",
                "hint": "scales \"Generate stats for level\" above (dungeon.RARITY_STAT_MULTIPLIERS) and "
                        "picks this item's colored-dot prefix everywhere it's displayed (dungeon.RARITY_EMOJI)",
            },
            {"name": "effects", "type": "equipment_effects", "required": True},
            {
                "name": "special", "type": "bool", "required": False, "default": False, "group": "Drop Info",
                "hint": "Physical (unchecked) rolls an On Use damage effect against ATK/DEF; Special "
                        "(checked) uses SpAtk/SpDef instead. Only matters if this item has a damage-shaped "
                        "On Use effect.",
            },
            {
                "name": "intended_level", "type": "int", "required": False, "min": 1, "group": "Drop Info",
                "hint": "optional -- the player level this item is meant for. Use \"Generate stats for "
                        "level\" above to pre-fill its constant effects from a level, or just label an "
                        "item you've already hand-tuned.",
            },
            {
                "name": "base_value", "type": "int", "required": True, "min": 0, "group": "Drop Info",
                "hint": "sell.py's basis for this item's sell price (half of this, rounded down) at any "
                        "NPC with \"buys_items\" checked",
            },
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {
                "name": "unsmashable_message", "type": "text", "required": False, "group": "Flavor Text",
                "hint": "optional -- if set, !smash refuses to destroy this item and shows this message "
                        "instead of removing it",
            },
            {
                "name": "vs_monster_debuff", "type": "vs_monster_debuff", "required": False, "group": "Drop Info",
                "hint": "optional -- when equipped, permanently shaves the listed stat(s) off this exact "
                        "monster the moment it's rolled into a fight (checked once at room-entry, not on "
                        "every hit). Leave the monster blank for an item with no monster-specific effect.",
            },
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
        "list_columns": ["id", "name", "rarity", "base_value"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "rarity", "type": "str", "required": True, "group": "Drop Info",
                "hint": "flavor label only (e.g. \"common\", \"rare\") -- not read by any game logic",
            },
            {
                "name": "base_value", "type": "int", "required": True, "min": 0, "group": "Drop Info",
                "hint": "sell.py's basis for this item's sell price (half of this, rounded down) at any "
                        "NPC with \"buys_items\" checked",
            },
            {
                "name": "garbage", "type": "bool", "required": False, "default": False, "group": "Drop Info",
                "hint": "if checked, smash.py can grant a random copy of this material as the byproduct "
                        "of destroying something with !smash",
            },
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {
                "name": "unsmashable_message", "type": "text", "required": False, "group": "Flavor Text",
                "hint": "optional -- if set, !smash refuses to destroy this item and shows this message "
                        "instead of removing it",
            },
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
        "list_columns": ["id", "name", "energy_restore", "effects", "effect_groups", "base_value"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "kind", "type": "str", "required": True, "default": "consumable", "group": "Identity",
                "hint": "must be exactly \"consumable\" -- the loader rejects anything else. Kept as an "
                        "explicit field rather than hardcoded for when a second kind of item exists.",
            },
            {
                "name": "base_value", "type": "int", "required": True, "min": 0, "group": "Identity",
                "hint": "sell.py's basis for this item's sell price (half of this, rounded down) at any "
                        "NPC with \"buys_items\" checked",
            },
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {
                "name": "unsmashable_message", "type": "text", "required": False, "group": "Flavor Text",
                "hint": "optional -- if set, !smash refuses to destroy this item and shows this message "
                        "instead of removing it",
            },
            {
                "name": "energy_restore", "type": "int", "required": False, "min": 1, "group": "Identity",
                "hint": "makes this a pure out-of-combat item, usable via !inventory's Use button "
                        "(not mid-delve) instead of effects/effect_groups below -- restores this much "
                        "energy, capped at db.ENERGY_CAP. Once per calendar day, shared across every "
                        "energy-restoring consumable a player owns (not tracked per item).",
            },
            {
                "name": "use_label", "type": "str", "required": False, "group": "Identity",
                "hint": "optional -- the verb !inventory's Use button shows for this item, as "
                        "'{use_label} {item name}' (e.g. 'Snort A bag of mystery powder'). Defaults "
                        "to 'Drink' when left blank.",
            },
            {
                "name": "effects", "type": "effects", "required": False,
                "hint": "fill in EITHER this OR effect_groups below, never both -- see effect_groups' "
                        "own hint for when you need that instead. Not needed at all for a pure "
                        "energy_restore item above. A plain heal_fraction entry here also makes this "
                        "item usable outside combat via !inventory's Use button, in addition to "
                        "mid-delve (dungeon.usable_outside_combat).",
            },
            {
                "name": "effect_groups", "type": "effect_groups", "required": False,
                "hint": "use INSTEAD of effects above for a \"50% this OR 50% that\" skill -- exactly "
                        "one group is chosen (weighted by its own chance) each cast. For \"50% chance "
                        "of X, independently also 75% chance of Y\" (could land both, either, or "
                        "neither), that's just two effects each with their own chance inside the plain "
                        "effects field above -- no effect_groups needed for that. Not usable outside "
                        "combat even with a heal_fraction inside a group -- see usable_outside_combat's "
                        "own docstring for why.",
            },
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
        # dungeon._load_recipes can't check a "quest_item"/"housing_item"-output recipe's output_id
        # itself (see its own comment for why) -- deferred here as an extra save-time check, same
        # "rooms" pattern above.
        "extra_validators": [
            lambda new_registry: quests.validate_recipe_quest_items(new_registry),
            lambda new_registry: quests.validate_recipe_housing_items(new_registry),
        ],
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
                "hint": "the item this recipe produces -- options populate once output_kind (above) is chosen",
            },
            {"name": "materials", "type": "materials", "required": True},
            {
                "name": "currency_cost", "type": "int", "required": False, "min": 0, "default": 0, "group": "Cost",
                "hint": "currency required in addition to the materials above (0 = materials only)",
            },
            {"name": "flavor", "type": "text", "required": False, "group": "Flavor Text"},
        ],
    },
    "classes": {
        "label": "Classes",
        "category": DUNGEON_CONTENT,
        "icon": "🎭",
        "json_path": "dungeon_classes.json",
        "module": dungeon,
        "registry_attr": "CLASSES",
        "loader": dungeon._load_classes,
        # Lowering a class's own chips below what an already-saved skill costs would otherwise only
        # be caught the next time dungeon_skills.json itself is saved (or, worse, at the next bot
        # restart) -- same "rooms"/"recipes"/"npcs" extra_validators pattern as elsewhere in this
        # file, see dungeon.validate_class_chip_costs' own docstring.
        "extra_validators": [
            lambda new_registry: dungeon.validate_class_chip_costs(new_registry, dungeon.SUBCLASSES),
        ],
        "list_columns": ["id", "display_name", "rank", "hp", "atk", "def", "spatk", "spdef", "chips", "speed"],
        "fields": [
            {
                "name": "id", "type": "str", "required": True, "group": "Identity",
                "hint": "must stay exactly one of: fighter, healer, mage, rogue -- the roster is "
                        "fixed (referenced by skills/characters/quests), only the other fields below "
                        "are editable",
            },
            {"name": "display_name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "rank", "type": "str", "required": True, "group": "Identity",
                "hint": "single-letter card rank (A/K/Q/J today) -- must be unique across all 4 classes",
            },
            {
                "name": "picker_blurb", "type": "str", "required": True, "group": "Flavor Text",
                "hint": "short one-line description shown on the !class picker",
            },
            {
                "name": "flavor", "type": "text", "required": True, "group": "Flavor Text",
                "hint": "longer narrative blurb shown in the !class rundown before a class is picked",
            },
            {"name": "hp", "type": "int", "required": True, "min": 1, "group": "Base Stats"},
            {"name": "atk", "type": "int", "required": True, "min": 0, "group": "Base Stats"},
            {"name": "def", "type": "int", "required": True, "min": 0, "group": "Base Stats"},
            {"name": "spatk", "type": "int", "required": True, "min": 0, "group": "Base Stats"},
            {"name": "spdef", "type": "int", "required": True, "min": 0, "group": "Base Stats"},
            {
                "name": "chips", "type": "int", "required": True, "min": 0, "group": "Base Stats",
                "hint": "max Chips pool before subclass modifiers -- lowering this can strand an "
                        "existing skill's chip_cost, checked at save time",
            },
            {
                "name": "speed", "type": "int", "required": True, "min": 0, "group": "Base Stats",
                "hint": "flat -- unlike every other base stat, speed has no level-up growth (see "
                        "the Leveling fields below). Only equipment/housing bonuses or an in-fight "
                        "buff/debuff ever move it from here.",
            },
            {
                "name": "level_hp_gain", "type": "int", "required": True, "min": 0, "group": "Leveling",
                "hint": "flat HP gained by this class on every level-up",
            },
            {"name": "level_atk_gain", "type": "int", "required": True, "min": 0, "group": "Leveling"},
            {"name": "level_def_gain", "type": "int", "required": True, "min": 0, "group": "Leveling"},
            {"name": "level_spatk_gain", "type": "int", "required": True, "min": 0, "group": "Leveling"},
            {"name": "level_spdef_gain", "type": "int", "required": True, "min": 0, "group": "Leveling"},
        ],
    },
    "subclasses": {
        "label": "Subclasses",
        "category": DUNGEON_CONTENT,
        "icon": "🃏",
        "json_path": "dungeon_subclasses.json",
        "module": dungeon,
        "registry_attr": "SUBCLASSES",
        "loader": dungeon._load_subclasses,
        "extra_validators": [
            lambda new_registry: dungeon.validate_class_chip_costs(dungeon.CLASSES, new_registry),
        ],
        "list_columns": ["id", "symbol", "archetype_label", "hp", "atk", "def", "spatk", "spdef", "chips", "speed", "loot_mult"],
        "fields": [
            {
                "name": "id", "type": "str", "required": True, "group": "Identity",
                "hint": "must stay exactly one of: clubs, spades, hearts, diamonds -- the roster is "
                        "fixed, only the other fields below are editable",
            },
            {"name": "symbol", "type": "str", "required": True, "group": "Identity", "hint": "suit glyph, e.g. ♣"},
            {
                "name": "archetype_label", "type": "str", "required": True, "group": "Identity",
                "hint": "short archetype tag, e.g. \"Brawler\"",
            },
            {
                "name": "picker_blurb", "type": "str", "required": True, "group": "Flavor Text",
                "hint": "short one-line description shown on the subclass picker",
            },
            {
                "name": "hp", "type": "int", "required": True, "group": "Stat Modifiers",
                "hint": "added on top of the class's own base stat -- can be negative",
            },
            {"name": "atk", "type": "int", "required": True, "group": "Stat Modifiers"},
            {"name": "def", "type": "int", "required": True, "group": "Stat Modifiers"},
            {"name": "spatk", "type": "int", "required": True, "group": "Stat Modifiers"},
            {"name": "spdef", "type": "int", "required": True, "group": "Stat Modifiers"},
            {
                "name": "chips", "type": "int", "required": True, "group": "Stat Modifiers",
                "hint": "lowering this can strand an existing skill's chip_cost, checked at save time",
            },
            {"name": "speed", "type": "int", "required": True, "group": "Stat Modifiers"},
            {
                "name": "loot_mult", "type": "float", "required": True, "group": "Stat Modifiers",
                "hint": "credit-roll multiplier, e.g. 1.25 for +25% loot",
            },
        ],
    },
    "class_builds": {
        "label": "Class Builds",
        "category": DUNGEON_CONTENT,
        "icon": "🏷️",
        "json_path": "dungeon_class_builds.json",
        "module": dungeon,
        "registry_attr": "CLASS_BUILDS",
        "loader": dungeon._load_class_builds,
        "list_columns": ["id", "main_class", "subclass", "display_name"],
        "fields": [
            {
                "name": "id", "type": "str", "required": True, "group": "Identity",
                "hint": "must be exactly \"{main_class}_{subclass}\" -- e.g. \"fighter_clubs\"",
            },
            {
                "name": "main_class", "type": "enum", "required": True, "group": "Identity",
                "choices": lambda: [(cid, e["display_name"]) for cid, e in dungeon.CLASSES.items()],
                "cascades_to": "class_build_subclass",
            },
            {
                "name": "subclass", "type": "cascaded_id", "required": True, "group": "Identity",
                "cascade": "class_build_subclass", "cascade_from": "main_class",
            },
            {
                "name": "display_name", "type": "str", "required": True, "group": "Identity",
                "hint": "the build's own name, e.g. \"The Muscle\" for fighter+clubs",
            },
        ],
    },
    "leveling": {
        "label": "Leveling",
        "category": DUNGEON_CONTENT,
        "icon": "📈",
        "json_path": "dungeon_leveling.json",
        "module": dungeon,
        "registry_attr": "LEVELING",
        "loader": dungeon._load_leveling,
        "list_columns": ["id", "xp_per_level"],
        "fields": [
            {
                "name": "id", "type": "str", "required": True, "group": "Identity",
                "hint": "must stay exactly \"global\" -- this is the one shared setting, not a per-class row",
            },
            {
                "name": "xp_per_level", "type": "int", "required": True, "min": 1, "group": "Pacing",
                "hint": "XP to advance from level N to N+1 is this number times N, shared by every class",
            },
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
        "list_columns": ["id", "main_class", "subclass", "unlock_level", "name", "chip_cost", "effects", "effect_groups"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "main_class", "type": "enum", "required": True, "group": "Class",
                # A callable, not a frozen list -- dungeon.CLASSES is hot-reloadable now (the
                # Classes content type below), so a plain list here would go stale the moment a
                # display_name gets edited, same class of bug the module docstring's "enum" entry
                # warns about for every other hot-reloadable choices list.
                "choices": lambda: [(cid, e["display_name"]) for cid, e in dungeon.CLASSES.items()],
                "cascades_to": "skill_subclass",
                "hint": "a character's broad class -- must match how characters are built elsewhere",
            },
            {
                "name": "subclass", "type": "cascaded_id", "required": True, "group": "Class",
                "cascade": "skill_subclass", "cascade_from": "main_class",
                "hint": "the specific build (suit) within main_class this skill belongs to -- shown "
                        "as that build's actual name and card (rank + suit), e.g. \"The Muscle (A♣)\"",
            },
            {
                "name": "unlock_level", "type": "int", "required": True, "min": 1, "group": "Class",
                "hint": "character level this unlocks automatically at -- no player choice involved",
            },
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {
                "name": "effects", "type": "effects", "required": False,
                "hint": "fill in EITHER this OR effect_groups below, never both -- see effect_groups' "
                        "own hint for when you need that instead",
            },
            {
                "name": "effect_groups", "type": "effect_groups", "required": False,
                "hint": "use INSTEAD of effects above for a \"50% this OR 50% that\" skill -- exactly "
                        "one group is chosen (weighted by its own chance) each cast. For \"50% chance "
                        "of X, independently also 75% chance of Y\" (could land both, either, or "
                        "neither), that's just two effects each with their own chance inside the plain "
                        "effects field above -- no effect_groups needed for that.",
            },
            {
                "name": "chip_cost", "type": "int", "required": True, "min": 1, "group": "Class",
                "hint": "Chips spent to cast this skill -- must not exceed the build's max Chips "
                        "(dungeon.CLASSES/SUBCLASSES) or it would be permanently unusable",
            },
            {
                "name": "special", "type": "bool", "required": False, "default": False, "group": "Class",
                "hint": "Physical (unchecked) rolls damage against ATK/DEF; Special (checked) uses "
                        "SpAtk/SpDef instead. Plain Attack is always Physical.",
            },
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
        # Presence of this key (not its content beyond these four names) is what admin_server.py
        # checks to decide whether a content type gets the draft/autosave/Publish flow instead of
        # a plain single Save button -- see FLOWCHART_FIELD_TYPES and every `"draft_publish" in
        # spec` check there. quests' own "quests" entry below carries the equivalent four.
        "draft_publish": {
            "save_draft": dungeon.save_delve_draft, "load_drafts": dungeon.load_delve_drafts,
            "delete_draft": dungeon.delete_delve_draft, "check_problems": dungeon.check_delve_problems,
        },
        "list_columns": ["id", "name", "active", "rooms"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "active", "type": "bool", "required": False, "default": True, "group": "Identity",
                "hint": "whether this delve is offered to players at all. Leave unchecked while "
                        "you're still building it out -- an inactive delve can be saved even if "
                        "some rooms aren't wired up yet or aren't reachable from the start room; "
                        "only an active delve has to be fully connected to save. Check this once "
                        "it's actually finished.",
            },
            {
                "name": "hidden_until_discovered", "type": "bool", "required": False, "default": False,
                "group": "Identity",
                "hint": "hides this delve from the no-arg !delve picker until a player has actually "
                        "committed to it once (via a room's own Delve button, const_args-pinned to "
                        "this id) -- typing the id directly is blocked the same way until "
                        "unlock_trigger (below) is satisfied. Leave unchecked for a normal, always-"
                        "listed delve.",
            },
            {
                "name": "unlock_trigger", "type": "trigger", "required": False, "group": "Identity",
                "hint": "optional -- gates whether !delve <id> works at all for this delve (e.g. "
                        "quest_complete), independent of hidden_until_discovered above (which only "
                        "controls the no-arg list). Pair with a room-exit's own visible_trigger "
                        "(rooms.json) using the same condition so a room stays physically "
                        "unreachable until this is satisfied too.",
            },
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {
                "name": "rooms", "type": "delve_flowchart", "required": True, "subdir": "dungeon/backgrounds",
                "hint": "drag rooms to arrange them, drag a room's own handle onto another room to "
                        "connect them, click a room to edit its fields, and click a room's flag "
                        "icon to make it the start room -- see the tooltips throughout for what "
                        "each control does. A combat room has one exit; a choice room's actions "
                        "each get their own exit, so 2+ actions already fork the path by player "
                        "choice alone -- a check (in an action's own detail fields) adds a second, "
                        "optional fork on top of that single action, for when a roll should also "
                        "matter.",
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
    "housing_items": {
        "label": "Housing Items",
        "category": HOUSING_CONTENT,
        "icon": "🛋️",
        "json_path": "housing_items.json",
        "module": housing,
        "registry_attr": "HOUSING_ITEMS",
        "loader": housing._load_housing_items,
        "list_columns": ["id", "name", "effect_type", "value", "stat", "base_value"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "emoji", "type": "str", "required": True, "group": "Identity"},
            {"name": "description", "type": "text", "required": True, "group": "Flavor Text"},
            {
                "name": "unsmashable_message", "type": "text", "required": False, "group": "Flavor Text",
                "hint": "optional -- if set, !smash refuses to destroy this item and shows this message "
                        "instead of removing it",
            },
            {
                "name": "image_path", "type": "image", "required": False, "subdir": "housing/items",
                "group": "Identity",
                "hint": "optional -- composited into this item's house grid cell (housing_render.py). "
                        "Without one, the item shows as a colored circle with its name's first letter "
                        "instead, so new items work immediately with no art required.",
            },
            {
                "name": "base_value", "type": "int", "required": True, "min": 0, "group": "Identity",
                "hint": "sell.py's basis for this item's sell price (half of this, rounded down) at any "
                        "NPC with \"buys_items\" checked -- NOT the same as \"value\" below, which is "
                        "this item's own effect magnitude",
            },
            {
                "name": "effect_type", "type": "enum", "required": True, "group": "Effect",
                "choices": lambda: sorted(housing.HOUSING_EFFECT_TYPES.keys()),
                "hint": "the passive bonus this item grants while placed in a house slot",
            },
            {
                "name": "value", "type": "int", "required": True, "group": "Effect",
                "hint": "a percent for dungeon_loot_bonus/dungeon_xp_bonus/ranch_training_bonus, a "
                        "flat point for stat_bonus, a flat amount for rest_energy_bonus/rest_gold_bonus/"
                        "luck_bonus",
            },
            {
                "name": "stat", "type": "enum", "required": False, "group": "Effect",
                "choices": list(housing.HOUSING_STATS),
                "hint": "only used (and required) when effect_type is stat_bonus -- which stat this "
                        "item boosts. Leave blank for every other effect_type.",
            },
            {
                "name": "use_label", "type": "str", "required": False, "group": "Use",
                "hint": "optional -- set together with use_message to give this item a 'Use' button "
                        "in !inventory reading '{use_label} {item name}' (e.g. 'Rub Delicious "
                        "Turrón'). Leave both blank for a purely passive item.",
            },
            {
                "name": "use_message", "type": "text", "required": False, "group": "Use",
                "hint": "optional -- the ephemeral message shown when a player presses this item's "
                        "use_label button. Required together with use_label; no other effect happens "
                        "yet.",
            },
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
    "dreams": {
        "label": "Dreams",
        "category": STORY_CONTENT,
        "icon": "💭",
        "json_path": "dreams.json",
        "module": dreams,
        "registry_attr": "DREAMS",
        "loader": dreams._load_dreams,
        # The loader itself is the whole "only one dream active at once" check -- see dreams.py's
        # module docstring for why this is self-contained (no extra_validators needed) unlike
        # rooms.json's command-key check.
        "list_columns": ["id", "name", "active"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "name", "type": "str", "required": True, "group": "Identity",
                "hint": "admin-facing label only -- never shown to players",
            },
            {
                "name": "message", "type": "text", "required": True, "group": "Content",
                "hint": "DM'd to a player the next time they !rest, while this dream is active",
            },
            {
                "name": "image_path", "type": "image", "required": False, "subdir": "dreams",
                "group": "Content",
                "hint": "optional -- attached to the DM alongside the message. A gif is attached "
                        "as-is (not re-encoded), so it animates normally.",
            },
            {
                "name": "active", "type": "bool", "required": False, "default": False, "group": "Content",
                "hint": "at most one dream may be active at a time -- saving a second active dream "
                        "is rejected. Deactivate the current one first, then activate the new one.",
            },
            {
                "name": "item_kind", "type": "enum", "required": False, "choices": list(npcs.SHOP_KINDS.keys()),
                "group": "Reward", "cascades_to": "dream_item",
                "hint": "optional -- an item granted alongside the message when this dream is "
                        "delivered. Leave blank (and item_id below blank too) for a message-only dream.",
            },
            {
                "name": "item_id", "type": "cascaded_id", "required": False, "group": "Reward",
                "cascade": "dream_item", "cascade_from": "item_kind",
                "hint": "which item -- options populate once item_kind (above) is chosen",
            },
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
        # npcs._load_npcs can't check a "housing_item"-kind shop entry's item_id itself (see
        # npcs.py's own SHOP_KINDS comment for why) -- deferred here as an extra save-time check,
        # same "rooms"/"recipes" pattern above.
        "extra_validators": [lambda new_registry: quests.validate_shop_housing_items(new_registry)],
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
                "name": "talk_label", "type": "str", "required": False, "group": "Dialogue",
                "hint": "optional -- overrides the Talk button's default \"Talk to X\" label.",
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
                        "consumable/quest_item/horse_clothes/housing_item).",
            },
            {
                "name": "buys_items", "type": "bool", "required": False, "default": False,
                "hint": "shows a Sell button for this NPC -- buys anything sellable (any kind but "
                        "quest_item) the player currently owns, at half its base_value (sell.py)",
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
        # Same draft/autosave/Publish flow as delves -- a quest is a stage GRAPH now too, and a
        # mid-edit graph is routinely, harmlessly broken (a stage not yet connected, no start_stage
        # picked yet) in exactly the way delves already needed this two-tier flow for. See the
        # "delves" entry above for what each of these four callables does.
        "draft_publish": {
            "save_draft": quests.save_quest_draft, "load_drafts": quests.load_quest_drafts,
            "delete_draft": quests.delete_quest_draft, "check_problems": quests.check_quest_problems,
        },
        # quests._load_quests only checks an "equipment"-kind reward_item at load time (see
        # REWARD_REGISTRIES' own comment for why the other kinds -- material, consumable,
        # horse_clothes, housing_item -- can't be fully validated there yet) -- the rest are caught
        # here instead, as an extra save-time check, same "rooms"/"recipes"/"npcs" pattern above.
        "extra_validators": [lambda new_registry: quests.validate_reward_item_kinds(new_registry)],
        "list_columns": ["id", "name", "npc"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "name", "type": "str", "required": True, "group": "Identity",
                "hint": "shown on !journal -- what identifies this quest, since multiple quests can share one npc",
            },
            {
                "name": "npc", "type": "enum", "required": True, "choices": list(npcs.NPCS.keys()), "group": "Identity",
                "hint": "who this quest belongs to",
            },
            {"name": "start_trigger", "type": "trigger", "required": True},
            {
                "name": "journal_startable", "type": "bool", "required": False, "default": False, "group": "Identity",
                "hint": "lets this quest start just by opening !journal, not only by talking to its npc",
            },
            {
                "name": "active", "type": "bool", "required": False, "default": True, "group": "Identity",
                "hint": "whether this quest can be newly started at all (talking to its npc or, if "
                        "journal_startable, opening !journal). Leave unchecked while you're still "
                        "building it out -- an inactive quest can be saved even if some stages "
                        "aren't wired up yet or aren't reachable from the start stage; only an "
                        "active quest has to be fully connected to save. A player already partway "
                        "through this quest is never affected either way -- this only ever gates a "
                        "brand new start.",
            },
            {
                "name": "complete_message", "type": "text", "required": False, "group": "Flavor Text",
                "hint": "shown once every stage below is complete (blank = a generic default message)",
            },
            {
                "name": "stages", "type": "quest_flowchart", "required": True,
                "hint": "drag stages to arrange them, drag a path's own handle onto another stage to "
                        "connect them, click a stage to edit its fields, and click a stage's flag "
                        "icon to make it the start stage -- see the tooltips throughout for what "
                        "each control does. A stage with two or more paths already forks the quest "
                        "by whichever path's trigger becomes satisfied first -- no separate "
                        "'branch' concept, same idea as a delve choice room's actions.",
            },
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
        "list_columns": ["id", "name", "slot", "base_value"],
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
            {
                "name": "base_value", "type": "int", "required": True, "min": 0, "group": "Identity",
                "hint": "sell.py's basis for this item's sell price (half of this, rounded down) at any "
                        "NPC with \"buys_items\" checked",
            },
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {
                "name": "unsmashable_message", "type": "text", "required": False, "group": "Flavor Text",
                "hint": "optional -- if set, !smash refuses to destroy this item and shows this message "
                        "instead of removing it",
            },
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
    "execute_multiplier": "base + scale x the target's missing HP fraction: e.g. base 1.0 / scale 1.7 = x1.0 at full HP, x2.7 against a nearly-dead target. Must be paired with a damage_multiplier/extra_attack in the same effects, and can't share a list with an aoe one.",
    "heal_fraction": "value: fraction of your max HP restored (0-1, e.g. 0.3 = 30%)",
    "guard": "reduction: the monster's next hit is multiplied by this (0-1, e.g. 0.5 = -50% damage taken)",
    "lifesteal_fraction": "value: fraction of the damage you deal that's restored as HP (0-1)",
    "def_shred": "value: flat amount the monster's DEF is lowered by, for the rest of this fight",
    "extra_attack": "multiplier: optional damage multiplier for the bonus attack (blank = 1.0, a full extra hit)",
    "atk_buff": "value: flat ATK bonus for the rest of this fight",
    "def_buff": "value: flat DEF bonus for the rest of this fight",
    "spatk_buff": "value: flat SpAtk bonus for the rest of this fight",
    "spdef_buff": "value: flat SpDef bonus for the rest of this fight",
    "hp_buff": "value: flat max HP bonus (current HP rises by the same amount) for the rest of this fight",
    "speed_buff": "value: flat Speed bonus for the rest of this fight -- faster turn order",
    "atk_debuff": "value: flat amount the target's ATK is lowered by, for the rest of this fight",
    "spatk_debuff": "value: flat amount the target's SpAtk is lowered by, for the rest of this fight",
    "spdef_debuff": "value: flat amount the target's SpDef is lowered by, for the rest of this fight",
    "speed_debuff": "value: flat amount the target's Speed is lowered by, for the rest of this fight -- slower turn order",
    "taunt": "value: flat threat gained against every monster in the fight right now -- higher means monsters are more likely to attack you instead of your allies. Player-only, party delves only.",
    "lower_threat": "value: flat threat lost against every monster in the fight right now -- lower means monsters are less likely to attack you. Player-only, party delves only.",
    "dodge_buff": "value: bonus chance (0-1) to fully avoid a Physical hit; duration: how many rounds it lasts",
    "resist_buff": "value: bonus chance (0-1) to fully avoid a Special hit; duration: how many rounds it lasts",
    "dot": "value: flat damage taken each round; duration: how many rounds it lasts",
    "hot": "value: fraction of max HP restored each round (0-1); duration: how many rounds it lasts",
    "sap": "duration: how many of the target's own turns it skips -- broken early the instant they take any damage (including from this same hit)",
    "stun": "duration: how many of the target's own turns it skips -- damage does NOT break it",
    "cleanse_dot": "No params -- removes any active damage-over-time from the caster (or the whole party with aoe)",
    "cleanse_cc": "No params -- removes any active Sap/Stun from the caster (or the whole party with aoe)",
}

# Compact type -> label used by the admin list view's stats-summary column and bot.py's !skills
# command alike (admin_server.format_effect) -- EFFECT_TYPE_HINTS above is the full sentence shown
# on the edit form; this is deliberately terse since several of these may need to fit in one table
# cell alongside every other effect the same item has.
EFFECT_SHORT_LABELS = {
    "damage_multiplier": "Dmg", "execute_multiplier": "Execute", "heal_fraction": "Heal", "guard": "Guard",
    "lifesteal_fraction": "Lifesteal", "def_shred": "DEF Shred", "extra_attack": "Extra Atk",
    "atk_buff": "ATK", "def_buff": "DEF", "spatk_buff": "SpATK", "spdef_buff": "SpDEF",
    "hp_buff": "HP", "speed_buff": "SPD",
    "atk_debuff": "ATK", "spatk_debuff": "SpATK", "spdef_debuff": "SpDEF", "speed_debuff": "SPD",
    "taunt": "Taunt", "lower_threat": "Threat", "dodge_buff": "Dodge", "resist_buff": "Resist",
    "dot": "DoT", "hot": "HoT", "sap": "Sap", "stun": "Stun",
    "cleanse_dot": "Cleanse DoT", "cleanse_cc": "Cleanse CC",
}

EQUIPMENT_EFFECT_TRIGGERS = list(dungeon.EQUIPMENT_EFFECT_TRIGGERS)

SHOP_KINDS = list(npcs.SHOP_KINDS.keys())
