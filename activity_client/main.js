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

window.addEventListener("keydown", (e) => {
    const dir = KEY_TO_DIRECTION[e.key];
    if (dir) {
        heldDirections.add(dir);
        e.preventDefault();
        return;
    }
    if (e.key === " ") {
        e.preventDefault();
        if (!blackjackMode && isAdjacentToDealer()) openBlackjack();
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
    heldDirections.clear(); // don't resume mid-stride the instant the table closes
    connectBlackjack();
}

function closeBlackjack() {
    // Only closes the view -- doesn't disconnect the WS or send any action, so the seat/table
    // state is exactly where you left it if you walk away and come back later.
    blackjackMode = false;
}

// One list, rebuilt every frame drawBlackjackUI() runs -- a lightweight immediate-mode button
// system since canvas has no native clickable elements. bjButton() both draws a button AND
// registers its hit-box; the click listener below just tests the latest frame's list.
let bjButtons = [];

function bjButton(x, y, w, h, label, enabled, onClick) {
    bjButtons.push({ x, y, w, h, enabled, onClick });
    ctx.fillStyle = enabled ? "#3a3a44" : "#2a2a30";
    ctx.fillRect(x, y, w, h);
    ctx.strokeStyle = "#52525e";
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = enabled ? "#e8e8ec" : "#6a6a74";
    ctx.font = "14px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, x + w / 2, y + h / 2);
    ctx.textBaseline = "alphabetic"; // restore the default the rest of the draw code assumes
}

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

// Draws the whole blackjack table each frame while blackjackMode is true, reading the exact same
// bjModel/localUserId the old DOM panel read (see the "Blackjack sync" section below) -- only
// the rendering target changed, not the state or networking. Read-only for now (stage 2): just
// table state + Leave Table, proving the mode-switch and one real button before stage 3 adds the
// full action set.
function bjAdjustBet(delta) {
    bjBetAmount = Math.max(BJ_BET_MIN, bjBetAmount + delta);
}

// Fixed button positions (rather than a dynamic flow layout) so buttons never jump around
// between frames just because something else became enabled/disabled -- only "Keep Bet" is ever
// fully hidden (not just disabled), matching the old DOM version's behavior of hiding it outside
// a pending between-hands decision.
const BJ_BTN_W = 92, BJ_BTN_H = 32, BJ_BTN_GAP = 6;
const BJ_ROW1_Y = 388, BJ_ROW2_Y = BJ_ROW1_Y + BJ_BTN_H + BJ_BTN_GAP;

function bjButtonX(index) {
    return 16 + index * (BJ_BTN_W + BJ_BTN_GAP);
}

function drawBlackjackUI() {
    bjButtons = [];

    ctx.fillStyle = "rgba(10, 10, 14, 0.92)";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = "#e8e8ec";
    ctx.font = "bold 18px sans-serif";
    ctx.fillText("🃏 Blackjack", 16, 30);

    ctx.font = "14px sans-serif";

    if (bjConnectError) {
        ctx.fillStyle = "#e85c5c";
        ctx.fillText(bjConnectError, 16, 60);
        bjButton(canvas.width - 140, canvas.height - 50, 120, 36, "✕ Leave Table", true, closeBlackjack);
        return;
    }

    const mySeat = bjModel ? bjModel.seats.find((s) => String(s.user_id) === String(localUserId)) : null;
    const round = bjModel ? bjModel.round : null;
    let myHand = null;
    let myTurn = false;

    if (!bjModel) {
        ctx.fillText("No table running in this channel.", 16, 60);
    } else {
        let y = 58;
        ctx.fillText("Seats:", 16, y);
        y += 20;
        if (bjModel.seats.length === 0) {
            ctx.fillText("No one seated yet.", 24, y);
            y += 18;
        }
        for (const s of bjModel.seats) {
            ctx.fillText(`${s.name} — betting ${s.bet}${s.standing ? " (standing up)" : ""}`, 24, y);
            y += 18;
        }

        if (round) {
            y += 10;
            const dealerCards = round.dealer_cards.map(cardText).join(" ");
            const dealerValue = round.dealer_hole_card_hidden ? "?" : round.dealer_value;
            ctx.fillText(`Dealer: ${dealerCards}  (Value: ${dealerValue})`, 16, y);
            y += 24;
            round.hands.forEach((h, i) => {
                if (String(h.user_id) === String(localUserId)) {
                    myHand = h;
                    myTurn = i === round.active_hand_index;
                }
                const active = i === round.active_hand_index;
                const cards = h.cards.map(cardText).join(" ");
                ctx.fillStyle = active ? "#e8813a" : "#e8e8ec";
                ctx.fillText(`${active ? "▶ " : "  "}${h.name}: ${cards}  (Value: ${h.value}${h.busted ? ", BUST" : ""})  — bet ${h.bet}`, 16, y);
                ctx.fillStyle = "#e8e8ec";
                y += 18;
            });
        }
    }

    const pending = round ? round.between_hands_pending : null;
    const amPending = pending !== null && pending.includes(String(localUserId));

    // Status line, just above the bet stepper/buttons.
    let status = "";
    if (myTurn) status = "Your turn!";
    else if (amPending) status = "Round over — keep your bet, change it, or quit before the next round.";
    else if (pending && pending.length > 0) status = `Waiting on ${pending.length} player(s) to decide.`;
    ctx.fillStyle = "#9a9aa4";
    ctx.font = "12px sans-serif";
    ctx.fillText(status, 16, BJ_ROW1_Y - 32);

    // Bet stepper
    ctx.font = "14px sans-serif";
    ctx.fillStyle = "#e8e8ec";
    bjButton(16, BJ_ROW1_Y - 26, 28, 22, "−", true, () => bjAdjustBet(-BJ_BET_STEP));
    ctx.textAlign = "center";
    ctx.fillText(`Bet: ${bjBetAmount}`, 130, BJ_ROW1_Y - 10);
    ctx.textAlign = "left";
    bjButton(220, BJ_ROW1_Y - 26, 28, 22, "+", true, () => bjAdjustBet(BJ_BET_STEP));

    if (!bjModel) {
        bjButton(bjButtonX(0), BJ_ROW1_Y, 140, BJ_BTN_H, "Create Table", true, () => sendBjAction("create_table", { bet: bjBetAmount }));
    } else {
        bjButton(bjButtonX(0), BJ_ROW1_Y, BJ_BTN_W, BJ_BTN_H, "Join", !mySeat, () => sendBjAction("join", { bet: bjBetAmount }));
        bjButton(bjButtonX(1), BJ_ROW1_Y, BJ_BTN_W, BJ_BTN_H, "Set Bet", !!mySeat, () => sendBjAction("set_bet", { bet: bjBetAmount }));
        let col = 2;
        if (amPending) {
            bjButton(bjButtonX(col), BJ_ROW1_Y, BJ_BTN_W, BJ_BTN_H, "Keep Bet", true, () => sendBjAction("keep_bet", {}));
            col += 1;
        }
        bjButton(bjButtonX(col), BJ_ROW1_Y, BJ_BTN_W, BJ_BTN_H, "Start", !!mySeat, () => sendBjAction("start", {}));
        bjButton(bjButtonX(col + 1), BJ_ROW1_Y, BJ_BTN_W, BJ_BTN_H, "Quit", !!mySeat, () => sendBjAction("quit", {}));

        bjButton(bjButtonX(0), BJ_ROW2_Y, BJ_BTN_W, BJ_BTN_H, "Hit", myTurn, () => sendBjAction("hit", {}));
        bjButton(bjButtonX(1), BJ_ROW2_Y, BJ_BTN_W, BJ_BTN_H, "Stand", myTurn, () => sendBjAction("stand", {}));
        bjButton(bjButtonX(2), BJ_ROW2_Y, BJ_BTN_W, BJ_BTN_H, "Double", myTurn && !!myHand && myHand.cards.length === 2, () => sendBjAction("double", {}));
    }

    bjButton(canvas.width - 140, canvas.height - 50, 120, 36, "✕ Leave Table", true, closeBlackjack);
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
    ctx.font = "16px sans-serif";
    ctx.textAlign = "center";
    const text = "Press Space to talk to the Dealer";
    const x = canvas.width / 2;
    const y = canvas.height - 14;
    ctx.lineWidth = 3;
    ctx.strokeStyle = "#000000";
    ctx.strokeText(text, x, y);
    ctx.fillStyle = "#e8e8ec";
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

function cardText(card) {
    return card === null ? "🂠" : `${card.rank}${card.suit}`;
}

main();
