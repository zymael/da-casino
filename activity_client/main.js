// Top-down RPG movement POC, flat colors/shapes only. The Discord wiring below decides *whether*
// and *when* to call startGame(); the game itself (movement, rendering, multiplayer sync) has no
// Discord awareness and also runs as an ordinary webpage against a plain WebSocket, so multiplayer
// can be tested locally with two browser tabs before ever touching Discord.
//
// The SDK is vendored locally (vendor/embedded-app-sdk.mjs, pulled from esm.sh) rather than
// imported from that CDN at runtime -- Discord's Activity iframe enforces a CSP that only allows
// requests to this app's own mapped origin (via /.proxy/) plus a small Discord allowlist, so a
// third-party CDN import gets silently blocked inside Discord even though it works fine in a
// plain browser tab. Serving it same-origin sidesteps that entirely.
import { DiscordSDK } from "./vendor/embedded-app-sdk.mjs";

const canvas = document.getElementById("game");
const ctx = canvas.getContext("2d");
const statusEl = document.getElementById("status");

// Surfaces to the status line rather than just the console -- devtools aren't always reachable
// (Discord's desktop in-app browser, mobile), so this is often the only way to actually see what
// went wrong instead of just "it stopped working."
window.addEventListener("error", (e) => {
    console.error("Uncaught error:", e.error ?? e.message);
    if (statusEl) statusEl.textContent = `Error: ${e.message}`;
});
window.addEventListener("unhandledrejection", (e) => {
    console.error("Unhandled promise rejection:", e.reason);
    if (statusEl) statusEl.textContent = `Error: ${e.reason?.message ?? e.reason}`;
});

const TILE_SIZE = 32;
const PLAYER_SIZE = 24;
const GRID_COLS = canvas.width / TILE_SIZE;
const GRID_ROWS = canvas.height / TILE_SIZE;
const MOVE_DURATION_MS = 150; // how long the tween between tiles takes -- also doubles as the
// per-step cadence when a direction key is held, since a new step only starts once the previous
// one's tween has finished.

const TILE_COLOR_A = "#33333d";
const TILE_COLOR_B = "#2b2b33";
const DEFAULT_COLOR = "#e8813a"; // used before the server's color assignment arrives
const DEFAULT_SEX = "male"; // ditto -- corrected once the server's state broadcast arrives

// LPC-style sprite sheets (see assets/CREDITS.md): 4 rows (facing up/left/down/right, in that
// order) x 9 walk-cycle frames, 64px square each. Bare "mannequin" bodies with no clothes/hair --
// gear gets layered on top of these later.
const SPRITE_FRAME_SIZE = 64;
const SPRITE_FRAMES_PER_ROW = 9;
const SPRITE_ROWS = { up: 0, left: 1, down: 2, right: 3 };
// Drawn at native resolution, deliberately -- LPC sprites are always meant to be shown larger
// than a single ground tile (that's standard for top-down RPGs: the character reads as roughly
// object-sized, not tile-sized). Scaling down from 64px by a non-integer factor (e.g. the old
// TILE_SIZE * 1.5 = 48px) made nearest-neighbor scaling drop pixels unevenly and look jagged;
// native size has zero scaling artifacts.
const SPRITE_DRAW_SIZE = SPRITE_FRAME_SIZE;
ctx.imageSmoothingEnabled = false; // keep pixel art crisp when scaling 64px frames up/down

// Explicit load/error handling (rather than just setting .src and hoping) so a failed fetch is
// visible on-screen -- devtools aren't always reachable (Discord desktop's in-app browser,
// mobile) -- and so a transient failure (a proxy/tunnel hiccup) retries instead of leaving that
// player stuck on the flat-square fallback for the rest of the session.
function loadSprite(name, src, attempt = 1) {
    const img = new Image();
    img.addEventListener("load", () => {
        console.log(`sprite loaded: ${name} (${img.naturalWidth}x${img.naturalHeight}, attempt ${attempt})`);
    });
    img.addEventListener("error", (err) => {
        console.error(`sprite failed to load: ${name} (${src}), attempt ${attempt}`, err);
        if (attempt >= 3) {
            if (statusEl) statusEl.textContent += ` [${name} sprite failed to load after ${attempt} tries]`;
            return;
        }
        setTimeout(() => {
            SPRITE_SHEETS[name] = loadSprite(name, src, attempt + 1);
        }, 500 * attempt);
    });
    img.src = src;
    return img;
}

const SPRITE_SHEETS = {
    male: loadSprite("male", "assets/sprite_male.png"),
    female: loadSprite("female", "assets/sprite_female.png"),
};

// RPG Maker 2000-style pixel fonts (vendored same-origin, see assets/CREDITS.md for license).
// Canvas text with an unloaded @font-face silently renders in a fallback font forever unless the
// load is explicitly kicked off -- fillText() alone doesn't trigger the fetch the way DOM text
// does. The rAF loop redraws every frame regardless, so once these resolve the very next frame
// just starts rendering correctly with no extra plumbing needed.
if (document.fonts) {
    Promise.all([
        document.fonts.load('14px "Press Start 2P"'),
        document.fonts.load('20px "VT323"'),
    ]).catch((err) => console.error("Pixel font load failed:", err));
}
const FONT_TITLE = '14px "Press Start 2P"';
const FONT_MENU = '9px "Press Start 2P"';
const FONT_BODY = '20px "VT323"';
const FONT_BODY_SMALL = '18px "VT323"';

const DIRECTION_VECTORS = {
    up: [0, -1],
    down: [0, 1],
    left: [-1, 0],
    right: [1, 0],
};

const KEY_TO_DIRECTION = {
    ArrowUp: "up", w: "up", W: "up",
    ArrowDown: "down", s: "down", S: "down",
    ArrowLeft: "left", a: "left", A: "left",
    ArrowRight: "right", d: "right", D: "right",
};

function tileEntity(tileX, tileY, color, username, sex) {
    return {
        tileX, tileY,
        pixelX: tileX * TILE_SIZE, pixelY: tileY * TILE_SIZE,
        animFromX: tileX * TILE_SIZE, animFromY: tileY * TILE_SIZE,
        animStart: 0, moving: false,
        facing: "down", // faces the viewer by default, like a fresh spawn should
        color, username, sex,
    };
}

const player = tileEntity(Math.floor(GRID_COLS / 2), Math.floor(GRID_ROWS / 2), DEFAULT_COLOR, "You", DEFAULT_SEX);

// Stationary blackjack dealer NPC -- reuses the same tileEntity shape (and drawEntity() renders
// it identically to a player) but never moves/animates. Distinct color so it reads as an NPC,
// not another player. Position is arbitrary/tunable -- just needs to be off the player's default
// spawn (grid center) so there's room to walk up to it.
const DEALER_COLOR = "#3ba85c";
const dealer = tileEntity(4, 4, DEALER_COLOR, "Dealer", "male");

// Other players in this same Activity instance (or, standalone, this same browser "room") --
// user_id -> entity. Populated entirely from the server's periodic full-state broadcasts.
const remotePlayers = new Map();

// Shared by keyboard and the on-screen D-pad -- both just add/remove "up"/"down"/"left"/"right"
// here, so tryStepPlayer doesn't care which input method produced them (and both can be held at
// once, e.g. touch + keyboard on a hybrid device, without conflicting).
const heldDirections = new Set();

// While a menu (currently just blackjack's command window) is active, arrow keys drive menu
// selection instead of player movement -- both read the exact same keys, so this has to be the
// first branch checked, with an early return, rather than layered alongside the movement handling.
window.addEventListener("keydown", (e) => {
    if (activeMenu) {
        if (e.key === "ArrowUp" || e.key === "w" || e.key === "W") { activeMenu.moveSelection(-1); e.preventDefault(); return; }
        if (e.key === "ArrowDown" || e.key === "s" || e.key === "S") { activeMenu.moveSelection(1); e.preventDefault(); return; }
        if (e.key === "ArrowLeft" || e.key === "a" || e.key === "A") { activeMenu.adjust(-1); e.preventDefault(); return; }
        if (e.key === "ArrowRight" || e.key === "d" || e.key === "D") { activeMenu.adjust(1); e.preventDefault(); return; }
        if (e.key === "Enter" || e.key === " " || e.key === "z" || e.key === "Z") { activeMenu.confirm(); e.preventDefault(); return; }
        if (e.key === "Escape" || e.key === "x" || e.key === "X") { closeBlackjack(); e.preventDefault(); return; }
        return;
    }
    const dir = KEY_TO_DIRECTION[e.key];
    if (dir) {
        heldDirections.add(dir);
        e.preventDefault();
        return;
    }
    if (e.key === " ") {
        e.preventDefault();
        if (isAdjacentToDealer()) openBlackjack();
    }
});
window.addEventListener("keyup", (e) => {
    const dir = KEY_TO_DIRECTION[e.key];
    if (dir) heldDirections.delete(dir);
});

function bindDpadButton(elementId, direction) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const press = (e) => {
        e.preventDefault();
        heldDirections.add(direction);
    };
    const release = (e) => {
        e.preventDefault();
        heldDirections.delete(direction);
    };
    // Pointer events cover touch, mouse, and pen with one API. pointerleave/pointercancel both
    // release too, so a finger sliding off the button (or an interrupted gesture) doesn't leave
    // it stuck "held" forever.
    el.addEventListener("pointerdown", press);
    el.addEventListener("pointerup", release);
    el.addEventListener("pointerleave", release);
    el.addEventListener("pointercancel", release);
}

bindDpadButton("dpad-up", "up");
bindDpadButton("dpad-down", "down");
bindDpadButton("dpad-left", "left");
bindDpadButton("dpad-right", "right");

// --- Dealer interaction / blackjack mode ------------------------------------------------------
// Talking to the dealer suspends normal movement and switches the canvas over to rendering the
// blackjack table instead of the game world -- same rAF loop (see frame()), just a different
// branch of what gets drawn, so no second render loop is needed.

let blackjackMode = false;

// The currently-active arrow-key-driven menu, if any -- null while walking around the world.
// Only one at a time exists today (the blackjack command window), but keying everything off this
// rather than blackjackMode directly is what lets the keydown handler above stay generic for
// whatever menu comes next (inventory, shop, etc.) instead of hardcoding blackjack into it.
// A reusable command-window widget: items are {label, enabled, onSelect, onAdjust}. Confirm
// (Enter/Z/click) invokes onSelect; left/right invoke onAdjust(±1) for stepper-style rows (e.g.
// the blackjack bet amount) that don't have a single "confirm" action. Recreated logically fresh
// each frame by the caller via setItems() (since enabled state depends on live server state), but
// the instance itself persists so selectedIndex survives across frames instead of resetting to
// the top every draw. (Rendering references FONT_MENU/UI_COLOR_*/drawWindow, all defined further
// down -- fine, since draw() only runs later inside frame(), well after the whole module has
// finished loading; only the class declaration itself needs to precede first use below.)
class Menu {
    constructor() {
        this.items = [];
        this.selectedIndex = 0;
    }

    setItems(items) {
        const prevLabel = this.items[this.selectedIndex]?.label;
        this.items = items;
        let idx = items.findIndex((it) => it.label === prevLabel && it.enabled !== false);
        if (idx === -1) idx = items.findIndex((it) => it.enabled !== false);
        this.selectedIndex = idx === -1 ? 0 : idx;
    }

    moveSelection(delta) {
        if (this.items.length === 0) return;
        let i = this.selectedIndex;
        for (let step = 0; step < this.items.length; step++) {
            i = (i + delta + this.items.length) % this.items.length;
            if (this.items[i].enabled !== false) {
                this.selectedIndex = i;
                return;
            }
        }
    }

    adjust(delta) {
        const item = this.items[this.selectedIndex];
        if (item && item.enabled !== false && item.onAdjust) item.onAdjust(delta);
    }

    confirm() {
        const item = this.items[this.selectedIndex];
        if (item && item.enabled !== false && item.onSelect) item.onSelect();
    }

    // Draws each item as one row, a "▶" cursor beside the selected (enabled) row, and registers
    // the same {x,y,w,h,enabled,onClick} hit-box shape the canvas click listener already expects
    // -- so a click both re-selects and immediately confirms that row, and arrow-key navigation
    // is additive on top of the pre-existing mouse path rather than a replacement for it.
    draw(x, y, w, rowH) {
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.font = FONT_MENU;
        this.items.forEach((item, i) => {
            const rowY = y + i * rowH;
            const enabled = item.enabled !== false;
            const selected = i === this.selectedIndex;
            ctx.fillStyle = !enabled ? "#8a7368" : selected ? UI_COLOR_GOLD : UI_COLOR_CREAM;
            if (selected) ctx.fillText("▶", x, rowY + rowH / 2);
            ctx.fillText(item.label, x + 18, rowY + rowH / 2);
            bjButtons.push({
                x, y: rowY, w, h: rowH, enabled,
                onClick: () => {
                    this.selectedIndex = i;
                    this.confirm();
                },
            });
        });
        ctx.textBaseline = "alphabetic";
    }
}

let activeMenu = null;
// True whenever the round is actively waiting on this player's own decision (their turn to hit/
// stand/double, or a pending between-hands keep/change/quit choice) -- set each frame by
// drawBlackjackUI(). closeBlackjack() checks this so Escape/X can't be used to hide the panel out
// from under a decision the table (and everyone else at it) is blocked on.
let bjMustAct = false;
const bjMenu = new Menu();

// Canvas has no native text input, so bet amount is a stepper instead of the DOM version's
// <input type="number"> -- shared across Create Table / Join / Set Bet, which is a reasonable
// simplification (there's no real reason those three would want different scratch values).
let bjBetAmount = 50;
const BJ_BET_STEP = 10;
const BJ_BET_MIN = 10;

const talkBtn = document.getElementById("talk-btn");
if (talkBtn) {
    const tryOpen = (e) => {
        e.preventDefault();
        if (!blackjackMode && isAdjacentToDealer()) openBlackjack();
    };
    talkBtn.addEventListener("pointerdown", tryOpen);
}

function openBlackjack() {
    blackjackMode = true;
    activeMenu = bjMenu;
    heldDirections.clear(); // don't resume mid-stride the instant the table closes
    connectBlackjack();
}

function closeBlackjack() {
    if (bjMustAct) return; // can't hide the panel while the table is waiting on this player's move
    // Only closes the view -- doesn't disconnect the WS or send any action, so the seat/table
    // state is exactly where you left it if you walk away and come back later.
    blackjackMode = false;
    activeMenu = null;
}

// One list, rebuilt every frame drawBlackjackUI() runs -- a lightweight immediate-mode hit-testing
// system since canvas has no native clickable elements. Menu.draw() (see the Menu class above)
// both draws each row AND registers its hit-box here; the click listener below just tests the
// latest frame's list.
let bjButtons = [];

canvas.addEventListener("click", (e) => {
    if (!blackjackMode) return;
    const rect = canvas.getBoundingClientRect();
    // cssScale (tracked below, near the label-font-sizing code) converts a CSS-space click point
    // back into the canvas's internal 640x480 coordinate space -- the same ratio already used to
    // keep player-name labels legible at any display size.
    const x = (e.clientX - rect.left) * cssScale;
    const y = (e.clientY - rect.top) * cssScale;
    for (const btn of bjButtons) {
        if (btn.enabled && x >= btn.x && x <= btn.x + btn.w && y >= btn.y && y <= btn.y + btn.h) {
            btn.onClick();
            break;
        }
    }
});

function bjAdjustBet(delta) {
    bjBetAmount = Math.max(BJ_BET_MIN, bjBetAmount + delta);
}

// --- Playing card rendering ----------------------------------------------------------------
// Ports cards_render.py's card design (the PIL renderer already used for the Discord-embed hand
// images: white rounded card, rank+suit mirrored into opposite corners, big center suit glyph,
// crosshatched blue back for hidden cards) to canvas, so both surfaces read as the same game
// instead of the Activity showing plain "A♠" text where Discord shows real card art.
const CARD_W = 38, CARD_H = 52, CARD_RADIUS = 5, CARD_GAP = 5;

// Mirrors blackjack_view.py's OUTCOME_LABELS, but in this UI's own palette/wording rather than
// reusing Discord's emoji-heavy embed text -- h.outcome is one of these 4 strings once a hand's
// round has settled (see table_view_model()'s "outcome"/"net" fields), null before that.
const BJ_OUTCOME_LABELS = { blackjack: "BLACKJACK!", win: "WIN", push: "PUSH", lose: "LOSE" };
// Hardcoded to the same values as UI_COLOR_GOLD/RED/TEAL rather than referencing those constants
// directly -- they're declared later in this file (in the windowskin section), and this object
// literal evaluates eagerly at module-load time, so referencing them here would hit the same
// const-before-declaration error the Menu class ordering bug did earlier this session.
const BJ_OUTCOME_COLORS = { blackjack: "#f3cc48", win: "#f3cc48", push: "#71a294", lose: "#d76d55" };
const CARD_RED_SUITS = new Set(["♥", "♦"]);
const CARD_COLOR_RED = "#c81e1e";
const CARD_COLOR_BLACK = "#141414";

function roundedRectPath(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
}

function drawCardBack(x, y) {
    roundedRectPath(x, y, CARD_W, CARD_H, CARD_RADIUS);
    ctx.fillStyle = "#1e3c82";
    ctx.fill();
    ctx.strokeStyle = "#0f1e46";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    ctx.save();
    roundedRectPath(x, y, CARD_W, CARD_H, CARD_RADIUS);
    ctx.clip();
    ctx.strokeStyle = "#3c5aa0";
    ctx.lineWidth = 1.5;
    for (let lx = -CARD_H; lx < CARD_W; lx += 7) {
        ctx.beginPath();
        ctx.moveTo(x + lx, y + CARD_H);
        ctx.lineTo(x + lx + CARD_H, y);
        ctx.stroke();
    }
    ctx.restore();

    const margin = 5;
    roundedRectPath(x + margin, y + margin, CARD_W - margin * 2, CARD_H - margin * 2, 3);
    ctx.strokeStyle = "#dcdcf0";
    ctx.lineWidth = 1;
    ctx.stroke();
}

function drawCard(x, y, card) {
    if (!card) {
        drawCardBack(x, y);
        return;
    }
    const color = CARD_RED_SUITS.has(card.suit) ? CARD_COLOR_RED : CARD_COLOR_BLACK;

    roundedRectPath(x, y, CARD_W, CARD_H, CARD_RADIUS);
    ctx.fillStyle = "#ffffff";
    ctx.fill();
    ctx.strokeStyle = "#3c3c3c";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    ctx.fillStyle = color;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.font = "bold 11px sans-serif";
    ctx.fillText(card.rank, x + 4, y + 3);
    ctx.font = "9px sans-serif";
    ctx.fillText(card.suit, x + 4, y + 15);

    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "22px sans-serif";
    ctx.fillText(card.suit, x + CARD_W / 2, y + CARD_H / 2 + 2);

    // Bottom-right corner mark, mirrored -- same trick cards_render.py uses (draw the same
    // top-left glyphs on their own layer, rotate 180°, composite) so it lands at the opposite
    // corner without colliding with the top-left text.
    ctx.save();
    ctx.translate(x + CARD_W, y + CARD_H);
    ctx.rotate(Math.PI);
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.font = "bold 11px sans-serif";
    ctx.fillText(card.rank, 4, 3);
    ctx.font = "9px sans-serif";
    ctx.fillText(card.suit, 4, 15);
    ctx.restore();

    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
}

// Draws a hand left-to-right starting at (x, y); a null entry (the hidden hole card) draws a
// card back, matching the WS protocol's existing null-means-hidden convention for that slot.
function drawHand(cards, x, y) {
    cards.forEach((card, i) => drawCard(x + i * (CARD_W + CARD_GAP), y, card));
}

// Draws the whole blackjack table each frame while blackjackMode is true, reading the exact same
// bjModel/localUserId the old DOM panel read (see the "Blackjack sync" section below) -- only the
// rendering target and input method changed (windowskin + arrow-key Menu instead of a DOM panel
// with mouse-only buttons), not the state or networking.
function drawBlackjackUI() {
    bjButtons = [];
    drawWindow(16, 16, 608, 448);

    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = UI_COLOR_CREAM;
    ctx.font = FONT_TITLE;
    ctx.fillText("BLACKJACK", 32, 46);

    ctx.font = FONT_BODY;

    if (bjConnectError) {
        bjMustAct = false; // nothing to break -- there's no live connection to be blocking anyone
        ctx.fillStyle = UI_COLOR_RED;
        ctx.fillText(bjConnectError, 32, 84);
        bjMenu.setItems([{ label: "✕ Leave Table", enabled: true, onSelect: closeBlackjack }]);
        bjMenu.draw(32, 420, 560, 24);
        return;
    }

    const mySeat = bjModel ? bjModel.seats.find((s) => String(s.user_id) === String(localUserId)) : null;
    const round = bjModel ? bjModel.round : null;

    // Data-only pass before any drawing: the command menu's contents (and whether "Leave Table"
    // is even offered) depend on myTurn/amPending, so those need to be known up front rather than
    // discovered mid-render.
    let myHand = null;
    let myTurn = false;
    if (round) {
        round.hands.forEach((h, i) => {
            if (String(h.user_id) === String(localUserId)) {
                myHand = h;
                myTurn = i === round.active_hand_index;
            }
        });
    }
    const pending = round ? round.between_hands_pending : null;
    const amPending = pending !== null && pending.includes(String(localUserId));
    // Whenever the round is actively waiting on this player's own decision, closing the panel
    // doesn't remove them from the table -- it just hides their controls while everyone else
    // (including the round logic itself) is stuck waiting on an action they can no longer see how
    // to take. "Leave Table" (a view-only close, no server-side quit) is only safe to offer
    // outside of that window; a real Quit is what belongs in the menu instead while it's blocking.
    // (Module-level bjMustAct, not a local const, so closeBlackjack() can see it too.)
    bjMustAct = myTurn || amPending;

    let infoY = 80;
    if (!bjModel) {
        ctx.fillStyle = UI_COLOR_CREAM;
        ctx.fillText("No table running in this channel.", 32, infoY);
        infoY += 26;
    } else {
        ctx.fillStyle = UI_COLOR_CREAM;
        ctx.fillText("Seats:", 32, infoY);
        infoY += 24;
        if (bjModel.seats.length === 0) {
            ctx.fillStyle = UI_COLOR_TEAL;
            ctx.fillText("No one seated yet.", 48, infoY);
            infoY += 24;
        }
        for (const s of bjModel.seats) {
            ctx.fillStyle = UI_COLOR_CREAM;
            ctx.fillText(`${s.name} — betting ${s.bet}${s.standing ? " (standing up)" : ""}`, 48, infoY);
            infoY += 24;
        }

        if (round) {
            infoY += 8;
            // "??" rather than a single "?" -- a lone "?" in this pixel font at small/blurry
            // sizes reads too close to "7" (confirmed live). Not the real scaling/legibility fix
            // (that's a deferred full UI pass), just a one-glyph swap to stop this specific misread.
            const dealerValue = round.dealer_hole_card_hidden ? "??" : round.dealer_value;
            ctx.fillStyle = UI_COLOR_GOLD;
            ctx.font = FONT_BODY;
            ctx.fillText(`Dealer (Value: ${dealerValue})`, 32, infoY);
            infoY += 24;
            drawHand(round.dealer_cards, 32, infoY);
            infoY += CARD_H + 14;

            // Only the dealer and the local player's own hand get real card art below -- with an
            // unbounded number of seated players, drawing every hand's cards would grow this
            // panel's height without limit and run straight into the command menu (the actual
            // "overlapping text" bug this replaced). Other players' hands still show their name,
            // value, and whose turn it is, just as a single compact line instead of a card row.
            round.hands.forEach((h, i) => {
                const active = i === round.active_hand_index;
                const isMine = String(h.user_id) === String(localUserId);
                // h.outcome is only non-null once the round has settled (see blackjack_view.py's
                // table_view_model) -- before that there's nothing to report yet.
                const resultText = h.outcome ? ` — ${BJ_OUTCOME_LABELS[h.outcome]} (${h.net >= 0 ? "+" : ""}${h.net})` : "";
                ctx.fillStyle = h.outcome ? BJ_OUTCOME_COLORS[h.outcome] : (active ? UI_COLOR_GOLD : UI_COLOR_CREAM);
                ctx.font = FONT_BODY;
                const label = `${active ? "▶ " : "  "}${h.name} (Value: ${h.value}${h.busted ? ", BUST" : ""}) — bet ${h.bet}${resultText}`;
                if (isMine) {
                    ctx.fillText(label, 32, infoY);
                    infoY += 24;
                    drawHand(h.cards, 32, infoY);
                    infoY += CARD_H + 14;
                } else {
                    ctx.fillText(label + ` [${h.cards.length} cards]`, 32, infoY);
                    infoY += 24;
                }
            });
        }
    }

    // Contextual rather than "always show all 8, just gray out the irrelevant ones" -- real card
    // art eats a lot more vertical room than the old single text line per hand did, and a command
    // window that only lists what's actually reachable from the current state (a seated player
    // mid-turn can't Join or Start anyway) is both more compact and closer to how an actual RPG
    // Maker command window behaves.
    const items = [];
    if (!bjModel) {
        items.push({ label: `Bet: ${bjBetAmount}`, enabled: true, onAdjust: (d) => bjAdjustBet(d * BJ_BET_STEP) });
        items.push({ label: "Create Table", enabled: true, onSelect: () => sendBjAction("create_table", { bet: bjBetAmount }) });
    } else if (myTurn) {
        items.push({ label: "Hit", enabled: true, onSelect: () => sendBjAction("hit", {}) });
        items.push({ label: "Stand", enabled: true, onSelect: () => sendBjAction("stand", {}) });
        if (myHand && myHand.cards.length === 2) {
            items.push({ label: "Double", enabled: true, onSelect: () => sendBjAction("double", {}) });
        }
    } else if (amPending) {
        items.push({ label: `Bet: ${bjBetAmount}`, enabled: true, onAdjust: (d) => bjAdjustBet(d * BJ_BET_STEP) });
        items.push({ label: "Keep Bet", enabled: true, onSelect: () => sendBjAction("keep_bet", {}) });
        items.push({ label: "Set Bet", enabled: true, onSelect: () => sendBjAction("set_bet", { bet: bjBetAmount }) });
        items.push({ label: "Quit", enabled: true, onSelect: () => sendBjAction("quit", {}) });
    } else if (!mySeat) {
        items.push({ label: `Bet: ${bjBetAmount}`, enabled: true, onAdjust: (d) => bjAdjustBet(d * BJ_BET_STEP) });
        items.push({ label: "Join", enabled: true, onSelect: () => sendBjAction("join", { bet: bjBetAmount }) });
    } else {
        items.push({ label: `Bet: ${bjBetAmount}`, enabled: true, onAdjust: (d) => bjAdjustBet(d * BJ_BET_STEP) });
        items.push({ label: "Set Bet", enabled: true, onSelect: () => sendBjAction("set_bet", { bet: bjBetAmount }) });
        items.push({ label: "Start", enabled: true, onSelect: () => sendBjAction("start", {}) });
        items.push({ label: "Quit", enabled: true, onSelect: () => sendBjAction("quit", {}) });
    }
    if (!bjMustAct) {
        items.push({ label: "✕ Leave Table", enabled: true, onSelect: closeBlackjack });
    }

    const rowH = 22;
    const menuY = 464 - 16 - items.length * rowH;

    let status = "";
    if (myTurn) status = "Your turn!";
    else if (amPending) status = "Round over — keep your bet, change it, or quit before the next round.";
    else if (pending && pending.length > 0) status = `Waiting on ${pending.length} player(s) to decide.`;
    if (status) {
        ctx.font = FONT_BODY_SMALL;
        ctx.fillStyle = UI_COLOR_TEAL;
        ctx.fillText(status, 32, menuY - 14);
    }

    bjMenu.setItems(items);
    bjMenu.draw(32, menuY, 560, rowH);
}

// --- RPG Maker 2000-style windowskin ----------------------------------------------------------
// Colors sampled directly from assets/ui_system.png (see CREDITS.md), but drawn procedurally
// rather than 9-sliced from that image: its frame block turned out to be a single fixed 32x32
// preview tile with a decorative crown baked right into the middle -- not generic 9-slice source
// material -- while its background fill turned out to be a plain top-to-bottom gradient, which a
// canvas gradient reproduces exactly and scales to any window size with zero stretching/seam
// artifacts a stretched raster tile would have.
const WINDOW_BG_TOP = "#783f31";
const WINDOW_BG_BOTTOM = "#421d2d";
const WINDOW_BORDER_GOLD = "#e18112";
const WINDOW_BORDER_DARK = "#391a32";
const WINDOW_CORNER_SIZE = 8;

// Text palette, also lifted from ui_system.png's color-chip rows (the classic RM2K message
// color-code / gauge-gradient swatches) rather than picked arbitrarily, so window chrome and text
// share one consistent source palette.
const UI_COLOR_CREAM = "#ffffff";
const UI_COLOR_GOLD = "#f3cc48";
const UI_COLOR_RED = "#d76d55";
const UI_COLOR_TEAL = "#71a294";

function drawWindow(x, y, w, h) {
    const grad = ctx.createLinearGradient(x, y, x, y + h);
    grad.addColorStop(0, WINDOW_BG_TOP);
    grad.addColorStop(1, WINDOW_BG_BOTTOM);
    ctx.fillStyle = grad;
    ctx.fillRect(x, y, w, h);

    ctx.lineWidth = 2;
    ctx.strokeStyle = WINDOW_BORDER_DARK;
    ctx.strokeRect(x + 1, y + 1, w - 2, h - 2);
    ctx.strokeStyle = WINDOW_BORDER_GOLD;
    ctx.strokeRect(x + 3, y + 3, w - 6, h - 6);

    // Corner accents (an L-shaped bracket, echoing the beveled corner squares in ui_system.png's
    // frame block) -- drawn once and rotated into each of the 4 corners rather than four
    // hand-mirrored copies. Works because an L with equal-length arms is symmetric under 90°
    // rotation about its own vertex: each arm always ends up tracing one of the two window edges
    // that meet at that corner, pointing inward, regardless of which corner it's rotated into.
    for (let i = 0; i < 4; i++) {
        ctx.save();
        ctx.translate(i === 1 || i === 2 ? x + w : x, i === 2 || i === 3 ? y + h : y);
        ctx.rotate((Math.PI / 2) * i);
        ctx.fillStyle = WINDOW_BORDER_GOLD;
        ctx.fillRect(0, 0, WINDOW_CORNER_SIZE, 3);
        ctx.fillRect(0, 0, 3, WINDOW_CORNER_SIZE);
        ctx.restore();
    }
}

function lerp(a, b, t) {
    return a + (b - a) * t;
}

function drawBackground() {
    for (let y = 0; y < canvas.height; y += TILE_SIZE) {
        for (let x = 0; x < canvas.width; x += TILE_SIZE) {
            const isEven = ((x / TILE_SIZE) + (y / TILE_SIZE)) % 2 === 0;
            ctx.fillStyle = isEven ? TILE_COLOR_A : TILE_COLOR_B;
            ctx.fillRect(x, y, TILE_SIZE, TILE_SIZE);
        }
    }
}

// The canvas has a fixed internal resolution (640x480) but its CSS *display* size shrinks on
// narrow viewports (style.css's max-width: 95vw) -- on a phone that can be well under half the
// internal width, so a fixed canvas-space font size that reads fine on desktop becomes tiny (in
// practice, unreadable) once scaled down. Track the ratio and size the label font by it so text
// stays roughly the same *physical* size everywhere. Cached and refreshed on resize rather than
// read every frame, since getBoundingClientRect() forces a layout.
let cssScale = 1;
function updateCssScale() {
    const rect = canvas.getBoundingClientRect();
    if (rect.width > 0) cssScale = canvas.width / rect.width;
}
window.addEventListener("resize", updateCssScale);
updateCssScale();

const BASE_LABEL_FONT_PX = 15;

function drawEntity(entity, label, now) {
    // Fall back to the male sheet for any unexpected entity.sex value (rather than the flat
    // square) so a data glitch degrades to "wrong sprite" instead of "no sprite at all."
    const sheet = SPRITE_SHEETS[entity.sex] || SPRITE_SHEETS.male;
    const dw = SPRITE_DRAW_SIZE;
    const dh = SPRITE_DRAW_SIZE;
    const dx = entity.pixelX + TILE_SIZE / 2 - dw / 2;
    const dy = entity.pixelY + TILE_SIZE - dh; // feet anchored at the tile's bottom edge -- the
    // sprite's actual top edge, used below for both drawing and label placement so they can't drift apart.

    if (sheet && sheet.complete && sheet.naturalWidth > 0) {
        const row = SPRITE_ROWS[entity.facing];
        // Idle (frame 0) when standing still; cycles through the full walk row while moving, in
        // step with the same tween progress that drives the tile-to-tile slide.
        const frame = entity.moving
            ? Math.floor(((now - entity.animStart) / MOVE_DURATION_MS) * SPRITE_FRAMES_PER_ROW) % SPRITE_FRAMES_PER_ROW
            : 0;
        ctx.drawImage(
            sheet,
            frame * SPRITE_FRAME_SIZE, row * SPRITE_FRAME_SIZE, SPRITE_FRAME_SIZE, SPRITE_FRAME_SIZE,
            dx, dy, dw, dh,
        );
    } else {
        // Sprite sheet hasn't finished loading yet -- a flat square beats an invisible player.
        ctx.fillStyle = entity.color;
        ctx.fillRect(
            entity.pixelX + (TILE_SIZE - PLAYER_SIZE) / 2,
            entity.pixelY + (TILE_SIZE - PLAYER_SIZE) / 2,
            PLAYER_SIZE, PLAYER_SIZE,
        );
    }

    const labelText = label ?? entity.username;
    const labelX = entity.pixelX + TILE_SIZE / 2;
    const labelY = dy - 2;
    ctx.font = `bold ${Math.round(BASE_LABEL_FONT_PX * cssScale)}px sans-serif`;
    ctx.textAlign = "center";
    // A dark outline keeps the label legible over both light and dark tiles/sprite pixels,
    // rather than relying on font size alone to fight low contrast.
    ctx.lineWidth = 3;
    ctx.strokeStyle = "#000000";
    ctx.strokeText(labelText, labelX, labelY);
    ctx.fillStyle = entity.color;
    ctx.fillText(labelText, labelX, labelY);
}

// One step (if any direction key is held and the last step's tween has finished) per call --
// called every frame, but naturally gated to MOVE_DURATION_MS cadence via entity.moving.
function tryStepPlayer(now) {
    if (player.moving) return;
    let dir = null;
    for (const d of heldDirections) {
        dir = d;
        break; // first-held-direction wins; no diagonals in tile movement
    }
    if (!dir) return;
    const [dx, dy] = DIRECTION_VECTORS[dir];

    const targetX = Math.max(0, Math.min(GRID_COLS - 1, player.tileX + dx));
    const targetY = Math.max(0, Math.min(GRID_ROWS - 1, player.tileY + dy));
    player.facing = dir; // turn to face the pressed direction even if the edge/dealer blocks the step
    if (targetX === player.tileX && targetY === player.tileY) return; // walked into the edge
    if (targetX === dealer.tileX && targetY === dealer.tileY) return; // the dealer is solid, not walkable

    player.animFromX = player.pixelX;
    player.animFromY = player.pixelY;
    player.tileX = targetX;
    player.tileY = targetY;
    player.animStart = now;
    player.moving = true;

    sendMove(targetX, targetY);
}

function advanceAnimation(entity, now) {
    if (!entity.moving) return;
    const t = Math.min(1, (now - entity.animStart) / MOVE_DURATION_MS);
    entity.pixelX = lerp(entity.animFromX, entity.tileX * TILE_SIZE, t);
    entity.pixelY = lerp(entity.animFromY, entity.tileY * TILE_SIZE, t);
    if (t >= 1) entity.moving = false;
}

function isAdjacentToDealer() {
    // Cardinal-only (no diagonal), matching the game's own 4-directional movement -- "adjacent"
    // means exactly one step away in a direction the player could actually walk.
    const dx = Math.abs(player.tileX - dealer.tileX);
    const dy = Math.abs(player.tileY - dealer.tileY);
    return dx + dy === 1;
}

function drawInteractPrompt() {
    ctx.font = FONT_BODY;
    ctx.textAlign = "center";
    const text = "Press Space to talk to the Dealer";
    const x = canvas.width / 2;
    const y = canvas.height - 14;
    ctx.lineWidth = 3;
    ctx.strokeStyle = "#000000";
    ctx.strokeText(text, x, y);
    ctx.fillStyle = UI_COLOR_CREAM;
    ctx.fillText(text, x, y);
}

function frame(timestamp) {
    // An uncaught exception anywhere in here would otherwise stop this function from ever
    // reaching the requestAnimationFrame(frame) call below -- permanently freezing the canvas on
    // whatever got drawn (or half-drawn) in that one bad frame, which looks exactly like "it
    // rendered fine, then silently stuck." One bad frame (e.g. a remote player's still-forming
    // state right after they join) should degrade, not kill the whole loop forever.
    try {
        if (blackjackMode) {
            drawBlackjackUI();
        } else {
            tryStepPlayer(timestamp);
            advanceAnimation(player, timestamp);
            for (const remote of remotePlayers.values()) advanceAnimation(remote, timestamp);

            drawBackground();
            drawEntity(dealer, undefined, timestamp);
            for (const remote of remotePlayers.values()) drawEntity(remote, undefined, timestamp);
            drawEntity(player, "You", timestamp);

            if (isAdjacentToDealer()) drawInteractPrompt();
        }
    } catch (err) {
        console.error("frame() error:", err);
        if (statusEl) statusEl.textContent = `Render error: ${err.message}`;
    }

    requestAnimationFrame(frame);
}

export function startGame() {
    requestAnimationFrame(frame);
}

// --- Multiplayer (WebSocket) ------------------------------------------------------------------
// Deliberately "basic": one full player-list snapshot broadcast per instance on every join/move/
// leave, no delta compression, no reconnect logic. Works identically embedded or standalone (see
// multiplayerWsUrl below), so two local browser tabs are enough to test it without Discord at all.

let ws = null;

// Discord's proxy injects instance_id (among others) into the URL when this page is loaded inside
// the Activity iframe -- it's what scopes multiplayer to "everyone who launched this same Activity
// instance." Standalone (no Discord), everyone shares a fixed room so local multi-tab testing works.
const instanceId = new URLSearchParams(window.location.search).get("instance_id") ?? "standalone";

function multiplayerWsUrl() {
    const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const path = isEmbedded ? "/.proxy/ws" : "/ws";
    return `${wsProtocol}//${window.location.host}${path}`;
}

function connectMultiplayer(userId, username) {
    ws = new WebSocket(multiplayerWsUrl());

    ws.addEventListener("open", () => {
        ws.send(JSON.stringify({
            type: "join",
            instance_id: instanceId,
            user_id: userId,
            username,
            tile_x: player.tileX,
            tile_y: player.tileY,
        }));
    });

    ws.addEventListener("message", (event) => {
        const data = JSON.parse(event.data);
        if (data.type === "state") applyRemoteState(data.players, userId);
    });

    ws.addEventListener("close", () => {
        if (statusEl) statusEl.textContent += " (multiplayer disconnected)";
    });

    ws.addEventListener("error", (err) => {
        console.error("Multiplayer WebSocket error:", err);
    });
}

function sendMove(tileX, tileY) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "move", tile_x: tileX, tile_y: tileY }));
    }
}

function applyRemoteState(players, selfUserId) {
    const seen = new Set();
    for (const p of players) {
        if (p.user_id === selfUserId) {
            // Adopt the server's canonical color/sex assignment for ourselves -- it's the same
            // deterministic-per-user_id function either way, but this keeps one source of truth.
            player.color = p.color;
            player.sex = p.sex;
            continue;
        }
        seen.add(p.user_id);
        let remote = remotePlayers.get(p.user_id);
        if (!remote) {
            remote = tileEntity(p.tile_x, p.tile_y, p.color, p.username, p.sex);
            remotePlayers.set(p.user_id, remote);
        } else {
            remote.color = p.color;
            remote.username = p.username;
            remote.sex = p.sex;
            if (remote.tileX !== p.tile_x || remote.tileY !== p.tile_y) {
                const ddx = p.tile_x - remote.tileX;
                const ddy = p.tile_y - remote.tileY;
                // Only a single-tile step can be unambiguously turned into a facing direction;
                // a bigger jump (e.g. this is the very first update we've seen for them) just
                // keeps whatever facing they already had.
                if (ddx === 1 && ddy === 0) remote.facing = "right";
                else if (ddx === -1 && ddy === 0) remote.facing = "left";
                else if (ddy === 1 && ddx === 0) remote.facing = "down";
                else if (ddy === -1 && ddx === 0) remote.facing = "up";
                remote.animFromX = remote.pixelX;
                remote.animFromY = remote.pixelY;
                remote.tileX = p.tile_x;
                remote.tileY = p.tile_y;
                remote.animStart = performance.now();
                remote.moving = true;
            }
        }
    }
    for (const id of Array.from(remotePlayers.keys())) {
        if (!seen.has(id)) remotePlayers.delete(id); // they left the instance
    }
}

// --- Discord SDK setup ---------------------------------------------------------------------

// This app's public OAuth2 Client ID (same id as the bot's Application) -- not a secret, safe to
// ship to the browser. The Client Secret never leaves activity_server.py.
const CLIENT_ID = "1538948361921499247";

// Discord's proxy injects frame_id (among others) into the URL when this page is loaded inside
// the Activity iframe -- its absence is how we tell "opened directly in a browser" (skip the SDK
// entirely) from "running inside Discord."
const isEmbedded = new URLSearchParams(window.location.search).has("frame_id");

async function setupDiscord() {
    const discordSdk = new DiscordSDK(CLIENT_ID);
    await discordSdk.ready();

    const { code } = await discordSdk.commands.authorize({
        client_id: CLIENT_ID,
        response_type: "code",
        state: "",
        prompt: "none",
        scope: ["identify"],
    });

    // /.proxy/ is Discord's own client-side proxy rewrite -- it forwards to whatever this
    // Activity's URL Mapping points at (activity_server.py's /api/token), without exposing that
    // real address to the sandboxed iframe.
    const response = await fetch("/.proxy/api/token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
    });
    const { access_token } = await response.json();

    const auth = await discordSdk.commands.authenticate({ access_token });
    if (!auth) throw new Error("authenticate() returned no auth");
    return auth;
}

async function main() {
    let userId, username;

    if (!isEmbedded) {
        userId = "guest-" + Math.random().toString(36).slice(2, 8);
        username = "Guest";
        if (statusEl) statusEl.textContent = "Running standalone (no Discord) — WASD / arrow keys to move.";
    } else {
        if (statusEl) statusEl.textContent = "Connecting to Discord...";
        try {
            const auth = await setupDiscord();
            userId = auth.user.id;
            username = auth.user.username;
            if (statusEl) statusEl.textContent = `Running in Discord as ${username} — WASD / arrow keys to move.`;
        } catch (err) {
            console.error("Discord SDK setup failed:", err);
            if (statusEl) statusEl.textContent = `Discord SDK error: ${err.message}`;
            return; // don't start the game on a broken auth handshake -- a visible error beats a silently inert canvas
        }
    }

    player.username = username;
    localUserId = userId;
    connectMultiplayer(userId, username);
    startGame();
}

// --- Blackjack sync --------------------------------------------------------------------------
// A separate WebSocket (/ws/blackjack, via /.proxy/ when embedded) synced to whichever blackjack
// table is running in the Discord channel this Activity was launched from -- channel_id is the
// same key both surfaces use. State/networking only lives here; rendering and input are entirely
// canvas-driven now (see drawBlackjackUI/bjButton above, opened via the dealer NPC), which is why
// there's no re-render call on message receipt below -- drawBlackjackUI() just reads whatever
// bjModel currently holds, fresh, every frame while blackjackMode is true.

let localUserId = null;
let bjWs = null;
let bjModel = null; // last received table_view_model() snapshot, or null if no table is running
let bjConnectError = null; // set when there's no channel_id to connect with (e.g. standalone testing)

// Discord's proxy injects channel_id into the URL for an Activity launched from a text channel
// (same param already used server-side for table lookup) -- absent when running standalone.
const bjChannelId = new URLSearchParams(window.location.search).get("channel_id");

function bjWsUrl() {
    const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const path = isEmbedded ? "/.proxy/ws/blackjack" : "/ws/blackjack";
    return `${wsProtocol}//${window.location.host}${path}`;
}

function connectBlackjack() {
    if (bjWs && (bjWs.readyState === WebSocket.OPEN || bjWs.readyState === WebSocket.CONNECTING)) return;
    if (!bjChannelId) {
        bjConnectError = "No channel_id available — blackjack sync only works launched from a Discord text channel.";
        return;
    }

    bjWs = new WebSocket(bjWsUrl());
    bjWs.addEventListener("open", () => {
        bjWs.send(JSON.stringify({ type: "join_table", channel_id: bjChannelId }));
    });
    bjWs.addEventListener("message", (event) => {
        const data = JSON.parse(event.data);
        if (data.type === "state") bjModel = data.model;
    });
    bjWs.addEventListener("error", (err) => console.error("Blackjack WebSocket error:", err));
}

function sendBjAction(action, extra) {
    if (!bjWs || bjWs.readyState !== WebSocket.OPEN || !localUserId) return;
    bjWs.send(JSON.stringify({ type: "action", action, user_id: localUserId, ...extra }));
}

main();
