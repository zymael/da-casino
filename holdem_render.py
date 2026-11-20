"""Renders a Hold'em table as one scene: NPC bots seated across the far rail (their npcs.json
portrait), humans along the near rail (their Discord avatar), each seat's cards / bet / dealer and
blind puck / status on the felt in front of them, the board and pot in the middle, and a speech
bubble over any bot that just said something.

Two optional painted art layers replace the procedural placeholders the moment the files exist
(export_art_templates.py writes templates at the exact size, with the seat layout marked):
  assets/holdem/room_background.png  -- drawn first, behind the far-row portraits (until it
                                        exists: the Casino room's own background, darkened)
  assets/holdem/table_foreground.png -- drawn over the portraits (transparent above the far rail),
                                        so the NPCs look like they're sitting behind the table
An NPC's optional poker_sprite_path (npcs.json) is a dedicated seated portrait; without one its
regular sprite_path is used, trimmed to its visible pixels.

Pure rendering: holdem_view.py builds the SeatView list; nothing here knows about discord or db.
"""

import io
import os
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont, ImageOps, features

import cards_render

WIDTH = 1100
HEIGHT = 650

FAR_SLOTS = 5  # across the back: NPCs first, overflow humans
NEAR_SLOTS = 3  # along the near rail: humans first, overflow NPCs
MAX_SEATS = FAR_SLOTS + NEAR_SLOTS

# Table geometry (the procedural table and the templates both use these).
TABLE_BOX = (28, 232, WIDTH - 28, HEIGHT - 14)
RAIL = 26
TABLE_RADIUS = 170
FAR_RAIL_Y = TABLE_BOX[1]  # portraits' bottoms tuck under this edge

PORTRAIT_BOX = (196, 205)  # max size of a far-row portrait
PORTRAIT_TUCK = 34  # how far a portrait's bottom sits below the far rail (hidden behind it)
AVATAR_SIZE = 70

SEAT_CARD_SCALE = 0.45
BOARD_CARD_SCALE = 0.64

STONE = (128, 128, 132, 255)
STONE_LIGHT = (188, 188, 186, 255)
STONE_DARK = (72, 72, 78, 255)
STONE_EDGE = (45, 45, 50, 255)
FELT = (96, 14, 24, 255)  # dark red
FELT_EDGE = (215, 175, 110, 200)
WALL_TOP = (38, 26, 40, 255)
WALL_BOTTOM = (16, 12, 20, 255)
GOLD = (255, 205, 60, 255)
TEXT = (240, 240, 235, 255)
DIM_TEXT = (175, 195, 185, 255)
RED = (235, 80, 80, 255)
PLATE = (0, 0, 0, 150)

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# Pillow only lays out right-to-left text (Persian, Arabic, Hebrew names) when it was built with
# libraqm. Without it, a name at least gets its letters in reading order (unjoined).
_HAVE_RAQM = features.check("raqm")
_name_font = ImageFont.truetype(_FONT_PATH, 15)
_small_font = ImageFont.truetype(_FONT_PATH, 12)
_pot_font = ImageFont.truetype(_FONT_PATH, 20)
_street_font = ImageFont.truetype(_FONT_PATH, 16)
_bubble_font = ImageFont.truetype(_FONT_PATH, 13)
_puck_font = ImageFont.truetype(_FONT_PATH, 11)

ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "holdem")
ROOM_BACKGROUND_PATH = os.path.join(ASSET_DIR, "room_background.png")
TABLE_FOREGROUND_PATH = os.path.join(ASSET_DIR, "table_foreground.png")


@dataclass
class SeatView:
    name: str
    is_npc: bool
    stack: int
    bet: int = 0  # in front of them this street
    folded: bool = False
    all_in: bool = False
    to_act: bool = False
    badge: str = ""  # "D", "SB", "BB", "D SB"
    portrait_path: str | None = None  # NPCs
    portrait_scale: float = 1.0  # NPCs: npcs.json poker_sprite_scale
    avatar: bytes | None = None  # humans (Discord avatar PNG)
    cards: list | None = None  # face-up cards (showdown); None = face-down backs while in the hand
    hand_label: str | None = None  # e.g. "Full House" at showdown
    won: int = 0
    speech: str | None = None


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------
def _slot_xs(n: int) -> list[float]:
    left, right = TABLE_BOX[0] + 60, TABLE_BOX[2] - 60
    step = (right - left) / n
    return [left + step * (i + 0.5) for i in range(n)]


FAR_XS = _slot_xs(FAR_SLOTS)
NEAR_XS = _slot_xs(NEAR_SLOTS)

# Far seat block, on the felt just inside the far rail.
FAR_NAME_Y = FAR_RAIL_Y + RAIL + 16
FAR_CARDS_Y = FAR_NAME_Y + 26
# Board in the middle (street name to its left, pot to its right).
BOARD_Y = 404
# Near seat block: cards on the felt, avatar + nameplate on the near rail.
NEAR_CARDS_Y = 516
NEAR_AVATAR_Y = HEIGHT - 50


def assign_rows(seats: list[SeatView]) -> tuple[list[SeatView], list[SeatView]]:
    """NPCs sit across the back, humans on the near side; whichever side fills up overflows to the
    other. `seats` is in table order, which each row keeps."""
    npcs = [s for s in seats if s.is_npc]
    humans = [s for s in seats if not s.is_npc]
    far = npcs[:FAR_SLOTS]
    near = humans[:NEAR_SLOTS]
    leftover = npcs[FAR_SLOTS:] + humans[NEAR_SLOTS:]
    for s in leftover:
        (far if len(far) < FAR_SLOTS else near).append(s)
    order = {id(s): i for i, s in enumerate(seats)}
    far.sort(key=lambda s: order[id(s)])
    near.sort(key=lambda s: order[id(s)])
    return far, near


def _spread(xs: list[float], n: int) -> list[float]:
    """Centers n seats in a row of len(xs) slots instead of bunching them on the left."""
    if n >= len(xs) or n == 0:
        return xs[:n]
    mid = (xs[0] + xs[-1]) / 2
    if n == 1:
        return [mid]
    # A little roomier than full-row spacing, but never wider than the full row.
    step = min((xs[1] - xs[0]) * 1.25, (xs[-1] - xs[0]) / (n - 1))
    return [mid + (i - (n - 1) / 2) * step for i in range(n)]


# ---------------------------------------------------------------------------
# art layers
# ---------------------------------------------------------------------------
_layer_cache: dict[str, tuple[float, Image.Image]] = {}


def _cached_file(path: str, size: tuple[int, int] | None = None) -> Image.Image | None:
    if not path or not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    key = f"{path}|{size}"
    hit = _layer_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    img = Image.open(path).convert("RGBA")
    if size:
        img = img.resize(size)
    _layer_cache[key] = (mtime, img)
    return img


def procedural_room() -> Image.Image:
    img = Image.new("RGBA", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / HEIGHT
        c = tuple(int(WALL_TOP[i] * (1 - t) + WALL_BOTTOM[i] * t) for i in range(3)) + (255,)
        draw.line([(0, y), (WIDTH, y)], fill=c)
    # a hanging lamp's pool of light over the felt
    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(260, 0, -20):
        a = int(26 * (1 - r / 260))
        gd.ellipse([WIDTH / 2 - r * 2, 120 - r * 0.6, WIDTH / 2 + r * 2, 120 + r * 0.6], fill=(255, 220, 150, a))
    return Image.alpha_composite(img, glow)


def stone_texture(size: tuple[int, int], seed: int = 7) -> Image.Image:
    """Grey stone: big soft blotches, mid-scale mottling, fine grit, and a few dark flecks, all
    from seeded noise so the rim looks the same on every render."""
    import random as _random

    from PIL import ImageFilter

    w, h = size
    rng = _random.Random(seed)

    def noise(cell: int, blur: float) -> Image.Image:
        small = Image.new("L", (max(1, w // cell), max(1, h // cell)))
        small.putdata([rng.randint(0, 255) for _ in range(small.width * small.height)])
        return small.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(blur))

    base = Image.blend(noise(40, 6), noise(10, 2), 0.45)
    base = Image.blend(base, noise(1, 0), 0.22)
    base = ImageOps.autocontrast(base, cutoff=1)
    stone = ImageOps.colorize(base, black=STONE_DARK[:3], mid=STONE[:3], white=STONE_LIGHT[:3]).convert("RGBA")
    draw = ImageDraw.Draw(stone)
    for _ in range(w * h // 900):  # dark flecks
        x, y = rng.randrange(w), rng.randrange(h)
        r = rng.choice((1, 1, 1, 2))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(60, 60, 64, 255))
    return stone


_procedural_table_cache: Image.Image | None = None


def procedural_table() -> Image.Image:
    """Transparent everywhere but the table (dark red felt, grey stone rim), so it can sit over the
    far-row portraits. Built once and cached."""
    global _procedural_table_cache
    if _procedural_table_cache is not None:
        return _procedural_table_cache
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = TABLE_BOX
    draw.rounded_rectangle([x0, y0 + 7, x1, y1 + 7], radius=TABLE_RADIUS, fill=(0, 0, 0, 130))  # shadow

    rim_mask = Image.new("L", (WIDTH, HEIGHT), 0)
    ImageDraw.Draw(rim_mask).rounded_rectangle([x0, y0, x1, y1], radius=TABLE_RADIUS, fill=255)
    img.paste(stone_texture((WIDTH, HEIGHT)), (0, 0), rim_mask)

    draw.rounded_rectangle([x0, y0, x1, y1], radius=TABLE_RADIUS, outline=STONE_EDGE, width=3)
    draw.rounded_rectangle([x0 + 4, y0 + 3, x1 - 4, y1 - 5], radius=TABLE_RADIUS - 4, outline=(255, 255, 255, 60), width=2)
    felt = [x0 + RAIL, y0 + RAIL, x1 - RAIL, y1 - RAIL]
    draw.rounded_rectangle([felt[0] - 3, felt[1] - 3, felt[2] + 3, felt[3] + 3], radius=TABLE_RADIUS - RAIL + 3,
                           outline=(40, 40, 44, 200), width=3)  # the rim's inner lip casts a shadow
    draw.rounded_rectangle(felt, radius=TABLE_RADIUS - RAIL, fill=FELT, outline=FELT_EDGE, width=2)
    _procedural_table_cache = img
    return img


_backdrop_cache: dict[str, tuple[float, Image.Image]] = {}


def _backdrop(path: str | None) -> Image.Image | None:
    """Any room photo (holdem_view passes the Casino room's background_path) cropped to the
    canvas, then softened and darkened so the portraits in front of it read clearly."""
    if not path:
        return None
    full = path if os.path.isabs(path) else os.path.join(os.path.dirname(__file__), path)
    if not os.path.exists(full):
        return None
    mtime = os.path.getmtime(full)
    hit = _backdrop_cache.get(full)
    if hit and hit[0] == mtime:
        return hit[1]
    from PIL import ImageEnhance, ImageFilter

    img = ImageOps.fit(Image.open(full).convert("RGB"), (WIDTH, HEIGHT), Image.LANCZOS, centering=(0.5, 0.3))
    img = img.filter(ImageFilter.GaussianBlur(2.5))
    img = ImageEnhance.Brightness(img).enhance(0.45)
    img = ImageEnhance.Color(img).enhance(0.8).convert("RGBA")
    _backdrop_cache[full] = (mtime, img)
    return img


def _room(backdrop_path: str | None = None) -> Image.Image:
    """Painted holdem room art if it exists, else the given room photo (the Casino's), else a
    plain procedural wall."""
    img = _cached_file(ROOM_BACKGROUND_PATH, (WIDTH, HEIGHT)) or _backdrop(backdrop_path) or procedural_room()
    return img.copy()


def _table() -> Image.Image:
    return _cached_file(TABLE_FOREGROUND_PATH, (WIDTH, HEIGHT)) or procedural_table()


# ---------------------------------------------------------------------------
# portraits
# ---------------------------------------------------------------------------
_portrait_cache: dict[tuple, tuple[float, Image.Image]] = {}  # (path, scale) -> (mtime, image)


def _framed(img: Image.Image) -> Image.Image:
    """A sprite with no transparency to trim (it would read as a pasted rectangle) goes in a
    picture frame instead."""
    w, h = PORTRAIT_BOX[0] - 30, PORTRAIT_BOX[1] - 30
    img = ImageOps.fit(img.convert("RGBA"), (w, h))
    frame = Image.new("RGBA", (w + 16, h + 16), (0, 0, 0, 0))
    fd = ImageDraw.Draw(frame)
    fd.rounded_rectangle([0, 0, w + 15, h + 15], radius=10, fill=STONE, outline=STONE_EDGE, width=3)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=6, fill=255)
    frame.paste(img, (8, 8), mask)
    return frame


def load_portrait(path: str | None, scale_mult: float = 1.0) -> Image.Image | None:
    """An NPC portrait trimmed to its visible pixels and scaled into PORTRAIT_BOX, then by the NPC's
    optional poker_sprite_scale (`scale_mult`, e.g. 0.5 for a small creature). Cached."""
    if not path:
        return None
    full = path if os.path.isabs(path) else os.path.join(os.path.dirname(__file__), path)
    if not os.path.exists(full):
        return None
    mtime = os.path.getmtime(full)
    key = (full, scale_mult)
    hit = _portrait_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    img = Image.open(full)
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA")
    # Trim to clearly visible pixels: faint leftovers from cutting a figure out of a background
    # shouldn't widen the box (and shrink the figure).
    bbox = img.getchannel("A").point(lambda a: 255 if a > 40 else 0).getbbox() if has_alpha else None
    if bbox is None or bbox == (0, 0, img.width, img.height):
        portrait = _framed(img)
    else:
        portrait = img.crop(bbox)
    scale = min(PORTRAIT_BOX[0] / portrait.width, PORTRAIT_BOX[1] / portrait.height) * scale_mult
    portrait = portrait.resize((max(1, int(portrait.width * scale)), max(1, int(portrait.height * scale))), Image.LANCZOS)
    _portrait_cache[key] = (mtime, portrait)
    return portrait


def _avatar_circle(data: bytes | None, size: int, name: str) -> Image.Image:
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    img = None
    if data:
        try:
            img = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGBA"), (size, size))
        except Exception:
            img = None
    if img is None:  # no avatar: an initial on a colored disc
        img = Image.new("RGBA", (size, size), (70, 90, 160, 255))
        d = ImageDraw.Draw(img)
        _centered(d, size / 2, size / 2, (name[:1] or "?").upper(), ImageFont.truetype(_FONT_PATH, size // 2), TEXT)
    out.paste(img, (0, 0), mask)
    ImageDraw.Draw(out).ellipse([0, 0, size - 1, size - 1], outline=GOLD, width=3)
    return out


def _dimmed(img: Image.Image) -> Image.Image:
    grey = ImageOps.grayscale(img.convert("RGB")).convert("RGBA")
    grey.putalpha(img.getchannel("A").point(lambda a: int(a * 0.55)))
    return grey


# ---------------------------------------------------------------------------
# drawing helpers
# ---------------------------------------------------------------------------
def _centered(draw, cx, cy, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def _is_rtl_char(ch: str) -> bool:
    return "֐" <= ch <= "ࣿ" or "יִ" <= ch <= "﻿"


def display_name(name: str) -> str:
    if _HAVE_RAQM or not any(_is_rtl_char(c) for c in name):
        return name
    return name[::-1]


def _fit(text: str, font, max_w: int, draw) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


_scaled_cards: dict[tuple, Image.Image] = {}


def _card(card, scale: float) -> Image.Image:
    key = (f"{card.rank}{card.suit}" if card is not None else "back", scale)
    if key not in _scaled_cards:
        base = cards_render._back_image() if card is None else cards_render._card_image(card)
        _scaled_cards[key] = base.resize(
            (int(cards_render.CARD_WIDTH * scale), int(cards_render.CARD_HEIGHT * scale)), Image.LANCZOS
        )
    return _scaled_cards[key]


def _plate(draw, cx, cy, lines: list[tuple[str, object, tuple]], highlight=None, max_w=180):
    """A dark rounded nameplate with one or more centered lines [(text, font, color)]."""
    lines = [(_fit(t, f, max_w - 14, draw), f, c) for t, f, c in lines]
    widths = [draw.textlength(t, font=f) for t, f, _ in lines]
    heights = [f.size + 4 for _, f, _ in lines]
    w = max(widths) + 16
    h = sum(heights) + 6
    box = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
    draw.rounded_rectangle(box, radius=8, fill=PLATE, outline=highlight, width=2 if highlight else 0)
    y = box[1] + 3
    for (t, f, c), lh in zip(lines, heights):
        _centered(draw, cx, y + lh / 2, t, f, c)
        y += lh
    return box


def _puck(draw, cx, cy, label: str):
    r = 13
    fill = (250, 250, 250, 255) if label == "D" else (40, 110, 200, 255) if label == "SB" else (235, 140, 30, 255)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=(20, 20, 20, 255), width=2)
    _centered(draw, cx, cy, label, _puck_font, (20, 20, 20, 255) if label == "D" else TEXT)


def _chips(draw, cx, cy, amount: int):
    for i in range(3):
        y = cy - i * 4
        draw.ellipse([cx - 11, y - 5, cx + 11, y + 5], fill=(25, 25, 25, 255) if i % 2 == 0 else (240, 240, 240, 255),
                     outline=(160, 160, 160, 255), width=1)
    tw = draw.textlength(str(amount), font=_small_font)
    draw.rounded_rectangle([cx + 14, cy - 9, cx + 22 + tw, cy + 7], radius=5, fill=PLATE)
    draw.text((cx + 18, cy - 8), str(amount), font=_small_font, fill=GOLD)


def _wrap(text: str, font, max_w: int, draw, max_lines: int = 3) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(lines[-1] + " …", font, max_w, draw)
    return [_fit(line, font, max_w, draw) for line in lines]


def _bubble(draw, anchor_x, anchor_y, text: str, right_side: bool):
    max_w = 190
    lines = _wrap(text, _bubble_font, max_w, draw)
    lh = _bubble_font.size + 4
    w = max(draw.textlength(line, font=_bubble_font) for line in lines) + 20
    h = lh * len(lines) + 14
    x0 = anchor_x + 18 if right_side else anchor_x - 18 - w
    x0 = max(6, min(WIDTH - 6 - w, x0))
    y0 = max(6, anchor_y - h)
    box = [x0, y0, x0 + w, y0 + h]
    tail_x = x0 + 14 if right_side else x0 + w - 14
    draw.polygon([(tail_x - 7, y0 + h - 1), (tail_x + 7, y0 + h - 1), (anchor_x, y0 + h + 14)], fill=(250, 250, 245, 255))
    draw.rounded_rectangle(box, radius=12, fill=(250, 250, 245, 255), outline=(30, 30, 30, 255), width=2)
    y = y0 + 7
    for line in lines:
        draw.text((x0 + 10, y), line, font=_bubble_font, fill=(25, 25, 25, 255))
        y += lh


# ---------------------------------------------------------------------------
# seat blocks
# ---------------------------------------------------------------------------
def _seat_cards(img, draw, cx, top_y, seat: SeatView, label_above: bool = False):
    """Two cards (backs, or faces at showdown), with the blind/dealer puck to the left and the bet
    to the right, and the showdown hand label under them (or above, for near-side seats whose
    nameplate sits right below the cards). Returns the y just below the cards."""
    if seat.folded:
        _centered(draw, cx, top_y + 22, "FOLDED", _small_font, DIM_TEXT)
        cards_bottom = top_y + 40
    else:
        cw = int(cards_render.CARD_WIDTH * SEAT_CARD_SCALE)
        ch = int(cards_render.CARD_HEIGHT * SEAT_CARD_SCALE)
        faces = seat.cards if seat.cards else [None, None]
        gap = 4
        x = cx - (2 * cw + gap) / 2
        for i, card in enumerate(faces[:2]):
            face = _card(card, SEAT_CARD_SCALE)
            img.alpha_composite(face, (int(x + i * (cw + gap)), int(top_y)))
        cards_bottom = top_y + ch
        if seat.hand_label:
            label = _fit(seat.hand_label, _small_font, 170, draw)
            tw = draw.textlength(label, font=_small_font)
            ly = top_y - 22 if label_above else cards_bottom + 3
            draw.rounded_rectangle([cx - tw / 2 - 6, ly, cx + tw / 2 + 6, ly + 16], radius=5, fill=PLATE)
            _centered(draw, cx, ly + 8, label, _small_font, GOLD if seat.won else TEXT)
            if not label_above:
                cards_bottom += 20
    half = int(cards_render.CARD_WIDTH * SEAT_CARD_SCALE) + 4
    badges = seat.badge.split()
    for i, b in enumerate(badges):
        _puck(draw, cx - half - 16 - i * 28, top_y + 16, b)
    if seat.bet and not seat.folded:
        _chips(draw, cx + half + 14, top_y + 20, seat.bet)
    return cards_bottom


def _status_line(seat: SeatView) -> tuple[str, tuple]:
    if seat.won:
        return f"WON {seat.won}", GOLD
    if seat.all_in:
        return f"ALL IN · {seat.stack}", RED
    return f"{seat.stack}", DIM_TEXT


def _draw_far(img, seats: list[SeatView], portraits_layer: Image.Image):
    xs = _spread(FAR_XS, len(seats))
    for seat, cx in zip(seats, xs):
        if seat.is_npc:
            p = load_portrait(seat.portrait_path, seat.portrait_scale)
        else:
            p = _avatar_circle(seat.avatar, 120, seat.name)
        if p is None:
            p = _avatar_circle(None, 120, seat.name)
        if seat.folded:
            p = _dimmed(p)
        tuck = PORTRAIT_TUCK if seat.is_npc else 6
        y = FAR_RAIL_Y + tuck - p.height
        portraits_layer.alpha_composite(p, (int(cx - p.width / 2), int(max(0, y))))


def _far_details(img, draw, seats: list[SeatView]):
    xs = _spread(FAR_XS, len(seats))
    for seat, cx in zip(seats, xs):
        status, color = _status_line(seat)
        _plate(draw, cx, FAR_NAME_Y, [(display_name(seat.name), _name_font, TEXT), (status, _small_font, color)],
               highlight=GOLD if (seat.to_act or seat.won) else None, max_w=190)
        _seat_cards(img, draw, cx, FAR_CARDS_Y + 6, seat)


def _near_details(img, draw, seats: list[SeatView]):
    xs = _spread(NEAR_XS, len(seats))
    for seat, cx in zip(seats, xs):
        _seat_cards(img, draw, cx, NEAR_CARDS_Y, seat, label_above=True)
        av_x = cx - 95
        if seat.is_npc:
            p = load_portrait(seat.portrait_path, seat.portrait_scale)
            av = _avatar_circle(None, AVATAR_SIZE, seat.name) if p is None else None
            if p is not None:  # an overflow NPC on the near side: a head-and-shoulders crop in a circle
                buf = io.BytesIO()
                ImageOps.fit(p, (p.width, p.width), centering=(0.5, 0.0)).save(buf, "PNG")
                av = _avatar_circle(buf.getvalue(), AVATAR_SIZE, seat.name)
        else:
            av = _avatar_circle(seat.avatar, AVATAR_SIZE, seat.name)
        if seat.folded:
            av = _dimmed(av)
        img.alpha_composite(av, (int(av_x - AVATAR_SIZE / 2), int(NEAR_AVATAR_Y - AVATAR_SIZE / 2)))
        status, color = _status_line(seat)
        _plate(draw, cx + 30, NEAR_AVATAR_Y, [(display_name(seat.name), _name_font, TEXT), (status, _small_font, color)],
               highlight=GOLD if (seat.to_act or seat.won) else None, max_w=170)


def render_table(
    seats: list[SeatView], board: list, pot: int, street: str = "", currency: str = "",
    backdrop_path: str | None = None,
) -> io.BytesIO:
    if len(seats) > MAX_SEATS:
        raise ValueError(f"at most {MAX_SEATS} seats")
    far, near = assign_rows(seats)

    img = _room(backdrop_path)
    portraits = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    _draw_far(img, far, portraits)
    img.alpha_composite(portraits)
    img.alpha_composite(_table())
    draw = ImageDraw.Draw(img)

    _far_details(img, draw, far)

    # board + pot
    cw = int(cards_render.CARD_WIDTH * BOARD_CARD_SCALE)
    ch = int(cards_render.CARD_HEIGHT * BOARD_CARD_SCALE)
    gap = 10
    x = WIDTH / 2 - (5 * cw + 4 * gap) / 2
    for i in range(5):
        bx = int(x + i * (cw + gap))
        if i < len(board):
            img.alpha_composite(_card(board[i], BOARD_CARD_SCALE), (bx, BOARD_Y))
        else:
            draw.rounded_rectangle([bx, BOARD_Y, bx + cw, BOARD_Y + ch], radius=7, outline=(255, 255, 255, 50), width=2)
    board_right = x + 5 * cw + 4 * gap
    mid_y = BOARD_Y + ch / 2
    pot_x = (board_right + TABLE_BOX[2] - RAIL) / 2
    _centered(draw, pot_x, mid_y - 12, f"POT {pot}", _pot_font, GOLD)
    if currency:
        _centered(draw, pot_x, mid_y + 12, currency, _small_font, DIM_TEXT)
    if street:
        _centered(draw, (TABLE_BOX[0] + RAIL + x) / 2, mid_y, street.upper(), _street_font, (255, 255, 255, 140))

    _near_details(img, draw, near)

    # speech bubbles last, over everything
    for seat, cx in zip(far, _spread(FAR_XS, len(far))):
        if seat.speech:
            _bubble(draw, cx + 30, 70, seat.speech, right_side=cx < WIDTH * 0.66)
    for seat, cx in zip(near, _spread(NEAR_XS, len(near))):
        if seat.speech:
            _bubble(draw, cx - 95, NEAR_AVATAR_Y - AVATAR_SIZE / 2, seat.speech, right_side=True)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
