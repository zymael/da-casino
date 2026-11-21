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
    to be (dungeon.constant_stat_bonuses folds these back into {hp,atk,def,spatk} everywhere
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
    flowchart script owns" story as start_room. A stage's own id/prompt/journal_text/topic_label
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
    if blank, for every quest authored before reward_item_kind existed). topic_label is optional
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
import all_in
import necromancy
import dreams
import dungeon
import horse_clothes
import housing
import manhattan
import npcs
import quests
import room_commands
import room_view
import rooms
import war

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
    "level": "int",
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
    "value": "flag_at_least: the flag must be at least this. flag_below: it must be under this",
    "amount": "how much currency must be paid to advance this stage",
    "main_class": "the player's dungeon class (e.g. mage) -- required for the \"class\" trigger",
    "subclass": "optional -- narrows to one specific subclass; blank means any subclass of main_class",
    "phase": "which of the 8 lunar phases (moon.py) must (moon_phase) or must not (not_moon_phase) "
              "currently be showing",
    "level": "the player's dungeon character level must be at least this (no character = never satisfied)",
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
            lambda new_registry: quests.validate_monster_drop_quest_items(new_registry),
            lambda new_registry: dungeon.validate_vs_monster_debuffs(dungeon.EQUIPMENT, new_registry),
        ],
        "list_columns": ["id", "name", "intended_level", "hp", "atk", "def", "spatk", "spd"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "hp", "type": "int", "required": True, "min": 0, "group": "Stats"},
            {"name": "atk", "type": "int", "required": True, "min": 0, "group": "Stats"},
            {
                "name": "def", "type": "int", "required": True, "min": 0, "group": "Stats",
                "hint": "Defense -- mitigates every incoming hit, physical or magic alike",
            },
            {
                "name": "spatk", "type": "int", "required": True, "min": 0, "group": "Stats",
                "hint": "Magic -- used instead of atk when this monster's own skill is flagged special, "
                        "and fuels Resist against status effects and debuffs aimed at this monster",
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
            {
                "name": "sprite_overlay", "type": "bool", "required": False, "default": False, "group": "Appearance",
                "hint": "draw the sprite as a full-screen overlay instead of a trimmed, centered cutout: "
                        "it's laid over the room exactly like the background (same size, same crop), so "
                        "art painted at the background's own size lines up perfectly. Meant for a boss "
                        "fought alone. sprite_scale is ignored.",
            },
            {
                # Own subdir: uploads are saved as <subdir>/<entry_id><ext>, so sharing
                # dungeon/monsters with sprite_path would overwrite the sprite itself.
                "name": "card_icon_path", "type": "image", "required": False, "subdir": "dungeon/monster_cards",
                "group": "Appearance",
                "hint": "optional art for this monster's turn-order card. Without it the card shows a "
                        "shrunk copy of sprite_path, which suits a normal cutout sprite; set this for a "
                        "sprite_overlay monster (its full-frame art can't shrink to a card) or any "
                        "sprite that reads poorly that small.",
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
                "hint": "Physical (unchecked) rolls an On Use damage effect against ATK; Special "
                        "(checked) uses Magic instead. Only matters if this item has a damage-shaped "
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
                "name": "unsellable", "type": "bool", "required": False, "default": False, "group": "Drop Info",
                "hint": "if checked, no NPC will buy this item (it never shows up in the sell popup)",
            },
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
            {
                "name": "stat_multiplier", "type": "float", "required": False, "group": "Drop Info",
                "hint": "optional -- multiplies ALL FIVE stats (hp/atk/def/spatk/speed) by this once "
                        "equipped, applied after every flat bonus above (0.01 = -99%). Floored at 1 per "
                        "stat, never 0 or negative. Leave blank for a normal item with no multiplier.",
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
            {
                "name": "pizza_weight", "type": "int", "required": False, "min": 0, "group": "Drop Info",
                "hint": "if above 0, this is a pizza leftover: !pizza can hand it to anyone who owns a "
                        "pizza_leftovers house item (the Necrognamicrumb). Higher = more common, relative "
                        "to the other leftovers' weights",
            },
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
            {
                "name": "unsellable", "type": "bool", "required": False, "default": False, "group": "Drop Info",
                "hint": "if checked, no NPC will buy this item (it never shows up in the sell popup)",
            },
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
                "name": "unsellable", "type": "bool", "required": False, "default": False, "group": "Drop Info",
                "hint": "if checked, no NPC will buy this item (it never shows up in the sell popup)",
            },
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
                "name": "station", "type": "enum", "required": False, "choices": list(dungeon.RECIPE_STATIONS),
                "group": "Output",
                "hint": "optional -- if set, this recipe can ONLY be made at that station (oven = !bake / "
                        "the Bake button at home, for whoever owns a house item with the same station), "
                        "never through !craft",
            },
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
        "list_columns": ["id", "display_name", "rank", "hp", "atk", "def", "spatk", "chips", "speed"],
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
            {
                "name": "resource_help", "type": "text", "required": False, "group": "Flavor Text",
                "hint": "optional: explains this class's combat resource (Charge/Grudge/Comps/Tells) and each "
                        "suit's twist on it, shown in !combathelp to players of this class",
            },
            {"name": "hp", "type": "int", "required": True, "min": 1, "group": "Base Stats"},
            {"name": "atk", "type": "int", "required": True, "min": 0, "group": "Base Stats"},
            {"name": "def", "type": "int", "required": True, "min": 0, "group": "Base Stats"},
            {"name": "spatk", "type": "int", "required": True, "min": 0, "group": "Base Stats"},
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
        "list_columns": ["id", "symbol", "archetype_label", "hp", "atk", "def", "spatk", "chips", "speed", "loot_mult"],
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
        "list_columns": ["id", "main_class", "subclass", "display_name", "picker_blurb"],
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
            {
                "name": "picker_blurb", "type": "str", "required": True, "group": "Flavor Text",
                "hint": "short role line shown in the subclass picker and as its dropdown description "
                        "(max 100 characters), e.g. \"Bruiser tank: takes the hits, swings back harder\"",
            },
            {"name": "flavor", "type": "text", "required": True, "group": "Flavor Text"},
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
        "extra_validators": [
            lambda new_registry: quests.validate_subclass_unlock_quest(leveling=new_registry),
        ],
        "list_columns": ["id", "xp_per_level", "max_level", "subclass_unlock_level", "subclass_max_level"],
        "fields": [
            {
                "name": "id", "type": "str", "required": True, "group": "Identity",
                "hint": "must stay exactly \"global\" -- this is the one shared setting, not a per-class row",
            },
            {
                "name": "xp_per_level", "type": "int", "required": True, "min": 1, "group": "Pacing",
                "hint": "XP to advance from level N to N+1 is this number times N, shared by every class",
            },
            {
                "name": "max_level", "type": "int", "required": True, "min": 1, "group": "Pacing",
                "hint": "characters stop gaining XP/levels once they hit this -- raise it any time to lift the cap",
            },
            {
                "name": "subclass_unlock_level", "type": "int", "required": True, "min": 1, "group": "Subclasses",
                "hint": "level a character must reach before !class offers the subclass picker -- subclass "
                        "skills below this level could never be used, so keep their unlock_level at or above it",
            },
            {
                "name": "subclass_max_level", "type": "int", "required": True, "min": 1, "group": "Subclasses",
                "hint": "level cap for a character who has picked a subclass (must be >= max_level)",
            },
            {
                "name": "subclass_unlock_quest", "type": "str", "required": False, "group": "Subclasses",
                "hint": "quest id that must be complete before !class offers the subclass picker -- "
                        "blank keeps subclass picking closed for everyone",
            },
            {
                "name": "subclass_change_base_cost", "type": "int", "required": False, "min": 0, "group": "Subclasses",
                "hint": "price of a character's 2nd subclass change (the 1st is free) -- doubles with each change after",
            },
            {
                "name": "subclass_change_max_cost", "type": "int", "required": False, "min": 0, "group": "Subclasses",
                "hint": "the doubling stops here -- every change past the cap costs exactly this",
            },
        ],
    },
    "all_in": {
        "label": "All In Mode",
        "category": DUNGEON_CONTENT,
        "icon": "♠️",
        "json_path": "dungeon_all_in.json",
        "module": all_in,
        "registry_attr": "SETTINGS",
        "loader": all_in._load_all_in,
        "list_columns": ["id", "max_tier", "monster_stat_per_tier", "gold_per_tier", "affixes_per_tier"],
        "fields": [
            {
                "name": "id", "type": "str", "required": True, "group": "Identity",
                "hint": "must stay exactly \"global\" -- one shared setting. All In always scales to the "
                        "Leveling page's subclass_max_level, so raising the cap there moves All In with it",
            },
            {
                "name": "max_tier", "type": "int", "required": True, "min": 1, "group": "Tiers",
                "hint": "highest All In tier. Clearing a delve normally unlocks tier 1; clearing tier N unlocks N+1",
            },
            {
                "name": "benchmark_delve", "type": "str", "required": False, "group": "Tiers",
                "hint": "delve id whose boss fight every other delve's All In is evened out to (each delve's "
                        "monsters get scaled so its boss fight is as tough as this one's). Blank = no evening-out. "
                        "Fine-tune a single delve with its own all_in_difficulty on the Delves page",
            },
            {
                "name": "extra_monster_start_tier", "type": "int", "required": True, "min": 0, "group": "Monsters",
                "hint": "first tier where every fight gets an extra monster (bosses get trash minions, never "
                        "a second boss). Solo and party alike, which is what pushes the top tiers toward parties",
            },
            {
                "name": "extra_monster_every_tiers", "type": "int", "required": True, "min": 1, "group": "Monsters",
                "hint": "one more extra monster every this many tiers after the start (start 3, every 2 = "
                        "tiers 3-4 get 1, tier 5 gets 2)",
            },
            {
                "name": "monster_stat_per_tier", "type": "float", "required": True, "group": "Monsters",
                "hint": "extra HP/ATK/DEF/Magic per tier on top of scaling to the cap (0.4 = +40% per tier). "
                        "Never applies to speed",
            },
            {
                "name": "monster_scaling_exponent", "type": "float", "required": True, "group": "Monsters",
                "hint": "damps how hard low-level monsters are lifted to the cap (1.0 = straight up the stat curve, "
                        "lower = gentler). Early content is authored stronger for its level than late content",
            },
            {
                "name": "monster_speed_max", "type": "int", "required": True, "min": 1, "group": "Monsters",
                "hint": "ceiling on any monster's speed in All In. Monster speed is otherwise never scaled "
                        "(player speed doesn't grow with level), so this only slows down monsters above it",
            },
            {
                "name": "gold_per_tier", "type": "float", "required": True, "group": "Rewards",
                "hint": "extra gold per tier, on top of gold scaling up with the monster's level (0.25 = +25% per tier)",
            },
            {
                "name": "gold_scaling_exponent", "type": "float", "required": True, "group": "Rewards",
                "hint": "damps how much a low-level monster's gold grows when lifted to the cap (0.5 = square root "
                        "of the stat lift), so early delves don't out-earn late ones",
            },
            {
                "name": "party_drop_bonus_per_member", "type": "float", "required": True, "group": "Rewards",
                "hint": "extra drop chance for everyone in an All In party, per member beyond the first "
                        "(0.15 = a party of 4 gets +45%). The high tiers are tuned around groups",
            },
            {
                "name": "party_monster_power_per_member", "type": "float", "required": True, "group": "Monsters",
                "hint": "makes All In party fights harder: monster HP and ATK/Magic go up this much per living "
                        "member beyond the first (0.1 = a party of 3 faces +20%), on top of the normal party HP "
                        "scaling",
            },
            {
                "name": "gear_value_per_tier", "type": "float", "required": True, "group": "Rewards",
                "hint": "extra sell value per tier for All In gear (0.2 = +20% per tier)",
            },
            {
                "name": "affixes_per_tier", "type": "int", "required": True, "min": 0, "group": "Gear Rolls",
                "hint": "random bonus stat rolls an All In drop gets per tier (1 = tier 3 gear has 3 rolls)",
            },
            {
                "name": "affix_budget_fraction", "type": "float", "required": True, "group": "Gear Rolls",
                "hint": "size of one bonus roll, as a fraction of the item level's stat budget (rarity-scaled)",
            },
            {
                "name": "affix_roll_min", "type": "float", "required": True, "group": "Gear Rolls",
                "hint": "low end of a roll, as a fraction of the size above (0.5 = a roll lands between half and full)",
            },
            {"name": "affix_roll_max", "type": "float", "required": True, "group": "Gear Rolls",
             "hint": "high end of a roll (1.0 = full size)"},
            {"name": "affix_weight_hp", "type": "float", "required": True, "group": "Gear Rolls",
             "hint": "relative chance a roll is HP"},
            {"name": "affix_weight_atk", "type": "float", "required": True, "group": "Gear Rolls"},
            {"name": "affix_weight_def", "type": "float", "required": True, "group": "Gear Rolls"},
            {"name": "affix_weight_spatk", "type": "float", "required": True, "group": "Gear Rolls"},
            {
                "name": "affix_weight_speed", "type": "float", "required": True, "group": "Gear Rolls",
                "hint": "keep this low -- speed compounds. At most one speed roll per item, capped below",
            },
            {
                "name": "speed_affix_max", "type": "int", "required": True, "min": 0, "group": "Gear Rolls",
                "hint": "largest speed bonus a single item can roll",
            },
        ],
    },
    "stat_labels": {
        "label": "Stat Labels",
        "category": DUNGEON_CONTENT,
        "icon": "🏷️",
        "json_path": "dungeon_stat_labels.json",
        "module": dungeon,
        "registry_attr": "STAT_LABELS",
        "loader": dungeon._load_stat_labels,
        "list_columns": ["id", "label", "emoji"],
        "fields": [
            {
                "name": "id", "type": "str", "required": True, "group": "Identity",
                "hint": "must stay exactly one of: " + ", ".join(sorted(dungeon._REQUIRED_STAT_LABEL_IDS))
                        + " -- the roster is fixed (wired to real stats/mechanics elsewhere), only "
                        "label/emoji are editable. stun/sap/dot/hot/guard name combat statuses on the "
                        "battle screen's status line.",
            },
            {
                "name": "label", "type": "str", "required": True, "group": "Display",
                "hint": "player-facing name shown everywhere this stat appears (character sheets, skill "
                        "lists, combat log) -- e.g. rename spatk's label to reflavor \"Magic\" into "
                        "something else entirely, no code change needed",
            },
            {"name": "emoji", "type": "str", "required": False, "group": "Display"},
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
        # _load_skills checks each skill alone; the per-build "exactly one entry-level skill" rule
        # lives in _build_skills_by_combo, which would otherwise only run (and refuse to boot) at
        # the next restart.
        "extra_validators": [lambda new_registry: dungeon._build_skills_by_combo(new_registry)],
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
                "name": "chip_cost", "type": "int", "required": True, "min": 0, "group": "Class",
                "hint": "Chips spent to cast this skill -- must not exceed the build's max Chips "
                        "(dungeon.CLASSES/SUBCLASSES) or it would be permanently unusable",
            },
            {
                "name": "replaces", "type": "str", "required": False, "group": "Class",
                "hint": "subclass skills only, optional -- the id of a base-class skill (same class) this one "
                        "upgrades once unlocked, e.g. \"marked_card\". Blank = added on top of the base skills",
            },
            {
                "name": "special", "type": "bool", "required": False, "default": False, "group": "Class",
                "hint": "Physical (unchecked) rolls damage against ATK; Special (checked) uses Magic "
                        "instead. Plain Attack is always Physical.",
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
            "drafts_path": dungeon._DELVE_DRAFTS_PATH,
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
                "name": "max_party_size", "type": "int", "required": False, "default": 4, "min": 2,
                "group": "Identity",
                "hint": "how many players a party lobby for this delve can hold. Leave at 4 for a "
                        "normal party delve -- raise it (e.g. to 8) for a raid. Doesn't affect solo.",
            },
            {
                "name": "raid", "type": "bool", "required": False, "default": False, "group": "Identity",
                "hint": "every party member pays their own energy when the leader clicks Start Delve "
                        "(instead of only the leader paying). Meant for a "
                        "max_party_size-raised raid delve, but independent of it -- a small raid stays "
                        "possible.",
            },
            {
                "name": "boss_room", "type": "str", "required": False, "group": "Identity",
                "hint": "room id of this delve's real final boss fight. Winning that fight is what counts "
                        "as clearing the delve for All In unlocks, so endings that skip the boss don't "
                        "count. Blank = reaching any ending counts. Must be a combat room in this delve.",
            },
            {
                "name": "all_in_difficulty", "type": "float", "required": False, "group": "Identity",
                "hint": "All In only: extra multiplier on this delve's monster stats and gold, on top of the "
                        "automatic evening-out to the benchmark delve (All In Mode page). Blank = 1.0",
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
            lambda new_registry: quests.validate_room_triggers(new_registry),
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
            {
                "name": "unlock_trigger", "type": "trigger", "required": False, "group": "Identity",
                "hint": "optional -- locks the whole room until this holds: every exit into it is hidden "
                        "(unless a locked message is set below), and the room's own feature commands "
                        "(e.g. !glue) refuse to run. Blank means always open.",
            },
            {
                "name": "locked_message", "type": "text", "required": False, "group": "Identity",
                "hint": "optional, needs an unlock trigger -- if set, exits into this room stay visible while "
                        "it's locked, and clicking one (or typing one of its commands) shows this instead. "
                        "The place to hint at what gets you in.",
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
                "name": "unsellable", "type": "bool", "required": False, "default": False, "group": "Drop Info",
                "hint": "if checked, no NPC will buy this item (it never shows up in the sell popup)",
            },
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
                "name": "station", "type": "enum", "required": False, "group": "Effect",
                "choices": list(dungeon.RECIPE_STATIONS),
                "hint": "optional -- owning this item (placed or not) unlocks crafting that station's "
                        "recipes (oven = !bake, plus a Bake button on the house screen)",
            },
            {
                "name": "effect_type", "type": "enum", "required": True, "group": "Effect",
                "choices": lambda: sorted(housing.HOUSING_EFFECT_TYPES.keys()),
                "hint": "the passive bonus this item grants while placed in a house slot "
                        "(pizza_leftovers works while merely owned, placed or not)",
            },
            {
                "name": "value", "type": "int", "required": True, "group": "Effect",
                "hint": "a percent for dungeon_loot_bonus/dungeon_xp_bonus/ranch_training_bonus, a "
                        "flat point for stat_bonus, a flat amount for rest_energy_bonus/rest_gold_bonus/"
                        "luck_bonus. For pizza_leftovers: each !pizza gives 1 leftover, plus this "
                        "percent chance of a second",
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
        # !glue grants the ectoglue grades by id (necromancy.GRADES), so none can be deleted
        # or renamed through here.
        "extra_validators": [
            lambda new_registry: necromancy.validate_ectoglue_items(new_registry),
            lambda new_registry: quests.validate_monster_drop_quest_items(quest_items=new_registry),
        ],
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
        "extra_validators": [
            lambda new_registry: quests.validate_shop_housing_items(new_registry),
            lambda new_registry: quests.validate_npc_poker_triggers(new_registry),
        ],
        "list_columns": ["id", "name", "room", "greet_achievement", "poker_bot"],
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
            {
                "name": "poker_bot", "type": "bool", "required": False, "default": False, "group": "Hold'em Bot",
                "hint": "puts this NPC in the pool of random Hold'em bots (4 are dealt in when a table opens, house-funded stack)",
            },
            {
                "name": "poker_tightness", "type": "float", "required": False, "group": "Hold'em Bot",
                "hint": "0-1, default 0.5. How much of an edge over the pot odds it wants before calling. "
                        "0 calls a little light, 1 only continues with a clear edge.",
            },
            {
                "name": "poker_aggression", "type": "float", "required": False, "group": "Hold'em Bot",
                "hint": "0-1, default 0.5. How readily it raises a good hand instead of calling, and how big.",
            },
            {
                "name": "poker_bluff", "type": "float", "required": False, "group": "Hold'em Bot",
                "hint": "0-1, default 0.08. Chance to bet a weak hand when it could check. Bluffs never "
                        "risk more than about a third of its stack.",
            },
            {
                "name": "poker_allin_rate", "type": "float", "required": False, "group": "Hold'em Bot",
                "hint": "0-1, default 0.05. Chance a raise becomes an all-in shove.",
            },
            {
                "name": "poker_chatter", "type": "float", "required": False, "group": "Hold'em Bot",
                "hint": "0-1, default 0.6. Chance it says one of its lines when it acts.",
            },
            *[
                {
                    "name": f"poker_lines_{kind}", "type": "text", "required": False, "group": "Hold'em Bot Lines",
                    "hint": f"one line per line, a random one is said when it {desc}",
                }
                for kind, desc in (
                    ("sit", "sits down"), ("fold", "folds"), ("check", "checks"), ("call", "calls"),
                    ("raise", "raises"), ("allin", "goes all-in"), ("win", "wins a pot"),
                    ("lose", "loses a showdown"),
                )
            ],
            {
                "name": "poker_sprite_path", "type": "image", "required": False, "subdir": "npcs",
                "hint": "optional seated portrait for the Hold'em table: a cut-out on a transparent "
                        "background, drawn behind the far rail (its bottom ~35px hides under the table). "
                        "Any size; it's trimmed to its visible pixels and scaled to fit 196x205. Without "
                        "one, sprite_path is used (or framed, if it has no transparency to trim).",
            },
            {
                "name": "poker_sprite_scale", "type": "float", "required": False, "group": "Hold'em Bot",
                "hint": "optional, default 1.0 (above 0, up to 2): multiplies the seated portrait's size at "
                        "the Hold'em table, e.g. 0.5 for a small creature.",
            },
            {
                "name": "poker_trigger", "type": "trigger", "required": False,
                "hint": "optional -- this NPC only sits at Hold'em tables while this holds (checked "
                        "against whoever opened the table), and leaves between hands once it stops. "
                        "Counted types (kill_monster/craft_item) aren't allowed.",
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
            "drafts_path": quests._QUEST_DRAFTS_PATH,
        },
        # quests._load_quests only checks an "equipment"-kind reward_item at load time (see
        # REWARD_REGISTRIES' own comment for why the other kinds -- material, consumable,
        # horse_clothes, housing_item -- can't be fully validated there yet) -- the rest are caught
        # here instead, as an extra save-time check, same "rooms"/"recipes"/"npcs" pattern above.
        "extra_validators": [
            lambda new_registry: quests.validate_reward_item_kinds(new_registry),
            lambda new_registry: quests.validate_subclass_unlock_quest(quests_by_id=new_registry),
        ],
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
                "name": "unsellable", "type": "bool", "required": False, "default": False, "group": "Drop Info",
                "hint": "if checked, no NPC will buy this item (it never shows up in the sell popup)",
            },
            {
                "name": "unsmashable_message", "type": "text", "required": False, "group": "Flavor Text",
                "hint": "optional -- if set, !smash refuses to destroy this item and shows this message "
                        "instead of removing it",
            },
            {
                "name": "war_speed", "type": "int", "required": False, "group": "War (Barding)",
                "hint": "optional -- added to the wearer's Speed in !war/!raid only (never in races). "
                        "Negative is allowed for a joke item.",
            },
            {
                "name": "war_endurance", "type": "int", "required": False, "group": "War (Barding)",
                "hint": "optional -- added to the wearer's Endurance in !war/!raid only",
            },
            {
                "name": "war_spirit", "type": "int", "required": False, "group": "War (Barding)",
                "hint": "optional -- added to the wearer's Spirit in !war only",
            },
        ],
    },
    "necromancy_spells": {
        "label": "Necromancy Spells",
        "category": RANCH_CONTENT,
        "icon": "🕯️",
        "json_path": "necromancy_spells.json",
        "module": necromancy,
        "registry_attr": "SPELLS",
        "loader": necromancy._load_spells,
        "list_columns": ["id", "name", "cost", "curse", "curse_required"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {"name": "emoji", "type": "str", "required": True, "group": "Identity"},
            {"name": "description", "type": "text", "required": True, "group": "Identity"},
            {
                "name": "cost", "type": "int", "required": True, "min": 1, "group": "Casting",
                "hint": "Ectoglue spent per cast, all of one grade the caster picks. The grade strengthens every "
                        "boon below (Faint x0.75, Ectoglue x1, Potent x1.25, Ancient x1.5) but never a price.",
            },
            {
                "name": "cooldown_hours", "type": "int", "required": True, "min": 0, "group": "Casting",
                "hint": "hours after casting before this spell can be cast again",
            },
            {
                "name": "curse", "type": "int", "required": False, "min": 0, "group": "Curse",
                "hint": "how evil it is: added to the caster's Curse every time it's cast (blank = 0)",
            },
            {
                "name": "curse_required", "type": "int", "required": False, "min": 0, "group": "Curse",
                "hint": "the caster's Curse must be at least this to cast it (blank = anyone). Titles: "
                        + ", ".join(f"{minimum} {title}" for minimum, title in necromancy.CURSE_TITLES),
            },
            {
                "name": "charge_pct", "type": "int", "required": False, "group": "Effects (next war)",
                "hint": "optional -- % added to your army's charge, negative for a price. A side's spells stack, "
                        f"clamped to +/-{war.SPELL_EFFECT_CAPS['charge_pct']}% total.",
            },
            {
                "name": "hp_pct", "type": "int", "required": False, "group": "Effects (next war)",
                "hint": f"optional -- % added to your army's max HP, negative for a price (cap {war.SPELL_EFFECT_CAPS['hp_pct']}%)",
            },
            {
                "name": "spirit", "type": "int", "required": False, "group": "Effects (next war)",
                "hint": f"optional -- flat Spirit added to your units, negative for a price (cap {war.SPELL_EFFECT_CAPS['spirit']})",
            },
            {
                "name": "enemy_charge_pct", "type": "int", "required": False, "group": "Effects (next war)",
                "hint": f"optional -- % knocked off the enemy's charge (cap {war.SPELL_EFFECT_CAPS['enemy_charge_pct']}%)",
            },
            {
                "name": "enemy_spirit", "type": "int", "required": False, "group": "Effects (next war)",
                "hint": f"optional -- Spirit knocked off the enemy's units (cap {war.SPELL_EFFECT_CAPS['enemy_spirit']})",
            },
            {
                "name": "rot_pct", "type": "int", "required": False, "min": 0, "group": "Dark Arts (next war)",
                "hint": f"optional -- every round, each enemy unit rots away this % of its max HP (cap {war.SPELL_EFFECT_CAPS['rot_pct']}%)",
            },
            {
                "name": "feast_pct", "type": "int", "required": False, "min": 0, "group": "Dark Arts (next war)",
                "hint": f"optional -- your units heal this % of the damage they deal (cap {war.SPELL_EFFECT_CAPS['feast_pct']}%)",
            },
            {
                "name": "harvest_pct", "type": "int", "required": False, "min": 0, "group": "Dark Arts (next war)",
                "hint": "optional -- every enemy horse your army knocks out rises as a ghost on your side at this % "
                        f"of its strength, mid-battle (cap {war.SPELL_EFFECT_CAPS['harvest_pct']}%)",
            },
            {
                "name": "offering_pct", "type": "int", "required": False, "min": 0, "group": "Dark Arts (next war)",
                "hint": "optional -- sacrifices the caster's weakest horse in the army before the battle (glued for "
                        "real, Ectoglue and all); this % of its charge is split among the rest. Needs 2+ horses.",
            },
            {
                "name": "summon_ghost_pct", "type": "int", "required": False, "min": 0, "group": "Dark Arts (next war)",
                "hint": "optional -- makes this a summon: the caster picks any glued horse in the graveyard (anyone's) "
                        "and its ghost rides in their next war at this % of its final stats, times the grade "
                        f"(capped at {war.GHOST_STAT_CAP_PCT}%). A bonus unit beyond the army cap; never injured.",
            },
            {
                "name": "burn_enemy_luck", "type": "int", "required": False, "min": 0, "group": "Luck",
                "hint": "optional -- Luck the enemy loses when the war starts (scales with grade)",
            },
            {
                "name": "burn_own_luck", "type": "int", "required": False, "min": 0, "group": "Luck",
                "hint": "optional -- Luck the caster loses when the war starts: a price, so it doesn't scale",
            },
        ],
    },
    "manhattan": {
        "label": "Mane-hay-tan Bombs",
        "category": RANCH_CONTENT,
        "icon": "☢️",
        "json_path": "manhattan.json",
        "module": manhattan,
        "registry_attr": "BOMBS",
        "loader": manhattan._load_bombs,
        "list_columns": ["id", "order", "name", "goal", "fallout_hours"],
        "fields": [
            {"name": "id", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "order", "type": "int", "required": True, "min": 1, "group": "Identity",
                "hint": "position on the bomb ladder (1 = the first bomb ever built). Past the last bomb, "
                        "it repeats with its goal growing x1.5 each cycle",
            },
            {"name": "name", "type": "str", "required": True, "group": "Identity"},
            {
                "name": "goal", "type": "int", "required": True, "min": 1, "group": "Tuning",
                "hint": "total gold the whole server must !donate to arm this bomb",
            },
            {
                "name": "fallout_hours", "type": "int", "required": True, "min": 1, "group": "Tuning",
                "hint": "how long Fallout lasts after this bomb is launched (stacks onto any Fallout already running)",
            },
            {
                "name": "log_25", "type": "text", "required": True, "group": "Research Log",
                "hint": "posted when a donation pushes the pot past 25% of the goal (flavor only)",
            },
            {"name": "log_50", "type": "text", "required": True, "group": "Research Log", "hint": "posted at 50%"},
            {"name": "log_75", "type": "text", "required": True, "group": "Research Log", "hint": "posted at 75%"},
            {
                "name": "armed_text", "type": "text", "required": True, "group": "Research Log",
                "hint": "posted when the donation that reaches the goal arms the bomb",
            },
            {
                "name": "detonation_text", "type": "text", "required": True, "group": "Research Log",
                "hint": "the !launch announcement",
            },
            {
                "name": "detonation_video", "type": "str", "required": False, "group": "Research Log",
                "hint": "optional -- repo-relative path to a video (.mp4/.mov/.webm, e.g. "
                        "assets/manhattan/little_colt.mp4) DM'd to every player when this bomb is launched. "
                        "The file must already exist in the repo; this panel can't upload videos.",
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
    "heal_fraction": "value: fraction of your max HP restored (0-1, e.g. 0.3 = 30%). overheal_barrier (optional, 0-1): that fraction of healing past full HP becomes a Barrier instead of being wasted.",
    "guard": "reduction: the monster's next hit is multiplied by this (0-1, e.g. 0.5 = -50% damage taken)",
    "lifesteal_fraction": "value: fraction of the damage you deal that's restored as HP (0-1)",
    "def_shred": "value: flat amount the monster's DEF is lowered by, for the rest of this fight",
    "extra_attack": "multiplier: optional damage multiplier for the bonus attack (blank = 1.0, a full extra hit)",
    "atk_buff": "value: flat ATK bonus for the rest of this fight",
    "def_buff": "value: flat DEF bonus for the rest of this fight",
    "spatk_buff": "value: flat Magic bonus for the rest of this fight",
    "hp_buff": "value: flat max HP bonus (current HP rises by the same amount) for the rest of this fight",
    "chip_gain": "value: Chips restored, capped at max Chips",
    "speed_buff": "value: flat Speed bonus for the rest of this fight -- faster turn order",
    "atk_debuff": "value: flat amount the target's ATK is lowered by, for the rest of this fight",
    "spatk_debuff": "value: flat amount the target's Magic is lowered by, for the rest of this fight",
    "speed_debuff": "value: flat amount the target's Speed is lowered by, for the rest of this fight -- slower turn order",
    "taunt": "value: flat threat gained against every monster in the fight right now -- higher means monsters are more likely to attack you instead of your allies. Each monster it lands on also attacks you on its very next turn. Player-only, party delves only.",
    "lower_threat": "value: flat threat lost against every monster in the fight right now -- lower means monsters are less likely to attack you. Player-only, party delves only.",
    "dodge_buff": "value: bonus chance (0-1) to fully avoid incoming damage; duration: how many rounds it lasts",
    "resist_buff": "value: bonus chance (0-1) to shake off incoming status effects/debuffs; duration: how many rounds it lasts",
    "dot": "value: flat damage taken each round; duration: how many rounds it lasts",
    "hot": "value: fraction of max HP restored each round (0-1); duration: how many rounds it lasts. overheal_barrier (optional, 0-1): that fraction of healing past full HP becomes a Barrier.",
    "sap": "duration: how many of the target's own turns it skips -- broken early the instant they take any damage (including from this same hit)",
    "stun": "duration: how many of the target's own turns it skips -- damage does NOT break it",
    "cleanse_dot": "No params -- removes any active damage-over-time from the caster (or the whole party with aoe)",
    "cleanse_cc": "No params -- removes any active Sap/Stun from the caster (or the whole party with aoe)",
    "charge": "value: damage multiplier your NEXT damaging Magic skill gets on every hit (e.g. 2.5). Optional: max_stacks (recasts that stack, default 1), step (extra multiplier per stack), safe_stacks (stacks with no risk), short_chance + short_damage (0-1: chance a risky stack shorts out, and the max-HP fraction it costs). Defaults to target self.",
    "hex": "duration: how many of the CASTER's turns it lasts (not the enemy's, so a fast boss can't burn through it; falls back to the enemy's turns if the caster goes down). Every point of damage the enemy takes meanwhile, from anyone, is stored; when it runs out it detonates for value (0-1) of the stored total, divided by sqrt(party size) since the whole party feeds it (solo: full value; party of 4: half). cap (optional, 0-1): detonation can't exceed this fraction of the enemy's max HP.",
    "hype_spend": "per_point: this cast's damage is multiplied by 1 + per_point x your Hype, then Hype resets. Hype builds by 1 whenever anyone in the party lands a hit or dodges, up to max_points (default 10), while you have this skill. Needs a damage_multiplier/extra_attack in the same effects.",
    "grudge_spend": "per_point: this cast's damage is multiplied by 1 + per_point x your Grudge, then Grudge resets. Grudge builds by 1 each time a monster hits you, up to max_points (default 10), while you have this skill. Needs a damage_multiplier/extra_attack in the same effects.",
    "counter": "value (0-1): for duration of your own turns, every monster hit you take is punched back at the attacker for this fraction of the damage. Defaults to target self.",
    "workmans_comp": "value: for duration of your own turns, every monster hit that lands on you refunds this many Chips, for at most max_hits hits (default 3). Defaults to target self.",
    "blood_in_water": "multiplier: size of each bonus bite. One bite each if the target is slowed at all, under half HP, and slowed as far as it can go (up to 3). Needs a non-aoe damage effect in the same effects.",
    "intercept": "value (0-1): aim at an ally -- the next single-target monster hit on them lands on you instead, and this fraction of the damage you take becomes a Barrier on every living party member.",
    "collect_tab": "value: monsters that hit you run up a Tab (damage dealt). This cashes the target's Tab as value x Tab bonus damage and clears it. An uncashed Tab raises the monster's loot on death by loot_per_point (default 0.01) per point, up to loot_cap (default 0.5). Needs a non-aoe damage effect in the same effects.",
    "corner": "value (0-1): once per fight, the ally this lands on gets back up at this fraction of max HP the first time they'd drop to 0 in combat.",
    "pass_the_buck": "No params -- strips every Stun/Sap/DoT and stat debuff off every living ally and puts it on the target enemy. Resistable.",
    "plate_spend": "value: the Collection Plate fills with healing past full HP from your own heals/HoTs (up to your max HP) while you have this skill. Spending heals every living ally for value x Plate and deals the same to every enemy.",
    "hook": "value (0-1): for duration of the CASTER's turns (not the enemy's), every hit the party lands on it drains this fraction of the damage back to the attacker (overflow becomes Barrier). growth (optional, 0-1): added to the drain each of the caster's turns.",
    "tells_spend": "per_point: finisher. Each hit you land adds 1 Tell to that target (+stunned_bonus if it's stunned), up to max_points (default 5), while you have this skill. Spends the target's Tells for x(1 + per_point x Tells); at stun_at+ Tells also stuns it 2 turns, no resist. Needs a non-aoe damage effect.",
    "contract": "value (0-1): each of your hits on the target does +value more damage than the last, up to max_bonus (default 0.5). Killing it gives you an immediate extra turn.",
    "charm": "duration: the target is Smitten for this many of its own turns -- it attacks one of its own allies instead (or swoons if it has none).",
    "pot_add": "value: raises your Pot by this much (up to max, default 3). Pairs with pot_cash on another outcome.",
    "pot_cash": "No params -- this hit is multiplied by x(1 + Pot), then the Pot empties. Needs a damage effect.",
    "overload": "min/max: each cast rolls a damage multiplier in this range for your next damaging Magic skill, replacing the last roll. bust_chance (0-1) per roll already made: a reroll can bust, losing the charge and stunning you for bust_stun turns (default 2). Defaults to target self.",
}

# Compact type -> label used by the admin list view's stats-summary column and bot.py's !skills
# command alike (admin_server.format_effect) -- EFFECT_TYPE_HINTS above is the full sentence shown
# on the edit form; this is deliberately terse since several of these may need to fit in one table
# cell alongside every other effect the same item has.
EFFECT_SHORT_LABELS = {
    "damage_multiplier": "Dmg", "execute_multiplier": "Execute", "heal_fraction": "Heal", "guard": "Guard",
    "lifesteal_fraction": "Lifesteal", "def_shred": "DEF Shred", "extra_attack": "Extra Atk",
    "atk_buff": "ATK", "def_buff": "DEF", "spatk_buff": "Magic",
    "hp_buff": "HP", "chip_gain": "Chips", "speed_buff": "SPD",
    "atk_debuff": "ATK", "spatk_debuff": "Magic", "speed_debuff": "SPD",
    "taunt": "Taunt", "lower_threat": "Threat", "dodge_buff": "Dodge", "resist_buff": "Resist",
    "dot": "DoT", "hot": "HoT", "sap": "Sap", "stun": "Stun",
    "cleanse_dot": "Cleanse DoT", "cleanse_cc": "Cleanse CC",
    "charge": "Charge", "overload": "Overload", "hex": "Hex", "hype_spend": "Hype",
    "grudge_spend": "Grudge", "counter": "Counter", "workmans_comp": "Workman's Comp", "blood_in_water": "Bites", "intercept": "Cover",
    "collect_tab": "Tab", "corner": "Corner", "pass_the_buck": "Pass the Buck", "plate_spend": "Plate",
    "hook": "Hook", "tells_spend": "Finisher", "contract": "Contract", "charm": "Charm", "pot_add": "Pot +",
    "pot_cash": "Cash Pot",
}

EQUIPMENT_EFFECT_TRIGGERS = list(dungeon.EQUIPMENT_EFFECT_TRIGGERS)

SHOP_KINDS = list(npcs.SHOP_KINDS.keys())
