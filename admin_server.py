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

import hashlib
import hmac
import html
import json
import os
import re

from aiohttp import web
from dotenv import load_dotenv

import achievements
import db
import dungeon
import quests
import room_commands
import rooms
from admin_schemas import (
    CATEGORIES, CONTENT_TYPES, EFFECT_PARAM_NAMES, EFFECT_TYPES, TRIGGER_PARAM_HINTS,
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
}

load_dotenv()

ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD")
PORT = int(os.getenv("ACTIVITY_SERVER_PORT", "8787"))

COOKIE_NAME = "admin_session"

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB -- generous for a sprite or a background, not a video


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
button { background: #3a3a44; color: #e8e8ec; border: 1px solid #52525e; padding: 8px 16px; border-radius: 6px; cursor: pointer; }
button:hover { background: #45454f; }
.error { background: #4a1f1f; border: 1px solid #a04040; color: #f0b0b0; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; }
.danger { background: #a04040; }
.image-preview { max-width: 220px; max-height: 150px; border-radius: 6px; margin-bottom: 6px; display: block; }
.field-hint { color: #6a6a74; font-size: 0.8rem; margin: -4px 0 4px; }
.remove-row { align-self: flex-start; padding: 4px 10px; font-size: 0.85rem; }
.add-row { align-self: flex-start; }
"""

# Attaches to every image-upload input on the page (there may be several) -- picking a file
# updates its own preview <img> immediately via a local object URL, no server round-trip needed
# just to see what you picked. Vanilla JS, no new dependency, one shared script rather than
# per-field inline handlers so there's no f-string quote-escaping to get wrong.
_PREVIEW_SCRIPT = """
document.querySelectorAll('input[type=file][data-preview-target]').forEach(function (input) {
    input.addEventListener('change', function () {
        if (this.files && this.files[0]) {
            var img = document.getElementById(this.dataset.previewTarget);
            img.src = URL.createObjectURL(this.files[0]);
            img.style.display = 'block';
        }
    });
});
"""

# TRIGGER_TYPES aren't hot-reloadable data (unlike dungeon.MONSTERS etc) -- they're a fixed set of
# trigger *kinds* defined in quests.py, same footing as EFFECT_TYPES -- so it's safe to freeze this
# lookup once at import time rather than recomputing it per request.
_TRIGGER_PARAMS_BY_TYPE = {
    trigger_type: sorted(required | optional) for trigger_type, (required, optional) in quests.TRIGGER_SCHEMAS.items()
}

# Mirrors rooms.py's own _COMMAND_KINDS -- fixed regardless of content, same footing as
# EFFECT_TYPES/TRIGGER_TYPES above.
_ROOM_COMMAND_KINDS = ["none", "amount"]

# Two independent behaviors, both needed to keep a "trigger" row (and a repeatable "quest_stages"
# list) usable instead of a wall of boxes:
#   1. Every trigger row shows every param any trigger type could ever use (see
#      _render_trigger_inputs) -- most are irrelevant once you've picked a type, so this hides
#      whichever ones TRIGGER_PARAMS_BY_TYPE says don't apply to the selected type.
#   2. "quest_stages" no longer pads the form with a fixed number of blank rows -- a "+ Add stage"
#      button clones a <template> instead, and each row gets its own "Remove" button. Server-side
#      parsing (see _parse_field's "quest_stages" case) reads whatever stage_<N>_* indices are
#      actually present in the submission, so removed/added rows never need renumbering.
# Both need to (re)run on freshly-cloned rows, not just once at page load, so wireTriggerSelects is
# exported and called both up front (root=document) and again after every clone.
_DYNAMIC_SCRIPT = "var TRIGGER_PARAMS_BY_TYPE = " + json.dumps(_TRIGGER_PARAMS_BY_TYPE) + ";\n" + """
function updateTriggerVisibility(select) {
    var container = select.closest('.trigger-fields');
    var visible = TRIGGER_PARAMS_BY_TYPE[select.value] || [];
    container.querySelectorAll('[data-param]').forEach(function (label) {
        label.style.display = visible.indexOf(label.dataset.param) === -1 ? 'none' : '';
    });
}

function wireTriggerSelects(root) {
    root.querySelectorAll('select.trigger-type-select').forEach(function (select) {
        updateTriggerVisibility(select);
        select.addEventListener('change', function () { updateTriggerVisibility(select); });
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

wireTriggerSelects(document);
wireCommandKindSelects(document);

document.querySelectorAll('[data-repeat-add]').forEach(function (button) {
    var container = document.getElementById(button.dataset.repeatAdd);
    var template = document.getElementById(button.dataset.repeatAdd + '-template');
    var nextIndex = container.children.length;
    button.addEventListener('click', function () {
        var clone = template.content.cloneNode(true);
        clone.querySelectorAll('[name]').forEach(function (el) {
            el.name = el.name.replace(/ROWIDX/g, String(nextIndex));
        });
        container.appendChild(clone);
        wireTriggerSelects(container.lastElementChild);
        wireCommandKindSelects(container.lastElementChild);
        nextIndex++;
    });
});

document.addEventListener('click', function (event) {
    if (event.target.matches('[data-remove-row]')) {
        event.target.closest('.row-group').remove();
    }
});

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
    return f'<nav class="sidebar"><a class="brand" href="/">🛠️ Content Editor</a>{"".join(sections)}</nav>'


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
        f"<script>{_PREVIEW_SCRIPT}{_DYNAMIC_SCRIPT}</script></body></html>"
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
    """Shared shell for every add/remove-able repeatable field (effects, materials, tier_list,
    quest_stages): the existing rows, a hidden <template> row the page script clones on "+ Add",
    and the add button itself. Each individual row (built by a per-type row-builder like
    _render_effect_row) supplies its own "Remove" button -- see the page script's
    data-repeat-add/data-remove-row wiring, which is generic over any field using this shell."""
    return (
        f'<div id="{container_id}">{"".join(rows_html)}</div>'
        f'<template id="{container_id}-template">{template_row_html}</template>'
        f'<button type="button" class="add-row" data-repeat-add="{container_id}">{add_label}</button>'
    )


def _render_effect_row(prefix: str, effect: dict) -> str:
    """One row of an "effects" list. See _parse_field's "effects" case for the matching parse
    side, and _render_stage_row for why `prefix` is sometimes a "ROWIDX" placeholder."""
    type_options = "".join(
        f'<option value="{t}"{" selected" if t == effect.get("type") else ""}>{t}</option>'
        for t in [""] + EFFECT_TYPES
    )
    param_inputs = "".join(
        f'<label>{p}<input type="number" step="any" name="{prefix}_{p}" value="{effect.get(p, "")}"></label>'
        for p in EFFECT_PARAM_NAMES
    )
    return (
        f'<div class="row-group"><label>type<select name="{prefix}_type">{type_options}</select></label>'
        f'{param_inputs}<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
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


def _render_tier_row(name: str, tier: int | None) -> str:
    """One row of a "tier_list" (delves' room_tiers -- an ordered list, so unlike the other
    repeatable types there's no id/type half, just the one number). See _parse_field's
    "tier_list" case for the matching parse side."""
    return (
        f'<div class="row-group"><label>tier<input type="number" min="1" name="{name}" '
        f'value="{tier if tier is not None else ""}"></label>'
        f'<button type="button" class="remove-row" data-remove-row>✕ Remove</button></div>'
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


def _render_room_exit_row(prefix: str, room_id: str | None, label: str | None) -> str:
    """One row of a "room_exits" list. See _parse_field's "room_exits" case for the matching parse
    side."""
    room_ids = sorted(rooms.ROOMS.keys())
    options = "".join(
        f'<option value="{r}"{" selected" if r == room_id else ""}>{r}</option>' for r in [""] + room_ids
    )
    return (
        f'<div class="row-group"><label>room_id<select name="{prefix}_room_id">{options}</select></label>'
        f'<label>label<input type="text" name="{prefix}_label" value="{html.escape(label or "")}"></label>'
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


def _render_field(field: dict, value) -> str:
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

    if ftype == "enum":
        # An optional enum gets a blank leading option (a real <select> otherwise always defaults
        # to its first choice, which would silently pick one for a value that was actually never
        # set) -- a required one doesn't, since it never needs to represent "no value".
        choices = field["choices"] if field.get("required", True) else [""] + list(field["choices"])
        options = "".join(
            f'<option value="{html.escape(c)}"{" selected" if c == (value or "") else ""}>'
            f'{html.escape(c) if c else "—"}</option>'
            for c in choices
        )
        return f'<label>{label}<select name="{html.escape(name)}">{options}</select></label>'

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

    if ftype == "stat_bonuses":
        value = value or {}
        inputs = "".join(
            f'<label>{stat}<input type="number" name="stat_{stat}" value="{value.get(stat, 0)}"></label>'
            for stat in ("hp", "atk", "def")
        )
        return f'<fieldset><legend>{label}</legend><div class="row-group">{inputs}</div></fieldset>'

    if ftype == "effects":
        effects = list(value or [])
        rows_html = [_render_effect_row(f"effect_{i}", e) for i, e in enumerate(effects)]
        template_html = _render_effect_row("effect_ROWIDX", {})
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add effect")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "materials":
        materials = list((value or {}).items())
        rows_html = [_render_material_row(f"material_{i}", m_id, qty) for i, (m_id, qty) in enumerate(materials)]
        template_html = _render_material_row("material_ROWIDX", None, None)
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add material")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "tier_list":
        tiers = list(value or [])
        rows_html = [_render_tier_row(f"tier_{i}", t) for i, t in enumerate(tiers)]
        template_html = _render_tier_row("tier_ROWIDX", None)
        repeatable = _render_repeatable(f"{name}-rows", rows_html, template_html, "+ Add room")
        return f'<fieldset><legend>{label}</legend>{repeatable}</fieldset>'

    if ftype == "image":
        # No _parse_field case for "image" -- it needs actual file I/O and the entry's current
        # value (to keep it when no new file is uploaded), neither of which a plain form->value
        # parser has access to. edit_view handles it separately; see _save_uploaded_image.
        preview_id = f"preview_{name}"
        src = f"/{value}" if value else ""
        display = "block" if value else "none"
        return (
            f'<label>{label}'
            f'<img id="{preview_id}" class="image-preview" src="{html.escape(src)}" style="display:{display}">'
            f'<input type="file" name="{html.escape(name)}_file" accept="image/*" '
            f'data-preview-target="{preview_id}"></label>'
        )

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


def _render_field_with_hint(field: dict, value) -> str:
    """_render_field, plus that field's optional schema-level "hint" (see admin_schemas.py's
    module docstring) as small print underneath -- for a top-level box like "npc" whose meaning
    isn't obvious from its name alone. Kept separate from _render_field itself so every field
    type's branch there stays focused on just its own markup."""
    field_html = _render_field(field, value)
    hint = field.get("hint")
    if not hint:
        return field_html
    return field_html + f'<small class="field-hint">{html.escape(hint)}</small>'


def _render_fields(fields: list[dict], entry: dict) -> str:
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
        parts.append(_render_field_with_hint(field, entry.get(field["name"])))
    return "".join(parts)


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

    if ftype == "stat_bonuses":
        bonuses = {}
        for stat in ("hp", "atk", "def"):
            raw = form.get(f"stat_{stat}", "").strip()
            if raw and int(raw) != 0:
                bonuses[stat] = int(raw)
        return (name, bonuses)

    if ftype == "effects":
        # Indices aren't contiguous from 0 -- rows can be added/removed client-side in any order
        # (see _render_field's "effects" case) -- so this discovers whatever effect_<N>_type keys
        # actually made it into the submission, same approach as "quest_stages" below.
        indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(r"effect_(\d+)_type", k)))
        effects = []
        for i in indices:
            prefix = f"effect_{i}"
            effect_type = form.get(f"{prefix}_type", "").strip()
            if not effect_type:
                continue
            effect = {"type": effect_type}
            for p in EFFECT_PARAM_NAMES:
                raw = form.get(f"{prefix}_{p}", "").strip()
                if raw:
                    effect[p] = float(raw) if "." in raw else int(raw)
            effects.append(effect)
        return (name, effects)

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

    if ftype == "tier_list":
        indices = sorted(int(m.group(1)) for k in form if (m := re.fullmatch(r"tier_(\d+)", k)))
        tiers = []
        for i in indices:
            raw = form.get(f"tier_{i}", "").strip()
            if raw:
                tiers.append(int(raw))
        return (name, tiers)

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


def _write_and_validate(spec: dict, entries: list[dict]) -> str | None:
    """Writes `entries` to a temp file, validates by calling the real dungeon.py loader against
    it (plus any of this content type's own "extra_validators" -- see admin_schemas.py's "rooms"
    entry for why a loader alone sometimes can't cover everything), and only replaces the live
    JSON file (and hot-reloads the in-memory registry) if that succeeds. Returns None on success,
    or the failing validator's own error message on failure -- in which case the live file is
    untouched."""
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
    header = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    rows = []
    for item_id, entry in getattr(spec["module"], spec["registry_attr"]).items():
        cells = "".join(f"<td>{html.escape(str(entry.get(c, '')))}</td>" for c in columns)
        rows.append(f'<tr><td><a class="row-link" href="/edit/{content_type}/{item_id}">✏️</a></td>{cells}</tr>')

    singular = spec["label"][:-1] if spec["label"].endswith("s") else spec["label"]
    body = (
        f'<h1>{spec["icon"]} {html.escape(spec["label"])}</h1>'
        f'<p><a class="row-link" href="/edit/{content_type}/new">+ New {html.escape(singular)}</a></p>'
        f'<input id="list-filter" type="text" placeholder="Filter {html.escape(spec["label"].lower())}...">'
        f'<table id="list-table"><thead><tr><th></th>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )
    breadcrumbs = [("Home", "/"), (spec["label"], None)]
    return _html_response(_page(spec["label"], body, active=content_type, breadcrumbs=breadcrumbs))


async def edit_view(request: web.Request) -> web.Response:
    content_type = request.match_info["content_type"]
    item_id = request.match_info["item_id"]
    spec = CONTENT_TYPES.get(content_type)
    if spec is None:
        raise web.HTTPNotFound()

    is_new = item_id == "new"
    registry = getattr(spec["module"], spec["registry_attr"])
    entry = {} if is_new else registry.get(item_id)
    if entry is None and not is_new:
        raise web.HTTPNotFound()

    error = ""
    if request.method == "POST":
        form = dict(await request.post())
        # "id" specifically needs to be known before the image-upload fields are handled (it's
        # the filename an upload gets saved under) -- always present since every schema lists it
        # first and required, but read directly here rather than relying on loop order.
        entry_id_for_upload = form.get("id", "").strip() or item_id
        new_entry = {}
        upload_error = None
        for field in spec["fields"]:
            if field["type"] == "image":
                try:
                    new_path = _save_uploaded_image(
                        form.get(f"{field['name']}_file"), field["subdir"], entry_id_for_upload
                    )
                except ValueError as e:
                    upload_error = str(e)
                    new_path = None
                if new_path is not None:
                    new_entry[field["name"]] = new_path
                elif entry.get(field["name"]):
                    new_entry[field["name"]] = entry[field["name"]]  # no new upload -- keep what was there
                continue
            parsed = _parse_field(field, form)
            if parsed is not None:
                new_entry[parsed[0]] = parsed[1]

        if upload_error is not None:
            error = f'<p class="error">{html.escape(upload_error)}</p>'
            entry = new_entry
        else:
            entries = _load_raw_entries(spec)
            original_id = None if is_new else item_id
            replaced = False
            for i, e in enumerate(entries):
                if e.get("id") == original_id:
                    entries[i] = new_entry
                    replaced = True
                    break
            if not replaced:
                entries.append(new_entry)

            error_msg = _write_and_validate(spec, entries)
            if error_msg is None:
                raise web.HTTPFound(f"/edit/{content_type}")
            error = f'<p class="error">{html.escape(error_msg)}</p>'
            entry = new_entry  # show what they submitted, not the stale pre-edit values

    fields_html = _render_fields(spec["fields"], entry)
    delete_button = (
        f'<button type="submit" form="delete-form" class="danger">Delete</button>' if not is_new else ""
    )
    crumb_label = "New" if is_new else f"Edit: {item_id}"
    body = f"""
    <h1>{spec["icon"]} {"New" if is_new else "Edit"} {html.escape(spec["label"])}</h1>
    {error}
    <form method="post" enctype="multipart/form-data">{fields_html}<button type="submit">Save</button></form>
    {f'<form id="delete-form" method="post" action="/delete/{content_type}/{item_id}"></form>' if not is_new else ""}
    {delete_button}
    """
    breadcrumbs = [("Home", "/"), (spec["label"], f"/edit/{content_type}"), (crumb_label, None)]
    return _html_response(_page(f"Edit {spec['label']}", body, active=content_type, breadcrumbs=breadcrumbs))


def _delete_blockers(content_type: str, item_id: str) -> list[str]:
    """Names of other entries that would be left with a dangling reference if item_id were
    deleted from `content_type`. Checked explicitly here because a delete only writes/validates
    the *one* file being changed (deleting a material doesn't re-load dungeon_recipes.json), so a
    plain re-validate-this-file-only save wouldn't catch damage done elsewhere.

    Two kinds of cross-reference exist in this content set:
      - Recipes reference materials (by key) and equipment/consumables (by output_id).
      - Delves reference monster *tiers*, not individual monsters -- so deleting one monster is
        only unsafe if it's the last one at its tier that some delve's room_tiers still needs.
    """
    if content_type == "materials":
        return [r["name"] for r in dungeon.RECIPES.values() if item_id in r["materials"]]
    if content_type in ("equipment", "consumables"):
        return [r["name"] for r in dungeon.RECIPES.values() if r["output_id"] == item_id]
    if content_type == "monsters":
        tier = dungeon.MONSTERS[item_id]["tier"]
        other_monsters_at_tier = [m for m in dungeon.MONSTERS.values() if m["tier"] == tier and m["id"] != item_id]
        if other_monsters_at_tier:
            return []  # another monster still covers this tier -- safe
        return [d["name"] for d in dungeon.DELVES.values() if tier in d["room_tiers"]]
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
    app.router.add_post("/delete/{content_type}/{item_id}", delete_view)
    # Serves uploaded images (and every other file already under assets/) for the edit-form
    # previews -- stored paths already look like "assets/dungeon/monsters/x.png", so mounting the
    # static route at "/assets" makes that same string a valid URL with just a leading "/".
    # Behind auth_middleware like everything else here, so this doesn't expose anything publicly.
    app.router.add_static("/assets", ASSETS_DIR)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), port=PORT)
