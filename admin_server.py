"""Password-gated content editor for dungeon.py's JSON-backed registries (monsters, equipment,
materials, consumables, recipes, skills). Replaces the old Discord Activity's activity_server.py
-- same merged-process pattern (bot.py's setup_hook starts this on its own event loop), same
reachable-over-the-existing-tunnel deployment, entirely different purpose.

Schema-driven (see admin_schemas.py) rather than six bespoke editors: the list/edit/save routes
below are generic over whatever CONTENT_TYPES describes, so a future content type that follows
dungeon.py's existing "JSON list of dicts with an id" convention only needs a schema entry, not
new routes.

Every save reuses that content type's actual dungeon.py loader as the validator -- write the
candidate JSON to a temp file, try loading it for real, only replace the live file (and hot-reload
the in-memory registry) if that succeeds. A bad edit can't corrupt content that's currently
working, and there is exactly one place each content type's rules live either way.
"""

import asyncio
import copy
import datetime
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import subprocess

from aiohttp import web
from dotenv import load_dotenv

import achievements
import db
import dungeon
import dungeon_view
import horse_clothes
import housing
import moon
import npcs
import quests
import room_commands
import rooms
import skill_balance
from admin_schemas import (
    CATEGORIES, CONTENT_TYPES, EFFECT_PARAM_NAMES, EFFECT_PARAMS_BY_TYPE, EFFECT_SHORT_LABELS,
    EFFECT_TYPE_HINTS, EFFECT_TYPES, EQUIPMENT_EFFECT_TRIGGERS, SHOP_KINDS, TRIGGER_PARAM_HINTS,
    TRIGGER_PARAM_KINDS, TRIGGER_PARAM_NAMES, TRIGGER_TYPES,
)

# kind -> a no-arg callable returning the live sorted list of valid ids for that kind of trigger
# param select. Callables (not frozen lists) because dungeon.MONSTERS/RECIPES are hot-reloadable
# (admin_server itself rebinds them on save) -- freezing these at import time would go stale the
# moment someone edits monsters/recipes/quest_items through this same panel.
_TRIGGER_PARAM_CHOICES = {
    "quest_item": lambda: sorted(quests.QUEST_ITEMS.keys()),
    "monster": lambda: sorted(dungeon.MONSTERS.keys()),
    "recipe": lambda: sorted(dungeon.RECIPES.keys()),
    "achievement": lambda: sorted(a["kind"] for a in achievements.ACHIEVEMENTS),
    "quest": lambda: sorted(quests.QUESTS_BY_ID.keys()),
    "main_class": lambda: sorted(dungeon.CLASSES.keys()),
    "subclass": lambda: sorted(dungeon.SUBCLASSES.keys()) + [dungeon.NO_SUBCLASS],
    "moon_phase": lambda: [p[0] for p in moon.PHASES],
}

load_dotenv()

ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD")
PORT = int(os.getenv("ACTIVITY_SERVER_PORT", "8787"))

COOKIE_NAME = "admin_session"

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB -- matches Discord's own non-boosted-server attachment cap

# Field type -> the CSS class its <form> tag needs for a full-width flowchart canvas (see
# .delve-canvas-wrap in _PAGE_CSS) -- edit_view checks membership in this dict's keys wherever it
# used to hardcode `content_type == "delves"`, and uses the matching value as the form's class.
# Each flowchart field type gets its own class (not one shared class) only because the delve and
# quest canvases are separate scripts/stylesheets-in-spirit, not because the CSS itself differs.
FLOWCHART_FORM_CLASSES = {"delve_flowchart": "delve-form", "quest_flowchart": "quest-form"}

BACKUPS_DIR = os.path.join(os.path.dirname(__file__), "backups")


def _signing_key() -> bytes:
    return hashlib.sha256(ADMIN_PANEL_PASSWORD.encode()).digest()


def _session_cookie_value() -> str:
    token = "ok"
    signature = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()
    return f"{token}.{signature}"


def _valid_session(cookie_value: str | None) -> bool:
    if not cookie_value or "." not in cookie_value:
        return False
    token, _, signature = cookie_value.partition(".")
    expected = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.StreamResponse:
    if request.path == "/login":
        return await handler(request)
    if not _valid_session(request.cookies.get(COOKIE_NAME)):
        raise web.HTTPFound("/login")
    return await handler(request)


# --- Tiny HTML helpers ---------------------------------------------------------------------
# No template engine -- this is a small internal tool, and plain string-building keeps it to one
# file with zero new dependencies. Every user-supplied value goes through html.escape.

_PAGE_CSS = """
body { font-family: system-ui, sans-serif; background: #1a1a1f; color: #e8e8ec; margin: 0; }
.app { display: flex; align-items: flex-start; min-height: 100vh; }
.sidebar { width: 220px; flex: 0 0 220px; background: #202027; border-right: 1px solid #35353f; min-height: 100vh; padding-bottom: 24px; }
.sidebar .brand { display: block; padding: 16px; font-weight: bold; color: #e8813a; text-decoration: none; border-bottom: 1px solid #35353f; margin-bottom: 8px; }
.nav-category { color: #6a6a74; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; padding: 16px 16px 6px; }
.nav-item { display: block; padding: 7px 16px; color: #e8e8ec; text-decoration: none; font-size: 0.92rem; }
.nav-item:hover { background: #2c2c34; }
.nav-item.active { background: #3a3a44; color: #e8813a; font-weight: bold; }
main { flex: 1; min-width: 0; padding: 24px; max-width: 900px; }
.breadcrumbs { color: #9a9aa4; font-size: 0.85rem; margin-bottom: 16px; }
.breadcrumbs a { color: #9a9aa4; text-decoration: none; }
.breadcrumbs a:hover { color: #e8813a; }
.breadcrumbs .sep { margin: 0 6px; }
h2 { color: #9a9aa4; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 12px; }
.content-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; margin-bottom: 28px; }
.content-card { display: flex; flex-direction: column; align-items: center; gap: 6px; background: #26262e; border: 1px solid #35353f; border-radius: 8px; padding: 20px 12px; text-decoration: none; color: #e8e8ec; }
.content-card:hover { border-color: #e8813a; }
.content-card-icon { font-size: 1.8rem; }
.content-card-label { font-weight: bold; text-align: center; }
.content-card-count { color: #9a9aa4; font-size: 0.85rem; }
table { border-collapse: collapse; width: 100%; margin-bottom: 16px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #35353f; }
th { color: #9a9aa4; }
#list-table th { cursor: pointer; user-select: none; }
#list-table th:hover { color: #e8e8ec; }
#list-table th.sort-asc::after { content: " ▲"; }
#list-table th.sort-desc::after { content: " ▼"; }
a.row-link { color: #e8e8ec; text-decoration: none; }
a.row-link:hover { color: #e8813a; }
#list-filter { max-width: 320px; margin-bottom: 12px; }
form { display: flex; flex-direction: column; gap: 10px; max-width: 500px; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 0.9rem; color: #9a9aa4; }
.checkbox-label { flex-direction: row; align-items: center; gap: 6px; }
input, select, textarea { background: #26262e; border: 1px solid #45454f; color: #e8e8ec; padding: 6px 8px; border-radius: 4px; font-family: inherit; }
textarea { min-height: 60px; }
fieldset { border: 1px solid #35353f; border-radius: 6px; margin: 0; }
legend { color: #9a9aa4; padding: 0 6px; }
.field-group-heading { color: #9a9aa4; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; margin: 10px 0 -4px; }
.field-group-heading:first-child { margin-top: 0; }
.row-group { border: 1px solid #35353f; border-radius: 6px; padding: 10px; margin-bottom: 8px; display: flex; gap: 10px; flex-wrap: wrap; }
.room-row { flex-direction: column; align-items: stretch; }
button { background: #3a3a44; color: #e8e8ec; border: 1px solid #52525e; padding: 8px 16px; border-radius: 6px; cursor: pointer; }
button:hover { background: #45454f; }
.error { background: #4a1f1f; border: 1px solid #a04040; color: #f0b0b0; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; }
.success { background: #1f4a2a; border: 1px solid #40a060; color: #b0f0c0; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; }
.danger { background: #a04040; }
.image-preview { max-width: 220px; max-height: 150px; border-radius: 6px; margin-bottom: 6px; display: block; }
.field-hint { color: #6a6a74; font-size: 0.8rem; margin: -4px 0 4px; }
.remove-row { align-self: flex-start; padding: 4px 10px; font-size: 0.85rem; }
.add-row { align-self: flex-start; }
.asset-thumb { max-width: 60px; max-height: 40px; border-radius: 4px; display: block; }
.asset-table form { flex-direction: row; max-width: none; }
.info-icon { cursor: help; color: #6a6a74; margin-left: 4px; font-size: 0.8rem; }

/* Skill Balance page (admin_server.py's skill_balance_view) */
.table-scroll { overflow-x: auto; }
.outlier-flag { display: inline-block; background: #4a3a1f; border: 1px solid #d09030; color: #f0c080; padding: 1px 6px; border-radius: 4px; font-size: 0.75rem; margin-left: 6px; white-space: nowrap; }
.bar-track { background: #26262e; border-radius: 3px; height: 10px; width: 120px; overflow: hidden; }
.bar-fill { background: #e8813a; height: 100%; }
.effect-tag { display: inline-block; background: #2c2c34; color: #9a9aa4; border-radius: 4px; padding: 1px 6px; font-size: 0.72rem; margin: 1px 2px 1px 0; }
.delve-picker { flex-direction: row; align-items: center; gap: 8px; max-width: none; margin-bottom: 16px; }

/* Delve flowchart editor (see admin_server.py's _render_delve_flowchart / _dynamic_script), and
   the quest flowchart editor (_render_quest_flowchart / the "Quest flowchart editor" section of
   _dynamic_script) -- a separate, simpler canvas/script (no room-type split, no monster groups, no
   fail handles, no image uploads) rather than a shared factory, since the delve script turned out
   to be far more tightly special-cased around room/action/group distinctions than a clean
   parameterization could absorb without real regression risk to already-working delve editing.
   Every selector below that lists both a room-* and stage-* class is intentionally shared styling
   for what's visually the identical shape in both editors -- only the class *names* differ, so
   each script's DOM queries stay unambiguous even though only one canvas is ever on a page at
   once. */
form.delve-form, form.quest-form { max-width: none; }
.delve-canvas-wrap { position: relative; overflow: auto; max-height: 640px; padding: 0; border: 1px solid #35353f; border-radius: 8px; background: #14141a; margin-bottom: 8px; max-width: calc(100vw - 640px); min-width: 320px; }
#delve-rooms-canvas, #quest-stages-canvas { position: relative; width: 2000px; height: 1000px; }
svg.delve-arrows { position: absolute; top: 0; left: 0; width: 2000px; height: 1000px; pointer-events: none; overflow: visible; }
svg.delve-arrows text { user-select: none; }
.add-row[data-repeat-add="delve-rooms-canvas"], .add-row[data-repeat-add="quest-stages-canvas"] { position: sticky; bottom: 8px; left: 8px; z-index: 5; }
.room-wrapper, .stage-wrapper { display: contents; }
.room-box, .stage-box { position: absolute; width: 170px; background: #26262e; border: 2px solid #45454f; border-radius: 8px; padding: 8px; cursor: grab; user-select: none; touch-action: none; z-index: 1; transition: border-color 0.1s, box-shadow 0.1s; }
.room-box.dragging, .stage-box.dragging { cursor: grabbing; z-index: 2; }
.room-box.selected, .stage-box.selected { border-color: #e8813a; }
.room-box.is-start, .stage-box.is-start { box-shadow: 0 0 0 2px #40a060; }
.room-box.drop-target-hover, .stage-box.drop-target-hover { border-color: #e8813a; box-shadow: 0 0 0 3px rgba(232, 129, 58, 0.55); z-index: 3; }
.room-box.has-error, .stage-box.has-error { border-color: #c04040; box-shadow: 0 0 0 2px rgba(192, 64, 64, 0.45); }
.action-node.has-error, .path-node.has-error { border-left-color: #c04040; box-shadow: 0 0 0 2px rgba(192, 64, 64, 0.45); }
.draft-tag { background: #4a3a1f; border: 1px solid #a08040; color: #f0d0a0; padding: 2px 8px; border-radius: 10px; font-size: 0.75rem; }
#draft-save-status.saving { color: #9a9aa4; }
#draft-save-status.saved { color: #6ac080; }
#draft-save-status.failed { color: #e08080; }
.room-box-header, .stage-box-header { display: flex; align-items: center; gap: 4px; }
.room-box-id, .stage-box-id { flex: 1; font-weight: bold; font-size: 0.85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.room-flag, .room-box-select, .stage-flag, .stage-box-select { background: none; border: none; padding: 2px; font-size: 0.85rem; cursor: pointer; color: #9a9aa4; }
.room-flag.is-start, .stage-flag.is-start { color: #40a060; }
.room-box-summary, .stage-box-summary { font-size: 0.78rem; color: #9a9aa4; margin-top: 4px; }
.connector-handle { position: absolute; right: -10px; bottom: -10px; width: 20px; height: 20px; border-radius: 50%; background: #40a060; border: 3px solid #1a1a1f; cursor: crosshair; touch-action: none; z-index: 4; }
.connector-handle:hover { transform: scale(1.15); }
.connector-handle.fail { background: #c04040; }
.action-node, .group-node, .path-node { position: absolute; min-width: 84px; max-width: 130px; background: #1c1c26; border: 1px solid #3a3a4a; border-left: 4px solid #7a7ae0; border-radius: 5px; padding: 5px 26px 5px 8px; font-size: 0.7rem; color: #c8c8f0; z-index: 1; user-select: none; cursor: grab; touch-action: none; box-shadow: 0 1px 3px rgba(0,0,0,0.4); }
.action-node.dragging, .group-node.dragging, .path-node.dragging { cursor: grabbing; z-index: 2; }
.action-node:hover, .group-node:hover, .path-node:hover { border-left-color: #9a9af0; }
.action-node .connector-handle, .group-node .connector-handle, .path-node .connector-handle { width: 16px; height: 16px; }
.action-node .connector-handle.fail { right: 14px; }
.action-node-label, .group-node-label, .path-node-label { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.action-owner-line { stroke: #7a7ae0; stroke-width: 1.5; stroke-dasharray: 4,3; opacity: 0.75; }
.arrow-disconnect { opacity: 0.55; transition: opacity 0.15s; }
.arrow-disconnect:hover { opacity: 1; }
.room-detail-panel, .stage-detail-panel { position: fixed; top: 90px; right: 24px; width: 340px; max-height: calc(100vh - 120px); overflow-y: auto; background: #202027; border: 1px solid #45454f; border-radius: 8px; padding: 14px; z-index: 20; max-width: 340px; box-shadow: -4px 0 16px rgba(0,0,0,0.5); }
.room-detail-panel-header, .stage-detail-panel-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.live-next-tag { font-weight: normal; color: #9a9aa4; font-size: 0.78rem; margin-left: 6px; }
#tooltip-bubble { position: fixed; z-index: 999; background: #101014; color: #e8e8ec; border: 1px solid #52525e; padding: 6px 10px; border-radius: 6px; font-size: 0.78rem; max-width: 260px; display: none; pointer-events: none; box-shadow: 0 2px 8px rgba(0,0,0,0.4); }

/* Live skill-odds display (see _dynamic_script's updateSkillOdds) -- a monster's "Skills" fieldset
   shows the actual percent chance of each option (plain attack + every skill) recomputed on every
   keystroke, so attack_chance/chance's relative-weight semantics don't have to be worked out by hand. */
.skill-odds-summary { background: #1c1c26; border: 1px solid #35353f; border-radius: 6px; padding: 8px 10px; margin-bottom: 10px; font-size: 0.85rem; display: flex; flex-wrap: wrap; gap: 4px 14px; }
.skill-odds-item b { color: #6ac0f0; }
.skill-odds-pct { font-size: 0.8rem; color: #6ac0f0; font-weight: bold; align-self: center; }
"""

# Attaches to every image-upload input on the page (there may be several) -- picking a file
# updates its own preview <img> immediately via a local object URL, no server round-trip needed
# just to see what you picked. Vanilla JS, no new dependency, one shared script rather than
# per-field inline handlers so there's no f-string quote-escaping to get wrong. A named function
# (rather than a bare top-level querySelectorAll) so wireRepeatAdd's clone handler can re-run it
# on a freshly-added row too -- a delve room's own background-image input (see
# _render_room_detail_panel) only exists once a room's been added via "+ Add Room", same reasoning
# as every other wireX call there.
_PREVIEW_SCRIPT = """
function wireImagePreviews(root) {
    root.querySelectorAll('input[type=file][data-preview-target]').forEach(function (input) {
        if (input.dataset.previewWired) return;
        input.dataset.previewWired = '1';
        input.addEventListener('change', function () {
            if (this.files && this.files[0]) {
                var img = document.getElementById(this.dataset.previewTarget);
                img.src = URL.createObjectURL(this.files[0]);
                img.style.display = 'block';
            }
        });
    });
}
wireImagePreviews(document);
"""

# TRIGGER_TYPES aren't hot-reloadable data (unlike dungeon.MONSTERS etc) -- they're a fixed set of
# trigger *kinds* defined in quests.py, same footing as EFFECT_TYPES -- so it's safe to freeze this
# lookup once at import time rather than recomputing it per request.
_TRIGGER_PARAMS_BY_TYPE = {
    trigger_type: sorted(required | optional) for trigger_type, (required, optional) in quests.TRIGGER_SCHEMAS.items()
}

# Which of a delve room's type-specific detail-panel field groups (see
# _render_room_detail_panel) apply to each room type -- same "type hides irrelevant fields" idea
# TRIGGER_PARAMS_BY_TYPE drives for trigger rows, just at the level of whole field groups (groups
# vs prompt/actions) rather than individual params. "next" isn't listed here at all -- room-to-room
# connections are drawn on the flowchart canvas (see _render_room_box), never typed into this
# panel. Named _DELVE_ROOM_FIELDS_BY_TYPE (not just "room") to stay clearly distinct from rooms.py's
# unrelated casino-hub room concept _ROOM_COMMAND_KINDS mirrors above.
_DELVE_ROOM_FIELDS_BY_TYPE = {"combat": ["groups", "prompt"], "choice": ["prompt", "actions"]}

# Mirrors rooms.py's own _COMMAND_KINDS -- fixed regardless of content, same footing as
# EFFECT_TYPES/TRIGGER_TYPES above.
_ROOM_COMMAND_KINDS = ["none", "amount"]

# Three independent behaviors, all needed to keep a row-shaped field (trigger, effects,
# room_commands, shop_items) usable instead of a wall of boxes:
#   1. Every trigger/effect row shows every param its field type could ever use (see
#      _render_trigger_inputs/_render_effect_row) -- most are irrelevant once you've picked a type,
#      so this hides whichever ones {TRIGGER,EFFECT}_PARAMS_BY_TYPE says don't apply to the
#      selected type (and clears their value, so a stale hidden param never rides along into a
#      save under a type it doesn't apply to).
#   2. A cascaded_id select's (recipes' output_id, a shop row's item_id) valid options depend on a
#      sibling kind/output_kind select -- CASCADE_OPTIONS repopulates it live when that sibling
#      changes. Unlike (1)/(3), the *initial* render already has the right options (the row-
#      builder knows the current kind server-side -- see _render_cascaded_select), so this only
#      needs to run on change, not immediately at wire time.
#   3. Every repeatable list (effects, materials, monster_drops, delve_rooms, quest stages/paths,
#      room_commands, shop_items) no longer pads the form with a fixed number of blank rows -- a "+ Add" button
#      clones a <template> instead, and each row gets its own "Remove" button. Server-side parsing
#      (see each "+ Add"-using field's own _parse_field case) reads whatever *_<N>_* indices are
#      actually present in the submission, so removed/added rows never need renumbering.
# (1) and (2) need to (re)run on freshly-cloned rows, not just once at page load, so their
# wire*Selects functions are called both up front (root=document) and again after every clone.
#
# CASCADE_OPTIONS/EFFECT_PARAMS_BY_TYPE/EFFECT_TYPE_HINTS are computed fresh per response (unlike
# TRIGGER_PARAMS_BY_TYPE, frozen at import time) since CASCADE_OPTIONS sources from dungeon.py/
# quests.py registries that hot-reload live from the admin panel itself -- a script frozen at
# import time would go stale the moment someone added a new consumable, exactly the kind of bug
# this whole codebase's "content is data" ethos exists to avoid. EFFECT_PARAMS_BY_TYPE/
# EFFECT_TYPE_HINTS aren't actually hot-reloadable (same footing as TRIGGER_PARAMS_BY_TYPE -- fixed
# by code, not data) but are cheap enough to just recompute alongside CASCADE_OPTIONS rather than
# carry a second script-assembly path for the one thing that's genuinely static.
def _cascade_options() -> dict:
    """id -> "id — name" choices for every cascaded_id-shaped select, one sub-dict per sibling
    kind value, grouped by cascade name (matches admin_schemas.py fields' "cascades_to"/"cascade"
    and _render_shop_row's hardcoded "shop"). "recipe_output" backs recipes' output_id (keyed by
    output_kind); "shop" backs an npc's shop row item_id (keyed by kind); "skill_subclass" backs a
    skill's own subclass select (keyed by main_class)."""
    def _choices(registry: dict) -> list[list[str]]:
        return [[item_id, f"{item_id} — {item['name']}"] for item_id, item in sorted(registry.items())]

    return {
        "recipe_output": {
            "equipment": _choices(dungeon.EQUIPMENT),
            "consumable": _choices(dungeon.CONSUMABLES),
            "quest_item": _choices(quests.QUEST_ITEMS),
            "horse_clothes": _choices(horse_clothes.HORSE_CLOTHES),
            "housing_item": _choices(housing.HOUSING_ITEMS),
        },
        "shop": {
            "equipment": _choices(dungeon.EQUIPMENT),
            "material": _choices(dungeon.MATERIALS),
            "consumable": _choices(dungeon.CONSUMABLES),
            "quest_item": _choices(quests.QUEST_ITEMS),
            "horse_clothes": _choices(horse_clothes.HORSE_CLOTHES),
            "housing_item": _choices(housing.HOUSING_ITEMS),
        },
        "dream_item": {
            "equipment": _choices(dungeon.EQUIPMENT),
            "material": _choices(dungeon.MATERIALS),
            "consumable": _choices(dungeon.CONSUMABLES),
            "quest_item": _choices(quests.QUEST_ITEMS),
            "horse_clothes": _choices(horse_clothes.HORSE_CLOTHES),
            "housing_item": _choices(housing.HOUSING_ITEMS),
        },
        "quest_reward": {
            "equipment": _choices(dungeon.EQUIPMENT),
            "material": _choices(dungeon.MATERIALS),
            "consumable": _choices(dungeon.CONSUMABLES),
            "quest_item": _choices(quests.QUEST_ITEMS),
            "horse_clothes": _choices(horse_clothes.HORSE_CLOTHES),
            "housing_item": _choices(housing.HOUSING_ITEMS),
        },
        "monster_drop": {
            # quest_only equipment (e.g. Mondor's Greasy Pencil) is excluded -- those are only
            # ever granted through a quest turn-in, never a monster's own drop table (enforced
            # again at save time by dungeon._validate_monster_drops, in case of a hand JSON edit).
            "equipment": _choices({k: v for k, v in dungeon.EQUIPMENT.items() if not v.get("quest_only")}),
            "material": _choices(dungeon.MATERIALS),
            "consumable": _choices(dungeon.CONSUMABLES),
            "housing_item": _choices(housing.HOUSING_ITEMS),
        },
        # A choice-room action's own cost -- backs its item_kind -> item_id select. Equipment isn't
        # here on purpose: costs are qty-based (db.craft_item's {item_id: qty} shape), which
        # equipped-or-stored gear doesn't fit, and nothing authored needs it as a spendable cost.
        "action_cost": {
            "material": _choices(dungeon.MATERIALS),
            "consumable": _choices(dungeon.CONSUMABLES),
            "quest_item": _choices(quests.QUEST_ITEMS),
        },
        # A skill's own subclass options depend on its main_class -- keyed by main_class the same
        # way every other cascade here is keyed by a sibling "kind" select, backing the 4 real
        # dungeon.SUBCLASSES rows plus dungeon.NO_SUBCLASS (added explicitly -- NO_SUBCLASS is
        # deliberately not a member of SUBCLASSES itself, see that registry's own comment). Each
        # real-suit option shows the actual 16-name-grid build name plus its card (rank letter +
        # suit symbol) rather than the bare suit id, e.g. "The Muscle (A♣)" instead of "clubs" --
        # NO_SUBCLASS gets its own label instead, since it has no suit glyph and display_name falls
        # back to the bare main-class name for it.
        "skill_subclass": {
            main_class: [
                [
                    subclass,
                    "— Base (no subclass yet) —" if subclass == dungeon.NO_SUBCLASS else
                    f"{dungeon.display_name(main_class, subclass)} "
                    f"({dungeon.CLASSES[main_class]['rank']}{dungeon.SUBCLASSES[subclass]['symbol']})",
                ]
                for subclass in list(dungeon.SUBCLASSES) + [dungeon.NO_SUBCLASS]
            ]
            for main_class in dungeon.CLASSES
        },
        # A class build's (dungeon_class_builds.json's) own subclass options -- same shape as
        # skill_subclass above but without the NO_SUBCLASS option, since a build's display name is
        # never "none" (that state is handled by display_name's own CLASSES fallback, not a
        # class_builds row).
        "class_build_subclass": {
            main_class: [
                [
                    subclass,
                    f"{dungeon.display_name(main_class, subclass)} "
                    f"({dungeon.CLASSES[main_class]['rank']}{dungeon.SUBCLASSES[subclass]['symbol']})",
                ]
                for subclass in dungeon.SUBCLASSES
            ]
            for main_class in dungeon.CLASSES
        },
    }


def _dynamic_script() -> str:
    data_script = (
        "var TRIGGER_PARAMS_BY_TYPE = " + json.dumps(_TRIGGER_PARAMS_BY_TYPE) + ";\n"
        "var EFFECT_PARAMS_BY_TYPE = " + json.dumps(EFFECT_PARAMS_BY_TYPE) + ";\n"
        "var EFFECT_TYPE_HINTS = " + json.dumps(EFFECT_TYPE_HINTS) + ";\n"
        "var CASCADE_OPTIONS = " + json.dumps(_cascade_options()) + ";\n"
        "var DELVE_ROOM_FIELDS_BY_TYPE = " + json.dumps(_DELVE_ROOM_FIELDS_BY_TYPE) + ";\n"
        "var ROOM_NAMES = " + json.dumps({rid: r["name"] for rid, r in rooms.ROOMS.items()}) + ";\n"
    )
    return data_script + """
// Same idea as trigger param visibility, for a delve room's detail panel: monsters only matters
// for a combat room, prompt/actions only for a choice room -- see _render_room_detail_panel's
// data-room-field wrappers and _DELVE_ROOM_FIELDS_BY_TYPE above.
function updateRoomTypeVisibility(select) {
    var container = select.closest('.room-row');
    if (!container) return;
    var visible = DELVE_ROOM_FIELDS_BY_TYPE[select.value] || [];
    container.querySelectorAll('[data-room-field]').forEach(function (el) {
        el.style.display = visible.indexOf(el.dataset.roomField) !== -1 ? '' : 'none';
    });
}

function wireRoomTypeSelects(root) {
    root.querySelectorAll('select.room-type-select').forEach(function (select) {
        updateRoomTypeVisibility(select);
        select.addEventListener('change', function () { updateRoomTypeVisibility(select); });
    });
}

function updateTriggerVisibility(select) {
    var container = select.closest('.trigger-fields');
    var visible = TRIGGER_PARAMS_BY_TYPE[select.value] || [];
    container.querySelectorAll('[data-param]').forEach(function (label) {
        var show = visible.indexOf(label.dataset.param) !== -1;
        label.style.display = show ? '' : 'none';
        if (!show) {
            var input = label.querySelector('input, select');
            if (input) input.value = '';
        }
    });
}

function wireTriggerSelects(root) {
    root.querySelectorAll('select.trigger-type-select').forEach(function (select) {
        updateTriggerVisibility(select);
        select.addEventListener('change', function () { updateTriggerVisibility(select); });
    });
}

// Same idea as trigger param visibility above, for an effects row: value/reduction/multiplier
// are flattened into every row (see _render_effect_row) but at most one ever applies to a given
// type, so the rest are hidden (and cleared) once a type is picked. effect-hint is swapped to
// EFFECT_TYPE_HINTS' plain-English explanation of what that one param actually means for this
// type, instead of a bare "value"/"reduction"/"multiplier" label with no context.
function updateEffectVisibility(select) {
    var container = select.closest('.row-group');
    if (!container) return;
    var visible = EFFECT_PARAMS_BY_TYPE[select.value] || [];
    container.querySelectorAll('[data-param]').forEach(function (label) {
        var show = visible.indexOf(label.dataset.param) !== -1;
        label.style.display = show ? '' : 'none';
        if (!show) {
            var input = label.querySelector('input');
            if (input) input.value = '';
        }
    });
    var hintEl = container.querySelector('.effect-hint');
    if (hintEl) hintEl.textContent = EFFECT_TYPE_HINTS[select.value] || '';
}

function wireEffectSelects(root) {
    root.querySelectorAll('select.effect-type-select').forEach(function (select) {
        updateEffectVisibility(select);
        select.addEventListener('change', function () { updateEffectVisibility(select); });
    });
}

// Same idea as trigger param visibility above, for a room_commands row: modal_title/input_label
// only matter when kind is "amount", so they're hidden otherwise instead of padding every row
// with boxes that don't apply to it.
function updateCommandKindVisibility(select) {
    var container = select.closest('.row-group');
    var isAmount = select.value === 'amount';
    container.querySelectorAll('[data-amount-only]').forEach(function (label) {
        label.style.display = isAmount ? '' : 'none';
    });
}

function wireCommandKindSelects(root) {
    root.querySelectorAll('select.command-kind-select').forEach(function (select) {
        updateCommandKindVisibility(select);
        select.addEventListener('change', function () { updateCommandKindVisibility(select); });
    });
}

// A room_exits row's label is free text, but it usually just wants to be the target room's name --
// picking a room_id fills it in as a convenience. Only overwrites the label when it's still blank
// or still holds whatever this same auto-fill last put there (tracked via data-auto-label), so an
// admin who typed a shorter/custom label never gets it silently clobbered by a later selection change.
function updateExitLabel(select) {
    var container = select.closest('.row-group');
    if (!container) return;
    var label = container.querySelector('.exit-label-input');
    if (!label) return;
    if (label.value && label.value !== label.dataset.autoLabel) return;
    var roomName = ROOM_NAMES[select.value] || '';
    label.value = roomName;
    label.dataset.autoLabel = roomName;
}

function wireRoomExitSelects(root) {
    root.querySelectorAll('select.room-exit-room-select').forEach(function (select) {
        select.addEventListener('change', function () { updateExitLabel(select); });
    });
}

// Same idea again, for an equipment effect row: chance only matters when trigger is "on_hit" (a
// constant/on_use effect never rolls a chance) -- see dungeon.py's module comment above
// _validate_equipment_effects for what each trigger means. Distinct from
// updateTriggerVisibility/wireTriggerSelects above, which is a different "trigger" concept
// entirely (a choice-room action's requires condition type).
function updateEquipmentTriggerVisibility(select) {
    var container = select.closest('.row-group');
    if (!container) return;
    var isOnHit = select.value === 'on_hit';
    container.querySelectorAll('[data-trigger-only]').forEach(function (label) {
        label.style.display = isOnHit ? '' : 'none';
        if (!isOnHit) {
            var input = label.querySelector('input');
            if (input) input.value = '';
        }
    });
}

function wireEquipmentTriggerSelects(root) {
    root.querySelectorAll('select.equipment-trigger-select').forEach(function (select) {
        updateEquipmentTriggerVisibility(select);
        select.addEventListener('change', function () { updateEquipmentTriggerVisibility(select); });
    });
}

// A skill/consumable/monster-skill authors EXACTLY ONE of "effects" or "effect_groups" (see
// dungeon._validate_effects_or_groups) -- this switches which fieldset is even visible, replacing
// the old "both boxes shown at once, fill in only one" warning-only UI. Only clears the
// now-hidden side's rows when a person actually flips the switch (`alsoClear`), never on the
// initial wire-time call -- an existing entry loaded with real data in one box must not lose it
// just because the page rendered.
function updateEffectsMode(select, alsoClear) {
    var wrap = select.closest('[data-effects-mode-wrap]');
    if (!wrap) return;
    var mode = select.value;
    var effectsBox = wrap.querySelector('[data-effects-box]');
    var groupsBox = wrap.querySelector('[data-groups-box]');
    if (effectsBox) {
        effectsBox.style.display = mode === 'effects' ? '' : 'none';
        if (alsoClear && mode !== 'effects') clearRepeatablesIn(effectsBox);
    }
    if (groupsBox) {
        groupsBox.style.display = mode === 'groups' ? '' : 'none';
        if (alsoClear && mode !== 'groups') clearRepeatablesIn(groupsBox);
    }
    updateSkillOdds();
    updateGroupOdds();
}

// Empties every repeatable's row container within `box` (back to zero rows) -- used when a mode
// toggle hides a side, so its now-invisible rows don't still ride along in the submitted form and
// trip the server's exactly-one-of XOR check.
function clearRepeatablesIn(box) {
    box.querySelectorAll('[data-repeat-add]').forEach(function (btn) {
        var container = document.getElementById(btn.dataset.repeatAdd);
        if (container) container.innerHTML = '';
    });
}

function wireEffectsModeToggles(root) {
    root.querySelectorAll('select.effects-mode-select').forEach(function (select) {
        if (select.dataset.modeWired) return;
        select.dataset.modeWired = '1';
        updateEffectsMode(select, false);
        select.addEventListener('change', function () { updateEffectsMode(select, true); });
    });
}

// A cascaded_id select's options depend on a sibling kind/output_kind select's value -- see
// admin_schemas.py's "cascaded_id" field type docs. The sibling carries data-cascade="<name>"
// (a key into CASCADE_OPTIONS); the target select carries data-cascade-target="<name>" and is
// looked up within the nearest .row-group (a shop row) or, if there isn't one (recipes' top-level
// output_kind/output_id pair), the whole document. Unlike updateTriggerVisibility/
// updateEffectVisibility this doesn't need to run at wire time -- the row's initial render
// already has the correct options for its current kind (see _render_cascaded_select /
// _render_shop_row), so this only ever runs in response to the user changing the kind select.
function updateCascade(select) {
    var scope = select.closest('.row-group') || document;
    var target = scope.querySelector('[data-cascade-target="' + select.dataset.cascade + '"]');
    if (!target) return;
    var options = (CASCADE_OPTIONS[select.dataset.cascade] || {})[select.value] || [];
    var current = target.value;
    target.textContent = '';
    var blank = document.createElement('option');
    blank.value = '';
    blank.textContent = '\\u2014';
    target.appendChild(blank);
    options.forEach(function (pair) {
        var opt = document.createElement('option');
        opt.value = pair[0];
        opt.textContent = pair[1];
        if (pair[0] === current) opt.selected = true;
        target.appendChild(opt);
    });
}

function wireCascadingSelects(root) {
    root.querySelectorAll('select.cascade-select').forEach(function (select) {
        select.addEventListener('change', function () { updateCascade(select); });
    });
}

// A monster's skills fieldset (data-skill-odds, see admin_schemas.py's "monster_skills" field
// type) shows the *actual* percent chance of each option -- dungeon.pick_monster_action picks one
// of [plain attack (weight = attack_chance), skill 1 (weight = its own chance), skill 2, ...] via
// random.choices, so "what are the real odds" only falls out once every weight in the row is known
// together, not from staring at one chance value in isolation. No-op (returns immediately) on any
// page without this fieldset, so it's safe to call unconditionally from every place a skill row
// could change -- typing in attack_chance/a chance field, adding a skill, removing one.
function updateSkillOdds() {
    var summary = document.getElementById('skill-odds-summary');
    if (!summary) return;
    var attackInput = document.querySelector('input[name="attack_chance"]');
    var attackRaw = attackInput ? attackInput.value.trim() : '';
    var attackWeight = attackRaw === '' ? 1 : parseFloat(attackRaw);
    if (isNaN(attackWeight) || attackWeight < 0) attackWeight = 0;

    var entries = [{label: '⚔️ Plain attack', weight: attackWeight, pctEl: null}];
    document.querySelectorAll('[data-skill-row]').forEach(function (row) {
        var chanceInput = row.querySelector('[data-skill-chance]');
        var nameInput = row.querySelector('[data-skill-name]');
        var raw = chanceInput ? chanceInput.value.trim() : '';
        var weight = raw === '' ? 0 : parseFloat(raw);
        if (isNaN(weight) || weight < 0) weight = 0;
        var label = (nameInput && nameInput.value.trim()) || '(unnamed skill)';
        entries.push({label: label, weight: weight, pctEl: row.querySelector('[data-skill-pct]')});
    });

    var total = entries.reduce(function (sum, e) { return sum + e.weight; }, 0);
    entries.forEach(function (e) {
        if (!e.pctEl) return;
        e.pctEl.textContent = total > 0 ? (e.weight / total * 100).toFixed(1) + '%' : '—';
    });

    if (total <= 0) {
        summary.textContent = 'Every weight is 0 -- this monster could never act at all.';
        return;
    }
    summary.innerHTML = entries.map(function (e) {
        var pct = (e.weight / total * 100).toFixed(1);
        var safeLabel = e.label.replace(/&/g, '&amp;').replace(/</g, '&lt;');
        return '<span class="skill-odds-item"><b>' + pct + '%</b> ' + safeLabel + '</span>';
    }).join('');
}

// Group sibling of updateSkillOdds -- a combat room's own monster_groups (dungeon.
// pick_monster_group) picks one group via the exact same weighted-random shape a monster's own
// skills use, so this mirrors updateSkillOdds almost exactly. The one real difference: a monster's
// skills are ONE fieldset per whole page, but a delve's flowchart canvas can have MANY combat
// rooms at once, each with its own independent set of groups -- so this scopes itself to every
// [data-group-odds] fieldset separately (one per room) rather than assuming a single page-wide
// summary element. Blank chance defaults to 1 here (unlike a skill's blank chance, which has no
// server-side default and just reads as weight 0) -- see dungeon.DEFAULT_MONSTER_GROUP_CHANCE.
// No-op wherever there's no such fieldset (a non-delve page, or a delve with no combat rooms yet),
// so it's safe to call unconditionally alongside updateSkillOdds everywhere that already runs.
function updateGroupOdds() {
    document.querySelectorAll('[data-group-odds]').forEach(function (fieldset) {
        var summary = fieldset.querySelector('[data-group-odds-summary]');
        if (!summary) return;
        var entries = [];
        fieldset.querySelectorAll('[data-group-row]').forEach(function (row, idx) {
            var chanceInput = row.querySelector('[data-group-chance]');
            var raw = chanceInput ? chanceInput.value.trim() : '';
            var weight = raw === '' ? 1 : parseFloat(raw);
            if (isNaN(weight) || weight < 0) weight = 0;
            entries.push({label: 'Group ' + (idx + 1), weight: weight, pctEl: row.querySelector('[data-group-pct]')});
        });

        var total = entries.reduce(function (sum, e) { return sum + e.weight; }, 0);
        entries.forEach(function (e) {
            if (e.pctEl) e.pctEl.textContent = total > 0 ? (e.weight / total * 100).toFixed(1) + '%' : '—';
        });

        if (entries.length === 0) {
            summary.textContent = 'Live odds appear here once groups are filled in.';
        } else if (total <= 0) {
            summary.textContent = 'Every weight is 0 -- this room could never pick a group at all.';
        } else {
            summary.innerHTML = entries.map(function (e) {
                var pct = (e.weight / total * 100).toFixed(1);
                return '<span class="skill-odds-item"><b>' + pct + '%</b> ' + e.label + '</span>';
            }).join('');
        }
    });
}

// Substitutes ONE nesting level's ROWIDX placeholder with `index` across name/id/data-repeat-add
// attributes under `root` -- ONLY the first ROWIDX occurrence in each attribute (no /g flag),
// since a doubly-nested repeatable's deeper placeholder (e.g. a skill's own
// "effectgroup_ROWIDX_effects_ROWIDX_type") has TWO ROWIDX tokens stacked in one string, one per
// level, and only the OUTERMOST (leftmost, since prefixes nest left-to-right) one belongs to
// *this* level -- the rest must stay untouched for whichever future "+ Add" click resolves that
// deeper level. Also recurses into any nested <template>'s .content, which a plain
// querySelectorAll on `root` would never reach (template content lives in a separate inert
// document) -- without this recursion a freshly-added outer row's own nested template keeps BOTH
// its ROWIDX placeholders live, so the *next* level's "+ Add" click (global-replacing every
// ROWIDX it can see) stomps the still-unresolved outer one with its own index too, silently
// renaming the row into a slot some other outer row already owns (the bug this fixes: two
// effect_groups' first effect each landing on "effectgroup_0_effects_0_..." instead of
// "effectgroup_0_..." / "effectgroup_1_...", so only one group's effects ever reached the server).
function substituteRowIdx(root, index) {
    root.querySelectorAll('[name]').forEach(function (el) {
        el.name = el.name.replace(/ROWIDX/, String(index));
    });
    root.querySelectorAll('[id]').forEach(function (el) {
        el.id = el.id.replace(/ROWIDX/, String(index));
    });
    root.querySelectorAll('[data-repeat-add]').forEach(function (el) {
        el.dataset.repeatAdd = el.dataset.repeatAdd.replace(/ROWIDX/, String(index));
    });
    root.querySelectorAll('template').forEach(function (tpl) {
        substituteRowIdx(tpl.content, index);
    });
}

// Wires every [data-repeat-add] button under `root` that isn't already wired -- called once at
// page load (root=document) and again on every freshly-cloned row (root=that row), since a clone
// can itself introduce a new "+ Add" button one level down (a delve room's own "+ Add monster"/
// "+ Add action" buttons, nested inside the "+ Add Room" template, or a skill/consumable's own
// "+ Add group"'s nested "+ Add effect") that only exist from this point on and need their own
// click handler wired. See substituteRowIdx above for how ROWIDX resolves one level at a time.
function wireRepeatAdd(root) {
    root.querySelectorAll('[data-repeat-add]').forEach(function (button) {
        if (button.dataset.repeatWired) return;
        button.dataset.repeatWired = '1';
        var container = document.getElementById(button.dataset.repeatAdd);
        var template = document.getElementById(button.dataset.repeatAdd + '-template');
        var nextIndex = container.children.length;
        button.addEventListener('click', function () {
            var clone = template.content.cloneNode(true);
            substituteRowIdx(clone, nextIndex);
            container.appendChild(clone);
            wireTriggerSelects(container.lastElementChild);
            wireEffectSelects(container.lastElementChild);
            wireCommandKindSelects(container.lastElementChild);
            wireEquipmentTriggerSelects(container.lastElementChild);
            wireCascadingSelects(container.lastElementChild);
            wireImagePreviews(container.lastElementChild);
            wireRoomTypeSelects(container.lastElementChild);
            wireRoomExitSelects(container.lastElementChild);
            wireEffectsModeToggles(container.lastElementChild);
            wireRepeatAdd(container.lastElementChild);
            // Flowchart-only hooks -- no-ops everywhere else (window.wireFlowchartNode only
            // exists on a delve's or a quest's own edit page, see the two flowchart scripts
            // below -- whichever one's canvas is actually on the page is the one that sets these).
            // Handles both a freshly-cloned top-level node (a .room-wrapper/.stage-wrapper straight
            // from "+ Add Room"/"+ Add Stage") and a freshly-cloned row nested inside an
            // already-open detail panel (a room's monster/action row, or a stage's path row).
            if (window.wireFlowchartNode) window.wireFlowchartNode(container.lastElementChild);
            if (window.refreshRoomBoxIfNested) window.refreshRoomBoxIfNested(container.lastElementChild);
            if (window.__scheduleFlowchartAutosave) window.__scheduleFlowchartAutosave();
            updateSkillOdds();
            updateGroupOdds();
            nextIndex++;
        });
    });
}

wireTriggerSelects(document);
wireEffectSelects(document);
wireCommandKindSelects(document);
wireEquipmentTriggerSelects(document);
wireRoomTypeSelects(document);
wireCascadingSelects(document);
wireRoomExitSelects(document);
wireEffectsModeToggles(document);
wireRepeatAdd(document);
updateSkillOdds();
updateGroupOdds();
// Cheap enough to just run on every keystroke anywhere on the page -- updateSkillOdds/
// updateGroupOdds both no-op immediately (see their own comments) on any page without their
// respective fieldset.
document.addEventListener('input', updateSkillOdds);
document.addEventListener('input', updateGroupOdds);

document.addEventListener('click', function (event) {
    if (event.target.matches('[data-remove-row]')) {
        var group = event.target.closest('.row-group');
        // A row being removed can be nested inside either flowchart's own wrapper (a delve room's
        // monster/action row, or a quest stage's path row) -- both flowchart scripts expose the
        // same window.refreshRoomBoxIfNested hook name for their own kind of node, so whichever
        // wrapper is actually found (at most one ever will be, since the two never coexist on one
        // page) drives the same generic call.
        var wrapper = group.closest('.room-wrapper') || group.closest('.stage-wrapper');
        group.remove();
        if (wrapper && window.refreshRoomBoxIfNested) window.refreshRoomBoxIfNested(wrapper);
        if (window.__scheduleFlowchartAutosave) window.__scheduleFlowchartAutosave();
        updateSkillOdds();
        updateGroupOdds();
    } else if (event.target.matches('[data-remove-room]')) {
        event.target.closest('.room-wrapper').remove();
        if (window.__redrawDelveArrows) window.__redrawDelveArrows();
        if (window.__scheduleFlowchartAutosave) window.__scheduleFlowchartAutosave();
    } else if (event.target.matches('[data-remove-stage]')) {
        event.target.closest('.stage-wrapper').remove();
        if (window.__redrawQuestArrows) window.__redrawQuestArrows();
        if (window.__scheduleFlowchartAutosave) window.__scheduleFlowchartAutosave();
    }
});

// Shared tooltip bubble for every [data-tooltip] element on any page (delve flowchart controls,
// but usable anywhere) -- one floating div, positioned under whatever's hovered, rather than
// relying on the native title="" tooltip (slow to appear, can't be styled, wraps badly).
(function () {
    var bubble = document.getElementById('tooltip-bubble');
    if (!bubble) {
        bubble = document.createElement('div');
        bubble.id = 'tooltip-bubble';
        document.body.appendChild(bubble);
    }
    document.addEventListener('mouseover', function (event) {
        var target = event.target.closest('[data-tooltip]');
        if (!target) return;
        bubble.textContent = target.dataset.tooltip;
        var rect = target.getBoundingClientRect();
        bubble.style.display = 'block';
        var left = rect.left;
        if (left + bubble.offsetWidth > window.innerWidth - 8) left = window.innerWidth - bubble.offsetWidth - 8;
        bubble.style.left = Math.max(8, left) + 'px';
        bubble.style.top = (rect.bottom + 6) + 'px';
    });
    document.addEventListener('mouseout', function (event) {
        if (event.target.closest('[data-tooltip]')) bubble.style.display = 'none';
    });
})();

// --- Delve flowchart editor -----------------------------------------------------------------
// No-ops on every page except a delve's edit page (guarded by the #delve-rooms-canvas lookup
// below) -- see admin_schemas.py's "delve_flowchart" doc block and admin_server.py's
// _render_delve_flowchart/_render_room_box/_render_room_detail_panel for the HTML this operates
// on. Exposes wireFlowchartNode/refreshRoomBoxIfNested/__redrawDelveArrows on window so the
// generic wireRepeatAdd/remove-row handlers above (which know nothing about delves specifically)
// can call into it without this script needing to know about *them* either.
(function () {
    var canvas = document.getElementById('delve-rooms-canvas');
    var svg = canvas ? canvas.parentElement.querySelector('svg.delve-arrows') : null;
    if (!canvas || !svg) return;

    function roomsById() {
        var map = {};
        canvas.querySelectorAll('.room-wrapper').forEach(function (w) {
            var idInput = w.querySelector('.room-id-input');
            if (idInput && idInput.value) map[idInput.value] = w;
        });
        return map;
    }

    function boxCenter(el) {
        var canvasRect = canvas.getBoundingClientRect();
        var r = el.getBoundingClientRect();
        return { x: r.left - canvasRect.left + r.width / 2, y: r.top - canvasRect.top + r.height / 2 };
    }

    function boxRect(el) {
        var canvasRect = canvas.getBoundingClientRect();
        var r = el.getBoundingClientRect();
        return { left: r.left - canvasRect.left, top: r.top - canvasRect.top, right: r.right - canvasRect.left, bottom: r.bottom - canvasRect.top };
    }

    // Clips the segment source->target to wherever it first crosses into `rect` -- the standard
    // ray/AABB "slab" test. Used so an arrow's arrowhead lands right on the target box's nearest
    // edge (whichever edge the straight line from the other end actually reaches first) instead
    // of terminating at its center, buried under the box's own content and, with several edges
    // converging on one room, indistinguishable from each other right where they'd matter most.
    function clipToRect(source, target, rect) {
        var dx = target.x - source.x, dy = target.y - source.y;
        var tmin = 0, tmax = 1;
        if (dx !== 0) {
            var tx1 = (rect.left - source.x) / dx, tx2 = (rect.right - source.x) / dx;
            tmin = Math.max(tmin, Math.min(tx1, tx2));
            tmax = Math.min(tmax, Math.max(tx1, tx2));
        } else if (source.x < rect.left || source.x > rect.right) {
            return target;
        }
        if (dy !== 0) {
            var ty1 = (rect.top - source.y) / dy, ty2 = (rect.bottom - source.y) / dy;
            tmin = Math.max(tmin, Math.min(ty1, ty2));
            tmax = Math.min(tmax, Math.max(ty1, ty2));
        } else if (source.y < rect.top || source.y > rect.bottom) {
            return target;
        }
        if (tmin > tmax) return target;
        return { x: source.x + tmin * dx, y: source.y + tmin * dy };
    }

    function clearSvg() {
        while (svg.firstChild) svg.removeChild(svg.firstChild);
        var defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
        [['success', '#40a060'], ['fail', '#c04040'], ['owner', '#7a7ae0']].forEach(function (pair) {
            var marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
            marker.setAttribute('id', 'arrowhead-' + pair[0]);
            marker.setAttribute('markerWidth', '10');
            marker.setAttribute('markerHeight', '10');
            marker.setAttribute('refX', '8');
            marker.setAttribute('refY', '3');
            marker.setAttribute('orient', 'auto');
            var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('d', 'M0,0 L8,3 L0,6 Z');
            path.setAttribute('fill', pair[1]);
            marker.appendChild(path);
            defs.appendChild(marker);
        });
        svg.appendChild(defs);
    }

    function drawEdge(fromBox, toBox, kind, label, onClear) {
        var from = boxCenter(fromBox);
        var to = clipToRect(from, boxCenter(toBox), boxRect(toBox));
        var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', from.x); line.setAttribute('y1', from.y);
        line.setAttribute('x2', to.x); line.setAttribute('y2', to.y);
        line.setAttribute('stroke', kind === 'fail' ? '#c04040' : '#40a060');
        line.setAttribute('stroke-width', '2');
        if (kind === 'fail') line.setAttribute('stroke-dasharray', '6,4');
        line.setAttribute('marker-end', 'url(#arrowhead-' + kind + ')');
        svg.appendChild(line);

        // Labels sit 35% of the way from source to target (not the exact midpoint) -- with several
        // actions/edges in a small area, exact midpoints collide far more often than an off-center
        // point does. A background rect behind the text means an unavoidable overlap at least
        // occludes cleanly instead of interleaving both labels' letters into an unreadable mess.
        var labelX = from.x + (to.x - from.x) * 0.35, labelY = from.y + (to.y - from.y) * 0.35;
        if (label) {
            var bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            var w = Math.min(label.length * 6 + 8, 140), h = 14;
            bg.setAttribute('x', labelX - w / 2); bg.setAttribute('y', labelY - 10 - h + 3);
            bg.setAttribute('width', w); bg.setAttribute('height', h);
            bg.setAttribute('fill', '#14141a'); bg.setAttribute('opacity', '0.85');
            svg.appendChild(bg);
            var text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', labelX); text.setAttribute('y', labelY - 10);
            text.setAttribute('fill', '#c0c0c8'); text.setAttribute('font-size', '11');
            text.setAttribute('text-anchor', 'middle');
            text.textContent = label;
            svg.appendChild(text);
        }
        // Faint by default (CSS :hover on the group brings it to full opacity) so a busy diagram's
        // lines aren't dominated by disconnect controls -- still exactly where a line visually is,
        // just not competing with it for attention until you're actually looking to remove that
        // connection. Grouped so the circle and its "x" fade together -- hovering only the text
        // (pointer-events: none, so it can never itself be the :hover target) would otherwise
        // leave it looking stuck at low opacity while the circle beneath it brightened.
        var clearGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        clearGroup.setAttribute('class', 'arrow-disconnect');
        var clearDot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        clearDot.setAttribute('cx', labelX); clearDot.setAttribute('cy', labelY); clearDot.setAttribute('r', '6');
        clearDot.setAttribute('fill', '#1a1a1f'); clearDot.setAttribute('stroke', '#52525e');
        clearDot.style.cursor = 'pointer'; clearDot.style.pointerEvents = 'auto';
        clearDot.setAttribute('data-tooltip', 'Disconnect this arrow');
        var clearX = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        clearX.setAttribute('x', labelX); clearX.setAttribute('y', labelY + 3);
        clearX.setAttribute('fill', '#e8e8ec'); clearX.setAttribute('font-size', '8'); clearX.setAttribute('text-anchor', 'middle');
        clearX.style.pointerEvents = 'none';
        clearX.textContent = '✕';
        clearDot.addEventListener('click', function (e) { e.stopPropagation(); onClear(); redrawAllArrows(); scheduleAutosave(); });
        clearGroup.appendChild(clearDot);
        clearGroup.appendChild(clearX);
        svg.appendChild(clearGroup);
    }

    function drawOwnerLine(roomBox, actionNode) {
        // Clipped at both ends -- unlike drawEdge's success/fail arrows, both endpoints here are
        // real boxes (a room and its action), not a small handle dot, so both deserve the same
        // "lands on the nearest edge" treatment instead of visibly starting inside one of them.
        var roomRect = boxRect(roomBox), actionRect = boxRect(actionNode);
        var roomCenter = boxCenter(roomBox), actionCenter = boxCenter(actionNode);
        var from = clipToRect(actionCenter, roomCenter, roomRect);
        var to = clipToRect(roomCenter, actionCenter, actionRect);
        var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', from.x); line.setAttribute('y1', from.y);
        line.setAttribute('x2', to.x); line.setAttribute('y2', to.y);
        line.setAttribute('marker-end', 'url(#arrowhead-owner)');
        line.setAttribute('class', 'action-owner-line');
        svg.appendChild(line);
    }

    function redrawAllArrows() {
        clearSvg();
        var byId = roomsById();
        canvas.querySelectorAll('.room-wrapper').forEach(function (w) {
            var typeSelect = w.querySelector('.room-type-select');
            var type = typeSelect ? typeSelect.value : 'combat';
            var box = w.querySelector('.room-box');
            if (type === 'combat') {
                var nextInput = w.querySelector('.room-next-input');
                var handle = box.querySelector('.connector-handle');
                if (handle && nextInput && nextInput.value && byId[nextInput.value]) {
                    drawEdge(handle, byId[nextInput.value].querySelector('.room-box'), 'success', null,
                        function () { nextInput.value = ''; });
                }
                // Each monster group has its own connector box, same "distinct line per sub-item"
                // shape as an action's own node below -- a group override is drawn as its own
                // edge rather than bunched onto the room's own handle above.
                w.querySelectorAll('.group-node').forEach(function (node) {
                    drawOwnerLine(box, node);
                    var prefix = node.dataset.groupNode;
                    var groupInput = document.getElementsByName(prefix + '_next')[0];
                    var groupHandle = node.querySelector('.connector-handle');
                    var labelSpan = node.querySelector('.group-node-label');
                    var label = labelSpan ? labelSpan.textContent : null;
                    if (groupHandle && groupInput && groupInput.value && byId[groupInput.value]) {
                        drawEdge(groupHandle, byId[groupInput.value].querySelector('.room-box'), 'success', label,
                            function () { groupInput.value = ''; });
                    }
                });
            } else {
                // Each action has its own connector box on the canvas (see _render_action_node /
                // syncActionNodes) instead of a handle bunched onto the room's own box -- edges
                // originate from that box, not `box` itself, so two actions leading to different
                // rooms produce two visually distinct lines instead of two lines leaving the exact
                // same point. The dashed line back to the room is just a "this belongs to that
                // room" indicator, not an editable connection.
                w.querySelectorAll('.action-node').forEach(function (node) {
                    drawOwnerLine(box, node);
                    var prefix = node.dataset.actionNode;
                    var successInput = document.getElementsByName(prefix + '_success_next')[0];
                    var failInput = document.getElementsByName(prefix + '_fail_next')[0];
                    var successHandle = node.querySelector('.connector-handle:not(.fail)');
                    var failHandle = node.querySelector('.connector-handle.fail');
                    var labelSpan = node.querySelector('.action-node-label');
                    var label = labelSpan ? labelSpan.textContent : null;
                    if (successHandle && successInput && successInput.value && byId[successInput.value]) {
                        drawEdge(successHandle, byId[successInput.value].querySelector('.room-box'), 'success', label,
                            function () { successInput.value = ''; });
                    }
                    if (failHandle && failInput && failInput.value && byId[failInput.value]) {
                        drawEdge(failHandle, byId[failInput.value].querySelector('.room-box'), 'fail', label,
                            function () { failInput.value = ''; });
                    }
                });
            }
        });
    }

    function updateLiveNextTags(root) {
        (root || document).querySelectorAll('[data-live-next]').forEach(function (span) {
            var input = document.getElementsByName(span.getAttribute('data-live-next'))[0];
            if (!input) return;
            span.textContent = input.value ? ('→ ' + input.value) : '→ (wins the delve)';
        });
    }

    function refreshRoomBox(wrapper) {
        var box = wrapper.querySelector('.room-box');
        var typeSelect = wrapper.querySelector('.room-type-select');
        var type = typeSelect ? typeSelect.value : 'combat';
        var summary = box.querySelector('[data-room-box-summary]');
        if (type === 'combat') {
            var monsterCount = wrapper.querySelectorAll('[data-room-field="groups"] [data-monster-row]').length;
            var groupCount = wrapper.querySelectorAll('[data-room-field="groups"] [data-monster-group]').length;
            summary.textContent = monsterCount + (monsterCount === 1 ? ' monster' : ' monsters') +
                ' (' + groupCount + (groupCount === 1 ? ' group)' : ' groups)');
        } else {
            var actionRows = wrapper.querySelectorAll('.action-row');
            summary.textContent = actionRows.length + (actionRows.length === 1 ? ' action' : ' actions');
        }
        var idInput = wrapper.querySelector('.room-id-input');
        var idLabel = box.querySelector('[data-room-box-id]');
        if (idInput && idLabel) idLabel.textContent = idInput.value || '(no id yet)';
        updateLiveNextTags(wrapper);
        redrawAllArrows();
    }

    // Finds whichever .room-box the given viewport point currently sits over, if any -- used both
    // to highlight a drop candidate while dragging and to resolve the actual drop target on
    // release, so what got highlighted is exactly what gets connected (no separate, potentially
    // inconsistent hit-test).
    function roomBoxAtPoint(clientX, clientY) {
        var found = null;
        canvas.querySelectorAll('.room-box').forEach(function (otherBox) {
            if (found) return;
            var r = otherBox.getBoundingClientRect();
            if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom) found = otherBox;
        });
        return found;
    }

    function wireConnectorHandle(handle) {
        if (!handle || handle.dataset.connectWired) return;
        handle.dataset.connectWired = '1';
        handle.addEventListener('pointerdown', function (e) {
            e.stopPropagation();
            e.preventDefault();
            handle.setPointerCapture(e.pointerId);
            var role = handle.dataset.connectorRole;
            var actionPrefix = handle.dataset.actionPrefix || null;
            var groupPrefix = handle.dataset.groupPrefix || null;
            var wrapper = handle.closest('.room-wrapper');
            var canvasRect = canvas.getBoundingClientRect();
            var start = boxCenter(handle);
            var rubberBand = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            rubberBand.setAttribute('x1', start.x); rubberBand.setAttribute('y1', start.y);
            rubberBand.setAttribute('x2', start.x); rubberBand.setAttribute('y2', start.y);
            rubberBand.setAttribute('stroke', role === 'fail' ? '#c04040' : '#40a060');
            rubberBand.setAttribute('stroke-width', '2');
            rubberBand.setAttribute('stroke-dasharray', '4,4');
            svg.appendChild(rubberBand);
            var hoverTarget = null;

            function setHover(box) {
                if (hoverTarget === box) return;
                if (hoverTarget) hoverTarget.classList.remove('drop-target-hover');
                hoverTarget = box;
                if (hoverTarget) hoverTarget.classList.add('drop-target-hover');
            }
            function onMove(ev) {
                rubberBand.setAttribute('x2', ev.clientX - canvasRect.left);
                rubberBand.setAttribute('y2', ev.clientY - canvasRect.top);
                setHover(roomBoxAtPoint(ev.clientX, ev.clientY));
            }
            function onUp(ev) {
                handle.removeEventListener('pointermove', onMove);
                handle.removeEventListener('pointerup', onUp);
                if (rubberBand.parentNode) rubberBand.parentNode.removeChild(rubberBand);
                setHover(null);

                // A miss (dropped on empty canvas, or the panel/anything else covering it) leaves
                // the existing connection exactly as it was -- it must NOT silently clear to
                // blank, since blank means "this leads to winning the delve." Only an explicit
                // click on an arrow's own "disconnect" glyph (see drawEdge) is allowed to do that.
                var targetBox = roomBoxAtPoint(ev.clientX, ev.clientY);
                if (!targetBox) { redrawAllArrows(); return; }
                var otherIdInput = targetBox.closest('.room-wrapper').querySelector('.room-id-input');
                if (!otherIdInput || !otherIdInput.value) { redrawAllArrows(); return; }

                var targetInput = actionPrefix
                    ? document.getElementsByName(actionPrefix + '_' + (role === 'fail' ? 'fail_next' : 'success_next'))[0]
                    : groupPrefix
                    ? document.getElementsByName(groupPrefix + '_next')[0]
                    : wrapper.querySelector('.room-next-input');
                if (targetInput) {
                    targetInput.value = otherIdInput.value;
                    updateLiveNextTags(wrapper);
                    scheduleAutosave();
                }
                redrawAllArrows();
            }
            handle.addEventListener('pointermove', onMove);
            handle.addEventListener('pointerup', onUp);
        });
    }

    function wireActionNodeClick(node, wrapper) {
        if (node.dataset.clickWired) return;
        node.dataset.clickWired = '1';
        node.addEventListener('click', function (e) {
            if (e.target.closest('[data-connector-role]')) return;
            selectRoom(wrapper);
        });
    }

    // Action/group nodes are freely draggable, same shape as wireBoxDrag but simpler (no
    // click-to-select filtering beyond the connector handles, and no ownership of anything else
    // that needs to move with it) -- position persists directly on the node's own hidden x/y
    // inputs, not derived from its parent room, so dragging a room no longer drags its
    // actions/groups along with it. xClass/yClass parametrize which hidden inputs to write to --
    // action nodes and group nodes each use their own class (.action-x-input/.action-y-input vs.
    // .group-x-input/.group-y-input) rather than sharing one, so the two node kinds' positions
    // can never cross-write into each other's inputs.
    function wireActionNodeDrag(node, xClass, yClass) {
        if (node.dataset.dragWired) return;
        node.dataset.dragWired = '1';
        node.addEventListener('pointerdown', function (e) {
            if (e.target.closest('[data-connector-role]')) return;
            node.setPointerCapture(e.pointerId);
            node.classList.add('dragging');
            var xInput = node.querySelector(xClass);
            var yInput = node.querySelector(yClass);
            function onMove(ev) {
                var newLeft = parseFloat(node.style.left) + ev.movementX;
                var newTop = parseFloat(node.style.top) + ev.movementY;
                node.style.left = newLeft + 'px';
                node.style.top = newTop + 'px';
                xInput.value = Math.round(newLeft);
                yInput.value = Math.round(newTop);
                redrawAllArrows();
            }
            function onUp() {
                node.classList.remove('dragging');
                node.removeEventListener('pointermove', onMove);
                node.removeEventListener('pointerup', onUp);
                scheduleAutosave();
            }
            node.addEventListener('pointermove', onMove);
            node.addEventListener('pointerup', onUp);
        });
    }

    // Rebuilds this room's action nodes from scratch from whatever action rows currently exist in
    // its (possibly still-hidden) detail panel -- same "clear and rebuild" approach the old
    // in-box handle list used, just producing standalone sibling boxes on the canvas instead (see
    // _render_action_node's docstring for why: separate boxes so different actions' lines don't
    // bunch up at the same corner of one shared room box). Existing nodes' dragged positions are
    // captured before the rebuild and carried over by prefix -- without this, editing anything
    // about an action (its label, its check) would silently snap every action in the room back to
    // its default stacked position, undoing whatever the user had just arranged.
    function syncActionNodes(wrapper) {
        var existingPositions = {};
        wrapper.querySelectorAll('.action-node').forEach(function (n) {
            existingPositions[n.dataset.actionNode] = { x: parseFloat(n.style.left) || 0, y: parseFloat(n.style.top) || 0 };
        });
        wrapper.querySelectorAll('.action-node').forEach(function (n) { n.remove(); });
        var typeSelect = wrapper.querySelector('.room-type-select');
        if (!typeSelect || typeSelect.value !== 'choice') { redrawAllArrows(); return; }

        var box = wrapper.querySelector('.room-box');
        var roomPos = { x: parseFloat(box.style.left) || 0, y: parseFloat(box.style.top) || 0 };
        var freshIndex = 0;
        wrapper.querySelectorAll('.action-row').forEach(function (actionRow) {
            var labelInput = actionRow.querySelector('input[name$="_label"]');
            if (!labelInput) return;
            var prefix = labelInput.name.slice(0, -('_label'.length));
            var statInput = actionRow.querySelector('[name="' + prefix + '_check_stat"]');
            var dcInput = actionRow.querySelector('[name="' + prefix + '_check_dc"]');
            var chanceInput = actionRow.querySelector('[name="' + prefix + '_check_chance"]');
            var hasCheck = !!((chanceInput && chanceInput.value) || (statInput && dcInput && statInput.value && dcInput.value));
            var label = labelInput.value || '(unlabeled)';

            var node = document.createElement('div');
            node.className = 'action-node';
            node.dataset.actionNode = prefix;
            var pos = existingPositions[prefix];
            if (!pos) { pos = { x: roomPos.x + 190, y: roomPos.y + freshIndex * 74 }; freshIndex++; }
            node.style.left = pos.x + 'px';
            node.style.top = pos.y + 'px';

            var xInput = document.createElement('input');
            xInput.type = 'hidden'; xInput.className = 'action-x-input'; xInput.name = prefix + '_x'; xInput.value = pos.x;
            node.appendChild(xInput);
            var yInput = document.createElement('input');
            yInput.type = 'hidden'; yInput.className = 'action-y-input'; yInput.name = prefix + '_y'; yInput.value = pos.y;
            node.appendChild(yInput);

            var labelSpan = document.createElement('span');
            labelSpan.className = 'action-node-label';
            labelSpan.textContent = label;
            node.appendChild(labelSpan);

            var successDot = document.createElement('div');
            successDot.className = 'connector-handle';
            successDot.dataset.connectorRole = 'success';
            successDot.dataset.actionPrefix = prefix;
            successDot.dataset.tooltip = 'Drag onto another room -- where ' + label + ' leads ' +
                (hasCheck ? 'if the check succeeds.' : 'when the player picks it.');
            node.appendChild(successDot);
            wireConnectorHandle(successDot);

            if (hasCheck) {
                var failDot = document.createElement('div');
                failDot.className = 'connector-handle fail';
                failDot.dataset.connectorRole = 'fail';
                failDot.dataset.actionPrefix = prefix;
                failDot.dataset.tooltip = 'Drag onto another room -- where ' + label + ' leads if the check fails.';
                node.appendChild(failDot);
                wireConnectorHandle(failDot);
            }

            wireActionNodeClick(node, wrapper);
            wireActionNodeDrag(node, '.action-x-input', '.action-y-input');
            wrapper.appendChild(node);
        });
        redrawAllArrows();
    }

    // Group sibling of syncActionNodes -- same "clear and rebuild from whatever rows currently
    // exist" approach, for a combat room's monster_groups instead of a choice room's actions. A
    // group has no label input to key its prefix off of (unlike an action), so its own "chance"
    // input's name is used instead -- every group row always renders one, even a freshly-added,
    // otherwise-empty group (see _render_room_monster_group_row). No "fail" handle variant --
    // groups have no check/fail concept, just the one override.
    function syncGroupNodes(wrapper) {
        var existingPositions = {};
        wrapper.querySelectorAll('.group-node').forEach(function (n) {
            existingPositions[n.dataset.groupNode] = { x: parseFloat(n.style.left) || 0, y: parseFloat(n.style.top) || 0 };
        });
        wrapper.querySelectorAll('.group-node').forEach(function (n) { n.remove(); });
        var typeSelect = wrapper.querySelector('.room-type-select');
        if (!typeSelect || typeSelect.value !== 'combat') { redrawAllArrows(); return; }

        var box = wrapper.querySelector('.room-box');
        var roomPos = { x: parseFloat(box.style.left) || 0, y: parseFloat(box.style.top) || 0 };
        var freshIndex = 0;
        wrapper.querySelectorAll('.monster-group').forEach(function (groupRow, i) {
            var chanceInput = groupRow.querySelector('[data-group-chance]');
            if (!chanceInput) return;
            var forkInput = groupRow.querySelector('.group-fork-input');
            if (!forkInput || !forkInput.checked) return;  // unforked -- no node, uses the room's default
            var prefix = chanceInput.name.slice(0, -('_chance'.length));
            var label = 'Group ' + (i + 1);

            var node = document.createElement('div');
            node.className = 'group-node';
            node.dataset.groupNode = prefix;
            var pos = existingPositions[prefix];
            if (!pos) { pos = { x: roomPos.x + 190, y: roomPos.y + freshIndex * 74 }; freshIndex++; }
            node.style.left = pos.x + 'px';
            node.style.top = pos.y + 'px';

            var xInput = document.createElement('input');
            xInput.type = 'hidden'; xInput.className = 'group-x-input'; xInput.name = prefix + '_x'; xInput.value = pos.x;
            node.appendChild(xInput);
            var yInput = document.createElement('input');
            yInput.type = 'hidden'; yInput.className = 'group-y-input'; yInput.name = prefix + '_y'; yInput.value = pos.y;
            node.appendChild(yInput);

            var labelSpan = document.createElement('span');
            labelSpan.className = 'group-node-label';
            labelSpan.textContent = label;
            node.appendChild(labelSpan);

            var dot = document.createElement('div');
            dot.className = 'connector-handle';
            dot.dataset.connectorRole = 'success';
            dot.dataset.groupPrefix = prefix;
            dot.dataset.tooltip = 'Drag onto another room -- where ' + label + ' leads after the fight, overriding the room\\'s own next. Drop on empty canvas to disconnect back to the room\\'s default.';
            node.appendChild(dot);
            wireConnectorHandle(dot);

            wireActionNodeClick(node, wrapper);
            wireActionNodeDrag(node, '.group-x-input', '.group-y-input');
            wrapper.appendChild(node);
        });
        redrawAllArrows();
    }

    function syncRoomHandles(wrapper) {
        var typeSelect = wrapper.querySelector('.room-type-select');
        var box = wrapper.querySelector('.room-box');
        var type = typeSelect.value;
        box.classList.remove('room-box-combat', 'room-box-choice');
        box.classList.add('room-box-' + type);
        box.querySelector('.room-box-icon').textContent = type === 'combat' ? '⚔️' : '💬';

        if (type === 'combat') {
            wrapper.querySelectorAll('.action-node').forEach(function (n) { n.remove(); });
            if (!box.querySelector('.connector-handle')) {
                var handle = document.createElement('div');
                handle.className = 'connector-handle';
                handle.dataset.connectorRole = 'success';
                handle.dataset.tooltip = 'Drag onto another room -- where the player goes after winning the fight here (a group\\'s own override, if any, wins over this). Drop on empty canvas to disconnect.';
                box.appendChild(handle);
                wireConnectorHandle(handle);
            }
            if (!wrapper.querySelector('.room-next-input')) {
                var nextInput = document.createElement('input');
                nextInput.type = 'hidden';
                nextInput.className = 'room-next-input';
                nextInput.name = wrapper.dataset.roomWrapper + '_next';
                box.appendChild(nextInput);
            }
            syncGroupNodes(wrapper);
        } else {
            var existingHandle = box.querySelector('.connector-handle');
            if (existingHandle) existingHandle.remove();
            wrapper.querySelectorAll('.group-node').forEach(function (n) { n.remove(); });
            syncActionNodes(wrapper);
        }
        refreshRoomBox(wrapper);
    }

    function selectRoom(wrapper) {
        canvas.querySelectorAll('.room-box').forEach(function (b) { b.classList.remove('selected'); });
        document.querySelectorAll('.room-detail-panel').forEach(function (p) { p.hidden = true; });
        wrapper.querySelector('.room-box').classList.add('selected');
        wrapper.querySelector('.room-detail-panel').hidden = false;
    }

    function setStartRoom(wrapper) {
        var idInput = wrapper.querySelector('.room-id-input');
        if (!idInput || !idInput.value) return;
        document.querySelectorAll('.room-box').forEach(function (b) { b.classList.remove('is-start'); });
        document.querySelectorAll('.room-flag').forEach(function (f) { f.classList.remove('is-start'); });
        wrapper.querySelector('.room-box').classList.add('is-start');
        wrapper.querySelector('.room-flag').classList.add('is-start');
        document.getElementById('start_room_field').value = idInput.value;
        scheduleAutosave();
    }

    function renameRoomReferences(oldId, newId) {
        if (!oldId || oldId === newId) return;
        canvas.querySelectorAll('.room-next-input, .group-next-input, input[name$="_success_next"], input[name$="_fail_next"]').forEach(function (input) {
            if (input.value === oldId) input.value = newId;
        });
        var startField = document.getElementById('start_room_field');
        if (startField.value === oldId) startField.value = newId;
        updateLiveNextTags(document);
        redrawAllArrows();
    }

    function wireIdInput(wrapper) {
        var idInput = wrapper.querySelector('.room-id-input');
        if (!idInput || idInput.dataset.wired) return;
        idInput.dataset.wired = '1';
        var lastValue = idInput.value;
        idInput.addEventListener('input', function () {
            wrapper.querySelector('[data-room-box-id]').textContent = idInput.value || '(no id yet)';
        });
        idInput.addEventListener('blur', function () {
            renameRoomReferences(lastValue, idInput.value);
            lastValue = idInput.value;
            if (!document.getElementById('start_room_field').value) setStartRoom(wrapper);
        });
    }

    function wireBoxDrag(box) {
        if (box.dataset.dragWired) return;
        box.dataset.dragWired = '1';
        box.addEventListener('pointerdown', function (e) {
            if (e.target.closest('[data-connector-role]') || e.target.closest('button')) return;
            box.setPointerCapture(e.pointerId);
            box.classList.add('dragging');
            var wrapper = box.closest('.room-wrapper');
            var xInput = wrapper.querySelector('.room-x-input');
            var yInput = wrapper.querySelector('.room-y-input');
            function onMove(ev) {
                var newLeft = parseFloat(box.style.left) + ev.movementX;
                var newTop = parseFloat(box.style.top) + ev.movementY;
                box.style.left = newLeft + 'px';
                box.style.top = newTop + 'px';
                xInput.value = Math.round(newLeft);
                yInput.value = Math.round(newTop);
                redrawAllArrows();
            }
            function onUp() {
                box.classList.remove('dragging');
                box.removeEventListener('pointermove', onMove);
                box.removeEventListener('pointerup', onUp);
                scheduleAutosave();
            }
            box.addEventListener('pointermove', onMove);
            box.addEventListener('pointerup', onUp);
        });
        box.addEventListener('click', function (e) {
            if (e.target.closest('[data-connector-role]') || e.target.closest('button')) return;
            selectRoom(box.closest('.room-wrapper'));
        });
    }

    // Wires the "keep the box's on-canvas display in sync" listeners for whatever action/label/
    // check inputs exist under `root` right now -- `root` can be a whole room (initial page load)
    // or just one freshly-added action row (root is narrower than `wrapper` in that case, since a
    // new action row is cloned into an already-open detail panel, not as part of a new room). This
    // must NOT be folded into wireFlowchartNode's own "only operates on a whole .room-wrapper"
    // guard below -- a freshly-added action row isn't a .room-wrapper itself, so that guard would
    // (and previously did) silently skip wiring it entirely, leaving its label/check inputs
    // permanently unwired: typing a label or adding a check would never update the box's handle
    // list or live-next text, which looked exactly like "the label never sticks until you save
    // and reopen" even though the value itself was being captured correctly all along.
    function wireActionSyncInputs(root, wrapper) {
        root.querySelectorAll('.action-check-input, input[name$="_label"]').forEach(function (input) {
            if (input.dataset.actionSyncWired) return;
            input.dataset.actionSyncWired = '1';
            input.addEventListener('input', function () { syncActionNodes(wrapper); refreshRoomBox(wrapper); });
        });
    }

    // Group sibling of wireActionSyncInputs -- the Fork checkbox is the only thing that decides
    // whether a group gets a canvas node at all (see syncGroupNodes' own early-return), so toggling
    // it needs an immediate re-sync, not just on the next add/remove-row pass. Unchecking also
    // clears the group's own hidden next input (its value would otherwise sit invisibly in effect
    // with no node left to show or edit it from) -- same "toggling off never leaves a stale
    // override behind" guarantee _parse_delve_flowchart's own server-side drop enforces either way.
    function wireGroupSyncInputs(root, wrapper) {
        root.querySelectorAll('.group-fork-input').forEach(function (input) {
            if (input.dataset.groupSyncWired) return;
            input.dataset.groupSyncWired = '1';
            input.addEventListener('change', function () {
                if (!input.checked) {
                    var container = input.closest('.row-group');
                    var nextInput = container ? container.querySelector('.group-next-input') : null;
                    if (nextInput) nextInput.value = '';
                    updateLiveNextTags(container || document);
                }
                syncGroupNodes(wrapper);
                refreshRoomBox(wrapper);
                scheduleAutosave();
            });
        });
    }

    function wireFlowchartNode(node) {
        if (!node || !node.classList || !node.classList.contains('room-wrapper')) return;
        var box = node.querySelector('.room-box');
        wireBoxDrag(box);
        node.querySelectorAll('[data-connector-role]').forEach(wireConnectorHandle);
        node.querySelectorAll('.action-node').forEach(function (actionNode) {
            wireActionNodeClick(actionNode, node);
            wireActionNodeDrag(actionNode, '.action-x-input', '.action-y-input');
        });
        node.querySelectorAll('.group-node').forEach(function (groupNode) {
            wireActionNodeClick(groupNode, node);
            wireActionNodeDrag(groupNode, '.group-x-input', '.group-y-input');
        });
        var flag = node.querySelector('[data-set-start]');
        if (flag && !flag.dataset.wired) {
            flag.dataset.wired = '1';
            flag.addEventListener('click', function (e) { e.stopPropagation(); setStartRoom(node); });
        }
        var selectBtn = node.querySelector('.room-box-select');
        if (selectBtn && !selectBtn.dataset.wired) {
            selectBtn.dataset.wired = '1';
            selectBtn.addEventListener('click', function (e) { e.stopPropagation(); selectRoom(node); });
        }
        var closeBtn = node.querySelector('.room-detail-close');
        if (closeBtn && !closeBtn.dataset.wired) {
            closeBtn.dataset.wired = '1';
            closeBtn.addEventListener('click', function () {
                node.querySelector('.room-detail-panel').hidden = true;
                box.classList.remove('selected');
            });
        }
        wireIdInput(node);
        var typeSelect = node.querySelector('.room-type-select');
        if (typeSelect && !typeSelect.dataset.flowchartWired) {
            typeSelect.dataset.flowchartWired = '1';
            typeSelect.addEventListener('change', function () { syncRoomHandles(node); });
        }
        wireActionSyncInputs(node, node);
        wireGroupSyncInputs(node, node);
        refreshRoomBox(node);
    }

    function refreshRoomBoxIfNested(el) {
        var wrapper = el && el.closest ? el.closest('.room-wrapper') : null;
        if (!wrapper) return;
        wireActionSyncInputs(el, wrapper);
        wireGroupSyncInputs(el, wrapper);
        syncActionNodes(wrapper);
        syncGroupNodes(wrapper);
        refreshRoomBox(wrapper);
    }

    // Draft autosave -- a delve's edit form has no "Save" button at all (see edit_view/
    // flowchart_autosave_view); every meaningful change instead debounces a background POST that
    // persists a draft (dungeon.save_delve_draft) with no validation gate, and the response's
    // structured problem list (dungeon.check_delve_problems) drives the .has-error highlighting
    // on the canvas below, so a still-broken draft is visible right where it's wrong without
    // waiting for an explicit Publish attempt. This is the one place this admin panel uses
    // fetch()/AJAX -- everywhere else is a plain full-page form POST (see the module docstring).
    var form = canvas.closest('form');
    var statusEl = document.getElementById('draft-save-status');
    var autosaveTimer = null;
    var autosaveInFlight = false;
    var autosavePending = false;

    function setStatus(text, cls) {
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.className = 'field-hint' + (cls ? ' ' + cls : '');
    }

    function applyProblems(problems) {
        canvas.querySelectorAll('.room-box.has-error, .action-node.has-error').forEach(function (el) {
            el.classList.remove('has-error');
            el.removeAttribute('data-tooltip');
        });
        var byId = roomsById();
        var roomMsgs = {}, actionMsgs = {};
        (problems || []).forEach(function (p) {
            if (!p.room_id) return; // delve-level (e.g. unreachable rooms) -- shown in the page banner only
            if (p.action_index === null || p.action_index === undefined) {
                (roomMsgs[p.room_id] = roomMsgs[p.room_id] || []).push(p.message);
            } else {
                actionMsgs[p.room_id] = actionMsgs[p.room_id] || {};
                (actionMsgs[p.room_id][p.action_index] = actionMsgs[p.room_id][p.action_index] || []).push(p.message);
            }
        });
        Object.keys(roomMsgs).forEach(function (rid) {
            var wrapper = byId[rid];
            if (!wrapper) return;
            var box = wrapper.querySelector('.room-box');
            box.classList.add('has-error');
            box.setAttribute('data-tooltip', roomMsgs[rid].join(' / '));
        });
        Object.keys(actionMsgs).forEach(function (rid) {
            var wrapper = byId[rid];
            if (!wrapper) return;
            Object.keys(actionMsgs[rid]).forEach(function (idx) {
                var node = wrapper.querySelector('[data-action-node="' + wrapper.dataset.roomWrapper + '_actions_' + idx + '"]');
                if (!node) return;
                node.classList.add('has-error');
                node.setAttribute('data-tooltip', actionMsgs[rid][idx].join(' / '));
            });
        });
    }

    function runAutosave(isRetry) {
        if (autosaveInFlight) { autosavePending = true; return; }
        autosaveInFlight = true;
        setStatus('Saving…', 'saving');
        var itemId = form.dataset.flowchartItemId;
        fetch('/edit/delves/' + encodeURIComponent(itemId) + '/autosave', { method: 'POST', body: new FormData(form) })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                autosaveInFlight = false;
                if (!data.ok) throw new Error(data.error || 'autosave rejected');
                if (data.id && data.id !== itemId) {
                    // A brand-new delve just got its first real id (or an existing one was
                    // renamed) -- retarget future autosave/publish calls and the visible URL
                    // without a reload, so the draft stays findable if the tab is closed/reopened
                    // (edit_view looks a draft up by the URL's own item_id).
                    form.dataset.flowchartItemId = data.id;
                    var publishBtn = form.querySelector('button[formaction]');
                    if (publishBtn) publishBtn.setAttribute('formaction', '/edit/delves/' + encodeURIComponent(data.id) + '/publish');
                    history.replaceState(null, '', '/edit/delves/' + encodeURIComponent(data.id));
                }
                setStatus(data.id ? ('Saved as draft · ' + new Date().toLocaleTimeString()) : 'Waiting for an id…', 'saved');
                applyProblems(data.problems);
                if (autosavePending) { autosavePending = false; scheduleAutosave(); }
            })
            .catch(function () {
                autosaveInFlight = false;
                if (!isRetry) {
                    setStatus('Autosave failed, retrying…', 'failed');
                    setTimeout(function () { runAutosave(true); }, 3000);
                } else {
                    setStatus('Autosave failed -- edits are only in this browser tab until it succeeds.', 'failed');
                }
            });
    }

    function scheduleAutosave() {
        if (autosaveTimer) clearTimeout(autosaveTimer);
        autosaveTimer = setTimeout(runAutosave, 1800);
    }

    if (form) {
        form.addEventListener('input', function (e) { if (e.target.type !== 'file' || e.target.value) scheduleAutosave(); });
        form.addEventListener('change', scheduleAutosave);
    }
    window.__scheduleFlowchartAutosave = scheduleAutosave;

    window.wireFlowchartNode = wireFlowchartNode;
    window.refreshRoomBoxIfNested = refreshRoomBoxIfNested;
    window.__redrawDelveArrows = redrawAllArrows;

    canvas.querySelectorAll('.room-wrapper').forEach(wireFlowchartNode);
    redrawAllArrows();
})();

// --- Quest flowchart editor -------------------------------------------------------------------
// No-ops on every page except a quest's edit page (guarded by the #quest-stages-canvas lookup
// below) -- see admin_schemas.py's "quest_flowchart" doc block and admin_server.py's
// _render_quest_flowchart/_render_stage_box/_render_stage_detail_panel for the HTML this operates
// on. A separate, simpler script from the delve one above rather than a shared factory -- a quest
// stage is always structurally the delve-choice-room shape (no type split, no monster groups, no
// check/fail concept on a path), so this needs none of that branching, and duplicating the small
// amount of shared geometry math (boxCenter/boxRect/clipToRect/drawEdge) was judged a better
// tradeoff than risking a regression to the already-working delve script for the sake of one
// shared factory -- see the CSS comment at the top of _PAGE_CSS for the same reasoning. Exposes
// wireFlowchartNode/refreshRoomBoxIfNested/__redrawQuestArrows on window under the SAME hook
// names the delve script above uses (never both at once -- only one canvas is ever on a page, so
// only one script's own guard below ever passes and actually assigns these), so the generic
// wireRepeatAdd/remove-row handlers (which know nothing about either flowchart specifically) can
// call into whichever one is active without needing to know which.
(function () {
    var canvas = document.getElementById('quest-stages-canvas');
    var svg = canvas ? canvas.parentElement.querySelector('svg.delve-arrows') : null;
    if (!canvas || !svg) return;

    function stagesById() {
        var map = {};
        canvas.querySelectorAll('.stage-wrapper').forEach(function (w) {
            var idInput = w.querySelector('.stage-id-input');
            if (idInput && idInput.value) map[idInput.value] = w;
        });
        return map;
    }

    // Same geometry as the delve script's own boxCenter/boxRect/clipToRect -- duplicated rather
    // than imported, since these are small, pure, canvas-local functions (each script has its own
    // `canvas` closed over) and not worth threading a shared module for.
    function boxCenter(el) {
        var canvasRect = canvas.getBoundingClientRect();
        var r = el.getBoundingClientRect();
        return { x: r.left - canvasRect.left + r.width / 2, y: r.top - canvasRect.top + r.height / 2 };
    }

    function boxRect(el) {
        var canvasRect = canvas.getBoundingClientRect();
        var r = el.getBoundingClientRect();
        return { left: r.left - canvasRect.left, top: r.top - canvasRect.top, right: r.right - canvasRect.left, bottom: r.bottom - canvasRect.top };
    }

    function clipToRect(source, target, rect) {
        var dx = target.x - source.x, dy = target.y - source.y;
        var tmin = 0, tmax = 1;
        if (dx !== 0) {
            var tx1 = (rect.left - source.x) / dx, tx2 = (rect.right - source.x) / dx;
            tmin = Math.max(tmin, Math.min(tx1, tx2));
            tmax = Math.min(tmax, Math.max(tx1, tx2));
        } else if (source.x < rect.left || source.x > rect.right) {
            return target;
        }
        if (dy !== 0) {
            var ty1 = (rect.top - source.y) / dy, ty2 = (rect.bottom - source.y) / dy;
            tmin = Math.max(tmin, Math.min(ty1, ty2));
            tmax = Math.min(tmax, Math.max(ty1, ty2));
        } else if (source.y < rect.top || source.y > rect.bottom) {
            return target;
        }
        if (tmin > tmax) return target;
        return { x: source.x + tmin * dx, y: source.y + tmin * dy };
    }

    function clearSvg() {
        while (svg.firstChild) svg.removeChild(svg.firstChild);
        var defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
        // Only 'success' and 'owner' -- a quest path never has a 'fail' edge (see the module
        // comment above: no check/fail concept exists for a path at all).
        [['success', '#40a060'], ['owner', '#7a7ae0']].forEach(function (pair) {
            var marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
            marker.setAttribute('id', 'arrowhead-' + pair[0]);
            marker.setAttribute('markerWidth', '10');
            marker.setAttribute('markerHeight', '10');
            marker.setAttribute('refX', '8');
            marker.setAttribute('refY', '3');
            marker.setAttribute('orient', 'auto');
            var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('d', 'M0,0 L8,3 L0,6 Z');
            path.setAttribute('fill', pair[1]);
            marker.appendChild(path);
            defs.appendChild(marker);
        });
        svg.appendChild(defs);
    }

    function drawEdge(fromBox, toBox, label, onClear) {
        var from = boxCenter(fromBox);
        var to = clipToRect(from, boxCenter(toBox), boxRect(toBox));
        var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', from.x); line.setAttribute('y1', from.y);
        line.setAttribute('x2', to.x); line.setAttribute('y2', to.y);
        line.setAttribute('stroke', '#40a060');
        line.setAttribute('stroke-width', '2');
        line.setAttribute('marker-end', 'url(#arrowhead-success)');
        svg.appendChild(line);

        var labelX = from.x + (to.x - from.x) * 0.35, labelY = from.y + (to.y - from.y) * 0.35;
        if (label) {
            var bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            var w = Math.min(label.length * 6 + 8, 140), h = 14;
            bg.setAttribute('x', labelX - w / 2); bg.setAttribute('y', labelY - 10 - h + 3);
            bg.setAttribute('width', w); bg.setAttribute('height', h);
            bg.setAttribute('fill', '#14141a'); bg.setAttribute('opacity', '0.85');
            svg.appendChild(bg);
            var text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', labelX); text.setAttribute('y', labelY - 10);
            text.setAttribute('fill', '#c0c0c8'); text.setAttribute('font-size', '11');
            text.setAttribute('text-anchor', 'middle');
            text.textContent = label;
            svg.appendChild(text);
        }
        var clearGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        clearGroup.setAttribute('class', 'arrow-disconnect');
        var clearDot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        clearDot.setAttribute('cx', labelX); clearDot.setAttribute('cy', labelY); clearDot.setAttribute('r', '6');
        clearDot.setAttribute('fill', '#1a1a1f'); clearDot.setAttribute('stroke', '#52525e');
        clearDot.style.cursor = 'pointer'; clearDot.style.pointerEvents = 'auto';
        clearDot.setAttribute('data-tooltip', 'Disconnect this arrow');
        var clearX = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        clearX.setAttribute('x', labelX); clearX.setAttribute('y', labelY + 3);
        clearX.setAttribute('fill', '#e8e8ec'); clearX.setAttribute('font-size', '8'); clearX.setAttribute('text-anchor', 'middle');
        clearX.style.pointerEvents = 'none';
        clearX.textContent = '✕';
        clearDot.addEventListener('click', function (e) { e.stopPropagation(); onClear(); redrawAllArrows(); scheduleAutosave(); });
        clearGroup.appendChild(clearDot);
        clearGroup.appendChild(clearX);
        svg.appendChild(clearGroup);
    }

    function drawOwnerLine(stageBox, pathNode) {
        var stageRect = boxRect(stageBox), pathRect = boxRect(pathNode);
        var stageCenter = boxCenter(stageBox), pathCenter = boxCenter(pathNode);
        var from = clipToRect(pathCenter, stageCenter, stageRect);
        var to = clipToRect(stageCenter, pathCenter, pathRect);
        var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', from.x); line.setAttribute('y1', from.y);
        line.setAttribute('x2', to.x); line.setAttribute('y2', to.y);
        line.setAttribute('marker-end', 'url(#arrowhead-owner)');
        line.setAttribute('class', 'action-owner-line');
        svg.appendChild(line);
    }

    function redrawAllArrows() {
        clearSvg();
        var byId = stagesById();
        canvas.querySelectorAll('.stage-wrapper').forEach(function (w) {
            var box = w.querySelector('.stage-box');
            w.querySelectorAll('.path-node').forEach(function (node) {
                drawOwnerLine(box, node);
                var prefix = node.dataset.pathNode;
                var nextInput = document.getElementsByName(prefix + '_next')[0];
                var handle = node.querySelector('.connector-handle');
                var labelSpan = node.querySelector('.path-node-label');
                var label = labelSpan ? labelSpan.textContent : null;
                if (handle && nextInput && nextInput.value && byId[nextInput.value]) {
                    drawEdge(handle, byId[nextInput.value].querySelector('.stage-box'), label,
                        function () { nextInput.value = ''; });
                }
            });
        });
    }

    function updateLiveNextTags(root) {
        (root || document).querySelectorAll('[data-live-next]').forEach(function (span) {
            var input = document.getElementsByName(span.getAttribute('data-live-next'))[0];
            if (!input) return;
            span.textContent = input.value ? ('→ ' + input.value) : '→ (ends the quest)';
        });
    }

    function refreshStageBox(wrapper) {
        var box = wrapper.querySelector('.stage-box');
        var summary = box.querySelector('[data-stage-box-summary]');
        var pathCount = wrapper.querySelectorAll('.path-row').length;
        summary.textContent = pathCount
            ? (pathCount + (pathCount === 1 ? ' path' : ' paths'))
            : 'Dialogue only (no paths yet)';
        var idInput = wrapper.querySelector('.stage-id-input');
        var idLabel = box.querySelector('[data-stage-box-id]');
        if (idInput && idLabel) idLabel.textContent = idInput.value || '(no id yet)';
        updateLiveNextTags(wrapper);
        redrawAllArrows();
    }

    function stageBoxAtPoint(clientX, clientY) {
        var found = null;
        canvas.querySelectorAll('.stage-box').forEach(function (otherBox) {
            if (found) return;
            var r = otherBox.getBoundingClientRect();
            if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom) found = otherBox;
        });
        return found;
    }

    function wireConnectorHandle(handle) {
        if (!handle || handle.dataset.connectWired) return;
        handle.dataset.connectWired = '1';
        handle.addEventListener('pointerdown', function (e) {
            e.stopPropagation();
            e.preventDefault();
            handle.setPointerCapture(e.pointerId);
            var pathPrefix = handle.dataset.pathPrefix;
            var wrapper = handle.closest('.stage-wrapper');
            var canvasRect = canvas.getBoundingClientRect();
            var start = boxCenter(handle);
            var rubberBand = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            rubberBand.setAttribute('x1', start.x); rubberBand.setAttribute('y1', start.y);
            rubberBand.setAttribute('x2', start.x); rubberBand.setAttribute('y2', start.y);
            rubberBand.setAttribute('stroke', '#40a060');
            rubberBand.setAttribute('stroke-width', '2');
            rubberBand.setAttribute('stroke-dasharray', '4,4');
            svg.appendChild(rubberBand);
            var hoverTarget = null;

            function setHover(box) {
                if (hoverTarget === box) return;
                if (hoverTarget) hoverTarget.classList.remove('drop-target-hover');
                hoverTarget = box;
                if (hoverTarget) hoverTarget.classList.add('drop-target-hover');
            }
            function onMove(ev) {
                rubberBand.setAttribute('x2', ev.clientX - canvasRect.left);
                rubberBand.setAttribute('y2', ev.clientY - canvasRect.top);
                setHover(stageBoxAtPoint(ev.clientX, ev.clientY));
            }
            function onUp(ev) {
                handle.removeEventListener('pointermove', onMove);
                handle.removeEventListener('pointerup', onUp);
                if (rubberBand.parentNode) rubberBand.parentNode.removeChild(rubberBand);
                setHover(null);

                // A miss leaves the existing connection exactly as it was -- it must NOT silently
                // clear to blank, since blank means "this path ends the quest." Only an explicit
                // click on an arrow's own "disconnect" glyph (see drawEdge) is allowed to do that.
                var targetBox = stageBoxAtPoint(ev.clientX, ev.clientY);
                if (!targetBox) { redrawAllArrows(); return; }
                var otherIdInput = targetBox.closest('.stage-wrapper').querySelector('.stage-id-input');
                if (!otherIdInput || !otherIdInput.value) { redrawAllArrows(); return; }

                var targetInput = document.getElementsByName(pathPrefix + '_next')[0];
                if (targetInput) {
                    targetInput.value = otherIdInput.value;
                    updateLiveNextTags(wrapper);
                    scheduleAutosave();
                }
                redrawAllArrows();
            }
            handle.addEventListener('pointermove', onMove);
            handle.addEventListener('pointerup', onUp);
        });
    }

    function wirePathNodeClick(node, wrapper) {
        if (node.dataset.clickWired) return;
        node.dataset.clickWired = '1';
        node.addEventListener('click', function (e) {
            if (e.target.closest('[data-connector-role]')) return;
            selectStage(wrapper);
        });
    }

    function wirePathNodeDrag(node) {
        if (node.dataset.dragWired) return;
        node.dataset.dragWired = '1';
        node.addEventListener('pointerdown', function (e) {
            if (e.target.closest('[data-connector-role]')) return;
            node.setPointerCapture(e.pointerId);
            node.classList.add('dragging');
            var xInput = node.querySelector('.path-x-input');
            var yInput = node.querySelector('.path-y-input');
            function onMove(ev) {
                var newLeft = parseFloat(node.style.left) + ev.movementX;
                var newTop = parseFloat(node.style.top) + ev.movementY;
                node.style.left = newLeft + 'px';
                node.style.top = newTop + 'px';
                xInput.value = Math.round(newLeft);
                yInput.value = Math.round(newTop);
                redrawAllArrows();
            }
            function onUp() {
                node.classList.remove('dragging');
                node.removeEventListener('pointermove', onMove);
                node.removeEventListener('pointerup', onUp);
                scheduleAutosave();
            }
            node.addEventListener('pointermove', onMove);
            node.addEventListener('pointerup', onUp);
        });
    }

    // Rebuilds this stage's path nodes from scratch from whatever path rows currently exist in
    // its (possibly still-hidden) detail panel -- same "clear and rebuild" approach
    // syncActionNodes (delves) uses. Existing nodes' dragged positions are captured before the
    // rebuild and carried over by prefix -- without this, editing anything about a path (its
    // trigger, its turn_in_label) would silently snap every path in the stage back to its default
    // stacked position.
    function syncPathNodes(wrapper) {
        var existingPositions = {};
        wrapper.querySelectorAll('.path-node').forEach(function (n) {
            existingPositions[n.dataset.pathNode] = { x: parseFloat(n.style.left) || 0, y: parseFloat(n.style.top) || 0 };
        });
        wrapper.querySelectorAll('.path-node').forEach(function (n) { n.remove(); });

        var box = wrapper.querySelector('.stage-box');
        var stagePos = { x: parseFloat(box.style.left) || 0, y: parseFloat(box.style.top) || 0 };
        var freshIndex = 0;
        wrapper.querySelectorAll('.path-row').forEach(function (pathRow) {
            var nextInput = pathRow.querySelector('input[name$="_next"]');
            if (!nextInput) return;
            var prefix = nextInput.name.slice(0, -('_next'.length));
            var turnInLabelInput = pathRow.querySelector('[name="' + prefix + '_turn_in_label"]');
            var typeSelect = pathRow.querySelector('[name="' + prefix + '_trigger_type"]');
            var label = (turnInLabelInput && turnInLabelInput.value) ||
                (typeSelect && typeSelect.value) || '(no trigger yet)';

            var node = document.createElement('div');
            node.className = 'path-node';
            node.dataset.pathNode = prefix;
            var pos = existingPositions[prefix];
            if (!pos) { pos = { x: stagePos.x + 190, y: stagePos.y + freshIndex * 74 }; freshIndex++; }
            node.style.left = pos.x + 'px';
            node.style.top = pos.y + 'px';

            var xInput = document.createElement('input');
            xInput.type = 'hidden'; xInput.className = 'path-x-input'; xInput.name = prefix + '_x'; xInput.value = pos.x;
            node.appendChild(xInput);
            var yInput = document.createElement('input');
            yInput.type = 'hidden'; yInput.className = 'path-y-input'; yInput.name = prefix + '_y'; yInput.value = pos.y;
            node.appendChild(yInput);

            var labelSpan = document.createElement('span');
            labelSpan.className = 'path-node-label';
            labelSpan.textContent = label;
            node.appendChild(labelSpan);

            var dot = document.createElement('div');
            dot.className = 'connector-handle';
            dot.dataset.connectorRole = 'success';
            dot.dataset.pathPrefix = prefix;
            dot.dataset.tooltip = 'Drag onto another stage -- where ' + label + ' leads once its trigger is satisfied.';
            node.appendChild(dot);
            wireConnectorHandle(dot);

            wirePathNodeClick(node, wrapper);
            wirePathNodeDrag(node);
            wrapper.appendChild(node);
        });
        redrawAllArrows();
    }

    function selectStage(wrapper) {
        canvas.querySelectorAll('.stage-box').forEach(function (b) { b.classList.remove('selected'); });
        document.querySelectorAll('.stage-detail-panel').forEach(function (p) { p.hidden = true; });
        wrapper.querySelector('.stage-box').classList.add('selected');
        wrapper.querySelector('.stage-detail-panel').hidden = false;
    }

    function setStartStage(wrapper) {
        var idInput = wrapper.querySelector('.stage-id-input');
        if (!idInput || !idInput.value) return;
        document.querySelectorAll('.stage-box').forEach(function (b) { b.classList.remove('is-start'); });
        document.querySelectorAll('.stage-flag').forEach(function (f) { f.classList.remove('is-start'); });
        wrapper.querySelector('.stage-box').classList.add('is-start');
        wrapper.querySelector('.stage-flag').classList.add('is-start');
        document.getElementById('start_stage_field').value = idInput.value;
        scheduleAutosave();
    }

    function renameStageReferences(oldId, newId) {
        if (!oldId || oldId === newId) return;
        canvas.querySelectorAll('input[name$="_next"]').forEach(function (input) {
            if (input.value === oldId) input.value = newId;
        });
        var startField = document.getElementById('start_stage_field');
        if (startField.value === oldId) startField.value = newId;
        updateLiveNextTags(document);
        redrawAllArrows();
    }

    function wireIdInput(wrapper) {
        var idInput = wrapper.querySelector('.stage-id-input');
        if (!idInput || idInput.dataset.wired) return;
        idInput.dataset.wired = '1';
        var lastValue = idInput.value;
        idInput.addEventListener('input', function () {
            wrapper.querySelector('[data-stage-box-id]').textContent = idInput.value || '(no id yet)';
        });
        idInput.addEventListener('blur', function () {
            renameStageReferences(lastValue, idInput.value);
            lastValue = idInput.value;
            if (!document.getElementById('start_stage_field').value) setStartStage(wrapper);
        });
    }

    function wireBoxDrag(box) {
        if (box.dataset.dragWired) return;
        box.dataset.dragWired = '1';
        box.addEventListener('pointerdown', function (e) {
            if (e.target.closest('[data-connector-role]') || e.target.closest('button')) return;
            box.setPointerCapture(e.pointerId);
            box.classList.add('dragging');
            var wrapper = box.closest('.stage-wrapper');
            var xInput = wrapper.querySelector('.stage-x-input');
            var yInput = wrapper.querySelector('.stage-y-input');
            function onMove(ev) {
                var newLeft = parseFloat(box.style.left) + ev.movementX;
                var newTop = parseFloat(box.style.top) + ev.movementY;
                box.style.left = newLeft + 'px';
                box.style.top = newTop + 'px';
                xInput.value = Math.round(newLeft);
                yInput.value = Math.round(newTop);
                redrawAllArrows();
            }
            function onUp() {
                box.classList.remove('dragging');
                box.removeEventListener('pointermove', onMove);
                box.removeEventListener('pointerup', onUp);
                scheduleAutosave();
            }
            box.addEventListener('pointermove', onMove);
            box.addEventListener('pointerup', onUp);
        });
        box.addEventListener('click', function (e) {
            if (e.target.closest('[data-connector-role]') || e.target.closest('button')) return;
            selectStage(box.closest('.stage-wrapper'));
        });
    }

    // Keeps a path's on-canvas node (label, live-next text) in sync as its trigger type or
    // turn_in_label changes -- see _describe_path's own docstring for why those two fields decide
    // a path node's displayed label. Same "root can be a whole stage or just one freshly-added
    // path row" flexibility wireActionSyncInputs (delves) documents, for the same reason: a new
    // path row is cloned into an already-open detail panel, not as part of a new stage.
    function wirePathSyncInputs(root, wrapper) {
        root.querySelectorAll('select[name$="_trigger_type"], input[name$="_turn_in_label"]').forEach(function (input) {
            if (input.dataset.pathSyncWired) return;
            input.dataset.pathSyncWired = '1';
            input.addEventListener('input', function () { syncPathNodes(wrapper); refreshStageBox(wrapper); });
        });
    }

    function wireFlowchartNode(node) {
        if (!node || !node.classList || !node.classList.contains('stage-wrapper')) return;
        var box = node.querySelector('.stage-box');
        wireBoxDrag(box);
        node.querySelectorAll('[data-connector-role]').forEach(wireConnectorHandle);
        node.querySelectorAll('.path-node').forEach(function (pathNode) {
            wirePathNodeClick(pathNode, node);
            wirePathNodeDrag(pathNode);
        });
        var flag = node.querySelector('[data-set-start]');
        if (flag && !flag.dataset.wired) {
            flag.dataset.wired = '1';
            flag.addEventListener('click', function (e) { e.stopPropagation(); setStartStage(node); });
        }
        var selectBtn = node.querySelector('.stage-box-select');
        if (selectBtn && !selectBtn.dataset.wired) {
            selectBtn.dataset.wired = '1';
            selectBtn.addEventListener('click', function (e) { e.stopPropagation(); selectStage(node); });
        }
        var closeBtn = node.querySelector('.stage-detail-close');
        if (closeBtn && !closeBtn.dataset.wired) {
            closeBtn.dataset.wired = '1';
            closeBtn.addEventListener('click', function () {
                node.querySelector('.stage-detail-panel').hidden = true;
                box.classList.remove('selected');
            });
        }
        wireIdInput(node);
        wirePathSyncInputs(node, node);
        refreshStageBox(node);
    }

    function refreshRoomBoxIfNested(el) {
        var wrapper = el && el.closest ? el.closest('.stage-wrapper') : null;
        if (!wrapper) return;
        wirePathSyncInputs(el, wrapper);
        syncPathNodes(wrapper);
        refreshStageBox(wrapper);
    }

    // Draft autosave -- same shape as the delve script's own scheduleAutosave/runAutosave, just
    // posting to this quest's own autosave/publish routes. See that script's own comment for why
    // this is the one place besides it that uses fetch()/AJAX rather than a plain form POST.
    var form = canvas.closest('form');
    var statusEl = document.getElementById('draft-save-status');
    var autosaveTimer = null;
    var autosaveInFlight = false;
    var autosavePending = false;

    function setStatus(text, cls) {
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.className = 'field-hint' + (cls ? ' ' + cls : '');
    }

    function applyProblems(problems) {
        canvas.querySelectorAll('.stage-box.has-error, .path-node.has-error').forEach(function (el) {
            el.classList.remove('has-error');
            el.removeAttribute('data-tooltip');
        });
        var byId = stagesById();
        var stageMsgs = {}, pathMsgs = {};
        (problems || []).forEach(function (p) {
            if (!p.stage_id) return; // quest-level (e.g. unreachable stages) -- page banner only
            if (p.path_index === null || p.path_index === undefined) {
                (stageMsgs[p.stage_id] = stageMsgs[p.stage_id] || []).push(p.message);
            } else {
                pathMsgs[p.stage_id] = pathMsgs[p.stage_id] || {};
                (pathMsgs[p.stage_id][p.path_index] = pathMsgs[p.stage_id][p.path_index] || []).push(p.message);
            }
        });
        Object.keys(stageMsgs).forEach(function (sid) {
            var wrapper = byId[sid];
            if (!wrapper) return;
            var box = wrapper.querySelector('.stage-box');
            box.classList.add('has-error');
            box.setAttribute('data-tooltip', stageMsgs[sid].join(' / '));
        });
        Object.keys(pathMsgs).forEach(function (sid) {
            var wrapper = byId[sid];
            if (!wrapper) return;
            Object.keys(pathMsgs[sid]).forEach(function (idx) {
                var node = wrapper.querySelector('[data-path-node="' + wrapper.dataset.stageWrapper + '_paths_' + idx + '"]');
                if (!node) return;
                node.classList.add('has-error');
                node.setAttribute('data-tooltip', pathMsgs[sid][idx].join(' / '));
            });
        });
    }

    function runAutosave(isRetry) {
        if (autosaveInFlight) { autosavePending = true; return; }
        autosaveInFlight = true;
        setStatus('Saving…', 'saving');
        var itemId = form.dataset.flowchartItemId;
        fetch('/edit/quests/' + encodeURIComponent(itemId) + '/autosave', { method: 'POST', body: new FormData(form) })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                autosaveInFlight = false;
                if (!data.ok) throw new Error(data.error || 'autosave rejected');
                if (data.id && data.id !== itemId) {
                    form.dataset.flowchartItemId = data.id;
                    var publishBtn = form.querySelector('button[formaction]');
                    if (publishBtn) publishBtn.setAttribute('formaction', '/edit/quests/' + encodeURIComponent(data.id) + '/publish');
                    history.replaceState(null, '', '/edit/quests/' + encodeURIComponent(data.id));
                }
                setStatus(data.id ? ('Saved as draft · ' + new Date().toLocaleTimeString()) : 'Waiting for an id…', 'saved');
                applyProblems(data.problems);
                if (autosavePending) { autosavePending = false; scheduleAutosave(); }
            })
            .catch(function () {
                autosaveInFlight = false;
                if (!isRetry) {
                    setStatus('Autosave failed, retrying…', 'failed');
                    setTimeout(function () { runAutosave(true); }, 3000);
                } else {
                    setStatus('Autosave failed -- edits are only in this browser tab until it succeeds.', 'failed');
                }
            });
    }

    function scheduleAutosave() {
        if (autosaveTimer) clearTimeout(autosaveTimer);
        autosaveTimer = setTimeout(runAutosave, 1800);
    }

    if (form) {
        form.addEventListener('input', function (e) { if (e.target.type !== 'file' || e.target.value) scheduleAutosave(); });
        form.addEventListener('change', scheduleAutosave);
    }
    window.__scheduleFlowchartAutosave = scheduleAutosave;

    window.wireFlowchartNode = wireFlowchartNode;
    window.refreshRoomBoxIfNested = refreshRoomBoxIfNested;
    window.__redrawQuestArrows = redrawAllArrows;

    canvas.querySelectorAll('.stage-wrapper').forEach(wireFlowchartNode);
    redrawAllArrows();
})();

// Filters a list_view's table rows by substring match against the whole row's text -- one shared
// listener rather than a per-page inline handler, matches every #list-filter box the same way.
var listFilter = document.getElementById('list-filter');
if (listFilter) {
    listFilter.addEventListener('input', function () {
        var needle = listFilter.value.trim().toLowerCase();
        document.querySelectorAll('#list-table tbody tr').forEach(function (row) {
            row.style.display = row.textContent.toLowerCase().indexOf(needle) === -1 ? 'none' : '';
        });
    });
}

// Click-to-sort headers for every list_view's table -- one shared listener, generic over
// whatever columns a given content type's schema happens to render (list_view never needs to
// know this exists). Numeric-looking columns (e.g. a monster's "tier") sort numerically; anything
// else sorts as case-insensitive text. Reorders actual <tr> elements in place via appendChild
// (which moves, not clones), so it composes for free with the filter box above -- a hidden row
// stays hidden after a re-sort, since sorting never touches style.display.
(function () {
    var table = document.getElementById('list-table');
    if (!table) return;
    var tbody = table.querySelector('tbody');
    var headers = table.querySelectorAll('thead th');
    var sortState = { col: -1, asc: true };
    headers.forEach(function (th, colIndex) {
        th.addEventListener('click', function () {
            var asc = sortState.col === colIndex ? !sortState.asc : true;
            sortState = { col: colIndex, asc: asc };
            headers.forEach(function (h) { h.classList.remove('sort-asc', 'sort-desc'); });
            th.classList.add(asc ? 'sort-asc' : 'sort-desc');

            var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
            rows.sort(function (a, b) {
                var av = (a.children[colIndex] ? a.children[colIndex].textContent : '').trim();
                var bv = (b.children[colIndex] ? b.children[colIndex].textContent : '').trim();
                var an = parseFloat(av), bn = parseFloat(bv);
                var cmp = (av !== '' && bv !== '' && !isNaN(an) && !isNaN(bn))
                    ? an - bn
                    : av.toLowerCase().localeCompare(bv.toLowerCase());
                return asc ? cmp : -cmp;
            });
            rows.forEach(function (row) { tbody.appendChild(row); });
        });
    });
})();
"""


def _sidebar_html(active_content_type: str | None) -> str:
    """Persistent left nav, present on every authenticated page -- grouped by category so the
    tool reads as a game's content layers (dungeon content vs. story) instead of a flat
    alphabetical list of JSON files. `active_content_type` highlights the current section."""
    sections = []
    for category in CATEGORIES:
        items = "".join(
            f'<a class="nav-item{" active" if key == active_content_type else ""}" href="/edit/{key}">'
            f'{spec["icon"]} {html.escape(spec["label"])}</a>'
            for key, spec in CONTENT_TYPES.items() if spec["category"] == category
        )
        sections.append(f'<div class="nav-category">{html.escape(category)}</div>{items}')
    assets_link = (
        f'<a class="nav-item{" active" if active_content_type == "assets" else ""}" href="/assets">🖼️ Assets</a>'
    )
    utilities_link = (
        f'<a class="nav-item{" active" if active_content_type == "utilities" else ""}" '
        f'href="/utilities">🧰 Utilities</a>'
    )
    player_debug_link = (
        f'<a class="nav-item{" active" if active_content_type == "player-debug" else ""}" '
        f'href="/player-debug">🐛 Player Debug</a>'
    )
    skill_balance_link = (
        f'<a class="nav-item{" active" if active_content_type == "skill-balance" else ""}" '
        f'href="/skill-balance">📊 Skill Balance</a>'
    )
    return (
        f'<nav class="sidebar"><a class="brand" href="/">🛠️ Content Editor</a>'
        f'{assets_link}{utilities_link}{player_debug_link}{skill_balance_link}{"".join(sections)}</nav>'
    )


def _breadcrumbs_html(crumbs: list[tuple[str, str | None]]) -> str:
    """crumbs: [(label, href)], href None for the current page (not a link)."""
    parts = [f'<a href="{href}">{html.escape(label)}</a>' if href else html.escape(label) for label, href in crumbs]
    return '<nav class="breadcrumbs">' + '<span class="sep">/</span>'.join(parts) + '</nav>'


def _page(title: str, body: str, nav: bool = True, active: str | None = None, breadcrumbs: list | None = None) -> str:
    if not nav:
        content = f"<main>{body}</main>"
    else:
        crumbs_html = _breadcrumbs_html(breadcrumbs) if breadcrumbs else ""
        content = f'<div class="app">{_sidebar_html(active)}<main>{crumbs_html}{body}</main></div>'
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        f"<style>{_PAGE_CSS}</style></head><body>{content}"
        f"<script>{_PREVIEW_SCRIPT}{_dynamic_script()}</script></body></html>"
    )


def _html_response(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, content_type="text/html", status=status)


# --- Field rendering / parsing -----------------------------------------------------------------

def _render_trigger_inputs(prefix: str, trigger: dict) -> str:
    """Renders one quests.TRIGGER_SCHEMAS condition's inputs -- a type <select> plus every known
    param flattened into the row (same "let the real loader enforce which params a given type
    actually needs" idea as _render_field's "effects" case). `prefix` namespaces the field names
    so this can be reused both for a quest's single top-level start_trigger and for each row of a
    stage's "paths" list.

    Wrapped in a "trigger-fields" div and each param label carries data-param="<name>" -- that's
    what the page script hooks onto TRIGGER_PARAMS_BY_TYPE to hide whichever params don't apply to
    the currently-selected type, so a trigger row shows only what's relevant instead of every param
    any type could ever use. Each param also gets its one-line TRIGGER_PARAM_HINTS explanation."""
    type_options = "".join(
        f'<option value="{t}"{" selected" if t == trigger.get("type") else ""}>{t}</option>'
        for t in [""] + TRIGGER_TYPES
    )
    param_inputs = []
    for p in TRIGGER_PARAM_NAMES:
        kind = TRIGGER_PARAM_KINDS[p]
        pval = trigger.get(p, "")
        if kind == "int":
            field_html = f'<input type="number" name="{prefix}_{p}" value="{pval}">'
        elif kind == "str":
            # A raw free-text param (currently just flag_at_least's "key") gets a <datalist> of
            # every flag key already in use, live from the DB -- still free text (new content can
            # still invent a brand-new key), but no longer a guessing game for what's already
            # meaningful. Each row needs its own datalist id (not shared globally) since the same
            # param name can appear in more than one row on a quest flowchart page.
            datalist_id = f"{prefix}_{p}_options"
            options = "".join(f"<option value=\"{html.escape(k)}\">" for k in db.get_distinct_flag_keys())
            field_html = (
                f'<input type="text" name="{prefix}_{p}" value="{html.escape(str(pval))}" list="{datalist_id}">'
                f'<datalist id="{datalist_id}">{options}</datalist>'
            )
        else:
            choice_options = "".join(
                f'<option value="{c}"{" selected" if c == pval else ""}>{c}</option>'
                for c in [""] + _TRIGGER_PARAM_CHOICES[kind]()
            )
            field_html = f'<select name="{prefix}_{p}">{choice_options}</select>'
        hint = html.escape(TRIGGER_PARAM_HINTS.get(p, ""))
        param_inputs.append(
            f'<label data-param="{p}">{p}{field_html}<small class="field-hint">{hint}</small></label>'
        )
    return (
        f'<div class="trigger-fields">'
        f'<label>type<select name="{prefix}_type" class="trigger-type-select">{type_options}</select></label>'
        f'{"".join(param_inputs)}'
        f'</div>'
    )


def _parse_trigger(prefix: str, form: dict) -> dict | None:
    """Inverse of _render_trigger_inputs -- None if no type was chosen (an optional trigger left
    blank; a required one like start_trigger then fails the real loader's own required-field
    check, same as any other required field left empty)."""
    trigger_type = form.get(f"{prefix}_type", "").strip()
    if not trigger_type:
        return None
    trigger = {"type": trigger_type}
    for p in TRIGGER_PARAM_NAMES:
        raw = form.get(f"{prefix}_{p}", "").strip()
        if raw:
            trigger[p] = int(raw) if TRIGGER_PARAM_KINDS[p] == "int" else raw
    return trigger


def _render_repeatable(container_id: str, rows_html: list[str], template_row_html: str, add_label: str) -> str:
    """Shared shell for every add/remove-able repeatable field (effects, materials, monster_drops,
    quest stages/paths): the existing rows, a hidden <template> row the page script clones on "+ Add",
    and the add button itself. Each individual row (built by a per-type row-builder like
    _render_effect_row) supplies its own "Remove" button -- see the page script's
    data-repeat-add/data-remove-row wiring, which is generic over any field using this shell."""
    return (
        f'<div id="{container_id}">{"".join(rows_html)}</div>'
        f'<template id="{container_id}-template">{template_row_html}</template>'
        f'<button type="button" class="add-row" data-repeat-add="{container_id}">{add_label}</button>'
    )


def _render_effect_row(
    prefix: str, effect: dict, allowed_types: list[str] = EFFECT_TYPES, include_trigger: bool = False,
) -> str:
    """One row of an "effects" list. See _parse_field's "effects" case (and _parse_effects_list,
    reused by monster skills' own nested effects) for the matching parse side, and
    _render_stage_node for why `prefix` is sometimes a "ROWIDX" placeholder.

    `allowed_types` narrows which types the dropdown even offers -- unused today (a monster's own
    skill has full parity with a player skill/consumable's effect vocabulary, so every caller uses
    the default full EFFECT_TYPES list), kept as a param since a future content type might still
    want a narrower dropdown. The page script's wireEffectSelects hide/show logic (keyed by
    EFFECT_PARAMS_BY_TYPE) needs no changes either way
    -- it only ever reacts to whichever type ends up selected, never enumerates the dropdown's own
    option list itself.

    Every row shows all of EFFECT_PARAM_NAMES (value/reduction/multiplier/duration), each wrapped
    in a label carrying data-param="<name>" -- the page script's wireEffectSelects hides whichever
    ones EFFECT_PARAMS_BY_TYPE says don't apply to the row's currently-selected type (and clears
    them), same "flatten every param, hide what doesn't apply" shape _render_trigger_inputs uses.
    The effect-hint <small> is swapped to EFFECT_TYPE_HINTS' explanation of the one param that's
    still showing, since "value"/"reduction"/"multiplier" alone don't say what they mean for a
    given type.

    `include_trigger` (equipment's own "effects" field only) additionally renders a `trigger`
    select (constant/on_use/on_hit) and a `chance` number input wrapped `data-trigger-only="on_hit"`
    -- shown only once trigger is set to on_hit, via the page script's wireEquipmentTriggerSelects,
    modeled on the existing wireCommandKindSelects/data-amount-only pattern (a room command's
    modal_title/input_label only mattering when kind=="amount"), the real precedent for "field B
    only relevant when sibling select A has value X" in this admin panel. dungeon's own
    trigger/type restriction (which types even make sense with which trigger) is NOT enforced here
    client-side -- same "let the dropdown show everything, catch a bad combination loudly at Save"
    choice this panel already makes for e.g. a delve room's free-typed next-room reference; the
    real check is dungeon._validate_equipment_effects, run at save time via the real loader."""
    type_options = "".join(
        f'<option value="{t}"{" selected" if t == effect.get("type") else ""}>{t}</option>'
        for t in [""] + allowed_types
    )
    param_inputs = "".join(
        f'<label data-param="{p}">{p}<input type="number" step="any" name="{prefix}_{p}" value="{effect.get(p, "")}"></label>'
        for p in EFFECT_PARAM_NAMES
    )
    # "aoe" is universal across every effect type (dungeon._validate_effects validates it
    # separately from the numeric EFFECT_PARAM_NAMES loop above) -- always shown, never hidden by
    # wireEffectSelects' per-type EFFECT_PARAMS_BY_TYPE logic the way value/reduction/multiplier/
    # duration are, since it isn't part of that per-type schema at all.
    aoe_checked = " checked" if effect.get("aoe") else ""
    aoe_html = (
        f'<label class="checkbox-label" data-tooltip="Hits every living entity in whichever pool '
        f'this effect\'s own target resolves to, instead of just one: the whole party if target is '
        f'ally, every living monster if target is enemy. Has no effect when target is self (there\'s '
        f'only ever one caster). Unchecked = single target (the usual behavior).">'
        f'<input type="checkbox" name="{prefix}_aoe"{aoe_checked}> aoe</label>'
    )
    # "target" (self/ally/enemy, dungeon.EFFECT_TARGETS) picks WHO this effect lands on, fully
    # decoupled from its type -- no restriction on which type can use which target. Blank means
    # dungeon.default_effect_target(type)'s type-based default (whatever this type always did
    # before "target" existed), so leaving it blank never changes existing content's behavior.
    # Shown on every row regardless of type, same "let the dropdown show everything, catch a bad
    # combination loudly at Save" choice aoe's own tooltip note already documents for this panel.
    target_options = "".join(
        f'<option value="{t}"{" selected" if t == effect.get("target") else ""}>{t}</option>'
        for t in [""] + list(dungeon.EFFECT_TARGETS)
    )
    target_html = (
        f'<label data-tooltip="Who this effect lands on: self (the caster), ally (a chosen/every '
        f'living ally), or enemy (the current/every living target) -- independent of the effect\'s '
        f'own type, so e.g. a stun or damage_multiplier can target self or ally just as freely as '
        f'enemy. Blank = this type\'s usual default (heals/buffs -> ally, damage/debuffs/CC -> '
        f'enemy).">target<select name="{prefix}_target">{target_options}</select></label>'
    )
    trigger_html = ""
    chance_html = ""
    if include_trigger:
        trigger_options = "".join(
            f'<option value="{t}"{" selected" if t == effect.get("trigger") else ""}>{t}</option>'
            for t in [""] + EQUIPMENT_EFFECT_TRIGGERS
        )
        trigger_html = (
            f'<label>trigger<select name="{prefix}_trigger" class="equipment-trigger-select">'
            f'{trigger_options}</select></label>'
            f'<label data-trigger-only="on_hit">chance (0-1)'
            f'<input type="number" step="any" min="0" max="1" name="{prefix}_chance" value="{effect.get("chance", "")}">'
            f'</label>'
        )
    else:
        # The universal independent per-effect fire probability (dungeon.resolve_cast_effects) --
        # rolled separately for EACH effect on a skill/consumable/monster-skill, so e.g. a 50%-stun
        # + 75%-damage skill can land both, either, or neither. Blank = always fires (probability
        # 1), same as every effect authored before this existed. Equipment's own effects
        # (include_trigger=True) already have an equivalent chance input of their own, scoped to
        # the on_hit trigger only -- see the branch above -- so this only ever appears here instead.
        chance_html = (
            f'<label data-tooltip="Independent chance THIS effect fires, rolled separately from '
            f'every other effect on the same skill/item. Blank = always fires. Two effects each '
            f'with their own chance can both land, either one, or neither -- for choosing between '
            f'mutually exclusive alternatives instead, use effect_groups.">chance (0-1)'
            f'<input type="number" step="any" min="0" max="1" name="{prefix}_chance" value="{effect.get("chance", "")}">'
            f'</label>'
        )
    return (
        f'<div class="row-group">'
        f'<label>type<select name="{prefix}_type" class="effect-type-select">{type_options}</select></label>'
        f'{trigger_html}'
        f'<small class="field-hint effect-hint"></small>'
        f'{param_inputs}{aoe_html}{target_html}{chance_html}'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
    )


def _render_effect_group_row(prefix: str, group: dict) -> str:
    """One group within a skill/consumable's own "effect_groups" repeatable (the alternative to
    plain "effects", for MUTUALLY EXCLUSIVE alternatives) -- a weight ("chance", relative to sibling
    groups, exactly the monster_groups/monster-skill convention -- NOT the same thing as an
    individual effect's own 0-1 "chance" a few lines down) plus a nested "effects" repeatable.
    dungeon.resolve_cast_effects picks exactly ONE group (weighted) each cast, then applies whatever
    that group's own effects list rolls (each still independently, via its own per-effect chance --
    see _render_effect_row). Reuses the exact same data-group-row/data-group-chance/data-group-pct
    markup a combat room's own monster_groups already established, so the page script's existing
    updateGroupOdds needs zero changes to also drive this field's live-odds display -- it already
    scopes itself per [data-group-odds] fieldset, not to any one specific content type."""
    effects_container = f"{prefix}_effects"
    effects = list(group.get("effects") or [])
    effect_rows_html = [_render_effect_row(f"{effects_container}_{i}", e) for i, e in enumerate(effects)]
    effect_template_html = _render_effect_row(f"{effects_container}_ROWIDX", {})
    effects_repeatable = _render_repeatable(effects_container, effect_rows_html, effect_template_html, "+ Add effect")
    return (
        f'<fieldset class="row-group monster-group" data-monster-group data-group-row>'
        f'<legend>Group</legend>'
        f'<label data-tooltip="A relative weight against this skill/item\'s OTHER groups -- NOT a '
        f'0-1 probability. Blank defaults to 1 (equal footing with every other untouched group). '
        f'The badge to the right shows this group\'s actual live odds given every weight currently '
        f'filled in.">chance (weight)'
        f'<input type="number" step="any" min="0" name="{prefix}_chance" '
        f'value="{group.get("chance", "")}" data-group-chance></label>'
        f'<span class="skill-odds-pct" data-group-pct>—</span>'
        f'{effects_repeatable}'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove group</button></fieldset>'
    )


def _render_monster_skill_row(prefix: str, skill: dict) -> str:
    """One entry in a monster's own "skills" repeatable -- name + chance (a relative WEIGHT
    against the monster's own attack_chance and every other skill's own chance, see
    dungeon.pick_monster_action -- NOT a 0-1 probability, so e.g. two skills both at chance=1 with
    attack_chance=1 split evenly three ways) plus EITHER its own nested "effects" repeatable OR (for
    a "50% this OR 50% that" monster skill) a nested "effect_groups" repeatable, one level deeper
    than the monster's top-level "drops"/"groups"-shaped fields -- same nesting-depth-agnostic
    wireRepeatAdd/ROWIDX machinery every other nested repeatable in this admin panel already relies
    on (rooms -> room -> groups -> group -> monster is the deepest existing precedent, matched here
    by skill -> effect_groups -> group -> effects -> effect). Effects here have full parity with a
    player skill/consumable's own effect vocabulary (dungeon.py's module comment above
    _validate_monster_skill), so this reuses _render_effect_row/_render_effect_group_row directly --
    exactly one of the two fields should be filled in, same XOR dungeon._validate_effects_or_groups
    enforces for a player skill/consumable. The two are wrapped in the same mode toggle
    (_render_effects_toggle) a top-level skill/consumable's own effects/effect_groups pair uses, so
    only one is ever visible/submitted here too."""
    effects_container = f"{prefix}_effects"
    effects = list(skill.get("effects") or [])
    effect_rows_html = [
        _render_effect_row(f"{effects_container}_{i}", e) for i, e in enumerate(effects)
    ]
    effect_template_html = _render_effect_row(f"{effects_container}_ROWIDX", {})
    effects_repeatable = _render_repeatable(effects_container, effect_rows_html, effect_template_html, "+ Add effect")

    groups_container = f"{prefix}_effectgroup"
    groups = list(skill.get("effect_groups") or [])
    group_rows_html = [_render_effect_group_row(f"{groups_container}_{i}", g) for i, g in enumerate(groups)]
    group_template_html = _render_effect_group_row(f"{groups_container}_ROWIDX", {})
    groups_repeatable = _render_repeatable(groups_container, group_rows_html, group_template_html, "+ Add group")
    groups_odds_summary = (
        '<div class="skill-odds-summary" data-group-odds-summary>'
        "Live odds appear here once groups are filled in.</div>"
    )
    effects_groups_toggle = _render_effects_toggle(
        f'<div>{effects_repeatable}</div>',
        f'<fieldset data-group-odds>{groups_odds_summary}{groups_repeatable}</fieldset>',
        bool(groups),
    )

    return (
        f'<fieldset class="row-group" data-skill-row><legend>Skill</legend>'
        f'<label>name<input type="text" name="{prefix}_name" value="{html.escape(skill.get("name", ""))}" '
        f'data-skill-name></label>'
        f'<label data-tooltip="A relative weight against this monster\'s own attack_chance and its '
        f'other skills\' chances -- NOT a 0-1 probability. E.g. two skills both at chance=1 with '
        f'attack_chance=1 split evenly three ways (~33% each). The badge to the right shows this '
        f'skill\'s actual live odds given every weight currently filled in.">chance (weight)'
        f'<input type="number" step="any" min="0" name="{prefix}_chance" '
        f'value="{skill.get("chance", "")}" data-skill-chance></label>'
        f'<span class="skill-odds-pct" data-skill-pct>—</span>'
        f'<label class="checkbox-label"><input type="checkbox" name="{prefix}_special"'
        f'{" checked" if skill.get("special") else ""}> Special (rolls SpAtk/SpDef instead of ATK/DEF)</label>'
        f'<label>flavor (optional -- shown in the combat log when this skill is used)'
        f'<textarea name="{prefix}_flavor" rows="2">{html.escape(skill.get("flavor", ""))}</textarea></label>'
        f'{effects_groups_toggle}'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove skill</button></fieldset>'
    )


def _render_material_row(prefix: str, material_id: str | None, qty) -> str:
    """One row of a "materials" list. See _parse_field's "materials" case for the matching parse
    side."""
    material_ids = sorted(dungeon.MATERIALS.keys())
    options = "".join(
        f'<option value="{m}"{" selected" if m == material_id else ""}>{m}</option>' for m in [""] + material_ids
    )
    return (
        f'<div class="row-group"><label>material<select name="{prefix}_id">{options}</select></label>'
        f'<label>qty<input type="number" min="1" name="{prefix}_qty" value="{qty or ""}"></label>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
    )


def _render_drop_row(prefix: str, drop: dict) -> str:
    """One row of a monster's "monster_drops" list -- see _parse_field's "monster_drops" case for
    the matching parse side. item_id is a _render_cascaded_select scoped to this row's own "kind"
    (not a top-level sibling field, same reasoning as _render_shop_row).

    The two "requires" inputs only ever round-trip a flag_at_least-shaped `requires` trigger (the
    one real use case so far -- gating a drop on a quest's own progress flag, e.g.
    {"key": "quest:some_id", "value": 1}, same idiom quests.py already uses everywhere else for
    "has this quest been started"). dungeon._validate_monster_drops accepts any TRIGGER_SCHEMAS
    shape structurally, so a hand-edited JSON drop with a different requires type will round-trip
    fine as long as nobody re-saves that specific monster through this panel -- doing so would
    silently drop a non-flag_at_least requires, since these two inputs are all this row can express.
    Widen this if a second trigger type on a drop ever comes up for real."""
    kind = drop.get("kind")
    kind_options = "".join(
        f'<option value="{k}"{" selected" if k == kind else ""}>{k}</option>'
        for k in [""] + list(dungeon.DROP_KINDS)
    )
    item_select = _render_cascaded_select(f"{prefix}_item_id", "monster_drop", kind, drop.get("item_id"))
    requires = drop.get("requires") or {}
    requires_key = requires.get("key", "") if requires.get("type") == "flag_at_least" else ""
    requires_value = requires.get("value", "") if requires.get("type") == "flag_at_least" else ""
    return (
        f'<div class="row-group">'
        f'<label>kind<select name="{prefix}_kind" class="cascade-select" data-cascade="monster_drop">{kind_options}</select></label>'
        f'<label>item_id{item_select}</label>'
        f'<label>chance (0-1)<input type="number" min="0" max="1" step="any" name="{prefix}_chance" '
        f'value="{drop.get("chance", "")}"></label>'
        f'<label>requires flag key (optional)<input type="text" name="{prefix}_requires_key" '
        f'value="{html.escape(str(requires_key))}"></label>'
        f'<label>requires min value<input type="number" name="{prefix}_requires_value" '
        f'value="{requires_value}"></label>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
    )


def _asset_src(rel_path: str) -> str:
    """Builds an /assets/... URL for a repo-root-relative asset path, with a cache-busting
    ?v=<mtime> query param -- the path itself doesn't change when a sprite is re-uploaded under
    the same filename, so without this a browser's HTTP cache can keep serving the old image
    bytes indefinitely after a replace."""
    abs_path = os.path.join(os.path.dirname(__file__), rel_path)
    try:
        version = int(os.path.getmtime(abs_path))
    except OSError:
        return f"/{rel_path}"
    return f"/{rel_path}?v={version}"


def _render_image_input(name: str, label: str, value: str | None) -> str:
    """The <label>+preview+file-input markup for one image upload -- shared by _render_field's
    top-level "image" case and _render_room_detail_panel's per-room background field, since both
    need the exact same live-preview wiring (see wireImagePreviews) and "keep existing on no
    upload" semantics (see _save_uploaded_image / _parse_delve_flowchart)."""
    preview_id = f"preview_{name}"
    src = _asset_src(value) if value else ""
    display = "block" if value else "none"
    return (
        f'<label>{label}'
        f'<img id="{preview_id}" class="image-preview" src="{html.escape(src)}" style="display:{display}">'
        f'<input type="file" name="{html.escape(name)}_file" accept="image/*" '
        f'data-preview-target="{preview_id}"></label>'
    )


def _monster_option_choices() -> list[tuple[str, str]]:
    """monster_id -> "Lvl N — Name", sorted by level then name -- the label shown in every
    monster <select> the delve room editor renders, so a large roster stays scannable by
    difficulty instead of just alphabetically. Uses each monster's own intended_level if it's been
    set, else dungeon.estimate_monster_level's reverse estimate off its actual stats -- same
    fallback xp_for_monster already uses, so every monster sorts sensibly even before it's been
    given an explicit intended_level."""
    def level_of(m: dict) -> float:
        return m.get("intended_level") or dungeon.estimate_monster_level(m)

    return [
        (mid, f"Lvl {level_of(m):.0f} — {m['name']}")
        for mid, m in sorted(dungeon.MONSTERS.items(), key=lambda kv: (level_of(kv[1]), kv[1]["name"]))
    ]


def _render_room_monster_row(name: str, monster_id: str | None) -> str:
    """One monster <select> row within a delve room's own nested repeatable list -- see
    _render_room_detail_panel below and _parse_delve_flowchart's matching parse side. Unlike every
    other row-builder here this one has just the one field, so `name` is the input's actual name,
    not a "prefix_suffix" pair."""
    options = "".join(
        f'<option value="{mid}"{" selected" if mid == monster_id else ""}>{html.escape(label)}</option>'
        for mid, label in [("", "—")] + _monster_option_choices()
    )
    return (
        f'<div class="row-group" data-monster-row><label>monster<select name="{name}">{options}</select></label>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
    )


def _render_room_monster_group_row(prefix: str, group: dict) -> str:
    """One monster GROUP within a combat room's own repeatable "groups" list -- a group is every
    monster that spawns simultaneously as one encounter (a group of one is an ordinary
    single-monster fight; see dungeon.py's module docstring), plus its own "chance" -- a relative
    weight against this room's OTHER groups (dungeon.pick_monster_group), the exact same convention
    _render_monster_skill_row's own chance already uses, right down to the live odds badge (see
    updateGroupOdds in _dynamic_script, the group sibling of updateSkillOdds). Nests
    _render_room_monster_row's own repeatable one level deeper than before (room -> groups -> group
    -> monster), reusing the exact same wireRepeatAdd/ROWIDX machinery already proven
    nesting-depth-agnostic by _render_action_row (rooms -> room -> actions) -- no JS changes needed
    for the nesting itself. Also carries "row-group" so the page's generic remove-row handler
    (`event.target.closest('.row-group')`) removes the whole group when its own remove button is
    clicked, same as it already removes just one nested monster row when *that* row's own remove
    button is clicked instead -- DOM proximity alone disambiguates which level a given remove click
    means, no extra JS needed here either.

    "fork" (bool, optional) is the only thing that controls whether this group gets its own
    draggable node + connector handle on the flowchart canvas (_dynamic_script's syncGroupNodes,
    the group sibling of syncActionNodes) -- unchecked (the common case) means this group just
    falls back to the room's own next, no node cluttering the canvas for something that isn't
    being used. Checking it reveals the node; unchecking it again (wireGroupForkCheckbox) clears
    "next" too, so toggling it off never leaves a stale override invisibly still in effect. "next"
    itself is never typed here -- same "not typed, dragged" story as an action's own on_success/
    on_fail next (_render_action_row): the value lives in this same-named hidden input, just
    written by JS instead of a person; the live-next span mirrors it read-only, purely so this
    panel shows where it leads without hunting for the arrow on canvas. Position (x/y) is NOT
    rendered here -- like an action's own x/y, it only ever exists as a canvas-node hidden input
    JS creates on the fly (syncGroupNodes), read back by _parse_delve_flowchart same as
    _parse_actions already does for actions. A group with no "fork" field yet (existing content
    from before this checkbox existed) defaults checked if it already has a "next", so an existing
    override doesn't silently hide itself the first time this loads. See _render_room_detail_panel
    below and _parse_delve_flowchart's matching parse side."""
    monsters = group.get("monsters", [])
    monsters_container = f"{prefix}_monsters"
    monster_rows_html = [_render_room_monster_row(f"{monsters_container}_{j}", mid) for j, mid in enumerate(monsters)]
    monster_template_html = _render_room_monster_row(f"{monsters_container}_ROWIDX", None)
    monsters_repeatable = _render_repeatable(monsters_container, monster_rows_html, monster_template_html, "+ Add monster")
    forked = group.get("fork", bool(group.get("next")))
    return (
        f'<fieldset class="row-group monster-group" data-monster-group data-group-row>'
        f'<legend>Group '
        f'<span class="live-next-tag" data-live-next="{prefix}_next">'
        f'{html.escape("→ " + group["next"]) if group.get("next") else "→ (room\'s default)"}'
        f'</span></legend>'
        f'<label data-tooltip="A relative weight against this room\'s OTHER groups -- NOT a 0-1 '
        f'probability. Blank defaults to 1 (equal footing with every other untouched group); lower '
        f'this for a rare encounter (e.g. 0.1 next to two groups left blank makes it roughly '
        f'1-in-21 instead of 1-in-3). The badge to the right shows this group\'s actual live odds '
        f'given every weight currently filled in for this room.">chance (weight)'
        f'<input type="number" step="any" min="0" name="{prefix}_chance" '
        f'value="{group.get("chance", "")}" data-group-chance></label>'
        f'<span class="skill-odds-pct" data-group-pct>—</span>'
        f'<label data-tooltip="Gives this group its own exit, overriding the room\'s own next '
        f'whenever this specific group is the one rolled. Shows a draggable node on the canvas '
        f'above to set it -- leave unchecked for an ordinary group that just uses the room\'s '
        f'default.">Fork (own exit)'
        f'<input type="checkbox" class="group-fork-input" name="{prefix}_fork"{" checked" if forked else ""}></label>'
        f'<input type="hidden" class="group-next-input" name="{prefix}_next" '
        f'value="{html.escape(group.get("next") or "")}">'
        f'{monsters_repeatable}'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove group</button></fieldset>'
    )


def _render_action_row(prefix: str, action: dict) -> str:
    """One action within a choice room's own nested "actions" repeatable -- a third level of
    nesting (rooms -> room -> actions), reusing the same wireRepeatAdd/ROWIDX machinery
    already documented as nesting-depth-agnostic. See dungeon.py's module docstring for the full
    action shape. `requires` reuses _render_trigger_inputs verbatim -- the exact same {type,
    ...params} vocabulary quest triggers use. `cost`'s item_id is a real cascading select
    (material/consumable/quest_item are all server-known registries).

    Unlike every other field here, on_success/on_fail's "next" room reference is NOT a typed
    input -- it's set by dragging this action's own connector handle (rendered on the room's box,
    see _render_room_box) onto a target room on the flowchart canvas. The actual value still lives
    in a same-named hidden input (`{prefix}_success_next`/`_fail_next`, read by _parse_actions
    completely unchanged), just written by JS instead of a person; the visible
    `<span data-live-next>` next to each outcome's legend is kept in sync with that hidden value by
    the flowchart script (see _dynamic_script's "Delve flowchart editor" section) purely so a
    person looking at this panel can see where the action currently leads without having to go
    find the arrow on the canvas. `check`/`on_fail` are always rendered, not toggled by JS -- whether "check" ends up in
    the saved action depends only on whether its chance, or its stat+dc together, were actually
    filled in (see _parse_actions), the same "blank means absent" convention every other optional
    field in this panel already follows; this action's own on-canvas node (_render_action_node)
    only grows a second "fail" connector handle once a check is actually configured (see the
    flowchart script's syncActionNodes)."""
    cost = action.get("cost") or {}
    check = action.get("check") or {}
    on_success = action.get("on_success") or {}
    on_fail = action.get("on_fail") or {}

    stat_options = "".join(
        f'<option value="{s}"{" selected" if s == check.get("stat") else ""}>{s}</option>'
        for s in [""] + list(dungeon.CHECK_STATS)
    )
    item_kind_options = "".join(
        f'<option value="{k}"{" selected" if k == cost.get("item_kind") else ""}>{k}</option>'
        for k in [""] + list(dungeon.ACTION_COST_ITEM_KINDS)
    )
    item_select = _render_cascaded_select(
        f"{prefix}_cost_item_id", "action_cost", cost.get("item_kind"), cost.get("item_id")
    )

    def _text(field_name: str, value: str | None) -> str:
        return f'<input type="text" name="{prefix}_{field_name}" value="{html.escape(value or "")}">'

    def _next_hidden(field_name: str, value: str | None) -> str:
        return f'<input type="hidden" name="{prefix}_{field_name}" value="{html.escape(value or "")}">'

    def _outcome_reward_fields(outcome_prefix: str, outcome: dict) -> str:
        """currency_delta + item give/take fields shared by on_success and on_fail below --
        item_qty's sign decides give vs. take (positive/negative), same single-field convention as
        currency_delta and hp_delta, so there's one input to fill in rather than a redundant
        give-or-take toggle plus a magnitude."""
        kind_options = "".join(
            f'<option value="{k}"{" selected" if k == outcome.get("item_kind") else ""}>{k}</option>'
            for k in [""] + list(dungeon.ACTION_COST_ITEM_KINDS)
        )
        reward_item_select = _render_cascaded_select(
            f"{outcome_prefix}_item_id", "action_cost", outcome.get("item_kind"), outcome.get("item_id")
        )
        achievement_options = "".join(
            f'<option value="{a["kind"]}"{" selected" if a["kind"] == outcome.get("achievement_kind") else ""}>'
            f'{html.escape(a["name"])} ({a["kind"]})</option>'
            for a in [{"kind": "", "name": "(none)"}] + achievements.ACHIEVEMENTS
        )
        return (
            f'<label>currency_delta (optional, +/-)<input type="number" name="{outcome_prefix}_currency_delta" '
            f'value="{outcome.get("currency_delta", "")}"></label>'
            f'<label>item_kind<select name="{outcome_prefix}_item_kind" class="cascade-select" '
            f'data-cascade="action_cost">{kind_options}</select></label>'
            f'<label>item_id{reward_item_select}</label>'
            f'<label>item_qty (optional, +/-)<input type="number" name="{outcome_prefix}_item_qty" '
            f'value="{outcome.get("item_qty", "")}"></label>'
            f'<label>achievement (optional)<select name="{outcome_prefix}_achievement_kind">'
            f'{achievement_options}</select>'
            f'<small class="field-hint" data-tooltip="Awards this achievement (idempotently) when this '
            f'outcome fires. In a party delve, every party member gets it, not just whoever attempted '
            f'the action.">?</small></label>'
        )

    return (
        f'<div class="row-group action-row">'
        f'<label>label{_text("label", action.get("label"))}'
        f'<small class="field-hint" data-tooltip="What the player sees on this action\'s button.">?</small>'
        f'</label>'
        f'{_render_trigger_inputs(f"{prefix}_requires", action.get("requires") or {})}'
        f'<fieldset><legend>cost (optional)</legend>'
        f'<label>currency<input type="number" min="0" name="{prefix}_cost_currency" value="{cost.get("currency", "")}"></label>'
        f'<label>item_kind<select name="{prefix}_cost_item_kind" class="cascade-select" data-cascade="action_cost">'
        f'{item_kind_options}</select></label>'
        f'<label>item_id{item_select}</label>'
        f'<label>item_qty<input type="number" min="1" name="{prefix}_cost_item_qty" value="{cost.get("item_qty", "")}"></label>'
        f'</fieldset>'
        f'<fieldset data-tooltip="Rolls this action against the character\'s own stat, or (fill in '
        f'chance instead) a flat probability independent of any stat. Add a check to make this one '
        f'action branch by luck, on top of (not instead of) branching by which action the player '
        f'picks. Fill in EITHER stat+dc OR chance, not both -- chance wins if both are set.">'
        f'<legend>check (optional -- stat-vs-DC, or a flat chance)</legend>'
        f'<label>stat<select name="{prefix}_check_stat" class="action-check-input">{stat_options}</select></label>'
        f'<label>dc<input type="number" min="1" name="{prefix}_check_dc" class="action-check-input" value="{check.get("dc", "")}"></label>'
        f'<label>chance (0-1)<input type="number" min="0" max="1" step="0.01" name="{prefix}_check_chance" '
        f'class="action-check-input" value="{check.get("chance", "")}"></label>'
        f'</fieldset>'
        f'<fieldset data-tooltip="Where this action leads if it succeeds (or always, if there\'s no '
        f'check above). Drag this action\'s green handle on the room\'s box, on the canvas above, to '
        f'the target room -- it can\'t be typed here.">'
        f'<legend>on_success <span class="live-next-tag" data-live-next="{prefix}_success_next">'
        f'{html.escape("→ " + on_success.get("next", "")) if on_success.get("next") else "→ (wins the delve)"}'
        f'</span></legend>'
        f'{_next_hidden("success_next", on_success.get("next"))}'
        f'<label>hp_delta (optional)<input type="number" name="{prefix}_success_hp_delta" value="{on_success.get("hp_delta", "")}"></label>'
        f'<label>message (optional){_text("success_message", on_success.get("message"))}</label>'
        f'{_outcome_reward_fields(f"{prefix}_success", on_success)}'
        f'</fieldset>'
        f'<fieldset data-tooltip="Only used once a check is set above. Where this action leads if '
        f'the roll fails -- drag the action\'s red handle on the canvas to set it.">'
        f'<legend>on_fail (only used if check is set) <span class="live-next-tag" data-live-next="{prefix}_fail_next">'
        f'{html.escape("→ " + on_fail.get("next", "")) if on_fail.get("next") else "→ (wins the delve)"}'
        f'</span></legend>'
        f'{_next_hidden("fail_next", on_fail.get("next"))}'
        f'<label>hp_delta (optional)<input type="number" name="{prefix}_fail_hp_delta" value="{on_fail.get("hp_delta", "")}"></label>'
        f'<label>message (optional){_text("fail_message", on_fail.get("message"))}</label>'
        f'{_outcome_reward_fields(f"{prefix}_fail", on_fail)}'
        f'</fieldset>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove action</button></div>'
    )


def _default_room_position(index: int) -> dict:
    """Deterministic grid fallback for a room with no stored `layout` entry yet (see the
    delve-level "layout" field in dungeon.py's module docstring) -- keeps a delve that's never
    been opened in the flowchart editor from stacking every room's box at (0, 0)."""
    columns = 4
    return {"x": 40 + (index % columns) * 220, "y": 40 + (index // columns) * 170}


def _default_action_position(room_pos: dict, index: int) -> dict:
    """Fallback position for a choice action's own connector box (see _render_action_node) that
    has never been dragged yet -- stacked below-and-right of its parent room, purely so a brand
    new action doesn't render on top of its room or a sibling action. Once dragged, an action's
    real position is persisted directly on the action itself ("x"/"y", see dungeon.py's module
    docstring) and this default is never consulted again for that action."""
    return {"x": room_pos["x"] + 190, "y": room_pos["y"] + index * 74}


def _render_action_node(action_prefix: str, action: dict, pos: dict, error_messages: list[str] | None = None) -> str:
    """A choice action's own connector box on the canvas -- each action gets one, freely
    draggable (its position persists on the action itself, "x"/"y", not auto-tracking its room)
    and connected to its parent room by a plain purple arrow, so a room with several actions
    doesn't fan multiple connections out of the exact same corner -- that's what made overlapping
    lines from different actions hard to tell apart before this. This box is purely a connection
    anchor: it carries no editable fields of its own (those stay in the parent room's detail
    panel, see _render_action_row) -- just this action's label (kept in sync client-side by
    syncActionNodes), its own x/y hidden inputs (set by dragging, see wireActionNodeDrag), and
    whichever connector handle(s) its outcome actually has: a "success" handle always, a second
    dashed "fail" handle only once this action has a check configured (see dungeon.py's module
    docstring for why that -- not the check -- is the primary way a delve is meant to branch).

    `error_messages`, if given, is this action's own share of dungeon.check_delve_problems's
    output (see _group_delve_problems) -- adds a ".has-error" class and a tooltip listing what's
    still wrong, so a draft's still-broken actions are visible right on the canvas instead of only
    in a page-level banner."""
    check = action.get("check") or {}
    has_check = bool(check.get("chance")) or bool(check.get("stat") and check.get("dc"))
    label = html.escape(action.get("label") or "(unlabeled)")
    fail_handle = (
        f'<div class="connector-handle fail" data-connector-role="fail" data-action-prefix="{action_prefix}" '
        f'data-tooltip="Drag onto another room -- where {label} leads if the check fails."></div>'
        if has_check else ""
    )
    error_attr = (
        f' data-tooltip="{html.escape(" / ".join(error_messages))}"' if error_messages else ""
    )
    return (
        f'<div class="action-node{" has-error" if error_messages else ""}" data-action-node="{action_prefix}" '
        f'style="left:{pos["x"]}px;top:{pos["y"]}px"{error_attr}>'
        f'<input type="hidden" name="{action_prefix}_x" class="action-x-input" value="{pos["x"]}">'
        f'<input type="hidden" name="{action_prefix}_y" class="action-y-input" value="{pos["y"]}">'
        f'<span class="action-node-label" data-action-node-label>{label}</span>'
        f'<div class="connector-handle" data-connector-role="success" data-action-prefix="{action_prefix}" '
        f'data-tooltip="Drag onto another room -- where {label} leads '
        f'{"if the check succeeds" if has_check else "when the player picks it"}."></div>'
        f'{fail_handle}'
        f'</div>'
    )


def _render_room_box(prefix: str, room: dict, pos: dict, is_start: bool, error_messages: list[str] | None = None) -> str:
    """The draggable box on the flowchart canvas -- id, a type icon, a summary, and (combat only)
    the one connector handle a connection is *drawn from* (see admin_server.py's flowchart script
    in _dynamic_script for the drag-to-move/drag-to-connect behavior itself). A choice room's own
    box carries no handles at all -- each of its actions gets its own separate connector box
    instead (see _render_action_node), so lines from different actions never bunch up at the same
    corner of one shared box. The hidden room_{i}_x/_y/_next inputs live here, next to the controls
    that actually set them; id/type/monsters/prompt/actions/background live in the paired detail
    panel (_render_room_detail_panel), which is what actually cuts down the old wall-of-text-boxes
    problem -- only the selected room's own fields are ever on screen at once.

    `error_messages`, if given, is this room's own share of dungeon.check_delve_problems's output
    (see _group_delve_problems) -- adds a ".has-error" class and a tooltip listing what's still
    wrong with this room specifically (not its actions, which get their own via
    _render_action_node), same "highlight it right where it is" reasoning."""
    room_type = room.get("type") or "combat"
    room_id = room.get("id", "")
    icon = "⚔️" if room_type == "combat" else "💬"
    if room_type == "combat":
        groups = room.get("monster_groups", [])
        monster_count = sum(len(g.get("monsters", [])) for g in groups)
        # Best-effort level-equivalent range across the room's candidate groups (see
        # dungeon.estimate_group_level) -- skips any not-yet-known monster id (an in-progress,
        # unsaved edit can reference one) rather than erroring, same "display, don't validate"
        # spirit as the rest of this box.
        group_levels = [
            dungeon.estimate_group_level(
                [dungeon.MONSTERS[mid] for mid in g.get("monsters", []) if mid in dungeon.MONSTERS]
            )
            for g in groups
        ]
        group_levels = [lvl for lvl in group_levels if lvl > 0]
        level_range = ""
        if group_levels:
            lo, hi = min(group_levels), max(group_levels)
            level_range = f", ≈ Lvl {lo:.0f}" if hi - lo < 0.5 else f", ≈ Lvl {lo:.0f}-{hi:.0f}"
        summary = (
            f"{monster_count} monster{'s' if monster_count != 1 else ''} "
            f"({len(groups)} group{'s' if len(groups) != 1 else ''}{level_range})"
        )
        handles_html = (
            f'<div class="connector-handle" data-connector-role="success" '
            f'data-tooltip="Drag onto another room -- where the player goes after winning the '
            f'fight here (a monster group\'s own override, if any, wins over this). Drop on empty '
            f'canvas to disconnect (that makes this room the end of the delve, a win)."></div>'
        )
    else:
        actions = room.get("actions", [])
        summary = f"{len(actions)} action{'s' if len(actions) != 1 else ''}"
        handles_html = ""
    next_hidden = (
        f'<input type="hidden" name="{prefix}_next" class="room-next-input" value="{html.escape(room.get("next") or "")}">'
        if room_type == "combat" else ""
    )
    error_attr = (
        f' data-tooltip="{html.escape(" / ".join(error_messages))}"' if error_messages else ""
    )
    return (
        f'<div class="room-box room-box-{room_type}{" is-start" if is_start else ""}'
        f'{" has-error" if error_messages else ""}" '
        f'data-room-prefix="{prefix}" style="left:{pos["x"]}px;top:{pos["y"]}px"{error_attr}>'
        f'<input type="hidden" name="{prefix}_x" class="room-x-input" value="{pos["x"]}">'
        f'<input type="hidden" name="{prefix}_y" class="room-y-input" value="{pos["y"]}">'
        f'{next_hidden}'
        f'<div class="room-box-header">'
        f'<button type="button" class="room-flag{" is-start" if is_start else ""}" data-set-start '
        f'data-tooltip="Set as the room every player starts this delve at.">🚩</button>'
        f'<span class="room-box-icon">{icon}</span>'
        f'<span class="room-box-id" data-room-box-id>{html.escape(room_id) or "(no id yet)"}</span>'
        f'<button type="button" class="room-box-select" data-tooltip="Click to edit this room\'s fields.">✏️</button>'
        f'</div>'
        f'<div class="room-box-summary" data-room-box-summary>{summary}</div>'
        f'{handles_html}'
        f'</div>'
    )


def _render_room_detail_panel(prefix: str, room: dict) -> str:
    """The collapsible per-room field panel -- id/type/monsters (combat) or prompt+actions
    (choice)/background image. Shown for at most one room at a time (see the flowchart script's
    selectRoom), which is what actually cuts down the old "million text boxes" problem: every
    other room's fields simply aren't on screen while it's collapsed. Wrapped in "room-row" so the
    existing data-room-field/wireRoomTypeSelects show/hide-by-type machinery
    (admin_server.py:269-283) keeps working unchanged. There is deliberately no "next" field here
    at all, for either room type -- that connection is made by dragging a handle on the room's own
    box (_render_room_box), never typed; see _render_action_row for why on_success/on_fail's own
    "next" is a hidden input kept in sync by the same drag gesture instead of a text box."""
    room_type = room.get("type") or "combat"
    type_options = "".join(
        f'<option value="{t}"{" selected" if t == room_type else ""}>{t}</option>' for t in dungeon.ROOM_TYPES
    )

    groups_container = f"{prefix}_groups"
    groups = room.get("monster_groups", [])
    group_rows_html = [_render_room_monster_group_row(f"{groups_container}_{j}", g) for j, g in enumerate(groups)]
    group_template_html = _render_room_monster_group_row(f"{groups_container}_ROWIDX", {})
    groups_repeatable = _render_repeatable(groups_container, group_rows_html, group_template_html, "+ Add group")
    # data-group-odds scopes updateGroupOdds (_dynamic_script) to just this room's own groups --
    # unlike a monster's single page-wide skill-odds fieldset, a delve can have many combat rooms
    # on the same canvas, each needing its own independent live-odds summary.
    groups_odds_summary = (
        '<div class="skill-odds-summary" data-group-odds-summary>'
        "Live odds appear here once groups are filled in.</div>"
    )
    groups_field_html = f'<fieldset data-group-odds>{groups_odds_summary}{groups_repeatable}</fieldset>'

    actions_container = f"{prefix}_actions"
    actions = room.get("actions", [])
    action_rows_html = [_render_action_row(f"{actions_container}_{j}", a) for j, a in enumerate(actions)]
    action_template_html = _render_action_row(f"{actions_container}_ROWIDX", {})
    actions_repeatable = _render_repeatable(actions_container, action_rows_html, action_template_html, "+ Add action")

    image_html = _render_image_input(
        f"{prefix}_background_path", "background (optional -- falls back to the delve's own)",
        room.get("background_path"),
    )
    return (
        f'<div class="room-detail-panel room-row" data-room-panel="{prefix}" hidden>'
        f'<div class="room-detail-panel-header"><strong>Room details</strong>'
        f'<button type="button" class="room-detail-close" '
        f'data-tooltip="Close this panel -- the room stays on the canvas.">✕ Close</button></div>'
        f'<label>id<input type="text" name="{prefix}_id" class="room-id-input" '
        f'value="{html.escape(room.get("id", ""))}">'
        f'<small class="field-hint">Other rooms connect to this room by this id -- renaming it '
        f'here automatically fixes up any arrows already pointing here.</small></label>'
        f'<label>type<select name="{prefix}_type" class="room-type-select">{type_options}</select>'
        f'<small class="field-hint">Combat: a monster fight with one exit. Choice: flavor text '
        f'plus player-picked actions, each with its own destination -- this is where branching '
        f'paths are authored.</small></label>'
        f'<div data-room-field="groups"><label>monster groups<small class="field-hint">One group is '
        f'picked each visit, weighted by each group\'s own chance below (blank = 1, equal footing); '
        f'every monster within a group spawns together as one simultaneous encounter. A group of '
        f'one monster is an ordinary single-monster fight.</small></label>{groups_field_html}</div>'
        f'<div data-room-field="prompt"><label>prompt'
        f'<small class="field-hint">Required for a choice room -- the menu text shown alongside '
        f'its actions. Optional for a combat room: shown once, right as the room is entered, ahead '
        f'of the monster\'s own flavor text, to introduce the room itself before the fight '
        f'starts.</small>'
        f'<textarea name="{prefix}_prompt">{html.escape(room.get("prompt", ""))}</textarea></label></div>'
        f'<div data-room-field="actions"><label>actions<small class="field-hint">Each action gets '
        f'its own arrow on the canvas -- two or more actions already fork the path by player '
        f'choice alone, with no check needed.</small></label>{actions_repeatable}</div>'
        f'{image_html}'
        f'<button type="button" class="remove-row" data-remove-room>✕ Remove room</button></div>'
    )


def _default_group_position(room_pos: dict, index: int) -> dict:
    """Group sibling of _default_action_position -- same stacked below-and-right fallback for a
    monster group's own connector node that's never been dragged yet."""
    return {"x": room_pos["x"] + 190, "y": room_pos["y"] + index * 74}


def _render_group_node(group_prefix: str, group: dict, pos: dict, index: int) -> str:
    """Group sibling of _render_action_node -- a monster group's own connector box on the canvas,
    labeled "Group N" (1-indexed; groups have no label field of their own to show instead). Just
    this label (kept in sync client-side by syncGroupNodes), its own x/y hidden inputs (set by
    dragging, see wireActionNodeDrag), and the one connector handle overriding the room's own next
    -- no "fail" variant, groups have no check/fail concept. Carries no editable fields itself
    (chance/monsters stay in the parent room's detail panel, see _render_room_monster_group_row)."""
    label = f"Group {index + 1}"
    return (
        f'<div class="group-node" data-group-node="{group_prefix}" '
        f'style="left:{pos["x"]}px;top:{pos["y"]}px">'
        f'<input type="hidden" name="{group_prefix}_x" class="group-x-input" value="{pos["x"]}">'
        f'<input type="hidden" name="{group_prefix}_y" class="group-y-input" value="{pos["y"]}">'
        f'<span class="group-node-label" data-group-node-label>{label}</span>'
        f'<div class="connector-handle" data-connector-role="success" data-group-prefix="{group_prefix}" '
        f'data-tooltip="Drag onto another room -- where {label} leads after the fight, overriding '
        f'the room\'s own next. Drop on empty canvas to disconnect back to the room\'s '
        f'default."></div>'
        f'</div>'
    )


def _render_room_node(
    prefix: str, room: dict, pos: dict, is_start: bool,
    room_errors: list[str] | None = None, action_errors: dict[int, list[str]] | None = None,
) -> str:
    """Box + action/group nodes + detail panel for one room, wrapped in a single .room-wrapper --
    the unit _render_repeatable's existing <template>/ROWIDX clone mechanism (see wireRepeatAdd)
    operates on, so "+ Add Room" keeps working with zero changes to that shared primitive. Action
    nodes (choice rooms) and group nodes (combat rooms) are siblings of the room's own box (not
    nested inside it) so each gets its own independent position in the same canvas coordinate
    space -- see _render_action_node/_render_group_node.

    `room_errors`/`action_errors` are this one room's slice of a delve-wide problem map (see
    _group_delve_problems) -- action_errors is keyed by action index within this room. Group nodes
    don't get their own error highlighting -- check_delve_problems doesn't produce per-group
    messages, only per-room/per-action ones."""
    action_nodes_html = ""
    group_nodes_html = ""
    if (room.get("type") or "combat") == "choice":
        action_nodes_html = "".join(
            _render_action_node(
                f"{prefix}_actions_{j}", action,
                {"x": action["x"], "y": action["y"]} if "x" in action and "y" in action
                else _default_action_position(pos, j),
                (action_errors or {}).get(j),
            )
            for j, action in enumerate(room.get("actions", []))
        )
    else:
        group_nodes_html = "".join(
            _render_group_node(
                f"{prefix}_groups_{j}", group,
                {"x": group["x"], "y": group["y"]} if "x" in group and "y" in group
                else _default_group_position(pos, j),
                j,
            )
            for j, group in enumerate(room.get("monster_groups", []))
            # Only a forked group gets a node -- same "fork checkbox is the only thing that
            # decides this" rule syncGroupNodes' own early-return enforces client-side; this is
            # the initial-page-load half of that, since wireFlowchartNode never calls
            # syncGroupNodes itself for a freshly-loaded page, only for a later edit.
            if group.get("fork", bool(group.get("next")))
        )
    return (
        f'<div class="room-wrapper" data-room-wrapper="{prefix}">'
        f'{_render_room_box(prefix, room, pos, is_start, room_errors)}'
        f'{action_nodes_html}'
        f'{group_nodes_html}'
        f'{_render_room_detail_panel(prefix, room)}'
        f'</div>'
    )


def _group_delve_problems(problems: list[dict]) -> tuple[dict[str, list[str]], dict[str, dict[int, list[str]]]]:
    """Splits dungeon.check_delve_problems's flat output into (room_id -> messages, room_id ->
    {action_index -> messages}) -- the shape _render_delve_flowchart needs to hand each room/action
    node its own slice. A delve-level problem (room_id is None -- e.g. unreachable rooms, a bad
    start_room) has nowhere on the canvas to attach to, so it's dropped here; it's still shown in
    the page-level banner (see edit_view), same as it always was."""
    room_msgs: dict[str, list[str]] = {}
    action_msgs: dict[str, dict[int, list[str]]] = {}
    for p in problems:
        room_id = p["room_id"]
        if room_id is None:
            continue
        if p["action_index"] is None:
            room_msgs.setdefault(room_id, []).append(p["message"])
        else:
            action_msgs.setdefault(room_id, {}).setdefault(p["action_index"], []).append(p["message"])
    return room_msgs, action_msgs


def _render_delve_flowchart(label: str, rooms: list[dict], entry: dict, problems: list[dict] | None = None) -> str:
    """Top-level renderer for a delve's "rooms" field -- a flowchart canvas (draggable room boxes
    + an SVG arrow overlay, see the flowchart script in _dynamic_script) instead of a flat stack of
    text-box rows. `entry` is the full delve dict, not just its "rooms" value -- _render_field
    already passes this through for every field type (see the "cascaded_id" case), so this reads
    entry.get("layout")/entry.get("start_room") with no new plumbing. The single page-level hidden
    "start_room" input lives here, replacing the old free-typed top-level field entirely (see
    admin_schemas.py) -- it's set by clicking a room's own flag icon (_render_room_box) instead.

    `problems`, if given, is dungeon.check_delve_problems's output for this exact entry -- grouped
    per room/action (see _group_delve_problems) so a draft's still-broken spots are highlighted
    directly on the canvas, not just named in a page-level banner."""
    layout = entry.get("layout") or {}
    start_room = entry.get("start_room") or (rooms[0]["id"] if rooms else "")
    room_msgs, action_msgs = _group_delve_problems(problems or [])

    room_nodes_html = []
    for i, room in enumerate(rooms):
        prefix = f"room_{i}"
        pos = layout.get(room.get("id"), _default_room_position(i))
        room_id = room.get("id")
        room_nodes_html.append(_render_room_node(
            prefix, room, pos, room.get("id") == start_room,
            room_msgs.get(room_id), action_msgs.get(room_id),
        ))
    template_html = _render_room_node("room_ROWIDX", {}, _default_room_position(len(rooms)), False)

    canvas_html = _render_repeatable("delve-rooms-canvas", room_nodes_html, template_html, "+ Add Room")
    return (
        f'<fieldset><legend>{label}</legend>'
        f'<small class="field-hint">Drag rooms to arrange them, drag a room\'s handle onto another '
        f'room to connect them, click a room to edit its fields, and click the flag to set the '
        f'start room.</small>'
        f'<input type="hidden" name="start_room" id="start_room_field" value="{html.escape(start_room)}">'
        f'<div class="delve-canvas-wrap">{canvas_html}<svg class="delve-arrows"></svg></div>'
        f'</fieldset>'
    )


def _default_stage_position(index: int) -> dict:
    """Deterministic grid fallback for a stage with no stored `layout` entry yet -- keeps a quest
    that's never been opened in the flowchart editor from stacking every stage's box at (0, 0).
    Exact sibling of _default_room_position (delves)."""
    columns = 4
    return {"x": 40 + (index % columns) * 220, "y": 40 + (index // columns) * 170}


def _default_path_position(stage_pos: dict, index: int) -> dict:
    """Fallback position for a path's own connector box (see _render_path_node) that has never
    been dragged yet -- stacked below-and-right of its parent stage. Exact sibling of
    _default_action_position (delves). Once dragged, a path's real position is persisted directly
    on the path itself ("x"/"y")."""
    return {"x": stage_pos["x"] + 190, "y": stage_pos["y"] + index * 74}


def _describe_path(path: dict) -> str:
    """Short label for a path's own canvas node -- a path has no free-text "label" field of its
    own (unlike a delve action), so this is derived from whatever's most identifying: its
    turn_in_label if the author set one, else its trigger's type."""
    if path.get("turn_in_label"):
        return path["turn_in_label"]
    trigger_type = (path.get("trigger") or {}).get("type")
    return trigger_type or "(no trigger yet)"


def _render_path_node(path_prefix: str, path: dict, pos: dict, error_messages: list[str] | None = None) -> str:
    """A stage path's own connector box on the canvas -- exact sibling of _render_action_node
    (delves), simpler in one respect: a path never gets a "fail" handle (a path has no check/fail
    concept at all, only a trigger that currently either holds or doesn't), so it always carries
    exactly one connector handle. Purely a connection anchor -- its editable fields live in the
    parent stage's detail panel (see _render_path_row); this box only carries a label (kept in
    sync client-side, see _describe_path), its own x/y hidden inputs, and the one handle.

    `error_messages`, if given, is this path's own share of quests.check_quest_problems's output
    (see _group_quest_problems)."""
    label = html.escape(_describe_path(path))
    error_attr = f' data-tooltip="{html.escape(" / ".join(error_messages))}"' if error_messages else ""
    return (
        f'<div class="path-node{" has-error" if error_messages else ""}" data-path-node="{path_prefix}" '
        f'style="left:{pos["x"]}px;top:{pos["y"]}px"{error_attr}>'
        f'<input type="hidden" name="{path_prefix}_x" class="path-x-input" value="{pos["x"]}">'
        f'<input type="hidden" name="{path_prefix}_y" class="path-y-input" value="{pos["y"]}">'
        f'<span class="path-node-label" data-path-node-label>{label}</span>'
        f'<div class="connector-handle" data-connector-role="success" data-path-prefix="{path_prefix}" '
        f'data-tooltip="Drag onto another stage -- where this path leads once its trigger is '
        f'satisfied. Drop on empty canvas to disconnect (that ends the quest via this path)."></div>'
        f'</div>'
    )


def _render_stage_box(prefix: str, stage: dict, pos: dict, is_start: bool, error_messages: list[str] | None = None) -> str:
    """The draggable box on the flowchart canvas -- id, an icon, and a summary of how many paths
    lead out of this stage (or "Dialogue only" for a terminal stage with none yet). Exact sibling
    of _render_room_box (delves), simplified since a quest stage is always structurally the
    delve-choice-room shape -- no type split, and so no connector handle of its own either: every
    path gets its own separate node instead (see _render_path_node), same "lines from different
    paths never bunch up at one corner" reasoning. id/prompt/journal_text/topic_label/paths live
    in the paired detail panel (_render_stage_detail_panel). The hidden "ordinal" input here is
    opaque bookkeeping only -- see quests.py's own module docstring for what it's for -- carried
    through unchanged on every save, never rendered as an editable field anywhere.

    `error_messages`, if given, is this stage's own share of quests.check_quest_problems's output
    (see _group_quest_problems)."""
    stage_id = stage.get("id", "")
    paths = stage.get("paths") or []
    summary = f"{len(paths)} path{'s' if len(paths) != 1 else ''}" if paths else "Dialogue only (no paths yet)"
    error_attr = f' data-tooltip="{html.escape(" / ".join(error_messages))}"' if error_messages else ""
    return (
        f'<div class="stage-box{" is-start" if is_start else ""}{" has-error" if error_messages else ""}" '
        f'data-stage-prefix="{prefix}" style="left:{pos["x"]}px;top:{pos["y"]}px"{error_attr}>'
        f'<input type="hidden" name="{prefix}_x" class="stage-x-input" value="{pos["x"]}">'
        f'<input type="hidden" name="{prefix}_y" class="stage-y-input" value="{pos["y"]}">'
        f'<input type="hidden" name="{prefix}_ordinal" value="{html.escape(str(stage["ordinal"]) if "ordinal" in stage else "")}">'
        f'<div class="stage-box-header">'
        f'<button type="button" class="stage-flag{" is-start" if is_start else ""}" data-set-start '
        f'data-tooltip="Set as the stage every player starts this quest at.">🚩</button>'
        f'<span class="stage-box-icon">📖</span>'
        f'<span class="stage-box-id" data-stage-box-id>{html.escape(stage_id) or "(no id yet)"}</span>'
        f'<button type="button" class="stage-box-select" data-tooltip="Click to edit this stage\'s fields.">✏️</button>'
        f'</div>'
        f'<div class="stage-box-summary" data-stage-box-summary>{summary}</div>'
        f'</div>'
    )


def _render_path_row(prefix: str, path: dict) -> str:
    """One path within a stage's own nested "paths" repeatable -- a third level of nesting
    (stages -> stage -> paths), same wireRepeatAdd/ROWIDX machinery already proven
    nesting-depth-agnostic by _render_action_row (delves). "trigger" reuses _render_trigger_inputs
    verbatim, required here (unlike a delve action's optional "requires") -- a path with no
    trigger could never actually be taken. reward_item_kind/reward_item is the same kind-select +
    _render_cascaded_select pairing _render_shop_row's kind/item_id uses, scoped to the
    "quest_reward" cascade (quests.REWARD_REGISTRIES' kinds) -- reward_item_kind defaults to
    "equipment" (blank in the dropdown resolves to the same default at parse/runtime time) for
    every quest authored before reward_item_kind existed.

    Like a delve action's on_success/on_fail "next", this path's own "next" is NOT a typed input
    -- it's set by dragging this path's own connector handle (_render_path_node) onto a target
    stage on the canvas. The value lives in a same-named hidden input (`{prefix}_next`, read by
    _parse_paths unchanged), just written by JS instead of a person; the visible
    <span data-live-next> mirrors it read-only so this panel shows where the path currently leads
    without hunting for the arrow on the canvas."""
    reward_item_kind = path.get("reward_item_kind")
    reward_item_kind_options = "".join(
        f'<option value="{k}"{" selected" if k == reward_item_kind else ""}>{k}</option>'
        for k in [""] + sorted(quests.REWARD_REGISTRIES.keys())
    )
    reward_item_select = _render_cascaded_select(
        f"{prefix}_reward_item", "quest_reward", reward_item_kind or "equipment", path.get("reward_item")
    )
    return (
        f'<div class="row-group path-row">'
        f'{_render_trigger_inputs(f"{prefix}_trigger", path.get("trigger") or {})}'
        f'<label>on_complete_message<textarea name="{prefix}_message">'
        f'{html.escape(path.get("on_complete_message", ""))}</textarea></label>'
        f'<label>reward (currency, optional)<input type="number" min="0" name="{prefix}_reward" '
        f'value="{path.get("reward", "")}"></label>'
        f'<label>reward_item_kind<select name="{prefix}_reward_item_kind" class="cascade-select" '
        f'data-cascade="quest_reward">{reward_item_kind_options}</select></label>'
        f'<label>reward_item{reward_item_select}</label>'
        f'<label>turn_in_label (optional)<input type="text" name="{prefix}_turn_in_label" '
        f'placeholder="e.g. Pay rent" '
        f'value="{html.escape(path.get("turn_in_label", ""))}"></label>'
        f'<fieldset data-tooltip="Where this path leads once its trigger is satisfied. Drag this '
        f'path\'s handle, on the canvas above, to the target stage -- it can\'t be typed here.">'
        f'<legend>next <span class="live-next-tag" data-live-next="{prefix}_next">'
        f'{html.escape("→ " + path["next"]) if path.get("next") else "→ (ends the quest)"}'
        f'</span></legend>'
        f'<input type="hidden" name="{prefix}_next" value="{html.escape(path.get("next") or "")}">'
        f'</fieldset>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove path</button></div>'
    )


def _render_discuss_with_row(name: str, npc_id: str | None) -> str:
    """One NPC <select> row within a stage's own nested "discuss_with" repeatable -- every NPC
    listed here offers this stage as a topic in their own conversation flow (npc_view.py), not just
    this quest's own giver. Unlike every other row-builder here this one has just the one field, so
    `name` is the input's actual name, not a "prefix_suffix" pair -- exact sibling of
    _render_room_monster_row (delves)."""
    options = "".join(
        f'<option value="{nid}"{" selected" if nid == npc_id else ""}>{html.escape(npc["name"])} ({nid})</option>'
        for nid, npc in npcs.NPCS.items()
    )
    return (
        f'<div class="row-group" data-discuss-with-row><label>npc<select name="{name}">'
        f'<option value=""{" selected" if not npc_id else ""}>—</option>{options}</select></label>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
    )


def _render_stage_detail_panel(prefix: str, stage: dict) -> str:
    """The collapsible per-stage field panel -- id/prompt/journal_text/topic_label/discuss_with
    plus the nested "paths" repeatable. Shown for at most one stage at a time (see the quest
    flowchart script's selectStage), exact sibling of _render_room_detail_panel (delves) -- same
    "only the selected stage's fields are ever on screen at once" idea. There is deliberately no
    "next" field here at all -- each path's own connection is made by dragging its node's handle on
    the canvas (_render_path_node), never typed."""
    paths_container = f"{prefix}_paths"
    paths = stage.get("paths") or []
    path_rows_html = [_render_path_row(f"{paths_container}_{j}", p) for j, p in enumerate(paths)]
    path_template_html = _render_path_row(f"{paths_container}_ROWIDX", {})
    paths_repeatable = _render_repeatable(paths_container, path_rows_html, path_template_html, "+ Add path")

    discuss_container = f"{prefix}_discuss_with"
    discuss_with = stage.get("discuss_with") or []
    discuss_rows_html = [_render_discuss_with_row(f"{discuss_container}_{j}", nid) for j, nid in enumerate(discuss_with)]
    discuss_template_html = _render_discuss_with_row(f"{discuss_container}_ROWIDX", None)
    discuss_repeatable = _render_repeatable(discuss_container, discuss_rows_html, discuss_template_html, "+ Add NPC")

    return (
        f'<div class="stage-detail-panel stage-row" data-stage-panel="{prefix}" hidden>'
        f'<div class="stage-detail-panel-header"><strong>Stage details</strong>'
        f'<button type="button" class="stage-detail-close" '
        f'data-tooltip="Close this panel -- the stage stays on the canvas.">✕ Close</button></div>'
        f'<label>id<input type="text" name="{prefix}_id" class="stage-id-input" '
        f'value="{html.escape(stage.get("id", ""))}">'
        f'<small class="field-hint">Other stages\' paths connect to this stage by this id -- '
        f'renaming it here automatically fixes up any arrows already pointing here.</small></label>'
        f'<label>prompt<small class="field-hint">What the NPC actually says while this is the '
        f'player\'s current stage.</small>'
        f'<textarea name="{prefix}_prompt">{html.escape(stage.get("prompt", ""))}</textarea></label>'
        f'<label>journal_text<small class="field-hint">The objective line shown in !journal -- '
        f'distinct from prompt, which is only ever what the NPC actually says.</small>'
        f'<textarea name="{prefix}_journal_text">{html.escape(stage.get("journal_text", ""))}</textarea></label>'
        f'<label>topic_label (optional)<input type="text" name="{prefix}_topic_label" '
        f'placeholder="e.g. Ask about a place to stay" '
        f'value="{html.escape(stage.get("topic_label", ""))}"></label>'
        f'<label>discuss_with<small class="field-hint">Every NPC listed here offers this stage as '
        f'a topic in their own Talk conversation while it\'s the player\'s current stage -- not '
        f'just this quest\'s own giver.</small></label>{discuss_repeatable}'
        f'<label>paths<small class="field-hint">Each path gets its own arrow on the canvas -- two '
        f'or more paths already fork the quest by whichever trigger becomes satisfied first, no '
        f'separate "branch" concept needed. Leave empty for a dialogue-only stage that just sits '
        f'here until more content is added.</small></label>{paths_repeatable}'
        f'<button type="button" class="remove-row" data-remove-stage>✕ Remove stage</button></div>'
    )


def _render_stage_node(
    prefix: str, stage: dict, pos: dict, is_start: bool,
    stage_errors: list[str] | None = None, path_errors: dict[int, list[str]] | None = None,
) -> str:
    """Box + path nodes + detail panel for one stage, wrapped in a single .stage-wrapper -- the
    unit _render_repeatable's existing <template>/ROWIDX clone mechanism (see wireRepeatAdd)
    operates on, so "+ Add Stage" keeps working with zero changes to that shared primitive. Path
    nodes are siblings of the stage's own box (not nested inside it) so each gets its own
    independent position in the same canvas coordinate space -- see _render_path_node. Exact
    sibling of _render_room_node (delves), simplified (no group-node concept -- quest stages have
    no monster-group equivalent).

    `stage_errors`/`path_errors` are this one stage's slice of a quest-wide problem map (see
    _group_quest_problems) -- path_errors is keyed by path index within this stage."""
    path_nodes_html = "".join(
        _render_path_node(
            f"{prefix}_paths_{j}", path,
            {"x": path["x"], "y": path["y"]} if "x" in path and "y" in path else _default_path_position(pos, j),
            (path_errors or {}).get(j),
        )
        for j, path in enumerate(stage.get("paths", []))
    )
    return (
        f'<div class="stage-wrapper" data-stage-wrapper="{prefix}">'
        f'{_render_stage_box(prefix, stage, pos, is_start, stage_errors)}'
        f'{path_nodes_html}'
        f'{_render_stage_detail_panel(prefix, stage)}'
        f'</div>'
    )


def _group_quest_problems(problems: list[dict]) -> tuple[dict[str, list[str]], dict[str, dict[int, list[str]]]]:
    """Splits quests.check_quest_problems's flat output into (stage_id -> messages, stage_id ->
    {path_index -> messages}) -- the shape _render_quest_flowchart needs to hand each stage/path
    node its own slice. Exact sibling of _group_delve_problems. A quest-level problem (stage_id is
    None -- e.g. unreachable stages, a bad start_stage) has nowhere on the canvas to attach to, so
    it's dropped here; it's still shown in the page-level banner (see edit_view), same as always."""
    stage_msgs: dict[str, list[str]] = {}
    path_msgs: dict[str, dict[int, list[str]]] = {}
    for p in problems:
        stage_id = p["stage_id"]
        if stage_id is None:
            continue
        if p["path_index"] is None:
            stage_msgs.setdefault(stage_id, []).append(p["message"])
        else:
            path_msgs.setdefault(stage_id, {}).setdefault(p["path_index"], []).append(p["message"])
    return stage_msgs, path_msgs


def _render_quest_flowchart(label: str, stages: list[dict], entry: dict, problems: list[dict] | None = None) -> str:
    """Top-level renderer for a quest's "stages" field -- a flowchart canvas (draggable stage
    boxes + an SVG arrow overlay, see the "Quest flowchart editor" section of _dynamic_script)
    instead of a flat stack of text-box rows. Exact sibling of _render_delve_flowchart, kept as a
    separate script/render tree rather than sharing one with delves -- see the CSS comment at the
    top of _PAGE_CSS for why. `entry` is the full quest dict, not just its "stages" value --
    _render_field already passes this through for every field type, so this reads
    entry.get("layout")/entry.get("start_stage") with no new plumbing. The single page-level
    hidden "start_stage" input lives here, replacing what would otherwise be its own top-level
    field entirely (see admin_schemas.py) -- it's set by clicking a stage's own flag icon
    (_render_stage_box) instead.

    `problems`, if given, is quests.check_quest_problems's output for this exact entry -- grouped
    per stage/path (see _group_quest_problems) so a draft's still-broken spots are highlighted
    directly on the canvas, not just named in a page-level banner."""
    layout = entry.get("layout") or {}
    start_stage = entry.get("start_stage") or (stages[0]["id"] if stages else "")
    stage_msgs, path_msgs = _group_quest_problems(problems or [])

    stage_nodes_html = []
    for i, stage in enumerate(stages):
        prefix = f"stage_{i}"
        pos = layout.get(stage.get("id"), _default_stage_position(i))
        stage_id = stage.get("id")
        stage_nodes_html.append(_render_stage_node(
            prefix, stage, pos, stage.get("id") == start_stage,
            stage_msgs.get(stage_id), path_msgs.get(stage_id),
        ))
    template_html = _render_stage_node("stage_ROWIDX", {}, _default_stage_position(len(stages)), False)

    canvas_html = _render_repeatable("quest-stages-canvas", stage_nodes_html, template_html, "+ Add Stage")
    return (
        f'<fieldset><legend>{label}</legend>'
        f'<small class="field-hint">Drag stages to arrange them, drag a path\'s handle onto '
        f'another stage to connect them, click a stage to edit its fields, and click the flag to '
        f'set the start stage.</small>'
        f'<input type="hidden" name="start_stage" id="start_stage_field" value="{html.escape(start_stage)}">'
        f'<div class="delve-canvas-wrap">{canvas_html}<svg class="delve-arrows"></svg></div>'
        f'</fieldset>'
    )


def _render_cascaded_select(name: str, cascade: str, kind: str | None, current: str | None) -> str:
    """A <select> of item ids whose valid options depend on a sibling kind/output_kind field's
    current value -- `kind` is that sibling's value right now (server-known at render time, so the
    initial page load already shows the right options with no dependency on JS having run yet);
    changing the sibling client-side repopulates this via CASCADE_OPTIONS[cascade][newKind] (see
    admin_server._dynamic_script's updateCascade). Always includes a blank leading option, same
    convention as every other optional-selection <select> in this form builder (materials,
    reward_item, room_exits)."""
    options = _cascade_options().get(cascade, {}).get(kind or "", [])
    opts_html = "".join(
        f'<option value="{html.escape(item_id)}"{" selected" if item_id == current else ""}>{html.escape(label)}</option>'
        for item_id, label in options
    )
    return f'<select name="{html.escape(name)}" data-cascade-target="{cascade}"><option value="">—</option>{opts_html}</select>'


def _render_shop_row(prefix: str, shop_entry: dict) -> str:
    """One row of an npc's "shop" list -- see _parse_field's "shop_items" case for the matching
    parse side. item_id is a _render_cascaded_select scoped to this row's own "kind" (not a
    top-level sibling field, so it can't reuse the "cascaded_id" field type -- see
    admin_schemas.py's "shop_items" docs)."""
    kind = shop_entry.get("kind")
    kind_options = "".join(
        f'<option value="{k}"{" selected" if k == kind else ""}>{k}</option>'
        for k in [""] + SHOP_KINDS
    )
    item_select = _render_cascaded_select(f"{prefix}_item_id", "shop", kind, shop_entry.get("item_id"))
    return (
        f'<div class="row-group">'
        f'<label>kind<select name="{prefix}_kind" class="cascade-select" data-cascade="shop">{kind_options}</select></label>'
        f'<label>item_id{item_select}</label>'
        f'<label>price<input type="number" min="1" name="{prefix}_price" value="{shop_entry.get("price", "")}"></label>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
    )


def _render_room_exit_row(prefix: str, room_id: str | None, label: str | None) -> str:
    """One row of a "room_exits" list. See _parse_field's "room_exits" case for the matching parse
    side."""
    room_ids = sorted(rooms.ROOMS.keys())
    options = "".join(
        f'<option value="{r}"{" selected" if r == room_id else ""}>{r}</option>' for r in [""] + room_ids
    )
    return (
        f'<div class="row-group">'
        f'<label>room_id<select name="{prefix}_room_id" class="room-exit-room-select">{options}</select></label>'
        f'<label>label<input type="text" name="{prefix}_label" class="exit-label-input" '
        f'value="{html.escape(label or "")}"></label>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
    )


def _render_room_command_row(prefix: str, command: dict) -> str:
    """One row of a "room_commands" list -- see _parse_field's "room_commands" case for the
    matching parse side. `key` is sourced live from room_commands.COMMANDS (not frozen at
    admin_schemas.py import time -- see that module's docstring for why this specific one can't
    be, unlike most other choices this editor offers)."""
    command_keys = sorted(room_commands.COMMANDS.keys())
    key_options = "".join(
        f'<option value="{k}"{" selected" if k == command.get("key") else ""}>{k}</option>'
        for k in [""] + command_keys
    )
    kind_options = "".join(
        f'<option value="{k}"{" selected" if k == command.get("kind") else ""}>{k}</option>'
        for k in _ROOM_COMMAND_KINDS
    )
    const_args = ", ".join(command.get("const_args", []))
    checked = " checked" if command.get("closes_hub") else ""
    return (
        f'<div class="row-group">'
        f'<label>key<select name="{prefix}_key" required>{key_options}</select></label>'
        f'<label>kind<select name="{prefix}_kind" class="command-kind-select">{kind_options}</select></label>'
        f'<label>label<input type="text" name="{prefix}_label" required '
        f'value="{html.escape(command.get("label", ""))}"></label>'
        f'<label>const_args<input type="text" name="{prefix}_const_args" value="{html.escape(const_args)}" '
        f'placeholder="comma-separated, e.g. speed"></label>'
        f'<label data-amount-only>modal_title<input type="text" name="{prefix}_modal_title" '
        f'value="{html.escape(command.get("modal_title", ""))}"></label>'
        f'<label data-amount-only>input_label<input type="text" name="{prefix}_input_label" '
        f'value="{html.escape(command.get("input_label", ""))}"></label>'
        f'<label class="checkbox-label"><input type="checkbox" name="{prefix}_closes_hub"{checked}> closes_hub</label>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
    )


def _render_field(field: dict, value, entry: dict | None = None, problems: list[dict] | None = None) -> str:
    name, ftype = field["name"], field["type"]
    label = html.escape(name)

    if ftype in ("str", "int", "float", "color"):
        input_type = {"str": "text", "int": "number", "float": "number", "color": "color"}[ftype]
        step_attr = ' step="any"' if ftype == "float" else ""
        v = "" if value is None else value
        return (
            f'<label>{label}<input type="{input_type}"{step_attr} name="{html.escape(name)}" '
            f'value="{html.escape(str(v))}"></label>'
        )

    if ftype == "text":
        v = "" if value is None else value
        return f'<label>{label}<textarea name="{html.escape(name)}">{html.escape(str(v))}</textarea></label>'

    if ftype == "bool":
        # Unlike every other field type, `value` being None doesn't mean "leave it blank" -- a
        # checkbox is always definitively on or off. For a field where the *real loader* treats a
        # missing key as true (e.g. dungeon.py's delve "active", absent = active, for pre-existing
        # content saved before this field existed), a schema "default" of True is what makes the
        # checkbox's initial state match that actual behavior instead of always starting unchecked
        # for old data -- otherwise resaving an old delve untouched would silently write "active":
        # false the first time, since an unchecked box is genuinely absent from the submitted form.
        v = value if value is not None else field.get("default", False)
        checked = " checked" if v else ""
        return f'<label class="checkbox-label"><input type="checkbox" name="{html.escape(name)}"{checked}> {label}</label>'

    if ftype == "enum":
        # An optional enum gets a blank leading option (a real <select> otherwise always defaults
        # to its first choice, which would silently pick one for a value that was actually never
        # set) -- a required one doesn't, since it never needs to represent "no value".
        #
        # Each choice is either a bare string (the stored value doubles as its own displayed
        # label -- every choices list before this one) or a (value, label) pair, for a dropdown
        # that needs to show something richer than the raw stored value itself (a skill's
        # main_class showing "The Enforcer (Ace)" while still storing "fighter"). Normalized to
        # pairs uniformly here so the rendering loop below never needs to care which shape a given
        # field's choices came in as.
        raw_choices = field["choices"]() if callable(field["choices"]) else field["choices"]
        pairs = [c if isinstance(c, (tuple, list)) else (c, c) for c in raw_choices]
        if not field.get("required", True):
            pairs = [("", "—")] + pairs
        options = "".join(
            f'<option value="{html.escape(v)}"{" selected" if v == (value or "") else ""}>{html.escape(lbl)}</option>'
            for v, lbl in pairs
        )
        # "cascades_to" (e.g. recipes' output_kind) names a CASCADE_OPTIONS entry a sibling
        # "cascaded_id" field's <select> repopulates from when this one changes -- see
        # admin_schemas.py's "cascaded_id" field type docs and _dynamic_script's updateCascade.
        cascade_attr = f' class="cascade-select" data-cascade="{field["cascades_to"]}"' if field.get("cascades_to") else ""
        return f'<label>{label}<select name="{html.escape(name)}"{cascade_attr}>{options}</select></label>'

    if ftype == "cascaded_id":
        kind_value = (entry or {}).get(field["cascade_from"])
        select_html = _render_cascaded_select(name, field["cascade"], kind_value, value)
        return f'<label>{label}{select_html}</label>'

    if ftype == "room_exits":
        exits = list(value or [])
        rows_html = [_render_room_exit_row(f"room_exit_{i}", e.get("room_id"), e.get("label")) for i, e in enumerate(exits)]
        template_html = _render_room_exit_row("room_exit_ROWIDX", None, None)
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add exit")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "room_commands":
        cmds = list(value or [])
        rows_html = [_render_room_command_row(f"room_command_{i}", c) for i, c in enumerate(cmds)]
        template_html = _render_room_command_row("room_command_ROWIDX", {})
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add command")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "equipment_effects":
        effects = list(value or [])
        rows_html = [
            _render_effect_row(f"equipment_effect_{i}", e, include_trigger=True) for i, e in enumerate(effects)
        ]
        template_html = _render_effect_row("equipment_effect_ROWIDX", {}, include_trigger=True)
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add effect")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "vs_monster_debuff":
        # Not a repeatable -- an item has at most one of these. Leaving "monster" blank on save
        # means "no vs_monster_debuff at all" (see _parse_field's matching case), same "absent key,
        # not an empty one" idea used elsewhere (e.g. npcs.json's shop).
        vs = value or {}
        vs_effects = vs.get("effects", {})
        monster_options = "".join(
            f'<option value="{mid}"{" selected" if mid == vs.get("monster_id") else ""}>{lbl}</option>'
            for mid, lbl in [("", "—")] + _monster_option_choices()
        )
        stat_inputs = "".join(
            f'<label>{stat}<input type="number" min="0" name="{name}_{stat}" value="{vs_effects.get(stat, "")}"></label>'
            for stat in dungeon.VS_MONSTER_DEBUFF_STATS
        )
        return (
            f'<fieldset><legend>{label}</legend>'
            f'<label>monster<select name="{name}_monster_id">{monster_options}</select></label>'
            f'{stat_inputs}'
            f'</fieldset>'
        )

    if ftype == "effects":
        effects = list(value or [])
        rows_html = [_render_effect_row(f"effect_{i}", e) for i, e in enumerate(effects)]
        template_html = _render_effect_row("effect_ROWIDX", {})
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add effect")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "effect_groups":
        groups = list(value or [])
        rows_html = [_render_effect_group_row(f"effectgroup_{i}", g) for i, g in enumerate(groups)]
        template_html = _render_effect_group_row("effectgroup_ROWIDX", {})
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add group")
        # data-group-odds/data-group-odds-summary reuse the exact same generic updateGroupOdds
        # (_dynamic_script) a combat room's own monster_groups already drives -- it scopes itself
        # per fieldset, not per content type, so this field's live-odds display needed zero new JS.
        odds_summary = (
            '<div class="skill-odds-summary" data-group-odds-summary>'
            "Live odds appear here once groups are filled in.</div>"
        )
        return f'<fieldset data-group-odds><legend>{label}</legend>{odds_summary}{repeatable}</fieldset>'

    if ftype == "monster_skills":
        skills = list(value or [])
        rows_html = [_render_monster_skill_row(f"skill_{i}", s) for i, s in enumerate(skills)]
        template_html = _render_monster_skill_row("skill_ROWIDX", {})
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add skill")
        # data-skill-odds + #skill-odds-summary: the page script's updateSkillOdds recomputes and
        # displays each option's actual live percent chance (attack_chance above plus every skill
        # row's own chance) on every keystroke -- see _dynamic_script.
        odds_summary = '<div class="skill-odds-summary" id="skill-odds-summary">Live odds appear here once attack_chance/skills are filled in.</div>'
        return f'<fieldset data-skill-odds><legend>{label}</legend>{odds_summary}{repeatable}</fieldset>'

    if ftype == "materials":
        materials = list((value or {}).items())
        rows_html = [_render_material_row(f"material_{i}", m_id, qty) for i, (m_id, qty) in enumerate(materials)]
        template_html = _render_material_row("material_ROWIDX", None, None)
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add material")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "monster_drops":
        drops = list(value or [])
        rows_html = [_render_drop_row(f"drop_{i}", d) for i, d in enumerate(drops)]
        template_html = _render_drop_row("drop_ROWIDX", {})
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add drop")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "delve_flowchart":
        rooms = list(value or [])
        return _render_delve_flowchart(label, rooms, entry or {}, problems)

    if ftype == "shop_items":
        shop_entries = list(value or [])
        rows_html = [_render_shop_row(f"shop_{i}", s) for i, s in enumerate(shop_entries)]
        template_html = _render_shop_row("shop_ROWIDX", {})
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add shop item")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "image":
        # No _parse_field case for "image" -- it needs actual file I/O and the entry's current
        # value (to keep it when no new file is uploaded), neither of which a plain form->value
        # parser has access to. edit_view handles it separately; see _save_uploaded_image.
        return _render_image_input(name, label, value)

    if ftype == "trigger":
        return f'<fieldset><legend>{label}</legend>{_render_trigger_inputs(name, value or {})}</fieldset>'

    if ftype == "quest_flowchart":
        stages = list(value or [])
        return _render_quest_flowchart(label, stages, entry or {}, problems)

    raise ValueError(f"admin_schemas.py: unknown field type {ftype!r}")


def _render_effects_toggle(effects_html: str, groups_html: str, has_groups: bool) -> str:
    """Wraps a plain "effects" fieldset and its "effect_groups" alternative in a mode toggle --
    dungeon._validate_effects_or_groups still enforces exactly one of the two server-side (in case
    JS never ran), but client-side this replaces the old "both boxes always visible, a hint says
    fill in only one" UI with an actual switch (wireEffectsModeToggles/updateEffectsMode in
    _dynamic_script) that shows one at a time and clears the other's rows on switch, so a normal
    save can't accidentally submit both. `has_groups` (whether the entry being edited already has
    a non-empty "effect_groups") picks which side starts visible."""
    initial_mode = "groups" if has_groups else "effects"
    options = (
        f'<option value="effects"{" selected" if initial_mode == "effects" else ""}>'
        f'Effects (independent per-effect chance)</option>'
        f'<option value="groups"{" selected" if initial_mode == "groups" else ""}>'
        f'Effect Groups (mutually-exclusive alternatives)</option>'
    )
    effects_display = "" if initial_mode == "effects" else "display:none"
    groups_display = "" if initial_mode == "groups" else "display:none"
    return (
        f'<div data-effects-mode-wrap>'
        f'<label>Mode<select class="effects-mode-select">{options}</select></label>'
        f'<div data-effects-box style="{effects_display}">{effects_html}</div>'
        f'<div data-groups-box style="{groups_display}">{groups_html}</div>'
        f'</div>'
    )


def _render_field_with_hint(field: dict, value, entry: dict | None = None, problems: list[dict] | None = None) -> str:
    """_render_field, plus that field's optional schema-level "hint" (see admin_schemas.py's
    module docstring) as small print underneath -- for a top-level box like "npc" whose meaning
    isn't obvious from its name alone. Kept separate from _render_field itself so every field
    type's branch there stays focused on just its own markup. `entry` is only ever consulted by
    the "cascaded_id" branch (it needs a sibling field's current value, not just its own);
    `problems` only by "delve_flowchart" (see _render_delve_flowchart)."""
    field_html = _render_field(field, value, entry, problems)
    hint = field.get("hint")
    if not hint:
        return field_html
    return field_html + f'<small class="field-hint">{html.escape(hint)}</small>'


def _render_fields(fields: list[dict], entry: dict, problems: list[dict] | None = None) -> str:
    """Renders a whole edit form's fields in schema order, inserting a heading each time a
    field's "group" (see admin_schemas.py's module docstring) differs from the previous field's --
    turns a flat stack of same-weight boxes into sections ("Identity", "Stats", "Loot", ...).
    Fields with no "group" (typically compound types like effects/materials/trigger, which already
    render inside their own labeled <fieldset>) just flow without a heading.

    A schema's "effects" field immediately followed by its "effect_groups" alternative (skills,
    consumables -- see admin_schemas.py) is special-cased into one combined mode-toggle block
    (_render_effects_toggle) instead of two independent fieldsets, so only one is ever visible/
    submitted at a time. Every other field renders exactly as before."""
    parts = []
    last_group = None
    i = 0
    while i < len(fields):
        field = fields[i]
        group = field.get("group")
        if group and group != last_group:
            parts.append(f'<div class="field-group-heading">{html.escape(group)}</div>')
        last_group = group
        if (
            field["type"] == "effects"
            and i + 1 < len(fields)
            and fields[i + 1]["type"] == "effect_groups"
        ):
            groups_field = fields[i + 1]
            effects_html = _render_field(field, entry.get(field["name"]), entry, problems)
            groups_html = _render_field_with_hint(groups_field, entry.get(groups_field["name"]), entry, problems)
            has_groups = bool(entry.get(groups_field["name"]))
            parts.append(_render_effects_toggle(effects_html, groups_html, has_groups))
            i += 2
            continue
        parts.append(_render_field_with_hint(field, entry.get(field["name"]), entry, problems))
        i += 1
    return "".join(parts)


def _parse_effects_list(container_prefix: str, form: dict) -> list[dict]:
    """Parses one "effects" repeatable's submitted rows -- shared by the top-level "effects" field
    type and each monster skill's own nested effects list (see "monster_skills" below).
    Indices aren't contiguous from 0 -- rows can be added/removed client-side in any order (see
    _render_effect_row) -- so this discovers whatever "<container_prefix>_<N>_type" keys actually
    made it into the submission."""
    pattern = re.compile(rf"{re.escape(container_prefix)}_(\d+)_type")
    indices = sorted(int(m.group(1)) for k in form if (m := pattern.fullmatch(k)))
    effects = []
    for i in indices:
        prefix = f"{container_prefix}_{i}"
        effect_type = form.get(f"{prefix}_type", "").strip()
        if not effect_type:
            continue
        effect = {"type": effect_type}
        for p in EFFECT_PARAM_NAMES:
            raw = form.get(f"{prefix}_{p}", "").strip()
            if raw:
                effect[p] = float(raw) if "." in raw else int(raw)
        # A checkbox's presence in the submitted form IS "checked" -- same convention _parse_field's
        # own "bool" case uses (see its comment there). Omitted (not just False) when unchecked, so
        # it matches every other effect authored before "aoe" existed.
        if f"{prefix}_aoe" in form:
            effect["aoe"] = True
        target = form.get(f"{prefix}_target", "").strip()
        if target:
            effect["target"] = target
        # The universal independent per-effect fire probability (dungeon.resolve_cast_effects) --
        # blank means "always fires" (dungeon._validate_effects' own default), same "absent, not a
        # 0" convention as every other optional numeric field here. This is the one place
        # _render_effect_row's chance input is NOT trigger-gated (that's equipment's own on_hit-only
        # chance, a different field on the very same row when include_trigger=True -- see that
        # function's own comment on why the two never collide despite sharing a name).
        raw_chance = form.get(f"{prefix}_chance", "").strip()
        if raw_chance:
            effect["chance"] = float(raw_chance)
        effects.append(effect)
    return effects


def _parse_effect_groups(container_prefix: str, form: dict) -> list[dict]:
    """Parses one "effect_groups" repeatable's submitted rows -- skills/consumables' alternative to
    plain "effects" (see _render_effect_group_row), nested one level deeper (group -> its own
    "effects" sub-repeatable). Group index is discovered by presence of its own nested
    "<prefix>_<i>_effects_<k>_type" key -- the same "index discovered by a nested key's presence"
    approach _parse_delve_flowchart's own monster groups already use -- rather than the group's own
    "chance" key, since a group can legitimately have a blank/default chance and still be real, but
    can't have zero effects. A group whose nested effects all ended up blank is dropped entirely,
    same reasoning an empty monster group already gets dropped for."""
    pattern = re.compile(rf"{re.escape(container_prefix)}_(\d+)_effects_\d+_type")
    indices = sorted({int(m.group(1)) for k in form if (m := pattern.fullmatch(k))})
    groups = []
    for i in indices:
        prefix = f"{container_prefix}_{i}"
        effects = _parse_effects_list(f"{prefix}_effects", form)
        if not effects:
            continue
        group: dict = {"effects": effects}
        raw_chance = form.get(f"{prefix}_chance", "").strip()
        if raw_chance:
            group["chance"] = float(raw_chance) if "." in raw_chance else int(raw_chance)
        groups.append(group)
    return groups


def _parse_equipment_effects(form: dict) -> list[dict]:
    """Parses equipment's own "equipment_effects" repeatable -- same index-discovery approach as
    _parse_effects_list, plus the two extra fields only an equipment effect row carries: trigger
    (always present -- every row renders the select) and chance (only meaningful for an on_hit
    row; the input itself is always present in the submission even when the page script has
    hidden it for a non-on_hit row, since data-trigger-only is JS-only display state, so this only
    keeps chance when trigger=="on_hit" rather than trusting whether the field was visible). No
    validation beyond shaping the dict correctly -- the real trigger/type-restriction/chance-range
    checks happen at save time via dungeon._validate_equipment_effects (the real loader)."""
    container_prefix = "equipment_effect"
    pattern = re.compile(rf"{re.escape(container_prefix)}_(\d+)_type")
    indices = sorted(int(m.group(1)) for k in form if (m := pattern.fullmatch(k)))
    effects = []
    for i in indices:
        prefix = f"{container_prefix}_{i}"
        effect_type = form.get(f"{prefix}_type", "").strip()
        if not effect_type:
            continue
        effect = {"type": effect_type}
        trigger = form.get(f"{prefix}_trigger", "").strip()
        if trigger:
            effect["trigger"] = trigger
        if trigger == "on_hit":
            raw_chance = form.get(f"{prefix}_chance", "").strip()
            if raw_chance:
                effect["chance"] = float(raw_chance)
        for p in EFFECT_PARAM_NAMES:
            raw = form.get(f"{prefix}_{p}", "").strip()
            if raw:
                effect[p] = float(raw) if "." in raw else int(raw)
        if f"{prefix}_aoe" in form:
            effect["aoe"] = True
        target = form.get(f"{prefix}_target", "").strip()
        if target:
            effect["target"] = target
        effects.append(effect)
    return effects


def _parse_field(field: dict, form: dict) -> tuple | None:
    """Returns (name, value) to set on the entry, or None to omit the key entirely (an optional
    field left blank)."""
    name, ftype = field["name"], field["type"]

    if ftype == "str":
        v = form.get(name, "").strip()
        # A blank value is always omitted, required or not -- writing "" for a required field
        # would satisfy _REQUIRED_*_FIELDS's "key present" check (missing = REQUIRED - entry.
        # keys()) while being exactly as invalid as not submitting it at all; omitting it instead
        # lets the real loader's required-field error fire with the correct message.
        return (name, v) if v else None

    if ftype == "color":
        v = form.get(name, "").strip()
        return (name, v) if v else None

    if ftype == "bool":
        # A checkbox is simply absent from the submitted form when unchecked (never "off" or
        # "false") -- presence is the whole signal. Always returns a value (never None), unlike
        # every other field type here, since "not checked" is itself a meaningful, always-valid
        # False rather than "the author left this blank."
        return (name, name in form)

    if ftype == "cascaded_id":
        # Parses identically to "str" -- it's still just a free-standing id, the <select>
        # rendering (_render_field's "cascaded_id" branch) is what's different, not the value
        # shape the real loader ends up checking.
        v = form.get(name, "").strip()
        return (name, v) if v else None

    if ftype == "text":
        v = form.get(name, "")
        # Same reasoning as "str" above -- a blank value is always omitted, letting the real
        # loader's required-field check fire instead of silently saving an empty string.
        return (name, v) if v else None

    if ftype == "int":
        v = form.get(name, "").strip()
        if not v:
            return (name, field["default"]) if "default" in field else None
        return (name, int(v))

    if ftype == "float":
        v = form.get(name, "").strip()
        if not v:
            return (name, field["default"]) if "default" in field else None
        return (name, float(v))

    if ftype == "enum":
        # Same blank-is-always-omitted reasoning as "str" above -- matters most for an optional
        # enum (e.g. a room's "specialization"), where a blank selection must come through as
        # "key absent", not a "" value the real loader would reject as an unknown choice.
        v = form.get(name, "").strip()
        return (name, v) if v else None

    if ftype == "equipment_effects":
        return (name, _parse_equipment_effects(form))

    if ftype == "vs_monster_debuff":
        monster_id = form.get(f"{name}_monster_id", "").strip()
        if not monster_id:
            return None  # no monster picked -- this item has no vs_monster_debuff at all
        effects = {}
        for stat in dungeon.VS_MONSTER_DEBUFF_STATS:
            raw = form.get(f"{name}_{stat}", "").strip()
            if raw:
                effects[stat] = float(raw) if "." in raw else int(raw)
        if not effects:
            return None  # a monster was picked but every stat was left blank -- nothing to apply
        return (name, {"monster_id": monster_id, "effects": effects})

    if ftype == "effects":
        # Omitted entirely (not an empty list) when blank -- unlike every other "effects" field
        # before effect_groups existed, this one can legitimately be left blank on purpose (the
        # skill/consumable uses effect_groups instead), and dungeon._validate_effects_or_groups
        # tells the two apart by key PRESENCE, not by whether the list happens to be empty. A
        # skill/consumable that's genuinely missing both still fails loudly at save time either way.
        effects = _parse_effects_list("effect", form)
        return (name, effects) if effects else None

    if ftype == "effect_groups":
        groups = _parse_effect_groups("effectgroup", form)
        return (name, groups) if groups else None

    if ftype == "monster_skills":
        # A skill row is discovered by its own "_name" key being non-blank -- same "not yet filled
        # in, just skip it" reasoning _render_room_monster_row's monster rows already use, rather
        # than actions'/rooms' presence-based-plus-error-banner approach, since a monster skill's
        # name has nowhere sensible to fall back to (no "blank name = special meaning" case exists
        # here the way a blank room "next" means "wins the delve").
        indices = sorted({
            int(m.group(1)) for k in form if (m := re.fullmatch(r"skill_(\d+)_name", k)) and form[k].strip()
        })
        skills = []
        for i in indices:
            prefix = f"skill_{i}"
            skill_name = form.get(f"{prefix}_name", "").strip()
            raw_chance = form.get(f"{prefix}_chance", "").strip()
            chance = (float(raw_chance) if "." in raw_chance else int(raw_chance)) if raw_chance else 0
            effects = _parse_effects_list(f"{prefix}_effects", form)
            groups = _parse_effect_groups(f"{prefix}_effectgroup", form)
            special = f"{prefix}_special" in form
            flavor = form.get(f"{prefix}_flavor", "").strip()
            if not effects and not groups:
                continue  # a skill with neither effects nor effect_groups yet isn't meaningful to save
            skill: dict = {"name": skill_name, "chance": chance, "special": special}
            # Exactly one of the two, same XOR dungeon._validate_effects_or_groups enforces at save
            # time -- if an author somehow filled in both, both land here and the real loader
            # rejects it loudly rather than this silently picking one.
            if effects:
                skill["effects"] = effects
            if groups:
                skill["effect_groups"] = groups
            if flavor:
                skill["flavor"] = flavor
            skills.append(skill)
        return (name, skills)

    if ftype == "materials":
        indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(r"material_(\d+)_id", k)))
        materials = {}
        for i in indices:
            prefix = f"material_{i}"
            m_id = form.get(f"{prefix}_id", "").strip()
            qty = form.get(f"{prefix}_qty", "").strip()
            if m_id and qty:
                materials[m_id] = int(qty)
        return (name, materials)

    if ftype == "monster_drops":
        indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(r"drop_(\d+)_kind", k)))
        drops = []
        for i in indices:
            prefix = f"drop_{i}"
            kind = form.get(f"{prefix}_kind", "").strip()
            item_id = form.get(f"{prefix}_item_id", "").strip()
            chance = form.get(f"{prefix}_chance", "").strip()
            if kind and item_id and chance:
                drop = {"kind": kind, "item_id": item_id, "chance": float(chance)}
                requires_key = form.get(f"{prefix}_requires_key", "").strip()
                requires_value = form.get(f"{prefix}_requires_value", "").strip()
                if requires_key and requires_value:
                    drop["requires"] = {"type": "flag_at_least", "key": requires_key, "value": int(requires_value)}
                drops.append(drop)
        return (name, drops)

    # No case here for "delve_flowchart" -- like "image", each room can carry an uploaded
    # background image, which needs real file I/O and the entry's previous value, neither of which
    # this plain form->value parser has access to. edit_view handles it separately, via
    # _parse_delve_flowchart.

    if ftype == "shop_items":
        indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(r"shop_(\d+)_kind", k)))
        shop_entries = []
        for i in indices:
            prefix = f"shop_{i}"
            kind = form.get(f"{prefix}_kind", "").strip()
            item_id = form.get(f"{prefix}_item_id", "").strip()
            price = form.get(f"{prefix}_price", "").strip()
            if kind and item_id and price:
                shop_entries.append({"kind": kind, "item_id": item_id, "price": int(price)})
        # Omitted entirely (not an empty list) when there's nothing to sell -- npcs._load_npcs
        # treats "shop" as either absent or a non-empty list, same "no key = no special
        # behavior" idea as visible_trigger being absent rather than {}.
        return (name, shop_entries) if shop_entries else None

    if ftype == "trigger":
        trigger = _parse_trigger(name, form)
        return (name, trigger) if trigger is not None else None

    # No case here for "quest_flowchart" -- like "delve_flowchart" above, it mixes repeatables and
    # drag state, so it's parsed separately in _build_entry_from_form, via _parse_quest_flowchart.

    if ftype == "room_exits":
        indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(r"room_exit_(\d+)_room_id", k)))
        exits = []
        for i in indices:
            prefix = f"room_exit_{i}"
            room_id = form.get(f"{prefix}_room_id", "").strip()
            exit_label = form.get(f"{prefix}_label", "").strip()
            if room_id and exit_label:
                exits.append({"room_id": room_id, "label": exit_label})
        return (name, exits)

    if ftype == "room_commands":
        indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(r"room_command_(\d+)_key", k)))
        room_command_list = []
        for i in indices:
            prefix = f"room_command_{i}"
            key = form.get(f"{prefix}_key", "").strip()
            kind = form.get(f"{prefix}_kind", "").strip()
            command_label = form.get(f"{prefix}_label", "").strip()
            if not (key or command_label):
                # A genuinely untouched row (e.g. "+ Add command" then never filled in) -- `kind`
                # deliberately has no blank option (it's a required field, always defaulting to
                # its first real choice, same "required enum" convention as everywhere else in
                # this schema), so it alone is never a signal that a row's actually been filled in.
                continue
            # A row with SOME but not all of key/kind/label filled in is still included (rather
            # than silently dropped) -- rooms._validate_command (run via the real loader in
            # _write_and_validate) is what rejects it loudly, same "let a bad combination surface
            # at Save, not disappear before validation even sees it" story as every other field
            # here. Previously this silently discarded the whole row on any one blank field, which
            # looked like "Save succeeded" while quietly not saving the command at all.
            command = {"key": key, "kind": kind, "label": command_label}
            const_args = [a.strip() for a in form.get(f"{prefix}_const_args", "").split(",") if a.strip()]
            if const_args:
                command["const_args"] = const_args
            modal_title = form.get(f"{prefix}_modal_title", "").strip()
            if modal_title:
                command["modal_title"] = modal_title
            input_label = form.get(f"{prefix}_input_label", "").strip()
            if input_label:
                command["input_label"] = input_label
            if form.get(f"{prefix}_closes_hub") == "on":
                command["closes_hub"] = True
            room_command_list.append(command)
        return (name, room_command_list)

    raise ValueError(f"admin_schemas.py: unknown field type {ftype!r}")


# --- File I/O + validation ----------------------------------------------------------------------

def _entries_path(spec: dict) -> str:
    return os.path.join(os.path.dirname(__file__), spec["json_path"])


def _load_raw_entries(spec: dict) -> list[dict]:
    with open(_entries_path(spec)) as f:
        return json.load(f)


def _auto_commit_content_save(path: str, spec: dict) -> None:
    """Commits `path`'s new content as its own git commit and pushes it to origin, right after a
    successful save/publish/delete -- every real content edit through this admin panel becomes its
    own checkpoint AND shows up on GitHub immediately, instead of piling up as uncommitted
    working-tree state or sitting locally-committed-but-unpushed indefinitely (these JSON files are
    edited live, constantly, exactly the kind of continuously-changing production data that's most
    exposed by sitting uncommitted/unpushed -- see CLAUDE.md/git history for why this exists).
    Also sweeps in every assets/ subdir this content type's "image"/"delve_flowchart" fields write
    to (e.g. "npcs", "rooms") -- a save that included a new or overwritten sprite/background would
    otherwise commit the JSON's new path while leaving the actual image file it points to
    uncommitted/unpushed, which looks like nothing went wrong (the save itself always succeeds --
    see _save_uploaded_image) until the image turns out missing wherever this repo gets pulled.
    Filtered to subdirs that actually exist, since `git add` on a path with no matches errors out
    (and would otherwise take the JSON `add` down with it in the same call).
    Best-effort and silent: never raises and never blocks a save from succeeding -- a git failure
    here (no repo, nothing actually changed, a lock held by something else, no network for the
    push) is a missed checkpoint, not a reason to reject content an admin just successfully
    validated and wrote. `push` uses "origin HEAD" (not a hardcoded branch name) so it works
    whatever branch this checkout happens to be on; a 15s timeout keeps a stalled connection from
    hanging the save request instead of just skipping the push."""
    repo_dir = os.path.dirname(__file__)
    asset_dirs = sorted({
        os.path.join(ASSETS_DIR, field["subdir"])
        for field in spec.get("fields", [])
        if field["type"] in ("image", "delve_flowchart")
    })
    asset_dirs = [d for d in asset_dirs if os.path.isdir(d)]
    paths = [path, *asset_dirs]
    try:
        subprocess.run(["git", "add", "--", *paths], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"content: save {os.path.basename(path)} via admin panel", "--", *paths],
            cwd=repo_dir, capture_output=True,
        )
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=repo_dir, capture_output=True, timeout=15)
    except Exception:
        pass  # best-effort checkpoint only -- see docstring


def _write_and_validate(spec: dict, entries: list[dict]) -> str | None:
    """Writes `entries` to a temp file, validates by calling the real dungeon.py loader against
    it (plus any of this content type's own "extra_validators" -- see admin_schemas.py's "rooms"
    entry for why a loader alone sometimes can't cover everything), and only replaces the live
    JSON file (and hot-reloads the in-memory registry) if that succeeds. Returns None on success,
    or the failing validator's own error message on failure -- in which case the live file is
    untouched. Every successful write also gets its own git commit (see
    _auto_commit_content_save) -- best-effort, never blocks the save itself."""
    path = _entries_path(spec)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")
    try:
        new_registry = spec["loader"](tmp_path)
        for extra_validator in spec.get("extra_validators", []):
            extra_validator(new_registry)
    except ValueError as e:
        os.remove(tmp_path)
        return str(e)
    os.replace(tmp_path, path)
    setattr(spec["module"], spec["registry_attr"], new_registry)
    _auto_commit_content_save(path, spec)
    return None


def _save_uploaded_image(file_field, subdir: str, entry_id: str) -> str | None:
    """Saves an uploaded image to assets/<subdir>/<entry_id><ext>, returning the relative path
    (e.g. "assets/dungeon/monsters/goblin_grunt.png") to store in the entry -- same convention
    the hand-placed sprite files already use. None if no real file was actually selected (an
    untouched <input type=file> still submits a form part, just with an empty filename) -- the
    caller keeps whatever path the entry already had in that case, since re-uploading isn't
    required on every edit. Raises ValueError (shown to the user, nothing written) on a
    disallowed extension or an oversized file."""
    if not isinstance(file_field, web.FileField) or not file_field.filename:
        return None
    ext = os.path.splitext(file_field.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported image type {ext!r} -- use one of {sorted(ALLOWED_IMAGE_EXTENSIONS)}")
    data = file_field.file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large (max {MAX_IMAGE_BYTES // (1024 * 1024)}MB)")
    dest_dir = os.path.join(ASSETS_DIR, subdir)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, f"{entry_id}{ext}")
    with open(dest_path, "wb") as f:
        f.write(data)
    return os.path.relpath(dest_path, os.path.dirname(__file__))


def _build_entry_from_form(spec: dict, form: dict, entry_id_for_upload: str, existing_entry: dict) -> tuple[dict, list[str], list[str]]:
    """Parses one submitted edit-form into the JSON-shaped dict `_write_and_validate` (or, for a
    delve draft, dungeon.save_delve_draft) expects, field by field per spec["fields"]. Shared by
    every content type's ordinary save AND (for delves) both the autosave and publish routes, so
    those three save paths can never drift from each other on what a submitted form actually
    means.

    Returns (new_entry, fatal_errors, soft_errors). fatal_errors is a bad image upload -- always
    fatal, for any content type, since there's no partial state worth preserving for a failed file
    write. soft_errors is delve-only: a still-blank room id/action label, collected by
    _parse_delve_flowchart instead of raised (see its own docstring) -- an ordinary save still
    treats this as fatal too (see edit_view, which merges both lists back together for that path),
    but a delve's draft autosave does not, which is the entire reason these are kept separate
    here rather than one merged list like before."""
    new_entry: dict = {}
    fatal_errors: list[str] = []
    soft_errors: list[str] = []
    for field in spec["fields"]:
        if field["type"] == "image":
            try:
                new_path = _save_uploaded_image(
                    form.get(f"{field['name']}_file"), field["subdir"], entry_id_for_upload
                )
            except ValueError as e:
                fatal_errors.append(str(e))
                new_path = None
            if new_path is not None:
                new_entry[field["name"]] = new_path
            elif existing_entry.get(field["name"]):
                new_entry[field["name"]] = existing_entry[field["name"]]  # no new upload -- keep what was there
            continue
        if field["type"] == "delve_flowchart":
            try:
                parsed = _parse_delve_flowchart(form, entry_id_for_upload, existing_entry, field["subdir"])
            except ValueError as e:
                # Still raised for a genuinely bad image upload (see that function's own
                # docstring) -- there's no partial room/action data to preserve in that case,
                # unlike a blank id/label below.
                fatal_errors.append(str(e))
            else:
                # Always applied, errors or not -- a blank id/label is carried in "errors" (see
                # _parse_delve_flowchart's docstring) rather than raised, specifically so "rooms"
                # here still reflects exactly what was submitted and the canvas re-renders
                # unchanged instead of coming back empty.
                new_entry[field["name"]] = parsed["rooms"]
                if parsed["layout"]:
                    new_entry["layout"] = parsed["layout"]
                new_entry["start_room"] = parsed["start_room"]
                soft_errors.extend(parsed["errors"])
            continue
        if field["type"] == "quest_flowchart":
            # Never raises (see _parse_quest_flowchart's own docstring -- no image uploads means
            # no ValueError path at all here, unlike delve_flowchart above) -- always applied, same
            # "blank id/trigger is carried in errors, not raised" non-raising philosophy.
            parsed = _parse_quest_flowchart(form, existing_entry)
            new_entry[field["name"]] = parsed["stages"]
            if parsed["layout"]:
                new_entry["layout"] = parsed["layout"]
            new_entry["start_stage"] = parsed["start_stage"]
            new_entry["next_ordinal"] = parsed["next_ordinal"]
            soft_errors.extend(parsed["errors"])
            continue
        parsed = _parse_field(field, form)
        if parsed is not None:
            new_entry[parsed[0]] = parsed[1]
    return new_entry, fatal_errors, soft_errors


def _parse_outcome(prefix: str, form: dict) -> dict:
    """An action's on_success/on_fail -- next/hp_delta/message/currency_delta/item give-or-take/
    achievement_kind, each omitted (not written as an empty string / null) if left blank, same
    "blank means absent" convention every other optional field here follows. item_qty's sign is
    what decides give vs. take (see dungeon._ACTION_OUTCOME_KEYS), so it's parsed as-is (a plain
    negative number), not split into separate give/take inputs."""
    outcome: dict = {}
    next_room = form.get(f"{prefix}_next", "").strip()
    if next_room:
        outcome["next"] = next_room
    hp_delta = form.get(f"{prefix}_hp_delta", "").strip()
    if hp_delta:
        outcome["hp_delta"] = int(hp_delta)
    message = form.get(f"{prefix}_message", "").strip()
    if message:
        outcome["message"] = message
    currency_delta = form.get(f"{prefix}_currency_delta", "").strip()
    if currency_delta:
        outcome["currency_delta"] = int(currency_delta)
    item_id = form.get(f"{prefix}_item_id", "").strip()
    if item_id:
        outcome["item_kind"] = form.get(f"{prefix}_item_kind", "").strip()
        outcome["item_id"] = item_id
        qty = form.get(f"{prefix}_item_qty", "").strip()
        if qty:
            outcome["item_qty"] = int(qty)
    achievement_kind = form.get(f"{prefix}_achievement_kind", "").strip()
    if achievement_kind:
        outcome["achievement_kind"] = achievement_kind
    return outcome


def _parse_actions(prefix: str, form: dict) -> tuple[list[dict], list[str]]:
    """Parses a choice room's nested "actions" repeatable -- action index i discovered from
    whichever "<prefix>_<i>_success_next" keys are *present* (every action row always renders this
    hidden input, even one added and left otherwise untouched -- unlike, say, "room_<i>_id" this
    one is fine being blank, since that's the valid "wins the delve" state, so presence rather than
    non-blank is the right signal here), rather than scanning nested per-field indices the way
    _parse_delve_flowchart does for monster rows, since an action's own fields are all fixed-shape
    (one row, one index, no further repeatable underneath it besides its own "requires" trigger,
    which _parse_trigger already handles as a unit).

    Returns (actions, errors) rather than raising straight away when an action still has a blank
    label -- the caller (_parse_delve_flowchart) needs the *rest* of this room's data regardless,
    so the canvas can still be redrawn exactly as submitted if these turn out to be the only
    problems (see that function's own docstring for why silently dropping one instead would be
    worse). Every blank label is reported, not just the first, so edit_view can show one error
    banner per problem in a single pass instead of a "fix one, resubmit, find the next" loop.
    Whether "check" (and therefore "on_fail") ends up in the saved action depends only on whether
    chance, or stat+dc together, were actually filled in (chance wins if both somehow are) -- see
    _render_action_row's docstring for why this isn't toggled by JS instead."""
    indices = sorted(
        int(m.group(1)) for k in form if (m := re.fullmatch(rf"{re.escape(prefix)}_(\d+)_success_next", k))
    )
    actions = []
    errors = []
    for i in indices:
        p = f"{prefix}_{i}"
        label = form.get(f"{p}_label", "").strip()
        if not label:
            errors.append("An action on the canvas needs a label before this can be saved.")
        action: dict = {"label": label}

        requires = _parse_trigger(f"{p}_requires", form)
        if requires is not None:
            action["requires"] = requires

        cost: dict = {}
        currency = form.get(f"{p}_cost_currency", "").strip()
        if currency:
            cost["currency"] = int(currency)
        item_id = form.get(f"{p}_cost_item_id", "").strip()
        if item_id:
            cost["item_kind"] = form.get(f"{p}_cost_item_kind", "").strip()
            cost["item_id"] = item_id
            qty = form.get(f"{p}_cost_item_qty", "").strip()
            if qty:
                cost["item_qty"] = int(qty)
        if cost:
            action["cost"] = cost

        stat = form.get(f"{p}_check_stat", "").strip()
        dc = form.get(f"{p}_check_dc", "").strip()
        chance = form.get(f"{p}_check_chance", "").strip()
        has_check = bool(chance) or bool(stat and dc)
        if chance:
            action["check"] = {"chance": float(chance)}
        elif has_check:
            action["check"] = {"stat": stat, "dc": float(dc) if "." in dc else int(dc)}

        action["on_success"] = _parse_outcome(f"{p}_success", form)
        if has_check:
            action["on_fail"] = _parse_outcome(f"{p}_fail", form)

        x, y = form.get(f"{p}_x", "").strip(), form.get(f"{p}_y", "").strip()
        if x and y:
            action["x"], action["y"] = float(x), float(y)

        actions.append(action)
    return actions, errors


def _parse_delve_flowchart(form: dict, entry_id_for_upload: str, existing_entry: dict, subdir: str) -> dict:
    """Parses a "delve_flowchart" field's submission -- pulled out of _parse_field entirely (unlike
    every other repeatable type) because each room can carry its own uploaded background image,
    which needs real file I/O and the entry's previous value, the same reason top-level "image"
    fields are handled outside _parse_field. Called directly from edit_view's POST handler.

    Room index i is discovered from whichever "room_<i>_x" keys are present -- every room ever
    added to the canvas has a position, even one added and left otherwise untouched, unlike a
    combat room's monster rows (which can legitimately be entirely empty while the row is being
    filled in, so those really are discovered by "non-blank"). Branches on that room's own
    "room_<i>_type" to decide which fields matter: combat parses its groups repeatable (three
    further levels of index -- "room_<i>_groups_<j>_monsters_<k>" -- group index j discovered from
    key *presence* the same way _parse_actions discovers action indices, monster index k within
    each group discovered by "non-blank" the same way a bare monster row already was; a group with
    no non-blank monster left in it is dropped entirely rather than saved as an empty group; a
    surviving group's own "room_<i>_groups_<j>_chance" is kept only if non-blank, same "absent
    means use dungeon.DEFAULT_MONSTER_GROUP_CHANCE" convention as every other optional numeric
    field here), its own "next" (written by the flowchart script's drag-to-connect, not typed -- see
    _render_room_box), and its own optional "prompt" (shown once at room-entry, ahead of the
    monsters' own flavor text -- see dungeon_view._combat_intro_text); choice parses its own
    required prompt plus its own nested actions repeatable (_parse_actions, whose on_success/
    on_fail "next" is the same drag-to-connect story). Also collects each room's
    canvas position ("room_<i>_x"/"_y", also script-written) into a top-level "layout" dict, and
    reads the one page-level "start_room" hidden input (see _render_delve_flowchart) -- all
    returned alongside "rooms" instead of a bare list, since this is now the sole place every piece
    the flowchart canvas owns get assembled.

    A room discovered this way with a still-blank id (or, via _parse_actions, an action with a
    still-blank label) does NOT raise here -- it's included in "rooms" as-is (blank id and all)
    and "errors" collects every such problem found across every room, instead of raising
    immediately on the first one. Building the full room list regardless of any errors is what lets
    edit_view re-render the canvas exactly as submitted (see that function's own handling of this
    "errors" key) rather than the earlier, much worse failure mode: raising immediately would abort
    this whole function before "rooms" is ever built, and edit_view's error-path re-render would
    then have nothing to show but an empty canvas -- "not saving at all" with no indication why,
    exactly the bug this replaced.

    Background image: a new upload at "room_<i>_background_path_file" is saved (via
    _save_uploaded_image, named "<entry_id>_room_<room id><ext>" -- keyed by the room's own id, not
    its position, so reordering rooms across saves can't misattribute one room's image to
    another). With no new upload, whichever existing room shares this same id keeps its old
    background_path, if it had one -- same "re-uploading isn't required on every edit" rule as any
    other image field. Raises ValueError (same as _save_uploaded_image) on a bad upload -- unlike a
    blank id/label there's no sensible partial state to preserve for a failed upload, so this one
    case still aborts the whole save, same as before."""
    room_indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(r"room_(\d+)_x", k)))
    existing_by_id = {r["id"]: r for r in existing_entry.get("rooms", []) if r.get("id")}

    rooms = []
    layout: dict = {}
    errors = []
    for i in room_indices:
        p = f"room_{i}"
        room_id = form.get(f"{p}_id", "").strip()
        if not room_id:
            errors.append(f"Room #{i + 1} on the canvas needs an id before this can be saved.")
        room_type = form.get(f"{p}_type", "combat").strip()
        room: dict = {"id": room_id, "type": room_type}

        if room_type == "combat":
            group_indices = sorted({
                int(m.group(1)) for k in form if (m := re.fullmatch(rf"{p}_groups_(\d+)_monsters_\d+", k))
            })
            groups = []
            for j in group_indices:
                gp = f"{p}_groups_{j}"
                monster_indices = sorted(
                    int(m.group(1)) for k in form
                    if (m := re.fullmatch(rf"{gp}_monsters_(\d+)", k)) and form[k].strip()
                )
                monsters = [form[f"{gp}_monsters_{k}"].strip() for k in monster_indices]
                if monsters:
                    group: dict = {"monsters": monsters}
                    raw_chance = form.get(f"{gp}_chance", "").strip()
                    if raw_chance:
                        group["chance"] = float(raw_chance) if "." in raw_chance else int(raw_chance)
                    # A checkbox is simply absent when unchecked (see _parse_field's own "bool"
                    # case) -- forked gates next/x/y below, not just this stored flag, so
                    # unchecking Fork always drops any leftover override rather than just hiding
                    # it (the client-side clear in wireGroupForkCheckbox is a belt-and-suspenders
                    # UX nicety, not the only thing preventing a stale override).
                    forked = f"{gp}_fork" in form
                    group["fork"] = forked
                    if forked:
                        group_next = form.get(f"{gp}_next", "").strip()
                        if group_next:
                            group["next"] = group_next
                        gx, gy = form.get(f"{gp}_x", "").strip(), form.get(f"{gp}_y", "").strip()
                        if gx and gy:
                            group["x"], group["y"] = float(gx), float(gy)
                    groups.append(group)
            if groups:
                room["monster_groups"] = groups
            next_room = form.get(f"{p}_next", "").strip()
            if next_room:
                room["next"] = next_room
            prompt = form.get(f"{p}_prompt", "").strip()
            if prompt:
                room["prompt"] = prompt
        else:
            prompt = form.get(f"{p}_prompt", "").strip()
            if prompt:
                room["prompt"] = prompt
            actions, action_errors = _parse_actions(f"{p}_actions", form)
            errors.extend(action_errors)
            if actions:
                room["actions"] = actions

        new_path = _save_uploaded_image(
            form.get(f"{p}_background_path_file"), subdir, f"{entry_id_for_upload}_room_{room_id}"
        )
        if new_path is not None:
            room["background_path"] = new_path
        elif room_id in existing_by_id and existing_by_id[room_id].get("background_path"):
            room["background_path"] = existing_by_id[room_id]["background_path"]

        rooms.append(room)

        x, y = form.get(f"{p}_x", "").strip(), form.get(f"{p}_y", "").strip()
        if room_id and x and y:
            layout[room_id] = {"x": float(x), "y": float(y)}

    return {"rooms": rooms, "layout": layout, "start_room": form.get("start_room", "").strip(), "errors": errors}


def _parse_paths(prefix: str, form: dict) -> tuple[list[dict], list[str]]:
    """Parses a stage's nested "paths" repeatable -- path index i discovered from whichever
    "<prefix>_<i>_next" keys are *present* (every path row always renders this hidden input, even
    one added and left otherwise untouched -- blank is the valid "ends the quest" state, same
    reasoning _parse_actions uses for a delve action's own success_next), rather than something
    like "non-blank trigger type", since an empty trigger is itself a real state worth reporting,
    not a sign the row was never really added.

    Returns (paths, errors) rather than raising immediately when a path still has no trigger --
    the caller (_parse_quest_flowchart) needs the *rest* of this stage's data regardless, so the
    canvas can still be redrawn exactly as submitted (see _parse_delve_flowchart's own docstring
    for why silently dropping one instead would be worse). Only a missing trigger gets its own
    explicit message here, same "the one truly load-bearing field gets checked immediately, every
    other blank-but-required field is caught by the next check_quest_problems pass instead" split
    _parse_delve_flowchart/_parse_actions already use for a room's id / an action's label -- a
    blank on_complete_message is simply omitted (not written as ""), which is exactly what makes
    quests._REQUIRED_PATH_FIELDS' own "missing field(s)" check able to catch it later."""
    indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(rf"{re.escape(prefix)}_(\d+)_next", k)))
    paths = []
    errors = []
    for i in indices:
        p = f"{prefix}_{i}"
        trigger = _parse_trigger(f"{p}_trigger", form)
        if trigger is None:
            errors.append("A path on the canvas needs a trigger before this can be saved.")
        path: dict = {}
        if trigger is not None:
            path["trigger"] = trigger

        message = form.get(f"{p}_message", "").strip()
        if message:
            path["on_complete_message"] = message

        reward = form.get(f"{p}_reward", "").strip()
        if reward and int(reward) != 0:
            path["reward"] = int(reward)
        reward_item = form.get(f"{p}_reward_item", "").strip()
        if reward_item:
            path["reward_item"] = reward_item
            reward_item_kind = form.get(f"{p}_reward_item_kind", "").strip()
            if reward_item_kind and reward_item_kind != "equipment":
                path["reward_item_kind"] = reward_item_kind
        turn_in_label = form.get(f"{p}_turn_in_label", "").strip()
        if turn_in_label:
            path["turn_in_label"] = turn_in_label

        next_stage = form.get(f"{p}_next", "").strip()
        if next_stage:
            path["next"] = next_stage

        x, y = form.get(f"{p}_x", "").strip(), form.get(f"{p}_y", "").strip()
        if x and y:
            path["x"], path["y"] = float(x), float(y)

        paths.append(path)
    return paths, errors


def _parse_quest_flowchart(form: dict, existing_entry: dict) -> dict:
    """Parses a "quest_flowchart" field's submission -- pulled out of _parse_field entirely (like
    "delve_flowchart"), called directly from edit_view's POST handler and the quest autosave/
    publish routes. Meaningfully simpler than _parse_delve_flowchart: quest stages carry no image
    uploads at all, so this never needs an entry-id-for-upload or subdir, and can never raise.

    Stage index i is discovered from whichever "stage_<i>_x" keys are present -- every stage ever
    added to the canvas has a position, even one added and left otherwise untouched, same reasoning
    _parse_delve_flowchart uses for room index discovery. Each stage's own "paths" nested
    repeatable is parsed by _parse_paths (path index discovered the same way, from "_next" key
    presence -- see that function's own docstring).

    Ordinal assignment is the one genuinely new concern here, with no delve precedent (a delve room
    has no equivalent second identity): each stage's own hidden "stage_<i>_ordinal" input is
    non-blank if it already had one assigned (an existing stage resubmitting itself); blank means
    this stage was added to the canvas this session and has never been saved before, so it gets the
    next value off a running counter seeded from `existing_entry`'s own "next_ordinal" (0 for a
    brand-new quest). The freshly-assigned ordinal is written into the returned stage dict, which
    is what makes it back into the *next* render automatically -- edit_view always re-renders from
    this function's own `new_entry`, never the raw submitted form, so the newly-minted ordinal is
    what a resubmission (e.g. the very next autosave tick) sees as "already assigned," closing the
    loop without any separate wiring. This is the correctness-critical property the whole "assigned
    once, never reused" guarantee quests.py's progress-flag encoding depends on: if a fresh
    ordinal's value were ever silently dropped on re-render, autosaving the same still-new stage
    twice before a page reload would assign it a *different* ordinal each tick.

    Also collects each stage's canvas position ("stage_<i>_x"/"_y", also script-written) into a
    top-level "layout" dict, and reads the one page-level "start_stage" hidden input (see
    _render_quest_flowchart). A stage discovered this way with a still-blank id (or, via
    _parse_paths, a path with no trigger) does NOT raise -- it's included in "stages" as-is and
    "errors" collects every such problem found, exactly the same non-raising philosophy
    _parse_delve_flowchart documents at length.

    "discuss_with" (a third nested repeatable, sibling to "paths") has no natural always-present
    companion field the way a path's hidden "_next" input gives it one -- a bare <select> submits
    whatever's currently chosen and nothing else -- so its rows are discovered by *non-blank value*
    presence instead, the same convention _parse_delve_flowchart already uses for a combat room's
    monster list. A row left on the blank "—" option is simply dropped, not stored as "" -- there's
    nothing to "fix up" about an unset row the way a blank id/trigger is worth flagging as an error."""
    stage_indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(r"stage_(\d+)_x", k)))
    next_ordinal = existing_entry.get("next_ordinal", 0)

    stages = []
    layout: dict = {}
    errors = []
    for i in stage_indices:
        p = f"stage_{i}"
        stage_id = form.get(f"{p}_id", "").strip()
        if not stage_id:
            errors.append(f"Stage #{i + 1} on the canvas needs an id before this can be saved.")
        stage: dict = {"id": stage_id}

        raw_ordinal = form.get(f"{p}_ordinal", "").strip()
        if raw_ordinal:
            stage["ordinal"] = int(raw_ordinal)
        else:
            stage["ordinal"] = next_ordinal
            next_ordinal += 1

        prompt = form.get(f"{p}_prompt", "").strip()
        if prompt:
            stage["prompt"] = prompt
        journal_text = form.get(f"{p}_journal_text", "").strip()
        if journal_text:
            stage["journal_text"] = journal_text
        topic_label = form.get(f"{p}_topic_label", "").strip()
        if topic_label:
            stage["topic_label"] = topic_label

        discuss_indices = sorted(
            int(m.group(1)) for k in form
            if (m := re.fullmatch(rf"{re.escape(p)}_discuss_with_(\d+)", k)) and form[k].strip()
        )
        stage["discuss_with"] = [form[f"{p}_discuss_with_{j}"].strip() for j in discuss_indices]

        paths, path_errors = _parse_paths(f"{p}_paths", form)
        errors.extend(path_errors)
        stage["paths"] = paths

        stages.append(stage)

        x, y = form.get(f"{p}_x", "").strip(), form.get(f"{p}_y", "").strip()
        if stage_id and x and y:
            layout[stage_id] = {"x": float(x), "y": float(y)}

    return {
        "stages": stages, "layout": layout, "start_stage": form.get("start_stage", "").strip(),
        "next_ordinal": next_ordinal, "errors": errors,
    }


def _list_asset_files() -> list[tuple[str, float]]:
    """Every file under assets/, as (path-relative-to-repo-root, size-in-KB) pairs sorted by path
    -- e.g. ("assets/dungeon/monsters/goblin_grunt.png", 12.4). Backs the standalone Assets page's
    browse table; unrelated to any single content entry's image field."""
    out = []
    for root, _dirs, files in os.walk(ASSETS_DIR):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, os.path.dirname(__file__)).replace(os.sep, "/")
            out.append((rel, os.path.getsize(full) / 1024))
    out.sort()
    return out


def _save_arbitrary_asset(file_field, subdir: str) -> str:
    """Saves an uploaded image to assets/<subdir>/<original filename>, for the standalone Assets
    page -- unlike _save_uploaded_image (which names the file after one specific content entry's
    id), this is for images not attached to any entry, e.g. prepping a file before pasting its
    path into a field by hand. Same extension/size rules as _save_uploaded_image. `subdir` is
    free-typed admin input, so any ".." component is rejected outright rather than trusting
    os.path.join to keep the result under ASSETS_DIR; the original filename is reduced to its
    basename for the same reason. Raises ValueError (shown to the user, nothing written) on any of
    that, a disallowed extension, or an oversized file."""
    if not isinstance(file_field, web.FileField) or not file_field.filename:
        raise ValueError("No file selected")
    ext = os.path.splitext(file_field.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported image type {ext!r} -- use one of {sorted(ALLOWED_IMAGE_EXTENSIONS)}")
    data = file_field.file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large (max {MAX_IMAGE_BYTES // (1024 * 1024)}MB)")

    subdir_parts = [p for p in subdir.strip().split("/") if p not in ("", ".")]
    if any(p == ".." for p in subdir_parts):
        raise ValueError("Invalid subdirectory")
    dest_dir = os.path.join(ASSETS_DIR, *subdir_parts)
    os.makedirs(dest_dir, exist_ok=True)

    filename = os.path.basename(file_field.filename)
    dest_path = os.path.join(dest_dir, filename)
    with open(dest_path, "wb") as f:
        f.write(data)
    return os.path.relpath(dest_path, os.path.dirname(__file__)).replace(os.sep, "/")


def _list_db_backups() -> list[tuple[str, float, str]]:
    """Every casino.db snapshot under backups/, as (filename, size-in-KB, saved-at) triples sorted
    newest first -- backs the Utilities page's browse table. Separate from _list_asset_files since
    these aren't images and don't live under assets/."""
    if not os.path.isdir(BACKUPS_DIR):
        return []
    out = []
    for fname in os.listdir(BACKUPS_DIR):
        full = os.path.join(BACKUPS_DIR, fname)
        if not os.path.isfile(full):
            continue
        saved_at = datetime.datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M:%S")
        out.append((fname, os.path.getsize(full) / 1024, saved_at))
    out.sort(reverse=True)  # filename embeds a sortable timestamp -- newest first with no extra key
    return out


def _create_db_backup() -> str:
    """Snapshots the live casino.db into backups/<timestamp>.db via sqlite3's own online backup
    API (Connection.backup) -- safe to run while the bot has the database open and mid-transaction,
    unlike a plain file copy, which can grab a torn/corrupt snapshot. Synchronous (the sqlite3
    module has no async API) -- see utilities_view for why this is always awaited via
    asyncio.to_thread rather than called directly from a route handler. Returns the new
    filename."""
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    filename = f"casino_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    source = sqlite3.connect(db.DB_PATH)
    try:
        dest = sqlite3.connect(os.path.join(BACKUPS_DIR, filename))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return filename


# --- Routes --------------------------------------------------------------------------------

async def login(request: web.Request) -> web.Response:
    error = ""
    if request.method == "POST":
        form = await request.post()
        if ADMIN_PANEL_PASSWORD and form.get("password") == ADMIN_PANEL_PASSWORD:
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE_NAME, _session_cookie_value(), httponly=True, samesite="Strict")
            raise resp
        error = '<p class="error">Wrong password.</p>'
    body = f"""
    {error}
    <form method="post">
        <label>Password<input type="password" name="password" autofocus></label>
        <button type="submit">Log in</button>
    </form>
    """
    return _html_response(_page("Log in", body, nav=False))


async def dashboard(request: web.Request) -> web.Response:
    sections = []
    for category in CATEGORIES:
        cards = "".join(
            f'<a class="content-card" href="/edit/{key}">'
            f'<span class="content-card-icon">{spec["icon"]}</span>'
            f'<span class="content-card-label">{html.escape(spec["label"])}</span>'
            f'<span class="content-card-count">{len(getattr(spec["module"], spec["registry_attr"]))}</span>'
            f'</a>'
            for key, spec in CONTENT_TYPES.items() if spec["category"] == category
        )
        sections.append(f'<h2>{html.escape(category)}</h2><div class="content-grid">{cards}</div>')
    return _html_response(_page("Content Editor", "".join(sections)))


# Effect types whose value reads as a reduction/loss (shown with a "-" in the list view's compact
# summary) rather than a gain ("+") -- everything else defaults to "+". def_shred/dot/lower_threat
# are debuffs even though their names don't end in "_debuff".
_DEBUFF_EFFECT_TYPES = {
    "atk_debuff", "spatk_debuff", "spdef_debuff", "speed_debuff", "def_shred", "lower_threat", "dot",
}
# Shown with a "x" prefix instead of a +/- sign -- these scale an existing number rather than
# adding/subtracting from one.
_MULTIPLIER_EFFECT_TYPES = {"damage_multiplier", "extra_attack", "execute_multiplier"}
# No meaningful value to show at all -- cleanse_* and stun/sap are pure duration/boolean effects.
_NO_VALUE_EFFECT_TYPES = {"stun", "sap", "cleanse_dot", "cleanse_cc"}


def format_effect(effect: dict) -> str:
    """One compact fragment for the list view's stats-summary column, e.g. "ATK +3" or "Heal +40%"
    -- not the full picture (skips trigger/chance/duration), just enough to tell at a glance what
    an item/skill/consumable roughly does without opening it. Reuses dungeon.EFFECT_PARAM_SCHEMAS'
    own fraction-param classification (the same one the edit form's validator uses) to decide
    percent vs. raw-number display, so this can never drift from what the real loader considers a
    fraction. Public (no leading underscore) since bot.py's !skills command reuses this exact
    formatting for a player-facing skill list too -- one place this vocabulary lives."""
    etype = effect.get("type", "?")
    label = EFFECT_SHORT_LABELS.get(etype, etype)
    if etype in _NO_VALUE_EFFECT_TYPES:
        return label
    schema = dungeon.EFFECT_PARAM_SCHEMAS.get(etype)
    if schema is None:
        return label
    _required, _optional, fraction_params = schema
    # "base" last: execute_multiplier is the only type with two required params (base and scale)
    # instead of the usual "at most one" -- this shows just base (e.g. "Execute x1.3") since a
    # compact fragment has no room for the scale/missing-HP formula too.
    param = next((p for p in ("value", "reduction", "multiplier", "base") if p in effect), None)
    if param is None:
        return label
    raw = effect[param]
    if etype in _MULTIPLIER_EFFECT_TYPES:
        return f"{label} ×{raw:g}"
    sign = "-" if etype in _DEBUFF_EFFECT_TYPES else "+"
    if param in fraction_params:
        return f"{label} {sign}{round(raw * 100)}%"
    return f"{label} {sign}{raw:g}"


def _list_cell_text(field: dict | None, value) -> str:
    """Plain text for one list-view cell. Most fields just stringify; the three effects-shaped
    field types (equipment's "equipment_effects", skills/consumables' "effects", consumables'
    alternative "effect_groups") get summarized via format_effect instead of showing their raw
    [{'type': ..., ...}] repr, which is what a bare str(value) produced before this existed."""
    if field is None:
        return "" if value is None else str(value)
    ftype = field["type"]
    if ftype in ("effects", "equipment_effects"):
        return ", ".join(format_effect(e) for e in (value or []))
    if ftype == "effect_groups":
        return ", ".join(format_effect(e) for group in (value or []) for e in group.get("effects", []))
    return "" if value is None else str(value)


async def list_view(request: web.Request) -> web.Response:
    content_type = request.match_info["content_type"]
    spec = CONTENT_TYPES.get(content_type)
    if spec is None:
        raise web.HTTPNotFound()

    columns = spec["list_columns"]
    # Looked up per column so _list_cell_text knows an effects-shaped column (equipment_effects/
    # effects/effect_groups) needs summarizing rather than a bare str() of its raw list-of-dicts --
    # None for a column name that isn't a real field (there aren't any today, but "" is a saner
    # fallback than a KeyError if list_columns and fields ever drift).
    field_by_name = {f["name"]: f for f in spec["fields"]}
    draft_publish = spec.get("draft_publish")
    # A draft/publish content type (delves, quests) gets one extra column beyond its own: whether
    # there's a draft with unpublished changes, or (for an entry never published at all) whether
    # the row IS only a draft -- see the "draft_publish" spec entry. No other content type has this
    # two-tier draft/publish split.
    header = "".join(f"<th>{html.escape(c)}</th>" for c in columns) + ("<th></th>" if draft_publish else "")
    drafts = draft_publish["load_drafts"]() if draft_publish else {}
    dup_link = lambda item_id: (
        f'<td><a class="row-link" data-tooltip="Duplicate -- opens a new entry pre-filled from this '
        f'one" href="/edit/{content_type}/new?duplicate_from={item_id}">📋</a></td>'
    )
    rows = []
    for item_id, entry in getattr(spec["module"], spec["registry_attr"]).items():
        cells = "".join(
            f"<td>{html.escape(_list_cell_text(field_by_name.get(c), entry.get(c)))}</td>" for c in columns
        )
        if draft_publish:
            has_unpublished = item_id in drafts and drafts[item_id] != entry
            status = (
                '<span class="draft-tag" data-tooltip="Has unpublished draft changes.">Draft</span>'
                if has_unpublished else ""
            )
            cells += f"<td>{status}</td>"
        rows.append(
            f'<tr><td><a class="row-link" href="/edit/{content_type}/{item_id}">✏️</a></td>'
            f'{dup_link(item_id)}{cells}</tr>'
        )
    if draft_publish:
        live_ids = set(getattr(spec["module"], spec["registry_attr"]).keys())
        for draft_id, draft_entry in drafts.items():
            if draft_id in live_ids:
                continue
            cells = "".join(f"<td>{html.escape(str(draft_entry.get(c, '')))}</td>" for c in columns)
            cells += '<td><span class="draft-tag" data-tooltip="Never published -- only exists as a draft.">Draft</span></td>'
            rows.append(
                f'<tr><td><a class="row-link" href="/edit/{content_type}/{draft_id}">✏️</a></td>'
                f'{dup_link(draft_id)}{cells}</tr>'
            )

    singular = spec["label"][:-1] if spec["label"].endswith("s") else spec["label"]
    # Every non-delve content type's save redirects here (see edit_view's "?saved=1") -- a delve
    # instead stays on its own edit page after saving, so this never fires for delves in practice,
    # but reusing the same query-param convention here too costs nothing and keeps the two save
    # flows consistent if that ever changes.
    saved_notice = '<p class="success">Saved.</p>' if request.query.get("saved") else ""
    body = (
        f'<h1>{spec["icon"]} {html.escape(spec["label"])}</h1>'
        f'{saved_notice}'
        f'<p><a class="row-link" href="/edit/{content_type}/new">+ New {html.escape(singular)}</a></p>'
        f'<input id="list-filter" type="text" placeholder="Filter {html.escape(spec["label"].lower())}...">'
        f'<table id="list-table"><thead><tr><th></th><th></th>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )
    breadcrumbs = [("Home", "/"), (spec["label"], None)]
    return _html_response(_page(spec["label"], body, active=content_type, breadcrumbs=breadcrumbs))


def _apply_generate_level(content_type: str, entry: dict, query) -> dict:
    """If the monster/equipment edit page's "Generate stats for level" sub-form was just
    submitted (?generate_level=N, for monsters also ?archetype=tank/balanced/glass_cannon, and for
    equipment also ?slot=... and ?rarity=...), returns a NEW dict with hp/atk/def (monsters) or a
    regenerated set of constant effects (equipment, scaled by rarity -- see
    dungeon.RARITY_STAT_MULTIPLIERS) plus intended_level overridden by
    dungeon.generate_monster_stats/generate_item_constant_effects. Equipment's own on_use/on_hit
    effects (if any were hand-authored) are preserved as-is -- only the constant ones get replaced
    -- so re-rolling an item's passive stats for a new level can't silently wipe out its "ring of
    fireball"-style effects. Never mutates `entry` in place -- it may be the LIVE registry
    entry itself when editing something that already exists -- and never saves anything; the
    admin still has to click the normal Save button. A no-op (returns `entry` unchanged) for any
    other content type or a missing/invalid generate_level, including on an ordinary POST/Save
    request whose URL happens to still carry a stale ?generate_level= from an earlier Generate
    click -- harmless either way, since edit_view's POST path parses fresh from the submitted
    form and never reads this return value for that branch."""
    if content_type not in ("monsters", "equipment"):
        return entry
    raw_level = query.get("generate_level", "").strip()
    if not raw_level.isdigit() or int(raw_level) < 1:
        return entry
    level = int(raw_level)
    entry = dict(entry)
    entry["intended_level"] = level
    if content_type == "monsters":
        # Absent/unrecognized archetype (e.g. the plain "Random" button) falls through to None,
        # which generate_monster_stats treats as "roll one" -- see dungeon.MONSTER_ARCHETYPES for
        # the named presets the Tank/Balanced/Glass Cannon buttons pass here.
        archetype = dungeon.MONSTER_ARCHETYPES.get(query.get("archetype", ""))
        entry.update(dungeon.generate_monster_stats(level, archetype=archetype))
    else:
        slot = query.get("slot") or entry.get("slot") or "weapon"
        rarity = query.get("rarity") or entry.get("rarity") or "common"
        entry["slot"] = slot
        entry["rarity"] = rarity
        non_constant = [e for e in entry.get("effects", []) if e.get("trigger") != "constant"]
        entry["effects"] = dungeon.generate_item_constant_effects(level, slot, rarity) + non_constant
    return entry


def _resolve_duplicate_source(spec: dict, source_id: str) -> dict | None:
    """Looks up `source_id` the same way edit_view resolves an existing item to display for editing
    (a draft/publish entry's own unpublished draft wins over its published version, otherwise the
    live registry) -- so Duplicate always clones whatever the admin is currently looking at, not a
    stale published copy behind an open draft. None if `source_id` doesn't exist (a stale link)."""
    draft_publish = spec.get("draft_publish")
    draft_entry = draft_publish["load_drafts"]().get(source_id) if draft_publish else None
    if draft_entry is not None:
        return draft_entry
    registry = getattr(spec["module"], spec["registry_attr"])
    return registry.get(source_id)


def _duplicate_entry(spec: dict, source_id: str) -> dict | None:
    """A deep copy of `source_id`'s entry with a fresh, not-yet-taken id (`<source_id>_copy`, then
    `_copy2`, `_copy3`, ... on collision against every id currently on disk) -- everything else
    left exactly as the source had it, since the whole point of Duplicate is "start from a working
    template" rather than a blank form. Never writes anything itself -- this only pre-fills
    edit_view's "new" form (same "GET query param reshapes the blank/existing entry, nothing is
    saved until the real Save button" convention _apply_generate_level already uses); the admin
    still reviews it (renaming it, most importantly) and clicks Save like any other new entry, so a
    duplicate that's never renamed just fails loudly at Save time with the same "duplicate id"
    error every hand-typed collision already gets -- no new validation needed here. Returns None if
    `source_id` doesn't exist."""
    source = _resolve_duplicate_source(spec, source_id)
    if source is None:
        return None
    existing_ids = {e.get("id") for e in _load_raw_entries(spec)}
    new_id = f"{source_id}_copy"
    suffix = 2
    while new_id in existing_ids:
        new_id = f"{source_id}_copy{suffix}"
        suffix += 1
    duplicated = copy.deepcopy(source)
    duplicated["id"] = new_id
    return duplicated


async def edit_view(request: web.Request) -> web.Response:
    content_type = request.match_info["content_type"]
    item_id = request.match_info["item_id"]
    spec = CONTENT_TYPES.get(content_type)
    if spec is None:
        raise web.HTTPNotFound()

    is_new = item_id == "new"
    draft_publish = spec.get("draft_publish")
    registry = getattr(spec["module"], spec["registry_attr"])
    # A draft/publish entry prefers its own draft over the live published version, if one exists --
    # that's the entire point of the draft/publish split (see the "draft_publish" spec entry):
    # reopening one that's mid-edit shows exactly the unpublished state it was left in, not what's
    # actually live. A brand-new entry never has a draft under "new" itself (see
    # flowchart_autosave_view -- drafts are always keyed by the entry's own real id, assigned
    # client-side once typed).
    draft_entry = draft_publish["load_drafts"]().get(item_id) if draft_publish else None
    entry = draft_entry if draft_entry is not None else ({} if is_new else registry.get(item_id))
    if entry is None and not is_new:
        raise web.HTTPNotFound()
    # "+ New X" on the list page links straight here; "Duplicate" (list_view's row action, and the
    # button next to Delete below) instead links to /edit/<type>/new?duplicate_from=<id> -- a dead
    # link (source since deleted) just falls through to the ordinary blank-new-entry form.
    if is_new and not entry and request.query.get("duplicate_from"):
        duplicated = _duplicate_entry(spec, request.query["duplicate_from"])
        if duplicated is not None:
            entry = duplicated
    entry = _apply_generate_level(content_type, entry, request.query)

    error = ""
    if request.method == "POST":
        form = dict(await request.post())
        # "id" specifically needs to be known before the image-upload fields are handled (it's
        # the filename an upload gets saved under) -- always present since every schema lists it
        # first and required, but read directly here rather than relying on loop order.
        entry_id_for_upload = form.get("id", "").strip() or item_id
        new_entry, fatal_errors, soft_errors = _build_entry_from_form(spec, form, entry_id_for_upload, entry)
        upload_errors = fatal_errors + soft_errors

        if upload_errors:
            # One banner per problem, same .error styling as every other error here -- so fixing a
            # delve with several still-unnamed rooms/actions doesn't mean "save, get told about
            # just the first one, fix it, save again, get told about the next."
            error = "".join(f'<p class="error">{html.escape(e)}</p>' for e in upload_errors)
            entry = new_entry
        else:
            entries = _load_raw_entries(spec)
            original_id = None if is_new else item_id
            entry_index = None
            for i, e in enumerate(entries):
                if e.get("id") == original_id:
                    entries[i] = new_entry
                    entry_index = i
                    break
            if entry_index is None:
                entries.append(new_entry)
                entry_index = len(entries) - 1

            # A draft/publish content type's flowchart editor no longer submits to this generic
            # route in normal use -- it autosaves to a draft and only ever commits through the
            # dedicated publish route (flowchart_publish_view), which is the one place "get rid of
            # saving broken entries entirely" is actually enforced. This path stays exactly as it
            # always was for every other content type, and as a harmless fallback if anything still
            # posts here directly.
            redirect_url = (
                f"/edit/{content_type}/{new_entry.get('id', item_id)}" if draft_publish
                else f"/edit/{content_type}"
            )

            error_msg = _write_and_validate(spec, entries)
            if error_msg is None:
                raise web.HTTPFound(f"{redirect_url}?saved=1")
            error = f'<p class="error">{html.escape(error_msg)}</p>'
            entry = new_entry  # show what they submitted, not the stale pre-edit values

    if request.method == "GET" and request.query.get("published"):
        error = '<p class="success">Published.</p>'
    elif request.method == "GET" and request.query.get("publish_error"):
        error = (
            '<p class="error">Publish failed, see the highlighted spots below for '
            "what's still wrong. Nothing changed; your draft is untouched.</p>"
        )
    elif request.method == "GET" and request.query.get("saved"):
        error = '<p class="success">Saved.</p>'

    # A draft/publish entry's own structured problem list -- always computed for display (not just
    # on a failed Publish), so reopening a still-broken draft shows its red highlights immediately
    # rather than waiting for the next autosave tick. Skipped for a genuinely blank new-entry page
    # (entry == {} is falsy) -- there's nothing to report yet and every required-field message
    # would just be noise before the author's typed anything.
    problems: list[dict] = []
    if draft_publish and entry:
        other_ids = set(getattr(spec["module"], spec["registry_attr"]).keys()) - {item_id, entry.get("id", "")}
        problems = draft_publish["check_problems"](entry, other_ids)

    fields_html = _render_fields(spec["fields"], entry, problems)
    delete_button = (
        f'<button type="submit" form="delete-form" class="danger">Delete</button>' if not is_new else ""
    )
    # Same /edit/<type>/new?duplicate_from=<id> link list_view's per-row 📋 icon uses -- offered
    # here too since an admin already reviewing one item is a natural moment to clone it, without
    # backtracking to the list first.
    duplicate_link = (
        f'<a class="row-link" href="/edit/{content_type}/new?duplicate_from={item_id}">📋 Duplicate</a>'
        if not is_new else ""
    )
    crumb_label = "New" if is_new else f"Edit: {item_id}"
    # A flowchart-field content type's canvas needs real width to be usable, unlike every other
    # content type's form -- see .delve-canvas-wrap in _PAGE_CSS -- so this is the one place a
    # schema field type needs to reach the <form> tag itself rather than just its own rendered
    # markup.
    flowchart_field_type = next((f["type"] for f in spec["fields"] if f["type"] in FLOWCHART_FORM_CLASSES), None)
    form_class = f" {FLOWCHART_FORM_CLASSES[flowchart_field_type]}" if flowchart_field_type else ""

    # "Generate stats for level" + the reverse "≈ balanced for level X" hint -- see
    # _apply_generate_level's docstring for how the GET sub-form below feeds back into this same
    # page pre-filled, never auto-saved. Plain GET form reload, same "server computes, no AJAX"
    # convention every other admin-panel interaction already follows.
    level_tools_html = ""
    if content_type in ("monsters", "equipment"):
        estimate = None
        if content_type == "monsters" and all(k in entry for k in ("hp", "atk", "def")):
            estimate = dungeon.estimate_monster_level(entry)
        elif content_type == "equipment" and dungeon.constant_stat_bonuses(entry):
            estimate = dungeon.estimate_item_level(entry)
        estimate_html = (
            f'<p class="field-hint">≈ balanced for level <strong>{estimate:.1f}</strong> (based on current stats)</p>'
            if estimate is not None else ""
        )
        slot_input = ""
        if content_type == "equipment":
            slot_options = "".join(
                f'<option value="{s}"{" selected" if s == entry.get("slot") else ""}>{s}</option>'
                for s in dungeon.EQUIPMENT_SLOTS
            )
            rarity_options = "".join(
                f'<option value="{r}"{" selected" if r == entry.get("rarity") else ""}>'
                f'{dungeon.RARITY_EMOJI[r]} {r}</option>'
                for r in dungeon.EQUIPMENT_RARITIES
            )
            slot_input = (
                f'<label>slot<select name="slot">{slot_options}</select></label>'
                f'<label>rarity<select name="rarity">{rarity_options}</select></label>'
            )
        default_level = entry.get("intended_level") or 1
        # Each archetype button submits the SAME form with its own name="archetype" value -- plain
        # HTML multi-submit-button behavior (whichever button was actually clicked is the only one
        # whose name/value pair is included), no JS needed. Monsters only -- equipment's "trade-off"
        # is already expressed by slot (weapon/armor/trinket), it has no archetype concept.
        generate_buttons = (
            f'<button type="submit" name="archetype" value="tank">🛡️ Tank</button>'
            f'<button type="submit" name="archetype" value="balanced">⚖️ Balanced</button>'
            f'<button type="submit" name="archetype" value="glass_cannon">🗡️ Glass Cannon</button>'
            f'<button type="submit">🎲 Random</button>'
            if content_type == "monsters" else '<button type="submit">Generate</button>'
        )
        level_tools_html = (
            f'<div>{estimate_html}'
            f'<form method="get" class="row-group">'
            f'<label>Generate stats for level<input type="number" name="generate_level" min="1" value="{default_level}"></label>'
            f'{slot_input}{generate_buttons}</form></div>'
        )

    # A draft/publish entry has no "Save" button at all -- edits autosave to a draft in the
    # background (see flowchart_autosave_view + the flowchart script's scheduleAutosave), so the
    # only explicit action left is Publish (flowchart_publish_view), which is also the one place a
    # broken entry can ever be rejected outright instead of silently accepted.
    # `data-flowchart-item-id` is how the autosave/publish JS knows what id to target -- updated
    # client-side (no reload) the first time a brand-new entry's draft gets assigned a real id.
    if draft_publish:
        draft_notice = (
            '<p class="field-hint">📝 Draft, autosaving as you edit. Nothing here is live '
            "until you hit Publish.</p>" if draft_entry is not None else ""
        )
        form_html = (
            f'<form method="post" enctype="multipart/form-data" class="{form_class.strip()}" '
            f'data-flowchart-item-id="{html.escape(item_id)}">{fields_html}'
            f'<div class="row-group"><span id="draft-save-status" class="field-hint"></span>'
            f'<button type="submit" formaction="/edit/{content_type}/{html.escape(item_id)}/publish">Publish</button>'
            f'</div></form>'
        )
    else:
        draft_notice = ""
        form_html = (
            f'<form method="post" enctype="multipart/form-data" class="{form_class.strip()}">'
            f'{fields_html}<button type="submit">Save</button></form>'
        )

    body = f"""
    <h1>{spec["icon"]} {"New" if is_new else "Edit"} {html.escape(spec["label"])}</h1>
    {error}
    {draft_notice}
    {level_tools_html}
    {form_html}
    {f'<form id="delete-form" method="post" action="/delete/{content_type}/{item_id}"></form>' if not is_new else ""}
    {duplicate_link}
    {delete_button}
    """
    breadcrumbs = [("Home", "/"), (spec["label"], f"/edit/{content_type}"), (crumb_label, None)]
    return _html_response(_page(f"Edit {spec['label']}", body, active=content_type, breadcrumbs=breadcrumbs))


async def flowchart_autosave_view(request: web.Request) -> web.Response:
    """AJAX draft-autosave for a draft/publish content type's flowchart editor (delves, quests --
    see the "draft_publish" spec entry, and the matching content_type in the URL) -- never blocked
    by content problems, a draft can be arbitrarily broken, that's the entire point (see e.g.
    quests.save_quest_draft). The one exception is a genuinely bad image upload (delves only --
    quest stages carry none), which has no sensible partial state worth persisting, same as
    everywhere else this codebase handles uploads. Always responds with the freshly-saved entry's
    structured problem list so the canvas can update its red highlights without waiting for an
    explicit Publish attempt."""
    content_type = request.match_info["content_type"]
    item_id = request.match_info["item_id"]
    spec = CONTENT_TYPES[content_type]
    draft_publish = spec["draft_publish"]
    registry = getattr(spec["module"], spec["registry_attr"])
    drafts = draft_publish["load_drafts"]()
    existing = drafts.get(item_id) or registry.get(item_id, {})
    form = dict(await request.post())
    entry_id_for_upload = form.get("id", "").strip() or item_id
    new_entry, fatal_errors, _soft_errors = _build_entry_from_form(spec, form, entry_id_for_upload, existing)

    if fatal_errors:
        return web.json_response({"ok": False, "error": "; ".join(fatal_errors)})

    new_id = new_entry.get("id", "").strip()
    if not new_id:
        # Nothing to key a draft by yet -- report problems (which will include the missing "id"
        # itself) without persisting anything.
        problems = draft_publish["check_problems"](new_entry, set(registry.keys()))
        return web.json_response({"ok": True, "id": None, "problems": problems})

    draft_publish["save_draft"](new_entry)
    if item_id not in ("new", new_id) and item_id in drafts:
        # The entry's own id was renamed mid-draft -- drop the stale draft filed under its old id
        # so it doesn't linger as an orphan now that everything's keyed by the new one.
        draft_publish["delete_draft"](item_id)

    problems = draft_publish["check_problems"](new_entry, set(registry.keys()) - {new_id})
    return web.json_response({"ok": True, "id": new_id, "problems": problems})


async def flowchart_publish_view(request: web.Request) -> web.Response:
    """Runs a draft/publish content type's draft through full validation and, only if it passes,
    commits it to the real content JSON -- the one place a broken entry can ever be rejected
    outright instead of silently accepted (see CLAUDE.md's "content is data" section). On failure
    the live file is never touched and the draft is left exactly as it was, so nothing is lost --
    the redirect back to the edit page recomputes the same structured problems fresh from that
    untouched draft (see edit_view) to highlight what's still wrong."""
    content_type = request.match_info["content_type"]
    item_id = request.match_info["item_id"]
    spec = CONTENT_TYPES[content_type]
    draft_publish = spec["draft_publish"]
    registry = getattr(spec["module"], spec["registry_attr"])
    draft = draft_publish["load_drafts"]().get(item_id)
    if draft is None:
        # Nothing autosaved yet under this id (Publish clicked before the first autosave tick
        # landed, or with JS unavailable) -- build it fresh from whatever the form just submitted,
        # same shape every other save path uses.
        existing = registry.get(item_id, {})
        form = dict(await request.post())
        entry_id_for_upload = form.get("id", "").strip() or item_id
        draft, fatal_errors, _soft = _build_entry_from_form(spec, form, entry_id_for_upload, existing)
        if fatal_errors:
            raise web.HTTPFound(f"/edit/{content_type}/{item_id}?publish_error=1")
        draft_publish["save_draft"](draft)

    new_id = draft.get("id", "").strip() or item_id
    entries = _load_raw_entries(spec)
    entry_index = None
    for i, e in enumerate(entries):
        if e.get("id") in (item_id, new_id):
            entries[i] = draft
            entry_index = i
            break
    if entry_index is None:
        entries.append(draft)

    error_msg = _write_and_validate(spec, entries)
    if error_msg is None:
        draft_publish["delete_draft"](new_id)
        if new_id != item_id:
            draft_publish["delete_draft"](item_id)
        raise web.HTTPFound(f"/edit/{content_type}/{new_id}?published=1")
    raise web.HTTPFound(f"/edit/{content_type}/{new_id}?publish_error=1")


def _delete_blockers(content_type: str, item_id: str) -> list[str]:
    """Names of other entries that would be left with a dangling reference if item_id were
    deleted from `content_type`. Checked explicitly here because a delete only writes/validates
    the *one* file being changed (deleting a material doesn't re-load dungeon_recipes.json), so a
    plain re-validate-this-file-only save wouldn't catch damage done elsewhere.

    Three kinds of cross-reference exist in this content set:
      - Recipes reference materials (by key) and equipment/consumables (by output_id).
      - Monsters reference equipment/materials/consumables/housing_items in their own `drops` list.
      - Delves reference individual monster ids within each combat room's monster_groups -- so
        deleting a monster is only unsafe if it's the *only* monster in some group (removing it
        from a group that still has others left, or from a room that has other groups, is fine,
        and is exactly what re-editing that delve would do).
    """
    if content_type == "materials":
        recipe_blockers = [r["name"] for r in dungeon.RECIPES.values() if item_id in r["materials"]]
        monster_blockers = [
            m["name"] for m in dungeon.MONSTERS.values()
            if any(d["kind"] == "material" and d["item_id"] == item_id for d in m.get("drops", []))
        ]
        return recipe_blockers + monster_blockers
    if content_type in ("equipment", "consumables"):
        drop_kind = "equipment" if content_type == "equipment" else "consumable"
        recipe_blockers = [r["name"] for r in dungeon.RECIPES.values() if r["output_id"] == item_id]
        monster_blockers = [
            m["name"] for m in dungeon.MONSTERS.values()
            if any(d["kind"] == drop_kind and d["item_id"] == item_id for d in m.get("drops", []))
        ]
        return recipe_blockers + monster_blockers
    if content_type == "housing_items":
        return [
            m["name"] for m in dungeon.MONSTERS.values()
            if any(d["kind"] == "housing_item" and d["item_id"] == item_id for d in m.get("drops", []))
        ]
    if content_type == "monsters":
        return [
            d["name"] for d in dungeon.DELVES.values()
            if any(
                group.get("monsters") == [item_id]
                for room in d["rooms"] if room.get("type") == "combat"
                for group in room.get("monster_groups", [])
            )
        ]
    return []


async def delete_view(request: web.Request) -> web.Response:
    content_type = request.match_info["content_type"]
    item_id = request.match_info["item_id"]
    spec = CONTENT_TYPES.get(content_type)
    if spec is None:
        raise web.HTTPNotFound()

    blockers = _delete_blockers(content_type, item_id)
    if blockers:
        error = html.escape(f"Can't delete {item_id!r} -- still used by: {', '.join(blockers)}")
        body = f'<p class="error">{error}</p><p><a class="row-link" href="/edit/{content_type}/{item_id}">← Back</a></p>'
        return _html_response(_page("Can't delete", body, active=content_type))

    entries = [e for e in _load_raw_entries(spec) if e.get("id") != item_id]
    error_msg = _write_and_validate(spec, entries)
    if error_msg is not None:
        body = f'<p class="error">{html.escape(error_msg)}</p><p><a class="row-link" href="/edit/{content_type}/{item_id}">← Back</a></p>'
        return _html_response(_page("Can't delete", body, active=content_type))
    raise web.HTTPFound(f"/edit/{content_type}")


async def assets_view(request: web.Request) -> web.Response:
    """Standalone upload/browse page for assets/, independent of any content entry -- for prepping
    an image (or grabbing its path) before it's needed by a monster/room/npc field, or for assets
    (like the casino/ranch/town banners) that aren't attached to any content entry at all."""
    error = ""
    message = ""
    if request.method == "POST":
        form = await request.post()
        subdir = form.get("subdir", "")
        errors = []
        saved_paths = []
        for file_field in form.getall("file"):
            try:
                saved_paths.append(_save_arbitrary_asset(file_field, subdir))
            except ValueError as e:
                errors.append(str(e))
        if saved_paths:
            items = "".join(f"<li><code>/{html.escape(p)}</code></li>" for p in saved_paths)
            message = f'<p class="success">Uploaded:</p><ul>{items}</ul>'
        if errors:
            error = "".join(f'<p class="error">{html.escape(e)}</p>' for e in errors)

    rows = "".join(
        f'<tr><td><img src="{html.escape(_asset_src(path))}" class="asset-thumb" loading="lazy"></td>'
        f'<td><code>{html.escape(path)}</code></td>'
        f'<td>{size_kb:.1f} KB</td>'
        f'<td><form method="post" action="/assets/delete" '
        f'onsubmit="return confirm(\'Delete {html.escape(path)}?\')">'
        f'<input type="hidden" name="path" value="{html.escape(path)}">'
        f'<button type="submit" class="danger">Delete</button></form></td></tr>'
        for path, size_kb in _list_asset_files()
    )
    body = f"""
    <h1>🖼️ Assets</h1>
    <p>Upload an image straight into <code>assets/</code>, no content entry required. Paste the
    resulting path into any "image" field's file picker later, or reference it directly (e.g. a
    banner URL).</p>
    {error}{message}
    <form method="post" enctype="multipart/form-data">
        <label>Subdirectory (optional)<input type="text" name="subdir" placeholder="e.g. dungeon/backgrounds"></label>
        <label>Files<input type="file" name="file" accept="image/*" multiple required></label>
        <button type="submit">Upload</button>
    </form>
    <table class="asset-table"><thead><tr><th></th><th>Path</th><th>Size</th><th></th></tr></thead>
    <tbody>{rows}</tbody></table>
    """
    return _html_response(_page("Assets", body, active="assets", breadcrumbs=[("Home", "/"), ("Assets", None)]))


async def delete_asset_view(request: web.Request) -> web.Response:
    form = await request.post()
    rel_path = form.get("path", "")
    full_path = os.path.realpath(os.path.join(os.path.dirname(__file__), rel_path))
    assets_real = os.path.realpath(ASSETS_DIR)
    if not full_path.startswith(assets_real + os.sep) or not os.path.isfile(full_path):
        raise web.HTTPBadRequest(text="Invalid path")
    os.remove(full_path)
    raise web.HTTPFound("/assets")


async def utilities_view(request: web.Request) -> web.Response:
    """Standalone page for one-off admin actions that aren't content edits -- just a manual
    "back up the database now" button for the moment (see _create_db_backup), served from
    /backups (a static route alongside /assets, added in build_app) so a saved snapshot can be
    pulled down directly -- e.g. by a scheduled `scp`/`rsync` from elsewhere -- without needing a
    separate download route."""
    message = ""
    if request.method == "POST":
        filename = await asyncio.to_thread(_create_db_backup)
        message = f'<p class="success">Saved <code>backups/{html.escape(filename)}</code>.</p>'

    rows = "".join(
        f'<tr><td><code>{html.escape(fname)}</code></td><td>{saved_at}</td><td>{size_kb:.1f} KB</td>'
        f'<td><a class="row-link" href="/backups/{html.escape(fname)}">Download</a></td>'
        f'<td><form method="post" action="/utilities/delete-backup" '
        f'onsubmit="return confirm(\'Delete {html.escape(fname)}?\')">'
        f'<input type="hidden" name="filename" value="{html.escape(fname)}">'
        f'<button type="submit" class="danger">Delete</button></form></td></tr>'
        for fname, size_kb, saved_at in _list_db_backups()
    )
    body = f"""
    <h1>🧰 Utilities</h1>
    <h2>Database Backup</h2>
    <p>Snapshots <code>casino.db</code> into <code>backups/</code> using SQLite's own online backup
    API, safe to run while the bot is live. Download a snapshot here, or pull it from
    <code>/backups/&lt;filename&gt;</code> directly (e.g. from a scheduled <code>scp</code>).</p>
    {message}
    <form method="post"><button type="submit">Back Up Now</button></form>
    <table><thead><tr><th>File</th><th>Saved</th><th>Size</th><th></th><th></th></tr></thead>
    <tbody>{rows}</tbody></table>
    """
    return _html_response(_page("Utilities", body, active="utilities", breadcrumbs=[("Home", "/"), ("Utilities", None)]))


async def delete_backup_view(request: web.Request) -> web.Response:
    form = await request.post()
    filename = form.get("filename", "")
    full_path = os.path.realpath(os.path.join(BACKUPS_DIR, filename))
    backups_real = os.path.realpath(BACKUPS_DIR)
    if not full_path.startswith(backups_real + os.sep) or not os.path.isfile(full_path):
        raise web.HTTPBadRequest(text="Invalid path")
    os.remove(full_path)
    raise web.HTTPFound("/utilities")


def _player_label(bot, guild_id: int, user_id: int) -> str:
    """A live Discord display name for user_id in guild_id if the bot's own member cache has it
    (request.app["bot"].get_guild/.get_member -- synchronous cache lookups, safe to call directly
    from a request handler), else just the bare id. The bot doesn't request the privileged Members
    intent, so this cache is only ever as complete as whoever's been recently active -- falling
    back to the id (rather than erroring) is what lets the player picker below list EVERY user
    db.list_known_users knows about regardless of cache coverage."""
    guild = bot.get_guild(guild_id) if bot else None
    member = guild.get_member(user_id) if guild else None
    return f"{member.display_name} ({user_id})" if member else f"Unknown player ({user_id})"


# Every kind SHOP_KINDS offers except horse_clothes -- horse clothing lives in its own
# per-horse ownership table (horse_clothes.py), not the generic inventory/equipment_inventory
# tables _grant_item below knows how to write to, so it's out of scope for this quick "give a
# player an item" tool.
GRANTABLE_ITEM_KINDS = [k for k in SHOP_KINDS if k != "horse_clothes"]


def _grant_item(guild_id: int, user_id: int, kind: str, item_id: str, qty: int) -> str | None:
    """Grants qty of item_id directly to a player -- the admin-panel equivalent of a dungeon drop
    or quest reward, for un-sticking a player who's missing something a bug cost them, or seeding
    test content. Equipment always lands unequipped in equipment_inventory (db.store_equipment_item,
    same as a found piece that wasn't an upgrade) rather than auto-equipping, so a grant never
    silently swaps out gear a player already chose to wear; material/consumable/quest_item all
    share the generic `inventory` table (db.add_inventory_item). Returns an error message to show
    the admin, or None on success."""
    registry = {
        "equipment": dungeon.EQUIPMENT, "material": dungeon.MATERIALS,
        "consumable": dungeon.CONSUMABLES, "quest_item": quests.QUEST_ITEMS,
        "housing_item": housing.HOUSING_ITEMS,
    }.get(kind)
    if registry is None or item_id not in registry:
        return f"Unknown {kind or 'item'} id {item_id!r}."
    if kind == "equipment":
        # Storing an item_id that's ALSO currently equipped puts the same id in both
        # character_equipment and equipment_inventory at once -- an invariant equip_item_smart
        # otherwise always maintains (it removes from storage the moment something's equipped).
        # dungeon_view._award_kill hit the same trap on a tied non-upgrade drop; this produces a
        # live crash (a duplicate Discord Select option value in EquipmentSlotSelect) rather than
        # just a cosmetic double-listing, so it's rejected outright here instead of silently
        # creating the same broken state again.
        if db.get_equipped_items(guild_id, user_id).get(registry[item_id]["slot"]) == item_id:
            return f"{registry[item_id]['name']} is already equipped by this player."
        db.store_equipment_item(guild_id, user_id, item_id, qty)
    else:
        db.add_inventory_item(guild_id, user_id, item_id, qty)
    return None


async def player_debug_view(request: web.Request) -> web.Response:
    """Standalone page (same "not a content edit" shape as Utilities) for directly inspecting and
    overriding one player's live balance/energy/dungeon-character state -- the manual DB-poke
    workflow an admin would otherwise need shell access for (see db.set_balance/set_energy/
    set_character_progress/_grant_item). Player lookup is a two-step guild -> known-player picker
    instead of free-typed ids: the guild list comes from the live bot's own connected guilds
    (request.app["bot"].guilds), and the player list comes from db.list_known_users (every user_id
    this guild's economy has ever touched -- a user with no row here has nothing to debug anyway),
    each resolved to a display name via _player_label. Each `<select>` auto-submits its own GET
    form on change (plain page reloads, no AJAX) so picking a server reveals that server's player
    list, and picking a player reveals their editable stats -- same "no bespoke JS beyond a trivial
    onchange" bar the rest of this admin panel holds to.

    Three independent POST forms share this one route (stats, grant-item, unstick-delve), told
    apart by a hidden "action" field -- kept separate rather than one combined form so submitting
    one never also re-submits (and risks zeroing/undoing) the others' fields. unstick-delve is a
    live in-memory fix, not a DB write: it just pops dungeon_view.active_delves/busy_players for
    this user_id, for the case where a delve start crashed after registering the player but before
    a session ever rendered, permanently wedging them as "already in a delve" (see dungeon_view.py's
    DelveModeChoiceView.solo_button/party_button, which now clean up after themselves on that same
    failure -- this form is only for state stuck from before that fix, or any other repeat of the
    same class of bug)."""
    bot = request.app.get("bot")
    grant_error = None

    if request.method == "POST":
        form = await request.post()
        gid, uid = int(form["guild_id"]), int(form["user_id"])
        if form.get("action") == "unstick_delve":
            dungeon_view.active_delves.pop(uid, None)
            dungeon_view.busy_players.discard(uid)
            raise web.HTTPFound(f"/player-debug?guild_id={gid}&user_id={uid}&unstuck=1")
        elif form.get("action") == "grant_item":
            kind = form.get("grant_kind", "").strip()
            item_id = form.get("grant_item_id", "").strip()
            raw_qty = form.get("grant_qty", "").strip()
            qty = int(raw_qty) if raw_qty.isdigit() and int(raw_qty) > 0 else 1
            if not kind or not item_id:
                grant_error = "Pick both a kind and an item."
            else:
                grant_error = await asyncio.to_thread(_grant_item, gid, uid, kind, item_id, qty)
            if grant_error is None:
                raise web.HTTPFound(f"/player-debug?guild_id={gid}&user_id={uid}&granted=1")
            # Falls through to re-render the page with grant_error shown -- no redirect, since the
            # error is real content to display, not just a one-word flag like "saved=1"/"granted=1".
        else:
            await asyncio.to_thread(db.set_balance, gid, uid, int(form.get("balance") or 0))
            await asyncio.to_thread(db.set_energy, gid, uid, int(form.get("energy") or 0))
            if "level" in form and "xp" in form and "current_hp" in form:
                character = db.get_character(gid, uid)
                if character is not None:
                    level = int(form["level"])
                    # Recomputed from the class's own leveling curve, not just persisted as typed --
                    # this is the whole point of this feature, see dungeon.compute_stats_at_level's
                    # own docstring: an admin setting Level here should never leave atk/def/etc stale.
                    new_stats = dungeon.compute_stats_at_level(character["main_class"], character["subclass"], level)
                    equipped = db.get_equipped_items(gid, uid)
                    housing_bonuses = housing.get_house_bonuses(gid, uid)
                    effective_character = {**character, **new_stats}
                    max_hp = dungeon.compute_effective_stats(
                        effective_character, equipped, housing_bonuses.get("stat_bonus", {})
                    )["hp"]
                    current_hp = max(0, min(int(form["current_hp"]), max_hp))
                    await asyncio.to_thread(
                        db.set_character_progress, gid, uid,
                        level, int(form["xp"]), current_hp,
                        new_stats["hp"], new_stats["atk"], new_stats["def"],
                        new_stats["spatk"], new_stats["spdef"], new_stats["speed"],
                    )
            raise web.HTTPFound(f"/player-debug?guild_id={gid}&user_id={uid}&saved=1")

    # On the grant-item error fall-through above, guild_id/user_id came from the POSTed form, not
    # the query string (there was no redirect) -- request.query has nothing for either case that
    # didn't happen, so this picks whichever one actually did.
    guild_id = request.query.get("guild_id") or (form.get("guild_id") if request.method == "POST" else "") or ""
    user_id = request.query.get("user_id") or (form.get("user_id") if request.method == "POST" else "") or ""
    saved_notice = '<p class="success">Saved.</p>' if request.query.get("saved") else ""
    granted_notice = '<p class="success">Item granted.</p>' if request.query.get("granted") else ""
    unstuck_notice = '<p class="success">Cleared.</p>' if request.query.get("unstuck") else ""

    guild_options = "".join(
        f'<option value="{g.id}"{" selected" if str(g.id) == guild_id else ""}>{html.escape(g.name)}</option>'
        for g in sorted(bot.guilds, key=lambda g: g.name.lower())
    ) if bot else ""
    guild_picker = (
        f'<form method="get" class="player-debug-picker"><label>Server'
        f'<select name="guild_id" onchange="this.form.submit()">'
        f'<option value="">— choose a server —</option>{guild_options}</select></label></form>'
    )

    player_picker = ""
    stats_form = ""
    grant_item_form = ""
    unstuck_form = ""
    if guild_id and bot:
        gid = int(guild_id)
        known_ids = db.list_known_users(gid)
        if known_ids:
            user_options = "".join(
                f'<option value="{uid}"{" selected" if str(uid) == user_id else ""}>'
                f'{html.escape(_player_label(bot, gid, uid))}</option>'
                for uid in known_ids
            )
            player_picker = (
                f'<form method="get" class="player-debug-picker">'
                f'<input type="hidden" name="guild_id" value="{gid}">'
                f'<label>Player<select name="user_id" onchange="this.form.submit()">'
                f'<option value="">— choose a player —</option>{user_options}</select></label></form>'
            )
        else:
            player_picker = "<p>No known players in this server yet.</p>"

        if user_id:
            uid = int(user_id)
            in_delve = uid in dungeon_view.active_delves
            in_busy = uid in dungeon_view.busy_players
            if in_delve or in_busy:
                entity = dungeon_view.active_delves.get(uid)
                status_bits = []
                if in_delve:
                    status_bits.append(f"holding an active_delves slot ({type(entity).__name__})")
                if in_busy:
                    status_bits.append("marked busy")
                unstuck_form = (
                    f'<h3>Delve/Busy State</h3>'
                    f'<p class="field-hint">Currently {" and ".join(status_bits)}. If this is stale -- e.g. a '
                    f'delve start that crashed before it could render -- clear it below so they can start a '
                    f'new delve. This is a live in-memory fix (no restart needed) and only frees this one '
                    f'player\'s own reservation; it does not touch other party/duel members who share the '
                    f'same session.</p>'
                    f'<form method="post">'
                    f'<input type="hidden" name="action" value="unstick_delve">'
                    f'<input type="hidden" name="guild_id" value="{gid}">'
                    f'<input type="hidden" name="user_id" value="{uid}">'
                    f'<button type="submit">Clear stuck delve state</button></form>'
                )
            else:
                unstuck_form = (
                    '<h3>Delve/Busy State</h3><p class="field-hint">Not currently registered in a delve.</p>'
                )
            balance = db.get_balance(gid, uid)
            energy = db.get_energy(gid, uid)
            character = db.get_character(gid, uid)
            if character:
                equipped = db.get_equipped_items(gid, uid)
                housing_bonuses = housing.get_house_bonuses(gid, uid)
                max_hp = dungeon.compute_effective_stats(character, equipped, housing_bonuses.get("stat_bonus", {}))["hp"]
                current_hp = min(character["current_hp"], max_hp)
                stats_line = (
                    f'ATK {character["atk"]} · DEF {character["def"]} · SpATK {character["spatk"]} · '
                    f'SpDEF {character["spdef"]} · Speed {character["speed"]} (base, before equipment)'
                )
                character_fields = (
                    f'<label>Level<input type="number" min="1" name="level" value="{character["level"]}">'
                    f'<small class="field-hint">Saving recomputes attributes from '
                    f'{html.escape(character["main_class"])}/{html.escape(character["subclass"])}\'s own '
                    f'leveling curve for whatever level is entered here -- not just this character\'s own '
                    f'level-up history, so this also fixes a character with stale/mismatched stats.</small>'
                    f'</label>'
                    f'<label>XP<input type="number" min="0" name="xp" value="{character["xp"]}"></label>'
                    f'<label>Current HP <small class="field-hint">max {max_hp}, clamped down on save if the '
                    f'new level\'s max HP is lower</small>'
                    f'<input type="number" min="0" max="{max_hp}" name="current_hp" '
                    f'value="{current_hp}"></label>'
                    f'<p class="field-hint">{stats_line}</p>'
                )
            else:
                character_fields = "<p><em>No dungeon character yet.</em></p>"
            stats_form = (
                f'<form method="post">'
                f'<input type="hidden" name="action" value="save_stats">'
                f'<input type="hidden" name="guild_id" value="{gid}">'
                f'<input type="hidden" name="user_id" value="{uid}">'
                f'<h2>{html.escape(_player_label(bot, gid, uid))}</h2>'
                f'<label>Balance<input type="number" min="0" name="balance" value="{balance}"></label>'
                f'<label>Energy <small class="field-hint">0-{db.ENERGY_CAP}</small>'
                f'<input type="number" min="0" max="{db.ENERGY_CAP}" name="energy" value="{energy}"></label>'
                f'{character_fields}'
                f'<button type="submit">Save</button></form>'
            )

            # Preserves whatever the admin last picked across a validation error re-render (e.g. a
            # kind chosen but no item yet) -- pure convenience, not needed on a fresh page load.
            posted = form if request.method == "POST" else {}
            grant_kind = posted.get("grant_kind", "")
            kind_options = "".join(
                f'<option value="{k}"{" selected" if k == grant_kind else ""}>{k}</option>'
                for k in [""] + GRANTABLE_ITEM_KINDS
            )
            item_select = _render_cascaded_select(
                "grant_item_id", "shop", grant_kind or None, posted.get("grant_item_id")
            )
            grant_qty = posted.get("grant_qty") or "1"
            grant_error_html = f'<p class="error">{html.escape(grant_error)}</p>' if grant_error else ""
            grant_item_form = (
                f'<h3>Give Item</h3>'
                f'{grant_error_html}'
                f'<form method="post" class="row-group">'
                f'<input type="hidden" name="action" value="grant_item">'
                f'<input type="hidden" name="guild_id" value="{gid}">'
                f'<input type="hidden" name="user_id" value="{uid}">'
                f'<label>kind<select name="grant_kind" class="cascade-select" data-cascade="shop">'
                f'{kind_options}</select></label>'
                f'<label>item_id{item_select}</label>'
                f'<label>qty<input type="number" min="1" name="grant_qty" value="{html.escape(grant_qty)}"></label>'
                f'<button type="submit">Give</button></form>'
            )

    body = f"""
    <h1>🐛 Player Debug</h1>
    <p>Look up one player's live economy/character state and override it directly -- for
    un-sticking a bad delve, refilling energy, or fixing a balance issue without shell access.</p>
    {saved_notice}
    {granted_notice}
    {unstuck_notice}
    {guild_picker}
    {player_picker}
    {unstuck_form}
    {stats_form}
    {grant_item_form}
    """
    return _html_response(
        _page("Player Debug", body, active="player-debug", breadcrumbs=[("Home", "/"), ("Player Debug", None)])
    )


def _bar_html(value: float, max_value: float) -> str:
    pct = 0 if max_value <= 0 else min(100, round(value / max_value * 100))
    return f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>'


def _outlier_badge(direction: str | None) -> str:
    return f'<span class="outlier-flag">⚠ {html.escape(direction)}</span>' if direction else ""


def _skill_balance_table_html(rows: list[dict]) -> str:
    max_dmg_per_chip = max((r["dmg_per_chip"] for r in rows if r["dmg_per_chip"] is not None), default=0)
    body_rows = []
    for r in sorted(rows, key=lambda r: (r["build_label"], r["unlock_level"], r["name"])):
        effects_html = "".join(f'<span class="effect-tag">{html.escape(t)}</span>' for t in r["effect_types"])
        if r["dmg_per_chip"] is not None:
            dmg_cell = (
                f'{r["avg_damage"]:.1f} dmg &middot; {r["dmg_per_chip"]:.2f}/chip '
                f'{_bar_html(r["dmg_per_chip"], max_dmg_per_chip)}'
                f'{_outlier_badge(r["dmg_outlier"])}'
            )
        elif r["heal_per_chip"] is not None:
            dmg_cell = (
                f'{r["avg_healed"]:.1f} HP &middot; {r["heal_per_chip"]:.2f}/chip'
                f'{_outlier_badge(r["heal_outlier"])}'
            )
        else:
            dmg_cell = "&mdash;"
        body_rows.append(
            f'<tr><td>{html.escape(r["build_label"])}</td><td>{html.escape(r["name"])}</td>'
            f'<td>{html.escape(r["type"])}</td><td>{r["chip_cost"]}</td><td>{r["unlock_level"]}</td>'
            f'<td>{effects_html}</td><td>{dmg_cell}</td></tr>'
        )
    return (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Build</th><th>Skill</th><th>Type</th><th>Chips</th><th>Lv</th><th>Effects</th>"
        "<th>Damage / Heal</th></tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>"
    )


def _delve_balance_table_html(rows: list[dict]) -> str:
    max_overall = max((r["overall_dpt"] for r in rows), default=0)
    body_rows = []
    for r in sorted(rows, key=lambda r: -r["overall_dpt"]):
        per_fight = " &nbsp; ".join(f"{v:.1f}" for v in r["per_fight_dpt"])
        body_rows.append(
            f'<tr><td>{html.escape(r["build_label"])}</td>'
            f'<td><span class="effect-tag">{html.escape(r["policy"])}</span></td>'
            f'<td>{html.escape(per_fight)}</td>'
            f'<td>{r["overall_dpt"]:.1f} {_bar_html(r["overall_dpt"], max_overall)}'
            f'{_outlier_badge(r["outlier"])}</td></tr>'
        )
    return (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Build</th><th>Best rotation</th><th>Damage/turn per fight (in delve order)</th><th>Overall avg</th>"
        "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>"
    )


def _rotation_explorer_table_html(by_policy: dict) -> str:
    best_policy = max(by_policy, key=lambda p: by_policy[p]["overall_dpt"])
    max_overall = max((r["overall_dpt"] for r in by_policy.values()), default=0)
    body_rows = []
    for policy, result in by_policy.items():
        per_fight = " &nbsp; ".join(f"{v:.1f}" for v in result["per_fight_dpt"]) or "&mdash;"
        winner_badge = ' <span class="outlier-flag">★ best</span>' if policy == best_policy else ""
        body_rows.append(
            f'<tr><td><strong>{html.escape(policy)}</strong>{winner_badge}'
            f'<div class="field-hint">{html.escape(skill_balance.ROTATION_POLICIES[policy])}</div></td>'
            f'<td>{per_fight}</td>'
            f'<td>{result["overall_dpt"]:.1f} {_bar_html(result["overall_dpt"], max_overall)}</td>'
            f'<td>{result["chips_leftover"]:.1f}</td></tr>'
        )
    return (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>Rotation</th><th>Damage/turn per fight</th><th>Overall avg</th><th>Chips left at delve end</th>"
        "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>"
    )


def _damage_ramp_html(report: dict) -> str:
    """report: {policy: {tier_label: [dmg turn 1..N]}} from skill_balance.damage_ramp_report --
    one small table per policy, tiers as rows, turns as columns, so a "burst then fizzle" or
    "ramps up once debuffed" shape reads directly left-to-right."""
    tiers_info = skill_balance.monster_tiers()
    sections = []
    for policy, tiers in report.items():
        if not tiers:
            continue
        turn_count = len(next(iter(tiers.values())))
        header = "".join(f"<th>T{i + 1}</th>" for i in range(turn_count))
        rows = "".join(
            f'<tr><td>{html.escape(label)} <span class="field-hint">'
            f'(n={tiers_info[label]["monster_count"]} monster(s), DEF/SpDef {tiers_info[label]["def"]:g})'
            f"</span></td>" + "".join(f"<td>{v:.1f}</td>" for v in values) + "</tr>"
            for label, values in tiers.items()
        )
        sections.append(
            f'<h4>{html.escape(policy)} <span class="field-hint">'
            f'{html.escape(skill_balance.ROTATION_POLICIES[policy])}</span></h4>'
            f'<div class="table-scroll"><table><thead><tr><th>Tier</th>{header}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
    return "".join(sections)


async def skill_balance_view(request: web.Request) -> web.Response:
    """Read-only balance report for every class+subclass build and skill -- no content edits, no
    POST, recomputed fresh on every load the same way horserace.current_probabilities is (a Monte-
    Carlo simulation cheap enough to run per-request, no DB writes). Reads dungeon.SKILLS/CLASSES/
    SUBCLASSES/DELVES live inside this function body (not captured at import time), so it always
    reflects whatever a content save through this same admin panel most recently hot-reloaded. See
    skill_balance.py for the actual simulation logic and its documented scope boundaries (output
    only -- no incoming damage/survivability/equipment modeling)."""
    delve_ids = list(dungeon.DELVES.keys())
    requested_delve = request.query.get("delve")
    delve_id = requested_delve if requested_delve in dungeon.DELVES else skill_balance.default_delve_id()

    all_builds = skill_balance.all_builds()
    requested_build = request.query.get("build")
    build = tuple(requested_build.split(":", 1)) if requested_build else None
    if build not in all_builds:
        build = all_builds[0]

    skill_rows = skill_balance.per_skill_table()
    skill_table = _skill_balance_table_html(skill_rows)

    if delve_id:
        delve_rows = skill_balance.per_build_delve_table(delve_id)
        delve_table = _delve_balance_table_html(delve_rows)
        delve_name = html.escape(dungeon.DELVES[delve_id].get("name", delve_id))
        explorer_result = skill_balance.simulate_build_rotations(build[0], build[1], delve_id)
        explorer_table = _rotation_explorer_table_html(explorer_result)
    else:
        delve_table = "<p>No delves defined.</p>"
        delve_name = "(none)"
        explorer_table = "<p>No delves defined.</p>"

    ramp_report = skill_balance.damage_ramp_report(build[0], build[1])
    ramp_html = _damage_ramp_html(ramp_report) if any(ramp_report.values()) else "<p>No monster tiers defined.</p>"

    delve_options = "".join(
        f'<option value="{html.escape(did)}"{" selected" if did == delve_id else ""}>'
        f'{html.escape(dungeon.DELVES[did].get("name", did))}'
        f'{"" if dungeon.DELVES[did].get("active", True) else " (inactive)"}</option>'
        for did in delve_ids
    )
    build_options = "".join(
        f'<option value="{mc}:{sc}"{" selected" if (mc, sc) == build else ""}>'
        f'{html.escape(skill_balance.build_label(mc, sc))} ({mc}/{sc})</option>'
        for mc, sc in all_builds
    )

    body = f"""
    <h1>📊 Skill Balance</h1>
    <p>Simulated, not measured from real play -- every build's raw class+subclass stats (no
    equipment/housing), simulated at a level high enough to unlock its full current skill kit.
    Models a build's own output (damage dealt, Chip economy) only, not incoming damage or
    survivability.</p>
    <p class="field-hint">
        <strong>⚠ flag</strong> &mdash; this skill or build's value is more than
        {skill_balance.OUTLIER_THRESHOLD:.0%} away from the typical (median) value among everything
        else in that same table -- worth a look, not automatically a bug.<br>
        <strong>Rotation</strong> &mdash; the strategy a build uses to pick which skill to cast each
        turn, given whatever Chips it has left. See "Rotation Explorer" below to compare strategies
        side by side for any one build.
    </p>

    <h2>Per-skill damage (isolated, vs. the game's real median monster DEF/SpDef)</h2>
    {skill_table}

    <h2>Per-build rotation through a whole delve ({delve_name})</h2>
    <p>Chips are spent across the delve's real fight sequence in order, never refilled mid-delve --
    watch how each build's damage/turn holds up (or falls off) fight to fight. Each build here is
    shown at its own best-scoring rotation (see "Best rotation" column) rather than one strategy
    forced on every build -- a chip-hungry build and a chip-light one don't necessarily play the
    same way. A branching delve's choice rooms follow their first listed outcome only -- one
    representative path, not full coverage.</p>
    <form method="get" class="delve-picker">
        <label style="flex-direction:row;align-items:center;gap:8px;">Delve
            <select name="delve" onchange="this.form.submit()">{delve_options}</select>
        </label>
    </form>
    {delve_table}

    <h2>Rotation Explorer</h2>
    <p>Pick one build to see how each candidate rotation strategy actually plays out for it -- this
    is what "Best rotation" in the table above is chosen from.</p>
    <form method="get" class="delve-picker">
        <input type="hidden" name="delve" value="{html.escape(delve_id or '')}">
        <label style="flex-direction:row;align-items:center;gap:8px;">Build
            <select name="build" onchange="this.form.submit()">{build_options}</select>
        </label>
    </form>

    <h3>Across a whole delve ({delve_name})</h3>
    {explorer_table}

    <h3>Damage ramp vs. monster tiers</h3>
    <p>Instead of a specific delve's rooms, this pits the selected build against three imagined
    monster difficulty tiers (Early/Mid/Late -- median DEF/SpDef of the game's own real monsters at
    that intended level range) and charts damage turn by turn (T1, T2, ...) across one continuous
    fight per tier, full Chips at the start. Buffs, DEF/SpDef-lowering debuffs, and DoT ticks all
    carry forward turn to turn, so a rotation that opens with a debuff before its big hits should
    visibly ramp up rather than hit the same every turn -- and a build that can barely dent a Late-
    tier monster's defense shows up as a near-flat, low line the same way it would in real play.</p>
    {ramp_html}
    """
    return _html_response(
        _page("Skill Balance", body, active="skill-balance", breadcrumbs=[("Home", "/"), ("Skill Balance", None)])
    )


def build_app(bot=None) -> web.Application:
    if not ADMIN_PANEL_PASSWORD:
        raise RuntimeError("Set ADMIN_PANEL_PASSWORD in .env before starting the content editor.")
    # aiohttp's own default request-body cap is 1MB, well under MAX_IMAGE_BYTES -- without
    # raising it here, an upload between 1MB and MAX_IMAGE_BYTES would get rejected by aiohttp
    # itself (a generic 413, before _save_uploaded_image's own friendlier size check ever runs)
    # rather than actually being allowed up to the limit this module advertises.
    app = web.Application(middlewares=[auth_middleware], client_max_size=MAX_IMAGE_BYTES + 1024 * 100)
    app["bot"] = bot
    app.router.add_get("/login", login)
    app.router.add_post("/login", login)
    app.router.add_get("/", dashboard)
    app.router.add_get("/edit/{content_type}", list_view)
    app.router.add_get("/edit/{content_type}/{item_id}", edit_view)
    app.router.add_post("/edit/{content_type}/{item_id}", edit_view)
    app.router.add_post("/edit/{content_type}/{item_id}/autosave", flowchart_autosave_view)
    app.router.add_post("/edit/{content_type}/{item_id}/publish", flowchart_publish_view)
    app.router.add_post("/delete/{content_type}/{item_id}", delete_view)
    app.router.add_get("/assets", assets_view)
    app.router.add_post("/assets", assets_view)
    app.router.add_post("/assets/delete", delete_asset_view)
    # Serves uploaded images (and every other file already under assets/) for the edit-form
    # previews -- stored paths already look like "assets/dungeon/monsters/x.png", so mounting the
    # static route at "/assets" makes that same string a valid URL with just a leading "/".
    # Behind auth_middleware like everything else here, so this doesn't expose anything publicly.
    app.router.add_static("/assets", ASSETS_DIR)
    app.router.add_get("/utilities", utilities_view)
    app.router.add_post("/utilities", utilities_view)
    app.router.add_post("/utilities/delete-backup", delete_backup_view)
    app.router.add_get("/player-debug", player_debug_view)
    app.router.add_post("/player-debug", player_debug_view)
    app.router.add_get("/skill-balance", skill_balance_view)
    # Same reasoning as /assets above -- lets a saved snapshot be pulled straight from
    # /backups/<filename> (e.g. by a scheduled scp/rsync), not just downloaded through the page.
    # add_static requires the directory to exist up front, unlike ASSETS_DIR (already present in
    # the repo) -- backups/ is never committed (git-ignored, created on first use), so it has to be
    # created here rather than assumed.
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    app.router.add_static("/backups", BACKUPS_DIR)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), port=PORT)
