import io
import math
import os
from functools import lru_cache

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


# Sprite/background art only changes via a code deploy (restart), never the admin panel's JSON
# hot-reload (see CLAUDE.md) -- safe to cache for the process's whole lifetime. Returned images are
# only ever read from (alpha_composite'd as a source, or .copy()'d before mutation), never mutated
# in place, so handing out the same cached object to every caller is safe. This matters far more
# now that a single combat turn can call render_room several times over for one animation instead
# of once -- without caching, every one of those re-opens and re-resizes the same files from disk.
@lru_cache(maxsize=256)
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


@lru_cache(maxsize=64)
def _load_monster_overlay(sprite_path: str) -> Image.Image | None:
    """A sprite_overlay monster's art (dungeon_monsters.json): no trimming or height-normalizing,
    just the same cover-fit the background gets, so art painted at the background's own size lands
    exactly where it was drawn. Same missing-file forgiveness and caching as _load_monster_sprite."""
    if not sprite_path or not os.path.exists(sprite_path):
        return None
    return _fit_frame(Image.open(sprite_path).convert("RGBA"))


# An overlay fills the frame, so an attack can't slide it around the way a cutout tackles (it would
# leave an empty strip at the bottom where the body should be). It lunges at the camera instead:
# the tackle's dy keyframes become a scale, anchored at the bottom center, so a rise is a pull-back
# (shrink) and the snap down is a lean in (grow). Pixels per 1.0 of scale.
_OVERLAY_LUNGE_PX = 120


def _place_overlay(img: Image.Image, overlay: Image.Image, offset: tuple[int, int] = (0, 0)) -> None:
    dx, dy = offset
    if dy:
        scale = 1 + dy / _OVERLAY_LUNGE_PX
        overlay = overlay.resize((round(overlay.width * scale), round(overlay.height * scale)), Image.NEAREST)
    x = round((WIDTH - overlay.width) / 2) + dx
    y = HEIGHT - overlay.height
    # Clip to the frame by hand: alpha_composite won't take a negative destination.
    sx, sy = max(-x, 0), max(-y, 0)
    x, y = max(x, 0), max(y, 0)
    w, h = min(overlay.width - sx, WIDTH - x), min(overlay.height - sy, HEIGHT - y)
    if w > 0 and h > 0:
        img.alpha_composite(overlay.crop((sx, sy, sx + w, sy + h)), (x, y))


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


@lru_cache(maxsize=64)
def _load_background_frames(background_path: str | None) -> tuple[list[Image.Image], list[int], int]:
    """The delve-specific background if it's set and the file actually exists, else the default
    -- same forgiving fallback as the old single-image loader, so a delve JSON entry with no
    background_path (or a since-deleted file) still renders instead of erroring mid-delve. Returns
    (frames, frame_durations_ms, loop_count) -- a plain image (or a single-frame GIF) comes back as
    one frame with an unused duration/loop, so render_room doesn't need two separate code paths for
    "static" vs "animated": it always composites onto every frame in this list and only decides
    PNG vs animated GIF afterward, from how many frames came back. Cached (see _load_monster_sprite's
    own comment on why that's safe) -- a background is opened/resized once per distinct path for
    the process's whole lifetime instead of on every single render_room call."""
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


@lru_cache(maxsize=1)
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
    # 5-6 only happen with All In's extra monsters on top of a party's own extra monster.
    5: [-200, -100, 0, 100, 200],
    6: [-210, -126, -42, 42, 126, 210],
}


def _sprite_height_for(count: int) -> int:
    if count <= 2:
        return SPRITE_HEIGHT
    return round(SPRITE_HEIGHT * (0.75 if count <= 4 else 0.6))


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
    _sprite_height_for). Deliberately just 4 keyframes, not 6 -- every keyframe here costs a full
    extra render_room frame (composite + GIF-encode), and this animation now plays on nearly every
    combat turn (see dungeon_view's last_attacker_slot) rather than fight_intro's rare once-per-room
    cost, so it's worth trimming to the minimum that still reads as a tackle. There's no final
    "back to (0, 0)" keyframe -- render_room's own resting frame already provides that."""
    rise = -round(sprite_height * 0.16)
    lunge = round(sprite_height * 0.07)
    return [
        (0, round(rise * 0.6), 90),
        (0, rise, 90),
        (0, lunge, 70),
        (0, round(lunge * 0.3), 90),
    ]


ATTACK_ANIMATIONS = {"tackle": _tackle_offsets}


def _compose_frame(
    base: Image.Image, sprites: list[tuple], room_label: str, strip: Image.Image | None,
    offset_index: int | None = None, offset: tuple[int, int] = (0, 0), tags: list[str | None] | None = None,
) -> Image.Image:
    """One fully-composited room frame -- `base` (a background frame) plus every monster
    sprite/placeholder, the room label, and a pre-built turn-order `strip` (see
    _build_turn_order_strip -- built once by render_room and passed in here rather than rebuilt per
    frame, since its content never varies across one render_room call's frames). The sprite at
    `offset_index` in `sprites` (if given) is nudged by `offset` pixels first -- an attack
    animation's own frames use this to move just the attacking monster, leaving the background and
    every other monster exactly where render_room's normal (no-offset) frames put them. Shared by
    render_room's per-background-frame loop and its attack-animation frame loop so both stay in
    lockstep with each other -- there's exactly one place that knows how a "room frame" is built.
    `tags` (parallel to `sprites`, optional) puts each monster's A/B/C plate under it -- drawn at
    its resting spot, so a lunging attacker's plate stays put rather than flying with it."""
    img = base.copy()
    cx, cy = WIDTH / 2, HEIGHT / 2 - 10
    count = len(sprites)
    tag_positions = []
    for i, (sprite, monster, x_offset) in enumerate(sprites):
        tag = tags[i] if tags and i < len(tags) else None
        if tag and not (sprite and monster.get("sprite_overlay")):
            feet = cy + 110 if sprite else cy + (60 if count <= 2 else 45)
            tag_positions.append((cx + x_offset, min(feet + 4, HEIGHT - 52), tag))
        dx, dy = offset if i == offset_index else (0, 0)
        mx, my = cx + x_offset + dx, cy + dy
        if sprite and monster.get("sprite_overlay"):
            _place_overlay(img, sprite, (dx, dy))
        elif sprite:
            pos = (round(mx - sprite.width / 2), round(my + 110 - sprite.height))
            img.alpha_composite(sprite, pos)
        else:
            draw = ImageDraw.Draw(img)
            radius = 60 if count <= 2 else 45
            _draw_monster_shape(draw, mx, my, radius, monster["shape"], _parse_color(monster["color"]))
    if tag_positions:
        draw_scene_tags(img, tag_positions)
    draw = ImageDraw.Draw(img)
    draw.text((16, HEIGHT - 32), room_label, font=_label_font, fill=(200, 200, 210, 255))
    if strip is not None:
        img = _append_strip(img, strip)
    return img


# FFX-style turn-order card strip, composited below the room scene (see dungeon.preview_next_turns
# for the schedule these cards visualize). The first card is whoever acts NOW, drawn larger so the
# eye lands there first; the rest are the upcoming schedule. Sized to fit 8 cards (dungeon_view's
# TURN_ORDER_PREVIEW_COUNT) within WIDTH.
#
# Identity is the whole point of this strip, so each card says who it is several ways at once:
#   - players: their Discord avatar (when the caller fetched one -- party/duel do, solo doesn't need
#     to), a colored border matching the colored dot in front of their name in the embed, and a
#     small rank+suit badge so the build is still readable.
#   - monsters: a thumbnail of their own sprite (or an authored card_icon_path), plus their A/B/C
#     tag when a fight has more than one -- the same letter drawn under their sprite in the scene
#     and printed in their embed field name.
#   - everyone: a short name under the card.
CARD_WIDTH, CARD_HEIGHT = 46, 62
NOW_CARD_WIDTH, NOW_CARD_HEIGHT = 58, 78
CARD_GAP = 5
CARD_LABEL_ROW = 20  # "Now" / "Next up:" captions above the cards
CARD_NAME_ROW = 15  # short name under each card
CARD_STRIP_HEIGHT = CARD_LABEL_ROW + NOW_CARD_HEIGHT + CARD_NAME_ROW + 8

_RED_SUITS = {"♥", "♦"}
_CARD_FACE = (245, 242, 230, 255)
_MONSTER_CARD_FACE = (35, 18, 18, 255)
_TAG_FACE = (250, 214, 90, 255)


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_PATH, size)


@lru_cache(maxsize=32)
def _decode_avatar(avatar_bytes: bytes) -> Image.Image | None:
    """A party member's Discord avatar, decoded once per distinct image (the same bytes object is
    handed in on every turn of a delve). Any decode failure just means "no avatar" -- the card
    falls back to its plain face, never crashes a render."""
    try:
        return Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    except Exception:
        return None


@lru_cache(maxsize=128)
def _load_card_icon(path: str) -> Image.Image | None:
    """A monster's card art: its sprite (or authored card_icon_path), trimmed of transparent margin
    like _load_monster_sprite does, at native size -- _fit_into scales it per card size."""
    if not path or not os.path.exists(path):
        return None
    try:
        icon = Image.open(path).convert("RGBA")
    except Exception:
        return None
    bbox = icon.getbbox()
    return icon.crop(bbox) if bbox else icon


def _fit_into(img: Image.Image, w: int, h: int, cover: bool) -> Image.Image:
    """Scales `img` to fit (cover=False, whole image visible) or fill (cover=True, center-cropped)
    a w x h box. NEAREST when enlarging keeps pixel art crisp, LANCZOS when shrinking keeps it clean."""
    scale = (max if cover else min)(w / img.width, h / img.height)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    resized = img.resize(size, Image.NEAREST if scale > 1 else Image.LANCZOS)
    if cover:
        left, top = (resized.width - w) // 2, (resized.height - h) // 2
        resized = resized.crop((left, top, left + w, top + h))
    return resized


def _paste_rounded(dest: Image.Image, img: Image.Image, x: int, y: int, radius: int) -> None:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=radius, fill=255)
    mask = Image.composite(img.getchannel("A"), Image.new("L", img.size, 0), mask)
    dest.paste(img, (x, y), mask)


def _draw_badge(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, size: int, fg: tuple, bg: tuple) -> None:
    """A small rounded label (rank+suit on an avatar card, A/B/C on a monster card or under a
    sprite) anchored at its top-left corner."""
    font = _font(size)
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    draw.rounded_rectangle([x, y, x + tw + 6, y + th + 6], radius=3, fill=bg, outline=(0, 0, 0, 255), width=1)
    draw.text((x + 3 - box[0], y + 3 - box[1]), text, font=font, fill=fg)


def _draw_player_card(strip: Image.Image, x: int, y: int, w: int, h: int, card: dict) -> None:
    """A player/party-member card. With an avatar: the avatar fills the card, with a small rank+suit
    badge in the corner. Without one: the classic face -- rank letter (A/K/Q/J) and suit symbol,
    suit-colored red/black. Either way the border is the member's own color (party/duel), or plain
    black when the caller didn't assign one (solo)."""
    draw = ImageDraw.Draw(strip)
    rank, suit = card["rank"], card["suit"]
    suit_color = (190, 25, 25, 255) if suit in _RED_SUITS else (20, 20, 20, 255)
    border = tuple(card["color"]) + (255,) if card.get("color") else (0, 0, 0, 255)
    border_w = 3 if card.get("color") else 2
    draw.rounded_rectangle([x, y, x + w, y + h], radius=6, fill=_CARD_FACE, outline=border, width=border_w)
    big = w >= NOW_CARD_WIDTH
    avatar = _decode_avatar(card["avatar"]) if card.get("avatar") else None
    if avatar is not None:
        inset = border_w
        _paste_rounded(strip, _fit_into(avatar, w - 2 * inset + 1, h - 2 * inset + 1, cover=True), x + inset, y + inset, 4)
        _draw_badge(ImageDraw.Draw(strip), x + 2, y + 2, f"{rank}{suit}", 11 if big else 10, suit_color, _CARD_FACE)
        return
    draw.text((x + 6, y + 4), rank, font=_font(21 if big else 17), fill=suit_color)
    suit_font = _font(17 if big else 13)
    sb = draw.textbbox((0, 0), suit, font=suit_font)
    draw.text((x + w - (sb[2] - sb[0]) - 7, y + h - (sb[3] - sb[1]) - 8), suit, font=suit_font, fill=suit_color)


def _draw_monster_card(strip: Image.Image, x: int, y: int, w: int, h: int, card: dict) -> None:
    """A monster card: a thumbnail of the monster's own art on a dark face, so the card looks like
    the thing standing in the scene above it. A sprite_overlay monster's art is a whole painted
    frame, useless shrunk to a card, so it uses only an authored card_icon_path; anything without
    usable art falls back to the old shape/color placeholder. A tag (A/B/C) badge goes top-right."""
    draw = ImageDraw.Draw(strip)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=6, fill=_MONSTER_CARD_FACE, outline=(0, 0, 0, 255), width=2)
    path = card.get("card_icon_path") or (None if card.get("sprite_overlay") else card.get("sprite_path"))
    icon = _load_card_icon(path) if path else None
    if icon is not None:
        thumb = _fit_into(icon, w - 8, h - 8, cover=False)
        strip.alpha_composite(thumb, (x + (w - thumb.width) // 2, y + (h - thumb.height) // 2))
        draw = ImageDraw.Draw(strip)
    else:
        _draw_monster_shape(draw, x + w / 2, y + h / 2 + 2, w * 0.34, card["shape"], _parse_color(card["color"]))
    if card.get("tag"):
        size = 12 if w >= NOW_CARD_WIDTH else 11
        tb = draw.textbbox((0, 0), card["tag"], font=_font(size))
        _draw_badge(draw, x + w - (tb[2] - tb[0]) - 8, y + 2, card["tag"], size, (20, 20, 20, 255), _TAG_FACE)


def _short_name(draw: ImageDraw.ImageDraw, name: str, max_width: int, font: ImageFont.FreeTypeFont) -> str:
    """`name` cut down (with an ellipsis) until it fits `max_width` pixels."""
    if draw.textlength(name, font=font) <= max_width:
        return name
    while name and draw.textlength(name + "…", font=font) > max_width:
        name = name[:-1]
    return (name.rstrip() + "…") if name else "?"


def _build_turn_order_strip(turn_order: list[dict]) -> Image.Image:
    """Builds the horizontal strip of playing-card-style icons for the next several turns
    (dungeon.preview_next_turns) -- FFX flavor, and the main way a party tells whose turn is
    coming. Each entry is `{"kind": "player", "rank", "suit", "name", "avatar"?, "color"?}` or
    `{"kind": "monster", "shape", "color", "name", "sprite_path"?, "sprite_overlay"?,
    "card_icon_path"?, "tag"?}` (see dungeon_view._player_card/_monster_card). The same combatant
    can (and does) appear on multiple cards when they're fast enough to act again before others get
    a turn -- intended, matches FFX's own UI. Card 0 is drawn larger under a "Now" caption.

    Deliberately its own function, built ONCE per render_room call and reused across every frame
    (see _append_strip) rather than redrawn per frame -- the turn_order content is identical for
    every frame of a single render."""
    strip = Image.new("RGBA", (WIDTH, CARD_STRIP_HEIGHT), (15, 15, 20, 255))
    draw = ImageDraw.Draw(strip)
    n = len(turn_order)
    total_width = NOW_CARD_WIDTH + max(0, n - 1) * (CARD_WIDTH + CARD_GAP)
    x = max(6, (WIDTH - total_width) // 2)
    bottom = CARD_LABEL_ROW + NOW_CARD_HEIGHT  # every card's bottom edge lines up here
    name_font = _font(11)
    for i, card in enumerate(turn_order):
        w, h = (NOW_CARD_WIDTH, NOW_CARD_HEIGHT) if i == 0 else (CARD_WIDTH, CARD_HEIGHT)
        y = bottom - h
        if i == 0:
            draw.text((x, 2), "Now", font=_card_strip_label_font, fill=_TAG_FACE)
        elif i == 1:
            draw.text((x, y - 18), "Next up:", font=_card_strip_label_font, fill=(190, 190, 200, 255))
        if card["kind"] == "player":
            _draw_player_card(strip, x, y, w, h, card)
        else:
            _draw_monster_card(strip, x, y, w, h, card)
        draw = ImageDraw.Draw(strip)
        label = _short_name(draw, card.get("name") or "?", w + CARD_GAP - 1, name_font)
        lw = draw.textlength(label, font=name_font)
        colored = card["kind"] == "player" and card.get("color")
        name_color = tuple(card["color"]) + (255,) if colored else (220, 220, 230, 255)
        draw.text((x + w / 2 - lw / 2, bottom + 3), label, font=name_font, fill=name_color)
        x += w + CARD_GAP
    return strip


def draw_scene_tags(img: Image.Image, positions: list[tuple[float, float, str]]) -> None:
    """Draws each monster's A/B/C tag as a small plate just under its sprite in the room scene --
    `positions` is [(center_x, top_y, tag)], see _compose_frame."""
    draw = ImageDraw.Draw(img)
    for cx, top, tag in positions:
        tb = draw.textbbox((0, 0), tag, font=_font(14))
        _draw_badge(draw, cx - (tb[2] - tb[0]) / 2 - 3, top, tag, 14, (20, 20, 20, 255), _TAG_FACE)


def _append_strip(img: Image.Image, strip: Image.Image) -> Image.Image:
    """Grows `img` downward to fit a pre-built `strip` (see _build_turn_order_strip) beneath it.
    Returns a new image; doesn't mutate `img`."""
    combined = Image.new("RGBA", (WIDTH, HEIGHT + CARD_STRIP_HEIGHT), (0, 0, 0, 255))
    combined.alpha_composite(img, (0, 0))
    combined.alpha_composite(strip, (0, HEIGHT))
    return combined


def render_room(
    visited_count: int, monsters: list[dict], background_path: str | None = None,
    turn_order: list[dict] | None = None, label: str | None = None, fight_intro: bool = False,
    attack: dict | None = None, tags: list[str | None] | None = None,
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
    add the FFX-style turn-order card strip -- see _build_turn_order_strip for its shape.

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
    monster's attack, then settle" is a reasonable future extension, not needed for a first cut.

    `tags` (optional, parallel to `monsters`) labels each monster with its A/B/C tag in the scene,
    matching its turn-order card and embed field -- see _compose_frame."""
    frames, durations, loop = _load_background_frames(background_path)

    count = len(monsters)
    sprite_height = _sprite_height_for(count)
    offsets = _GROUP_X_OFFSETS.get(count, _GROUP_X_OFFSETS[4])
    sprites = [
        (_load_monster_overlay(monster.get("sprite_path")) if monster.get("sprite_overlay")
         else _load_monster_sprite(monster.get("sprite_path"), round(sprite_height * monster.get("sprite_scale", 1.0))),
         monster, x_offset)
        for monster, x_offset in zip(monsters, offsets)
    ]
    room_label = label or f"Room {visited_count}"
    strip = _build_turn_order_strip(turn_order) if turn_order else None

    out_frames = [_compose_frame(base, sprites, room_label, strip, tags=tags) for base in frames]

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
            # An overlay is frame-sized, so its lunge is scaled like a normal sprite's (see _place_overlay).
            overlay = sprites[idx][1].get("sprite_overlay")
            attacker_height = attacker_sprite.height if attacker_sprite and not overlay else sprite_height
            keyframes = keyframes_fn(attacker_height)
            attack_frames = [
                _compose_frame(frames[0], sprites, room_label, strip, offset_index=idx, offset=(dx, dy), tags=tags)
                for dx, dy, _ in keyframes
            ]
            out_frames = attack_frames + out_frames
            durations = [dur for _, _, dur in keyframes] + [max(d, 100) for d in durations]
            animate_once = True

    buf = io.BytesIO()
    if len(out_frames) > 1:
        # A plain per-frame .save() lets Pillow rebuild a fresh 256-color ADAPTIVE (median-cut)
        # palette from scratch for every single frame -- that per-frame rebuild, not the pixel work
        # of actually painting each frame, is the expensive part of GIF encoding. Building ONE
        # palette from every frame's pixels combined (so no frame's unique colors -- e.g. only the
        # fight-intro banner's reds, or only the tail frame's plain background -- get left out just
        # because the reference happened to be a different frame) and reusing it via a cheap
        # per-frame nearest-color mapping pays that median-cut cost once instead of once per frame.
        # Tried Pillow's fixed WEB palette first for even less computation (no median-cut at all),
        # but its 216 fixed colors band/dither badly against this game's dark, gradient-heavy
        # dungeon backgrounds -- a full adaptive 256-color budget is worth keeping.
        rgb_frames = [f.convert("RGB") for f in out_frames]
        strip = Image.new("RGB", (rgb_frames[0].width * len(rgb_frames), rgb_frames[0].height))
        for i, f in enumerate(rgb_frames):
            strip.paste(f, (i * rgb_frames[0].width, 0))
        shared_palette = strip.quantize(colors=256)
        gif_frames = [f.quantize(palette=shared_palette, dither=Image.Dither.NONE) for f in rgb_frames]
        save_kwargs = {} if animate_once else {"loop": loop}
        gif_frames[0].save(
            buf, format="GIF", save_all=True, append_images=gif_frames[1:],
            duration=durations, disposal=2, **save_kwargs,
        )
        ext = "gif"
    else:
        out_frames[0].save(buf, format="PNG")
        ext = "png"
    buf.seek(0)
    return buf, ext
