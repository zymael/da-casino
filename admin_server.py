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
import horse_clothes
import quests
import room_commands
import rooms
from admin_schemas import (
    CATEGORIES, CONTENT_TYPES, EFFECT_PARAM_NAMES, EFFECT_PARAMS_BY_TYPE, EFFECT_TYPE_HINTS,
    EFFECT_TYPES, EQUIPMENT_EFFECT_TRIGGERS, SHOP_KINDS, TRIGGER_PARAM_HINTS, TRIGGER_PARAM_KINDS,
    TRIGGER_PARAM_NAMES, TRIGGER_TYPES,
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
    "subclass": lambda: sorted(dungeon.SUBCLASSES.keys()),
}

load_dotenv()

ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD")
PORT = int(os.getenv("ACTIVITY_SERVER_PORT", "8787"))

COOKIE_NAME = "admin_session"

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB -- generous for a sprite or a background, not a video

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

/* Delve flowchart editor (see admin_server.py's _render_delve_flowchart / _dynamic_script) */
form.delve-form { max-width: none; }
.delve-canvas-wrap { position: relative; overflow: auto; max-height: 640px; padding: 0; border: 1px solid #35353f; border-radius: 8px; background: #14141a; margin-bottom: 8px; max-width: calc(100vw - 640px); min-width: 320px; }
#delve-rooms-canvas { position: relative; width: 2000px; height: 1000px; }
svg.delve-arrows { position: absolute; top: 0; left: 0; width: 2000px; height: 1000px; pointer-events: none; overflow: visible; }
svg.delve-arrows text { user-select: none; }
.add-row[data-repeat-add="delve-rooms-canvas"] { position: sticky; bottom: 8px; left: 8px; z-index: 5; }
.room-wrapper { display: contents; }
.room-box { position: absolute; width: 170px; background: #26262e; border: 2px solid #45454f; border-radius: 8px; padding: 8px; cursor: grab; user-select: none; touch-action: none; z-index: 1; transition: border-color 0.1s, box-shadow 0.1s; }
.room-box.dragging { cursor: grabbing; z-index: 2; }
.room-box.selected { border-color: #e8813a; }
.room-box.is-start { box-shadow: 0 0 0 2px #40a060; }
.room-box.drop-target-hover { border-color: #e8813a; box-shadow: 0 0 0 3px rgba(232, 129, 58, 0.55); z-index: 3; }
.room-box.has-error { border-color: #c04040; box-shadow: 0 0 0 2px rgba(192, 64, 64, 0.45); }
.action-node.has-error { border-left-color: #c04040; box-shadow: 0 0 0 2px rgba(192, 64, 64, 0.45); }
.draft-tag { background: #4a3a1f; border: 1px solid #a08040; color: #f0d0a0; padding: 2px 8px; border-radius: 10px; font-size: 0.75rem; }
#draft-save-status.saving { color: #9a9aa4; }
#draft-save-status.saved { color: #6ac080; }
#draft-save-status.failed { color: #e08080; }
.room-box-header { display: flex; align-items: center; gap: 4px; }
.room-box-id { flex: 1; font-weight: bold; font-size: 0.85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.room-flag, .room-box-select { background: none; border: none; padding: 2px; font-size: 0.85rem; cursor: pointer; color: #9a9aa4; }
.room-flag.is-start { color: #40a060; }
.room-box-summary { font-size: 0.78rem; color: #9a9aa4; margin-top: 4px; }
.connector-handle { position: absolute; right: -10px; bottom: -10px; width: 20px; height: 20px; border-radius: 50%; background: #40a060; border: 3px solid #1a1a1f; cursor: crosshair; touch-action: none; z-index: 4; }
.connector-handle:hover { transform: scale(1.15); }
.connector-handle.fail { background: #c04040; }
.action-node { position: absolute; min-width: 84px; max-width: 130px; background: #1c1c26; border: 1px solid #3a3a4a; border-left: 4px solid #7a7ae0; border-radius: 5px; padding: 5px 26px 5px 8px; font-size: 0.7rem; color: #c8c8f0; z-index: 1; user-select: none; cursor: grab; touch-action: none; box-shadow: 0 1px 3px rgba(0,0,0,0.4); }
.action-node.dragging { cursor: grabbing; z-index: 2; }
.action-node:hover { border-left-color: #9a9af0; }
.action-node .connector-handle { width: 16px; height: 16px; }
.action-node .connector-handle.fail { right: 14px; }
.action-node-label { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.action-owner-line { stroke: #7a7ae0; stroke-width: 1.5; stroke-dasharray: 4,3; opacity: 0.75; }
.arrow-disconnect { opacity: 0.55; transition: opacity 0.15s; }
.arrow-disconnect:hover { opacity: 1; }
.room-detail-panel { position: fixed; top: 90px; right: 24px; width: 340px; max-height: calc(100vh - 120px); overflow-y: auto; background: #202027; border: 1px solid #45454f; border-radius: 8px; padding: 14px; z-index: 20; max-width: 340px; box-shadow: -4px 0 16px rgba(0,0,0,0.5); }
.room-detail-panel-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.live-next-tag { font-weight: normal; color: #9a9aa4; font-size: 0.78rem; margin-left: 6px; }
#tooltip-bubble { position: fixed; z-index: 999; background: #101014; color: #e8e8ec; border: 1px solid #52525e; padding: 6px 10px; border-radius: 6px; font-size: 0.78rem; max-width: 260px; display: none; pointer-events: none; box-shadow: 0 2px 8px rgba(0,0,0,0.4); }
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
#   3. Every repeatable list (effects, materials, monster_drops, delve_rooms, quest_stages,
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
    output_kind); "shop" backs an npc's shop row item_id (keyed by kind)."""
    def _choices(registry: dict) -> list[list[str]]:
        return [[item_id, f"{item_id} — {item['name']}"] for item_id, item in sorted(registry.items())]

    return {
        "recipe_output": {
            "equipment": _choices(dungeon.EQUIPMENT),
            "consumable": _choices(dungeon.CONSUMABLES),
        },
        "shop": {
            "equipment": _choices(dungeon.EQUIPMENT),
            "material": _choices(dungeon.MATERIALS),
            "consumable": _choices(dungeon.CONSUMABLES),
            "quest_item": _choices(quests.QUEST_ITEMS),
            "horse_clothes": _choices(horse_clothes.HORSE_CLOTHES),
        },
        "monster_drop": {
            # quest_only equipment (e.g. Mondor's Greasy Pencil) is excluded -- those are only
            # ever granted through a quest turn-in, never a monster's own drop table (enforced
            # again at save time by dungeon._validate_monster_drops, in case of a hand JSON edit).
            "equipment": _choices({k: v for k, v in dungeon.EQUIPMENT.items() if not v.get("quest_only")}),
            "material": _choices(dungeon.MATERIALS),
        },
        # A choice-room action's own cost -- backs its item_kind -> item_id select. Equipment isn't
        # here on purpose: costs are qty-based (db.craft_item's {item_id: qty} shape), which
        # equipped-or-stored gear doesn't fit, and nothing authored needs it as a spendable cost.
        "action_cost": {
            "material": _choices(dungeon.MATERIALS),
            "consumable": _choices(dungeon.CONSUMABLES),
            "quest_item": _choices(quests.QUEST_ITEMS),
        },
    }


def _dynamic_script() -> str:
    data_script = (
        "var TRIGGER_PARAMS_BY_TYPE = " + json.dumps(_TRIGGER_PARAMS_BY_TYPE) + ";\n"
        "var EFFECT_PARAMS_BY_TYPE = " + json.dumps(EFFECT_PARAMS_BY_TYPE) + ";\n"
        "var EFFECT_TYPE_HINTS = " + json.dumps(EFFECT_TYPE_HINTS) + ";\n"
        "var CASCADE_OPTIONS = " + json.dumps(_cascade_options()) + ";\n"
        "var DELVE_ROOM_FIELDS_BY_TYPE = " + json.dumps(_DELVE_ROOM_FIELDS_BY_TYPE) + ";\n"
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

// Wires every [data-repeat-add] button under `root` that isn't already wired -- called once at
// page load (root=document) and again on every freshly-cloned row (root=that row), since a clone
// can itself introduce a new "+ Add" button one level down (a delve room's own "+ Add monster"/
// "+ Add action" buttons, nested inside the "+ Add Room" template -- the only nested repeatables
// in this schema, see admin_server._render_room_detail_panel) that only exist from this point on
// and need their own click handler wired. ROWIDX substitution covers name/id/data-repeat-add --
// id/data-repeat-add matter once a repeatable can nest, so a newly-added room's own "+ Add
// monster" button ends up pointing at that room's own freshly-renamed container/template, not a
// colliding shared one. A nested <template>'s own content is inert to querySelectorAll run from
// an ancestor's clone (a standard DOM quirk -- template content lives in a separate document, not
// the light tree), so its own ROWIDX placeholders are left untouched until that inner template is
// itself cloned later -- the same "ROWIDX" token works at any nesting depth without collision.
function wireRepeatAdd(root) {
    root.querySelectorAll('[data-repeat-add]').forEach(function (button) {
        if (button.dataset.repeatWired) return;
        button.dataset.repeatWired = '1';
        var container = document.getElementById(button.dataset.repeatAdd);
        var template = document.getElementById(button.dataset.repeatAdd + '-template');
        var nextIndex = container.children.length;
        button.addEventListener('click', function () {
            var clone = template.content.cloneNode(true);
            clone.querySelectorAll('[name]').forEach(function (el) {
                el.name = el.name.replace(/ROWIDX/g, String(nextIndex));
            });
            clone.querySelectorAll('[id]').forEach(function (el) {
                el.id = el.id.replace(/ROWIDX/g, String(nextIndex));
            });
            clone.querySelectorAll('[data-repeat-add]').forEach(function (el) {
                el.dataset.repeatAdd = el.dataset.repeatAdd.replace(/ROWIDX/g, String(nextIndex));
            });
            container.appendChild(clone);
            wireTriggerSelects(container.lastElementChild);
            wireEffectSelects(container.lastElementChild);
            wireCommandKindSelects(container.lastElementChild);
            wireEquipmentTriggerSelects(container.lastElementChild);
            wireCascadingSelects(container.lastElementChild);
            wireImagePreviews(container.lastElementChild);
            wireRoomTypeSelects(container.lastElementChild);
            wireRepeatAdd(container.lastElementChild);
            // Flowchart-only hooks -- no-ops everywhere else (window.wireFlowchartNode only
            // exists on a delve's edit page, see the flowchart script below). Handles both a
            // freshly-cloned room (a .room-wrapper straight from "+ Add Room") and a freshly-
            // cloned monster/action row nested inside an already-open room's detail panel.
            if (window.wireFlowchartNode) window.wireFlowchartNode(container.lastElementChild);
            if (window.refreshRoomBoxIfNested) window.refreshRoomBoxIfNested(container.lastElementChild);
            if (window.__scheduleDelveAutosave) window.__scheduleDelveAutosave();
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
wireRepeatAdd(document);

document.addEventListener('click', function (event) {
    if (event.target.matches('[data-remove-row]')) {
        var group = event.target.closest('.row-group');
        var wrapper = group.closest('.room-wrapper');
        group.remove();
        if (wrapper && window.refreshRoomBoxIfNested) window.refreshRoomBoxIfNested(wrapper);
        if (window.__scheduleDelveAutosave) window.__scheduleDelveAutosave();
    } else if (event.target.matches('[data-remove-room]')) {
        event.target.closest('.room-wrapper').remove();
        if (window.__redrawDelveArrows) window.__redrawDelveArrows();
        if (window.__scheduleDelveAutosave) window.__scheduleDelveAutosave();
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

    // Action nodes are freely draggable, same shape as wireBoxDrag but simpler (no click-to-select
    // filtering beyond the connector handles, and no ownership of anything else that needs to move
    // with it) -- position persists directly on the action's own hidden x/y inputs, not derived
    // from its parent room, so dragging a room no longer drags its actions along with it.
    function wireActionNodeDrag(node) {
        if (node.dataset.dragWired) return;
        node.dataset.dragWired = '1';
        node.addEventListener('pointerdown', function (e) {
            if (e.target.closest('[data-connector-role]')) return;
            node.setPointerCapture(e.pointerId);
            node.classList.add('dragging');
            var xInput = node.querySelector('.action-x-input');
            var yInput = node.querySelector('.action-y-input');
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
            var hasCheck = !!(statInput && dcInput && statInput.value && dcInput.value);
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
            wireActionNodeDrag(node);
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
                handle.dataset.tooltip = 'Drag onto another room -- where the player goes after winning the fight here. Drop on empty canvas to disconnect.';
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
        } else {
            var existingHandle = box.querySelector('.connector-handle');
            if (existingHandle) existingHandle.remove();
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
        canvas.querySelectorAll('.room-next-input, input[name$="_success_next"], input[name$="_fail_next"]').forEach(function (input) {
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

    function wireFlowchartNode(node) {
        if (!node || !node.classList || !node.classList.contains('room-wrapper')) return;
        var box = node.querySelector('.room-box');
        wireBoxDrag(box);
        node.querySelectorAll('[data-connector-role]').forEach(wireConnectorHandle);
        node.querySelectorAll('.action-node').forEach(function (actionNode) {
            wireActionNodeClick(actionNode, node);
            wireActionNodeDrag(actionNode);
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
        refreshRoomBox(node);
    }

    function refreshRoomBoxIfNested(el) {
        var wrapper = el && el.closest ? el.closest('.room-wrapper') : null;
        if (!wrapper) return;
        wireActionSyncInputs(el, wrapper);
        syncActionNodes(wrapper);
        refreshRoomBox(wrapper);
    }

    // Draft autosave -- a delve's edit form has no "Save" button at all (see edit_view/
    // delve_autosave_view); every meaningful change instead debounces a background POST that
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
        var itemId = form.dataset.delveItemId;
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
                    form.dataset.delveItemId = data.id;
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
    window.__scheduleDelveAutosave = scheduleAutosave;

    window.wireFlowchartNode = wireFlowchartNode;
    window.refreshRoomBoxIfNested = refreshRoomBoxIfNested;
    window.__redrawDelveArrows = redrawAllArrows;

    canvas.querySelectorAll('.room-wrapper').forEach(wireFlowchartNode);
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
    return (
        f'<nav class="sidebar"><a class="brand" href="/">🛠️ Content Editor</a>'
        f'{assets_link}{utilities_link}{player_debug_link}{"".join(sections)}</nav>'
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
    "quest_stages" list.

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
            # param name can appear in more than one row on a "quest_stages" page.
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
    quest_stages): the existing rows, a hidden <template> row the page script clones on "+ Add",
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
    _render_stage_row for why `prefix` is sometimes a "ROWIDX" placeholder.

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
    trigger_html = ""
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
    return (
        f'<div class="row-group">'
        f'<label>type<select name="{prefix}_type" class="effect-type-select">{type_options}</select></label>'
        f'{trigger_html}'
        f'<small class="field-hint effect-hint"></small>'
        f'{param_inputs}<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
    )


def _render_monster_skill_row(prefix: str, skill: dict) -> str:
    """One entry in a monster's own "skills" repeatable -- name + chance (a relative WEIGHT
    against the monster's own attack_chance and every other skill's own chance, see
    dungeon.pick_monster_action -- NOT a 0-1 probability, so e.g. two skills both at chance=1 with
    attack_chance=1 split evenly three ways) plus its own nested effects repeatable, one level
    deeper than the monster's top-level "drops"/"groups"-shaped fields -- same nesting-depth-
    agnostic wireRepeatAdd/ROWIDX machinery every other nested repeatable in this admin panel
    already relies on (rooms -> room -> groups -> group -> monster is the deepest existing
    precedent). Effects here have full parity with a player skill/consumable's own effect
    vocabulary (dungeon.py's module comment above _validate_monster_skill), so this reuses
    _render_effect_row with its default (unrestricted) allowed_types."""
    effects_container = f"{prefix}_effects"
    effects = list(skill.get("effects") or [])
    effect_rows_html = [
        _render_effect_row(f"{effects_container}_{i}", e) for i, e in enumerate(effects)
    ]
    effect_template_html = _render_effect_row(f"{effects_container}_ROWIDX", {})
    effects_repeatable = _render_repeatable(effects_container, effect_rows_html, effect_template_html, "+ Add effect")
    return (
        f'<fieldset class="row-group"><legend>Skill</legend>'
        f'<label>name<input type="text" name="{prefix}_name" value="{html.escape(skill.get("name", ""))}"></label>'
        f'<label>chance (weight)<input type="number" step="any" min="0" name="{prefix}_chance" '
        f'value="{skill.get("chance", "")}"></label>'
        f'<small class="field-hint">A relative weight against this monster\'s own attack_chance '
        f'and its other skills\' chances -- not a 0-1 probability. Higher = more likely relative '
        f'to the others.</small>'
        f'<label class="checkbox-label"><input type="checkbox" name="{prefix}_special"'
        f'{" checked" if skill.get("special") else ""}> Special (rolls SpAtk/SpDef instead of ATK/DEF)</label>'
        f'<div>{effects_repeatable}</div>'
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
    (not a top-level sibling field, same reasoning as _render_shop_row)."""
    kind = drop.get("kind")
    kind_options = "".join(
        f'<option value="{k}"{" selected" if k == kind else ""}>{k}</option>'
        for k in [""] + list(dungeon.DROP_KINDS)
    )
    item_select = _render_cascaded_select(f"{prefix}_item_id", "monster_drop", kind, drop.get("item_id"))
    return (
        f'<div class="row-group">'
        f'<label>kind<select name="{prefix}_kind" class="cascade-select" data-cascade="monster_drop">{kind_options}</select></label>'
        f'<label>item_id{item_select}</label>'
        f'<label>chance (0-1)<input type="number" min="0" max="1" step="any" name="{prefix}_chance" '
        f'value="{drop.get("chance", "")}"></label>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
    )


def _render_image_input(name: str, label: str, value: str | None) -> str:
    """The <label>+preview+file-input markup for one image upload -- shared by _render_field's
    top-level "image" case and _render_room_detail_panel's per-room background field, since both
    need the exact same live-preview wiring (see wireImagePreviews) and "keep existing on no
    upload" semantics (see _save_uploaded_image / _parse_delve_flowchart)."""
    preview_id = f"preview_{name}"
    src = f"/{value}" if value else ""
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


def _render_room_monster_group_row(prefix: str, group: list[str]) -> str:
    """One monster GROUP within a combat room's own repeatable "groups" list -- a group is every
    monster that spawns simultaneously as one encounter (a group of one is an ordinary
    single-monster fight; see dungeon.py's module docstring). Nests _render_room_monster_row's own
    repeatable one level deeper than before (room -> groups -> group -> monster), reusing the exact
    same wireRepeatAdd/ROWIDX machinery already proven nesting-depth-agnostic by _render_action_row
    (rooms -> room -> actions) -- no JS changes needed. Also carries "row-group" so the page's
    generic remove-row handler (`event.target.closest('.row-group')`) removes the whole group when
    its own remove button is clicked, same as it already removes just one nested monster row when
    *that* row's own remove button is clicked instead -- DOM proximity alone disambiguates which
    level a given remove click means, no extra JS needed here either. See
    _render_room_detail_panel below and _parse_delve_flowchart's matching parse side."""
    monsters_container = f"{prefix}_monsters"
    monster_rows_html = [_render_room_monster_row(f"{monsters_container}_{j}", mid) for j, mid in enumerate(group)]
    monster_template_html = _render_room_monster_row(f"{monsters_container}_ROWIDX", None)
    monsters_repeatable = _render_repeatable(monsters_container, monster_rows_html, monster_template_html, "+ Add monster")
    return (
        f'<fieldset class="row-group monster-group" data-monster-group>'
        f'<legend>Group</legend>{monsters_repeatable}'
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
    the saved action depends only on whether its stat+dc were actually filled in (see
    _parse_actions), the same "blank means absent" convention every other optional field in this
    panel already follows; this action's own on-canvas node (_render_action_node) only grows a
    second "fail" connector handle once both are filled (see the flowchart script's
    syncActionNodes)."""
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
        f'<fieldset data-tooltip="Rolls this action against the character\'s own stat. Add a check '
        f'to make this one action branch by luck, on top of (not instead of) branching by which '
        f'action the player picks."><legend>check (optional -- scales against the character\'s own stat)</legend>'
        f'<label>stat<select name="{prefix}_check_stat" class="action-check-input">{stat_options}</select></label>'
        f'<label>dc<input type="number" min="1" name="{prefix}_check_dc" class="action-check-input" value="{check.get("dc", "")}"></label>'
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
        f'</fieldset>'
        f'<fieldset data-tooltip="Only used once a check is set above. Where this action leads if '
        f'the roll fails -- drag the action\'s red handle on the canvas to set it.">'
        f'<legend>on_fail (only used if check is set) <span class="live-next-tag" data-live-next="{prefix}_fail_next">'
        f'{html.escape("→ " + on_fail.get("next", "")) if on_fail.get("next") else "→ (wins the delve)"}'
        f'</span></legend>'
        f'{_next_hidden("fail_next", on_fail.get("next"))}'
        f'<label>hp_delta (optional)<input type="number" name="{prefix}_fail_hp_delta" value="{on_fail.get("hp_delta", "")}"></label>'
        f'<label>message (optional){_text("fail_message", on_fail.get("message"))}</label>'
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
    has_check = bool(check.get("stat") and check.get("dc"))
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
        monster_count = sum(len(g) for g in groups)
        # Best-effort level-equivalent range across the room's candidate groups (see
        # dungeon.estimate_group_level) -- skips any not-yet-known monster id (an in-progress,
        # unsaved edit can reference one) rather than erroring, same "display, don't validate"
        # spirit as the rest of this box.
        group_levels = [
            dungeon.estimate_group_level([dungeon.MONSTERS[mid] for mid in g if mid in dungeon.MONSTERS])
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
            f'fight here. Drop on empty canvas to disconnect (that makes this room the end of the '
            f'delve, a win)."></div>'
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
    group_template_html = _render_room_monster_group_row(f"{groups_container}_ROWIDX", [])
    groups_repeatable = _render_repeatable(groups_container, group_rows_html, group_template_html, "+ Add group")

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
        f'picked at random each visit; every monster within a group spawns together as one '
        f'simultaneous encounter. A group of one monster is an ordinary single-monster '
        f'fight.</small></label>{groups_repeatable}</div>'
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


def _render_room_node(
    prefix: str, room: dict, pos: dict, is_start: bool,
    room_errors: list[str] | None = None, action_errors: dict[int, list[str]] | None = None,
) -> str:
    """Box + action nodes (choice rooms only) + detail panel for one room, wrapped in a single
    .room-wrapper -- the unit _render_repeatable's existing <template>/ROWIDX clone mechanism (see
    wireRepeatAdd) operates on, so "+ Add Room" keeps working with zero changes to that shared
    primitive. Action nodes are siblings of the room's own box (not nested inside it) so each gets
    its own independent position in the same canvas coordinate space -- see _render_action_node.

    `room_errors`/`action_errors` are this one room's slice of a delve-wide problem map (see
    _group_delve_problems) -- action_errors is keyed by action index within this room."""
    action_nodes_html = ""
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
    return (
        f'<div class="room-wrapper" data-room-wrapper="{prefix}">'
        f'{_render_room_box(prefix, room, pos, is_start, room_errors)}'
        f'{action_nodes_html}'
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


def _render_stage_row(prefix: str, stage: dict) -> str:
    """One row of a "quest_stages" list -- shared by real rows (prefix like "stage_0") and the
    blank <template> row (prefix "stage_ROWIDX") that "+ Add stage" clones. See _parse_field's
    "quest_stages" case for the matching parse side."""
    equipment_ids = sorted(dungeon.EQUIPMENT.keys())
    reward_item = stage.get("reward_item")
    reward_item_options = "".join(
        f'<option value="{item_id}"{" selected" if item_id == reward_item else ""}>{item_id}</option>'
        for item_id in [""] + equipment_ids
    )
    return (
        f'<div class="row-group">'
        f'<label>prompt<textarea name="{prefix}_prompt">{html.escape(stage.get("prompt", ""))}</textarea></label>'
        f'{_render_trigger_inputs(f"{prefix}_trigger", stage.get("trigger") or {})}'
        f'<label>on_complete_message<textarea name="{prefix}_message">'
        f'{html.escape(stage.get("on_complete_message", ""))}</textarea></label>'
        f'<label>reward<input type="number" min="0" name="{prefix}_reward" value="{stage.get("reward", "")}"></label>'
        f'<label>reward_item<select name="{prefix}_reward_item">{reward_item_options}</select></label>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove stage</button>'
        f'</div>'
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
        f'<label>key<select name="{prefix}_key">{key_options}</select></label>'
        f'<label>kind<select name="{prefix}_kind" class="command-kind-select">{kind_options}</select></label>'
        f'<label>label<input type="text" name="{prefix}_label" value="{html.escape(command.get("label", ""))}"></label>'
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

    if ftype in ("str", "int", "color"):
        input_type = {"str": "text", "int": "number", "color": "color"}[ftype]
        v = "" if value is None else value
        return (
            f'<label>{label}<input type="{input_type}" name="{html.escape(name)}" '
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
        raw_choices = field["choices"]() if callable(field["choices"]) else field["choices"]
        choices = raw_choices if field.get("required", True) else [""] + list(raw_choices)
        options = "".join(
            f'<option value="{html.escape(c)}"{" selected" if c == (value or "") else ""}>'
            f'{html.escape(c) if c else "—"}</option>'
            for c in choices
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

    if ftype == "effects":
        effects = list(value or [])
        rows_html = [_render_effect_row(f"effect_{i}", e) for i, e in enumerate(effects)]
        template_html = _render_effect_row("effect_ROWIDX", {})
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add effect")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "monster_skills":
        skills = list(value or [])
        rows_html = [_render_monster_skill_row(f"skill_{i}", s) for i, s in enumerate(skills)]
        template_html = _render_monster_skill_row("skill_ROWIDX", {})
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add skill")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

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

    if ftype == "quest_stages":
        # A row's index is a placeholder ("ROWIDX") in the template rather than a real number --
        # the page script clones it and substitutes a real, ever-increasing index in every cloned
        # name attribute each time "+ Add" is clicked. Never reused, so removing a row and adding
        # another can't collide with an index still present in the form (see every _parse_field
        # case below that discovers indices via regex rather than assuming they're contiguous).
        stages = list(value or [])
        rows_html = [_render_stage_row(f"stage_{i}", stage) for i, stage in enumerate(stages)]
        template_html = _render_stage_row("stage_ROWIDX", {})
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add stage")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    raise ValueError(f"admin_schemas.py: unknown field type {ftype!r}")


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
    render inside their own labeled <fieldset>) just flow without a heading."""
    parts = []
    last_group = None
    for field in fields:
        group = field.get("group")
        if group and group != last_group:
            parts.append(f'<div class="field-group-heading">{html.escape(group)}</div>')
        last_group = group
        parts.append(_render_field_with_hint(field, entry.get(field["name"]), entry, problems))
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
        effects.append(effect)
    return effects


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

    if ftype == "enum":
        # Same blank-is-always-omitted reasoning as "str" above -- matters most for an optional
        # enum (e.g. a room's "specialization"), where a blank selection must come through as
        # "key absent", not a "" value the real loader would reject as an unknown choice.
        v = form.get(name, "").strip()
        return (name, v) if v else None

    if ftype == "equipment_effects":
        return (name, _parse_equipment_effects(form))

    if ftype == "effects":
        return (name, _parse_effects_list("effect", form))

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
            special = f"{prefix}_special" in form
            if effects:  # a skill with no effects yet isn't meaningful to save -- drop it silently
                skills.append({"name": skill_name, "chance": chance, "effects": effects, "special": special})
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
                drops.append({"kind": kind, "item_id": item_id, "chance": float(chance)})
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

    if ftype == "quest_stages":
        # Indices aren't contiguous from 0 any more -- rows can be added/removed client-side in
        # any order (see _render_field's "quest_stages" case) -- so this discovers whatever
        # stage_<N>_prompt keys actually made it into the submission instead of walking 0, 1, 2...
        # until one's missing.
        indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(r"stage_(\d+)_prompt", k)))
        stages = []
        for i in indices:
            prefix = f"stage_{i}"
            prompt = form.get(f"{prefix}_prompt", "").strip()
            if not prompt:
                continue
            stage = {"prompt": prompt}
            trigger = _parse_trigger(f"{prefix}_trigger", form)
            if trigger is not None:
                stage["trigger"] = trigger
            message = form.get(f"{prefix}_message", "").strip()
            if message:
                stage["on_complete_message"] = message
            reward = form.get(f"{prefix}_reward", "").strip()
            if reward and int(reward) != 0:
                stage["reward"] = int(reward)
            reward_item = form.get(f"{prefix}_reward_item", "").strip()
            if reward_item:
                stage["reward_item"] = reward_item
            stages.append(stage)
        return (name, stages)

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
            if not (key and kind and command_label):
                continue
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
    """Commits `path`'s new content as its own git commit, right after a successful save/publish/
    delete -- every real content edit through this admin panel becomes its own checkpoint instead
    of piling up as uncommitted working-tree state indefinitely (these JSON files are edited live,
    constantly, exactly the kind of continuously-changing production data that's most exposed by
    sitting uncommitted -- see CLAUDE.md/git history for why this exists). Best-effort and silent:
    never raises and never blocks a save from succeeding -- a git failure here (no repo, nothing
    actually changed, a lock held by something else) is a missed checkpoint, not a reason to
    reject content an admin just successfully validated and wrote."""
    repo_dir = os.path.dirname(__file__)
    try:
        subprocess.run(["git", "add", "--", path], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"content: save {os.path.basename(path)} via admin panel", "--", path],
            cwd=repo_dir, capture_output=True,
        )
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
        parsed = _parse_field(field, form)
        if parsed is not None:
            new_entry[parsed[0]] = parsed[1]
    return new_entry, fatal_errors, soft_errors


def _parse_outcome(prefix: str, form: dict) -> dict:
    """An action's on_success/on_fail -- next/hp_delta/message, each omitted (not written as an
    empty string / null) if left blank, same "blank means absent" convention every other optional
    field here follows."""
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
    stat+dc were both actually filled in -- see _render_action_row's docstring for why this isn't
    toggled by JS instead."""
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
        has_check = bool(stat and dc)
        if has_check:
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
    no non-blank monster left in it is dropped entirely rather than saved as an empty group), its
    own "next" (written by the flowchart script's drag-to-connect, not typed -- see
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
                    groups.append(monsters)
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


async def list_view(request: web.Request) -> web.Response:
    content_type = request.match_info["content_type"]
    spec = CONTENT_TYPES.get(content_type)
    if spec is None:
        raise web.HTTPNotFound()

    columns = spec["list_columns"]
    is_delve = content_type == "delves"
    # Delves get one extra column beyond their own: whether there's a draft with unpublished
    # changes, or (for a delve never published at all) whether the row IS only a draft -- see
    # dungeon.load_delve_drafts. No other content type has this two-tier draft/publish split.
    header = "".join(f"<th>{html.escape(c)}</th>" for c in columns) + ("<th></th>" if is_delve else "")
    drafts = dungeon.load_delve_drafts() if is_delve else {}
    rows = []
    for item_id, entry in getattr(spec["module"], spec["registry_attr"]).items():
        cells = "".join(f"<td>{html.escape(str(entry.get(c, '')))}</td>" for c in columns)
        if is_delve:
            has_unpublished = item_id in drafts and drafts[item_id] != entry
            status = (
                '<span class="draft-tag" data-tooltip="Has unpublished draft changes.">Draft</span>'
                if has_unpublished else ""
            )
            cells += f"<td>{status}</td>"
        rows.append(f'<tr><td><a class="row-link" href="/edit/{content_type}/{item_id}">✏️</a></td>{cells}</tr>')
    if is_delve:
        live_ids = set(getattr(spec["module"], spec["registry_attr"]).keys())
        for draft_id, draft_entry in drafts.items():
            if draft_id in live_ids:
                continue
            cells = "".join(f"<td>{html.escape(str(draft_entry.get(c, '')))}</td>" for c in columns)
            cells += '<td><span class="draft-tag" data-tooltip="Never published -- only exists as a draft.">Draft</span></td>'
            rows.append(f'<tr><td><a class="row-link" href="/edit/{content_type}/{draft_id}">✏️</a></td>{cells}</tr>')

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
        f'<table id="list-table"><thead><tr><th></th>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )
    breadcrumbs = [("Home", "/"), (spec["label"], None)]
    return _html_response(_page(spec["label"], body, active=content_type, breadcrumbs=breadcrumbs))


def _apply_generate_level(content_type: str, entry: dict, query) -> dict:
    """If the monster/equipment edit page's "Generate stats for level" sub-form was just
    submitted (?generate_level=N, for monsters also ?archetype=tank/balanced/glass_cannon, and
    for equipment also ?slot=...), returns a NEW dict with hp/atk/def (monsters) or a regenerated
    set of constant effects (equipment) plus intended_level overridden by
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
        entry["slot"] = slot
        non_constant = [e for e in entry.get("effects", []) if e.get("trigger") != "constant"]
        entry["effects"] = dungeon.generate_item_constant_effects(level, slot) + non_constant
    return entry


async def edit_view(request: web.Request) -> web.Response:
    content_type = request.match_info["content_type"]
    item_id = request.match_info["item_id"]
    spec = CONTENT_TYPES.get(content_type)
    if spec is None:
        raise web.HTTPNotFound()

    is_new = item_id == "new"
    is_delve = content_type == "delves"
    registry = getattr(spec["module"], spec["registry_attr"])
    # A delve prefers its own draft over the live published version, if one exists -- that's the
    # entire point of the draft/publish split (see dungeon.save_delve_draft): reopening a delve
    # that's mid-edit shows exactly the unpublished state it was left in, not what's actually
    # live. A brand-new delve never has a draft under "new" itself (see delve_autosave_view --
    # drafts are always keyed by the delve's own real id, assigned client-side once typed).
    draft_entry = dungeon.load_delve_drafts().get(item_id) if is_delve else None
    entry = draft_entry if draft_entry is not None else ({} if is_new else registry.get(item_id))
    if entry is None and not is_new:
        raise web.HTTPNotFound()
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

            # A delve's flowchart editor no longer submits to this generic route in normal use --
            # it autosaves to a draft and only ever commits through the dedicated publish route
            # below (delve_publish_view), which is the one place "get rid of saving broken delves
            # entirely" is actually enforced. This path stays exactly as it always was for every
            # other content type, and as a harmless fallback if anything still posts here directly.
            redirect_url = (
                f"/edit/{content_type}/{new_entry.get('id', item_id)}" if is_delve
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
            '<p class="error">Publish failed — see the highlighted room(s)/action(s) below for '
            "what's still wrong. Nothing changed; your draft is untouched.</p>"
        )
    elif request.method == "GET" and request.query.get("saved"):
        error = '<p class="success">Saved.</p>'

    # A delve's own structured problem list -- always computed for display (not just on a failed
    # Publish), so reopening a still-broken draft shows its red highlights immediately rather than
    # waiting for the next autosave tick. Skipped for a genuinely blank new-delve page (entry == {}
    # is falsy) -- there's nothing to report yet and every required-field message would just be
    # noise before the author's typed anything.
    problems: list[dict] = []
    if is_delve and entry:
        other_ids = set(dungeon.DELVES.keys()) - {item_id, entry.get("id", "")}
        problems = dungeon.check_delve_problems(entry, other_ids)

    fields_html = _render_fields(spec["fields"], entry, problems)
    delete_button = (
        f'<button type="submit" form="delete-form" class="danger">Delete</button>' if not is_new else ""
    )
    crumb_label = "New" if is_new else f"Edit: {item_id}"
    # A delve's flowchart canvas needs real width to be usable, unlike every other content type's
    # form -- see .delve-canvas-wrap in _PAGE_CSS -- so this is the one place a schema field type
    # needs to reach the <form> tag itself rather than just its own rendered markup.
    form_class = " delve-form" if any(f["type"] == "delve_flowchart" for f in spec["fields"]) else ""

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
            slot_input = f'<label>slot<select name="slot">{slot_options}</select></label>'
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

    # Delves have no "Save" button at all -- edits autosave to a draft in the background (see
    # delve_autosave_view + the flowchart script's scheduleAutosave), so the only explicit action
    # left is Publish (delve_publish_view), which is also the one place a broken delve can ever be
    # rejected outright instead of silently accepted. `data-delve-item-id` is how the autosave/
    # publish JS knows what id to target -- updated client-side (no reload) the first time a
    # brand-new delve's draft gets assigned a real id.
    if is_delve:
        draft_notice = (
            '<p class="field-hint">📝 Draft — autosaving as you edit. Nothing here is live '
            "until you hit Publish.</p>" if draft_entry is not None else ""
        )
        form_html = (
            f'<form method="post" enctype="multipart/form-data" class="{form_class.strip()}" '
            f'data-delve-item-id="{html.escape(item_id)}">{fields_html}'
            f'<div class="row-group"><span id="draft-save-status" class="field-hint"></span>'
            f'<button type="submit" formaction="/edit/delves/{html.escape(item_id)}/publish">Publish</button>'
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
    {delete_button}
    """
    breadcrumbs = [("Home", "/"), (spec["label"], f"/edit/{content_type}"), (crumb_label, None)]
    return _html_response(_page(f"Edit {spec['label']}", body, active=content_type, breadcrumbs=breadcrumbs))


async def delve_autosave_view(request: web.Request) -> web.Response:
    """AJAX draft-autosave for one delve's flowchart editor (see the flowchart script's
    scheduleAutosave) -- never blocked by content problems, a draft can be arbitrarily broken,
    that's the entire point (see dungeon.save_delve_draft). The one exception is a genuinely bad
    image upload, which has no sensible partial state worth persisting, same as everywhere else
    this codebase handles uploads. Always responds with the freshly-saved entry's structured
    problem list (dungeon.check_delve_problems) so the canvas can update its red highlights
    without waiting for an explicit Publish attempt."""
    item_id = request.match_info["item_id"]
    spec = CONTENT_TYPES["delves"]
    drafts = dungeon.load_delve_drafts()
    existing = drafts.get(item_id) or dungeon.DELVES.get(item_id, {})
    form = dict(await request.post())
    entry_id_for_upload = form.get("id", "").strip() or item_id
    new_entry, fatal_errors, _soft_errors = _build_entry_from_form(spec, form, entry_id_for_upload, existing)

    if fatal_errors:
        return web.json_response({"ok": False, "error": "; ".join(fatal_errors)})

    new_id = new_entry.get("id", "").strip()
    if not new_id:
        # Nothing to key a draft by yet -- report problems (which will include the missing "id"
        # itself, via dungeon._REQUIRED_DELVE_FIELDS) without persisting anything.
        problems = dungeon.check_delve_problems(new_entry, set(dungeon.DELVES.keys()))
        return web.json_response({"ok": True, "id": None, "problems": problems})

    dungeon.save_delve_draft(new_entry)
    if item_id not in ("new", new_id) and item_id in drafts:
        # The delve's own id was renamed mid-draft -- drop the stale draft filed under its old id
        # so it doesn't linger as an orphan now that everything's keyed by the new one.
        dungeon.delete_delve_draft(item_id)

    problems = dungeon.check_delve_problems(new_entry, set(dungeon.DELVES.keys()) - {new_id})
    return web.json_response({"ok": True, "id": new_id, "problems": problems})


async def delve_publish_view(request: web.Request) -> web.Response:
    """Runs a delve draft through full validation and, only if it passes, commits it to the real
    dungeon_delves.json -- the one place a broken delve can ever be rejected outright instead of
    silently accepted (see CLAUDE.md's "content is data" section and the module docstring above).
    On failure the live file is never touched and the draft is left exactly as it was, so nothing
    is lost -- the redirect back to the edit page recomputes the same structured problems fresh
    from that untouched draft (see edit_view) to highlight what's still wrong."""
    item_id = request.match_info["item_id"]
    spec = CONTENT_TYPES["delves"]
    draft = dungeon.load_delve_drafts().get(item_id)
    if draft is None:
        # Nothing autosaved yet under this id (Publish clicked before the first autosave tick
        # landed, or with JS unavailable) -- build it fresh from whatever the form just submitted,
        # same shape every other save path uses.
        existing = dungeon.DELVES.get(item_id, {})
        form = dict(await request.post())
        entry_id_for_upload = form.get("id", "").strip() or item_id
        draft, fatal_errors, _soft = _build_entry_from_form(spec, form, entry_id_for_upload, existing)
        if fatal_errors:
            raise web.HTTPFound(f"/edit/delves/{item_id}?publish_error=1")
        dungeon.save_delve_draft(draft)

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
        dungeon.delete_delve_draft(new_id)
        if new_id != item_id:
            dungeon.delete_delve_draft(item_id)
        raise web.HTTPFound(f"/edit/delves/{new_id}?published=1")
    raise web.HTTPFound(f"/edit/delves/{new_id}?publish_error=1")


def _delete_blockers(content_type: str, item_id: str) -> list[str]:
    """Names of other entries that would be left with a dangling reference if item_id were
    deleted from `content_type`. Checked explicitly here because a delete only writes/validates
    the *one* file being changed (deleting a material doesn't re-load dungeon_recipes.json), so a
    plain re-validate-this-file-only save wouldn't catch damage done elsewhere.

    Three kinds of cross-reference exist in this content set:
      - Recipes reference materials (by key) and equipment/consumables (by output_id).
      - Monsters reference equipment/materials in their own `drops` list.
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
        recipe_blockers = [r["name"] for r in dungeon.RECIPES.values() if r["output_id"] == item_id]
        monster_blockers = []
        if content_type == "equipment":
            monster_blockers = [
                m["name"] for m in dungeon.MONSTERS.values()
                if any(d["kind"] == "equipment" and d["item_id"] == item_id for d in m.get("drops", []))
            ]
        return recipe_blockers + monster_blockers
    if content_type == "monsters":
        return [
            d["name"] for d in dungeon.DELVES.values()
            if any(
                group == [item_id]
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
        f'<tr><td><img src="/{path}" class="asset-thumb" loading="lazy"></td>'
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
    }.get(kind)
    if registry is None or item_id not in registry:
        return f"Unknown {kind or 'item'} id {item_id!r}."
    if kind == "equipment":
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

    Two independent POST forms share this one route (stats vs. grant-item), told apart by a hidden
    "action" field -- kept separate rather than one combined form so granting an item never also
    re-submits (and risks zeroing) the balance/energy/character fields, and vice versa."""
    bot = request.app.get("bot")
    grant_error = None

    if request.method == "POST":
        form = await request.post()
        gid, uid = int(form["guild_id"]), int(form["user_id"])
        if form.get("action") == "grant_item":
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
                await asyncio.to_thread(
                    db.set_character_progress, gid, uid,
                    int(form["level"]), int(form["xp"]), int(form["current_hp"]),
                )
            raise web.HTTPFound(f"/player-debug?guild_id={gid}&user_id={uid}&saved=1")

    # On the grant-item error fall-through above, guild_id/user_id came from the POSTed form, not
    # the query string (there was no redirect) -- request.query has nothing for either case that
    # didn't happen, so this picks whichever one actually did.
    guild_id = request.query.get("guild_id") or (form.get("guild_id") if request.method == "POST" else "") or ""
    user_id = request.query.get("user_id") or (form.get("user_id") if request.method == "POST" else "") or ""
    saved_notice = '<p class="success">Saved.</p>' if request.query.get("saved") else ""
    granted_notice = '<p class="success">Item granted.</p>' if request.query.get("granted") else ""

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
            balance = db.get_balance(gid, uid)
            energy = db.get_energy(gid, uid)
            character = db.get_character(gid, uid)
            if character:
                character_fields = (
                    f'<label>Level<input type="number" min="1" name="level" value="{character["level"]}"></label>'
                    f'<label>XP<input type="number" min="0" name="xp" value="{character["xp"]}"></label>'
                    f'<label>Current HP <small class="field-hint">max {character["hp"]}</small>'
                    f'<input type="number" min="0" max="{character["hp"]}" name="current_hp" '
                    f'value="{character["current_hp"]}"></label>'
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
                f'<label>Energy <small class="field-hint">0-{db.ENERGY_MAX}</small>'
                f'<input type="number" min="0" max="{db.ENERGY_MAX}" name="energy" value="{energy}"></label>'
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
    {guild_picker}
    {player_picker}
    {stats_form}
    {grant_item_form}
    """
    return _html_response(
        _page("Player Debug", body, active="player-debug", breadcrumbs=[("Home", "/"), ("Player Debug", None)])
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
    app.router.add_post("/edit/delves/{item_id}/autosave", delve_autosave_view)
    app.router.add_post("/edit/delves/{item_id}/publish", delve_publish_view)
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
