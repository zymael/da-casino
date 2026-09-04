import io
import math
import os

from PIL import Image, ImageDraw, ImageFont, ImageSequence

# Room background is a real image (assets/dungeon/dungeon1.png), sized to WIDTH x HEIGHT so it
# drops in with no scaling. Monsters with a `sprite_path` in dungeon_monsters.json get that art
# composited in; monsters without one (or a missing file) fall back to the placeholder
# shape/color, so new monster JSON entries work immediately without needing art first.
WIDTH, HEIGHT = 500, 350
ROOM_BG_PATH = "assets/dungeon/dungeon1.png"

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_label_font = ImageFont.truetype(_FONT_PATH, 20)
_card_rank_font = ImageFont.truetype(_FONT_PATH, 17)
_card_suit_font = ImageFont.truetype(_FONT_PATH, 13)
_card_strip_label_font = ImageFont.truetype(_FONT_PATH, 14)
_card_initial_font = ImageFont.truetype(_FONT_PATH, 13)

# The dungeon-entrance banner -- a separate photo banner, not the room background above.
# Dialogue rendering itself now lives in npc_render.py (shared by every NPC/hub); this module just
# owns the path, same as ranch_render.BANNER_PATH/casino_render.BANNER_PATH.
BANNER_PATH = "assets/dungeon_banner.png"

# Fighting-game-style "FIGHT!" stinger, composited over a fresh combat room's first render (see
# render_room's fight_intro param). A plain art asset, same "missing file just means it's skipped"
# forgiveness as monster sprites -- no banner shouldn't crash a delve.
FIGHT_BANNER_PATH = "assets/dungeon/fight.png"


def _parse_color(hex_color: str) -> tuple[int, int, int, int]:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return (r, g, b, 255)


def _regular_polygon(cx: float, cy: float, radius: float, sides: int) -> list[tuple[float, float]]:
    rotation = -math.pi / 2  # point one vertex straight up
    return [
        (cx + radius * math.cos(rotation + i * (2 * math.pi / sides)),
         cy + radius * math.sin(rotation + i * (2 * math.pi / sides)))
        for i in range(sides)
    ]


_SHAPE_SIDES = {"triangle": 3, "pentagon": 5, "hexagon": 6}


def _draw_monster_shape(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float, shape: str, color: tuple):
    if shape == "circle":
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=color, outline=(0, 0, 0, 180), width=3)
        return
    sides = _SHAPE_SIDES.get(shape, 8)  # unrecognized shapes fall back to an octagon, not an error
    draw.polygon(_regular_polygon(cx, cy, radius, sides), fill=color, outline=(0, 0, 0, 180), width=3)


# Pixel-art sprites all get scaled to this height regardless of their source size, so mismatched
# source resolutions (e.g. a 60px vs a 132px export) still read as the same in-world scale.
# NEAREST keeps upscaling blocky/crisp instead of blurring the pixel art. Height-only scaling means
# a narrow/slender sprite (a humanoid standing straight) occupies far less on-screen AREA than a
# wide one (a blobby prop) at the same height, reading as "smaller" even though it isn't shorter --
# a monster's own optional sprite_scale (render_room, dungeon_monsters.json) multiplies this target
# height per-monster to compensate, rather than this constant changing for everyone.
SPRITE_HEIGHT = 140


def _load_monster_sprite(sprite_path: str, target_height: int = SPRITE_HEIGHT) -> Image.Image | None:
    if not sprite_path or not os.path.exists(sprite_path):
        return None
    sprite = Image.open(sprite_path).convert("RGBA")
    # Trim transparent canvas margin first -- render_room anchors a sprite's bottom edge to the
    # floor line, so any blank padding an artist's canvas happens to have below the character's
    # feet would otherwise render as a gap, making them look like they're floating.
    bbox = sprite.getbbox()
    if bbox:
        sprite = sprite.crop(bbox)
    scale = target_height / sprite.height
    new_size = (max(1, round(sprite.width * scale)), target_height)
    return sprite.resize(new_size, Image.NEAREST)


def _fit_frame(img: Image.Image) -> Image.Image:
    """Cover-fits one frame to exactly WIDTH x HEIGHT -- scale to fill, then center-crop, rather
    than stretching (which would distort) or leaving the frame partially empty. Shared by every
    background frame (a static image is just the one-frame case), so an animated background's
    frames all get the identical crop instead of each independently drifting."""
    if img.size != (WIDTH, HEIGHT):
        scale = max(WIDTH / img.width, HEIGHT / img.height)
        new_size = (round(img.width * scale), round(img.height * scale))
        img = img.resize(new_size, Image.LANCZOS)
        left = (img.width - WIDTH) // 2
        top = (img.height - HEIGHT) // 2
        img = img.crop((left, top, left + WIDTH, top + HEIGHT))
    return img


def _load_background_frames(background_path: str | None) -> tuple[list[Image.Image], list[int], int]:
    """The delve-specific background if it's set and the file actually exists, else the default
    -- same forgiving fallback as the old single-image loader, so a delve JSON entry with no
    background_path (or a since-deleted file) still renders instead of erroring mid-delve. Returns
    (frames, frame_durations_ms, loop_count) -- a plain image (or a single-frame GIF) comes back as
    one frame with an unused duration/loop, so render_room doesn't need two separate code paths for
    "static" vs "animated": it always composites onto every frame in this list and only decides
    PNG vs animated GIF afterward, from how many frames came back."""
    path = background_path if background_path and os.path.exists(background_path) else ROOM_BG_PATH
    with Image.open(path) as src:
        if not getattr(src, "is_animated", False):
            return [_fit_frame(src.convert("RGBA"))], [0], 0
        frames, durations = [], []
        for frame in ImageSequence.Iterator(src):
            frames.append(_fit_frame(frame.convert("RGBA")))
            durations.append(frame.info.get("duration", 100) or 100)
        loop = src.info.get("loop", 0)
        return frames, durations, loop


def _load_fight_banner() -> Image.Image | None:
    if not os.path.exists(FIGHT_BANNER_PATH):
        return None
    return Image.open(FIGHT_BANNER_PATH).convert("RGBA")


# (scale, opacity, duration_ms) keyframes for the fight-intro banner, punched over the room's own
# first composited frame (background + monsters + label, already built by render_room) rather than
# a blank canvas -- the combatants are already standing there when "FIGHT!" slams in, same beat as
# a fighting game's own intro. Oversized and faint -> overshoots slightly past full size for a
# little bounce -> holds -> fades out. The LAST keyframe (opacity 0) hands off to render_room's own
# already-built frame(s) with no banner at all, so the animation's final visible state always
# matches whatever the next per-turn render shows -- no visible seam when this stops playing.
_FIGHT_INTRO_KEYFRAMES = [
    (1.8, 0.4, 40),
    (1.35, 0.85, 45),
    (0.95, 1.0, 60),
    (1.08, 1.0, 60),
    (1.0, 1.0, 500),
    (1.0, 0.5, 70),
]


def _banner_frame(base: Image.Image, banner: Image.Image, scale: float, opacity: float) -> Image.Image:
    """One fight-intro frame -- `base` (already has background/monsters/label/turn-order strip
    composited) with `banner` scaled and alpha-faded on top, centered on the WIDTH x HEIGHT scene
    area specifically (not the taller turn-order-strip image `base` may actually be), so the banner
    never bleeds down into the card strip."""
    img = base.copy()
    size = (max(1, round(banner.width * scale)), max(1, round(banner.height * scale)))
    scaled = banner.resize(size, Image.LANCZOS)
    if opacity < 1:
        alpha = scaled.getchannel("A").point(lambda a: round(a * opacity))
        scaled.putalpha(alpha)
    x = round(WIDTH / 2 - scaled.width / 2)
    y = round(HEIGHT / 2 - scaled.height / 2)
    img.alpha_composite(scaled, (x, y))
    return img


# x-offsets from center for each living monster, keyed by how many are being drawn -- count 1 is
# byte-identical to the old single-monster centering (offset 0). Beyond 2, sprites also shrink
# (see _sprite_height_for) so a 4-wide group doesn't overlap at WIDTH=500.
_GROUP_X_OFFSETS = {
    1: [0],
    2: [-90, 90],
    3: [-150, 0, 150],
    4: [-180, -60, 60, 180],
}


def _sprite_height_for(count: int) -> int:
    return SPRITE_HEIGHT if count <= 2 else round(SPRITE_HEIGHT * 0.75)


# --- Enemy attack animations ---------------------------------------------------------------------
# A monster's own brief flourish on its attack turn -- composited the same way fight_intro is (a
# few extra GIF frames prepended, single playthrough via render_room's shared `animate_once` save
# path), except here it's one specific monster's SPRITE that moves, not an overlay. Keyed by a
# "kind" string purely so a second animation is just a new ATTACK_ANIMATIONS entry -- render_room
# doesn't care where the kind string comes from (a per-monster or per-skill field in
# dungeon_monsters.json/dungeon_skills.json would be natural additions later; every monster uses
# "tackle" for now, chosen by whichever caller passes render_room's `attack` param).


def _tackle_offsets(sprite_height: int) -> list[tuple[int, int, int]]:
    """(dx, dy, duration_ms) sprite offsets from its resting position -- a Pokemon-style tackle: a
    slow pull-back rise, then a fast snap down past resting position (the "hit"), then settling
    back. Offsets scale with the sprite's own height so the motion reads the same regardless of how
    big this particular monster is drawn (SPRITE_HEIGHT vs. a shrunk group sprite, see
    _sprite_height_for)."""
    rise = -round(sprite_height * 0.16)
    lunge = round(sprite_height * 0.07)
    return [
        (0, round(rise * 0.5), 70),
        (0, rise, 90),
        (0, round(rise * 0.2), 40),
        (0, lunge, 60),
        (0, round(lunge * 0.3), 80),
        (0, 0, 90),
    ]


ATTACK_ANIMATIONS = {"tackle": _tackle_offsets}


def _compose_frame(
    base: Image.Image, sprites: list[tuple], room_label: str, turn_order: list[dict] | None,
    offset_index: int | None = None, offset: tuple[int, int] = (0, 0),
) -> Image.Image:
    """One fully-composited room frame -- `base` (a background frame) plus every monster
    sprite/placeholder, the room label, and the optional turn-order strip. The sprite at
    `offset_index` in `sprites` (if given) is nudged by `offset` pixels first -- an attack
    animation's own frames use this to move just the attacking monster, leaving the background and
    every other monster exactly where render_room's normal (no-offset) frames put them. Shared by
    render_room's per-background-frame loop and its attack-animation frame loop so both stay in
    lockstep with each other -- there's exactly one place that knows how a "room frame" is built."""
    img = base.copy()
    cx, cy = WIDTH / 2, HEIGHT / 2 - 10
    count = len(sprites)
    for i, (sprite, monster, x_offset) in enumerate(sprites):
        dx, dy = offset if i == offset_index else (0, 0)
        mx, my = cx + x_offset + dx, cy + dy
        if sprite:
            pos = (round(mx - sprite.width / 2), round(my + 110 - sprite.height))
            img.alpha_composite(sprite, pos)
        else:
            draw = ImageDraw.Draw(img)
            radius = 60 if count <= 2 else 45
            _draw_monster_shape(draw, mx, my, radius, monster["shape"], _parse_color(monster["color"]))
    draw = ImageDraw.Draw(img)
    draw.text((16, HEIGHT - 32), room_label, font=_label_font, fill=(200, 200, 210, 255))
    if turn_order:
        img = _composite_turn_order_strip(img, turn_order)
    return img


# FFX-style turn-order card strip, composited below the room scene (see dungeon.preview_next_turns
# for the schedule these cards visualize). Fixed at a size that comfortably fits up to 10 cards --
# the largest count any call site passes -- within WIDTH, so there's no dynamic per-card resizing
# to keep in sync with font sizes.
CARD_WIDTH, CARD_HEIGHT, CARD_GAP = 42, 58, 4
INITIAL_ROW_HEIGHT = 16  # room below each card for its combatant's name-initial letter
CARD_STRIP_HEIGHT = CARD_HEIGHT + 28 + INITIAL_ROW_HEIGHT  # "Next up:" label row + card row + initial row

_RED_SUITS = {"♥", "♦"}


def _draw_player_card(draw: ImageDraw.ImageDraw, x: float, y: float, rank: str, suit: str) -> None:
    """A player/party-member card: existing rank letter (A/K/Q/J, dungeon.CLASSES[...]["rank"]) +
    suit symbol (♠♥♦♣, dungeon.subclass_entry(...)["symbol"]), suit-colored red/black per standard
    card convention -- zero new art, reuses the exact pairing that already names every build."""
    color = (190, 25, 25, 255) if suit in _RED_SUITS else (20, 20, 20, 255)
    draw.rounded_rectangle(
        [x, y, x + CARD_WIDTH, y + CARD_HEIGHT], radius=5, fill=(245, 242, 230, 255), outline=(0, 0, 0, 255), width=2
    )
    draw.text((x + 5, y + 3), rank, font=_card_rank_font, fill=color)
    suit_box = draw.textbbox((0, 0), suit, font=_card_suit_font)
    sw, sh = suit_box[2] - suit_box[0], suit_box[3] - suit_box[1]
    draw.text((x + CARD_WIDTH - sw - 6, y + CARD_HEIGHT - sh - 7), suit, font=_card_suit_font, fill=color)


def _draw_monster_card(draw: ImageDraw.ImageDraw, x: float, y: float, shape: str, color_hex: str) -> None:
    """A monster card: reuses _draw_monster_shape's own shape/color, drawn small and centered on a
    dark card outline -- the same placeholder identity already used for the room scene itself."""
    draw.rounded_rectangle(
        [x, y, x + CARD_WIDTH, y + CARD_HEIGHT], radius=5, fill=(35, 18, 18, 255), outline=(0, 0, 0, 255), width=2
    )
    cx, cy = x + CARD_WIDTH / 2, y + CARD_HEIGHT / 2 + 2
    _draw_monster_shape(draw, cx, cy, CARD_WIDTH * 0.34, shape, _parse_color(color_hex))


def _composite_turn_order_strip(img: Image.Image, turn_order: list[dict]) -> Image.Image:
    """Grows the room scene downward to fit a horizontal strip of playing-card-style icons for the
    next several turns (dungeon.preview_next_turns) -- purely cosmetic FFX flavor. Each entry is
    `{"kind": "player", "rank": ..., "suit": ..., "initial": ...}` or `{"kind": "monster", "shape":
    ..., "color": ..., "initial": ...}`; the same combatant can (and does) appear on multiple cards
    when they're fast enough to act again before others get a turn -- intended, matches FFX's own
    UI. `initial` (first letter of the combatant's own name) is printed under each card so same-rank
    party members or same-species monsters stay distinguishable at a glance. Returns a new taller
    image; doesn't mutate `img`."""
    strip = Image.new("RGBA", (WIDTH, CARD_STRIP_HEIGHT), (15, 15, 20, 255))
    draw = ImageDraw.Draw(strip)
    draw.text((8, 3), "Next up:", font=_card_strip_label_font, fill=(190, 190, 200, 255))
    n = len(turn_order)
    total_width = n * CARD_WIDTH + max(0, n - 1) * CARD_GAP
    start_x = max(6, (WIDTH - total_width) // 2)
    y = CARD_STRIP_HEIGHT - CARD_HEIGHT - 5 - INITIAL_ROW_HEIGHT
    for i, card in enumerate(turn_order):
        x = start_x + i * (CARD_WIDTH + CARD_GAP)
        if card["kind"] == "player":
            _draw_player_card(draw, x, y, card["rank"], card["suit"])
        else:
            _draw_monster_card(draw, x, y, card["shape"], card["color"])
        initial = card.get("initial", "?")
        ibox = draw.textbbox((0, 0), initial, font=_card_initial_font)
        iw = ibox[2] - ibox[0]
        draw.text(
            (x + CARD_WIDTH / 2 - iw / 2, y + CARD_HEIGHT + 2), initial,
            font=_card_initial_font, fill=(220, 220, 230, 255),
        )
    combined = Image.new("RGBA", (WIDTH, HEIGHT + CARD_STRIP_HEIGHT), (0, 0, 0, 255))
    combined.alpha_composite(img, (0, 0))
    combined.alpha_composite(strip, (0, HEIGHT))
    return combined


def render_room(
    visited_count: int, monsters: list[dict], background_path: str | None = None,
    turn_order: list[dict] | None = None, label: str | None = None, fight_intro: bool = False,
    attack: dict | None = None,
) -> tuple[io.BytesIO, str]:
    """Renders the corridor view for one dungeon room -- with its living monster(s) standing at the
    far end if there are any (combat rooms), or just the empty scene if not (choice rooms, or a
    combat room whose group has been fully cleared, pass an empty list). Combat HP/stats are shown
    as embed text by the caller, not baked into this image -- this only draws the scene.
    `visited_count` labels the room ("Room N") with no denominator, since a branching delve graph
    has no single well-defined total room count the way a flat list did -- a fork's two paths can
    have different lengths, and a room can even be revisited via a dead-end self-loop. `label`
    overrides that bottom-left text entirely (e.g. dueling's own "Duel" -- `visited_count` means
    nothing there) -- defaults to the usual "Room N" when not given. `turn_order` (optional, only
    ever passed for combat rooms with someone still alive to schedule) grows the image downward to
    add the FFX-style turn-order card strip -- see _composite_turn_order_strip for its shape.

    Returns (buf, ext) rather than a bare BytesIO -- an animated GIF background (see
    _load_background_frames) means every monster/label/turn-order overlay gets composited onto
    EVERY one of its frames, then re-encoded as its own animated GIF instead of a flat PNG, so the
    background keeps playing in Discord instead of freezing on frame one. `ext` ("png" or "gif")
    is the caller's cue for which filename/attachment extension actually matches what's in `buf` --
    see dungeon_view._room_image_file. Sprites are loaded/scaled once, not once per frame.

    `fight_intro` and `attack` are mutually exclusive one-shot animations, each of which prepends
    its own extra frames ahead of this render's normal frame(s) and forces the whole thing to save
    as a GIF with no loop count at all (not loop=0) -- every renderer including Discord takes that
    as "play once", so it settles on and stays frozen at this render's normal frame(s) with no
    visible seam versus whatever plain render the next turn sends. `fight_intro` (only ever True
    for a freshly-entered combat room -- see dungeon_view's is_room_entry threading) animates the
    FIGHT_BANNER_PATH art in/out over the scene. `attack` (`{"index": int, "kind": str}`, `index`
    into `monsters`/`sprites` -- dungeon_view resolves a MonsterInstance's combat slot to this
    positional index since render_room has no notion of slots) instead moves that one monster
    sprite through ATTACK_ANIMATIONS[kind]'s offsets, everything else held still. The two aren't
    combined (a caller passing both gets the fight_intro) -- chaining "banner, then the ambushing
    monster's attack, then settle" is a reasonable future extension, not needed for a first cut."""
    frames, durations, loop = _load_background_frames(background_path)

    count = len(monsters)
    sprite_height = _sprite_height_for(count)
    offsets = _GROUP_X_OFFSETS.get(count, _GROUP_X_OFFSETS[4])
    sprites = [
        (_load_monster_sprite(monster.get("sprite_path"), round(sprite_height * monster.get("sprite_scale", 1.0))),
         monster, x_offset)
        for monster, x_offset in zip(monsters, offsets)
    ]
    room_label = label or f"Room {visited_count}"

    out_frames = [_compose_frame(base, sprites, room_label, turn_order) for base in frames]

    animate_once = False
    if fight_intro and (banner := _load_fight_banner()) is not None:
        intro_frames = [_banner_frame(out_frames[0], banner, scale, opacity) for scale, opacity, _ in _FIGHT_INTRO_KEYFRAMES]
        intro_durations = [d for _, _, d in _FIGHT_INTRO_KEYFRAMES]
        out_frames = intro_frames + out_frames
        durations = intro_durations + [max(d, 100) for d in durations]
        animate_once = True
    elif attack:
        keyframes_fn = ATTACK_ANIMATIONS.get(attack.get("kind", "tackle"))
        idx = attack.get("index")
        if keyframes_fn is not None and idx is not None and 0 <= idx < len(sprites):
            attacker_sprite = sprites[idx][0]
            attacker_height = attacker_sprite.height if attacker_sprite else sprite_height
            keyframes = keyframes_fn(attacker_height)
            attack_frames = [
                _compose_frame(frames[0], sprites, room_label, turn_order, offset_index=idx, offset=(dx, dy))
                for dx, dy, _ in keyframes
            ]
            out_frames = attack_frames + out_frames
            durations = [dur for _, _, dur in keyframes] + [max(d, 100) for d in durations]
            animate_once = True

    buf = io.BytesIO()
    if len(out_frames) > 1:
        rgb_frames = [f.convert("RGB") for f in out_frames]
        save_kwargs = {} if animate_once else {"loop": loop}
        rgb_frames[0].save(
            buf, format="GIF", save_all=True, append_images=rgb_frames[1:],
            duration=durations, disposal=2, **save_kwargs,
        )
        ext = "gif"
    else:
        out_frames[0].save(buf, format="PNG")
        ext = "png"
    buf.seek(0)
    return buf, ext
