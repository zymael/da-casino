"""Craps table + dice pictures. Procedural placeholder art in the same wood-rail/green-felt style
as blackjack_render.py, swapped for real painted art (assets/craps/craps_table.png) the moment that
file exists. Every _*_rect below is still what lines chips and win highlights up against the art,
same arrangement as roulette_render.py's grid rects -- repaint 1:1 over the exported template
(export_art_templates.py) and nothing else needs to change.

Chips reuse roulette_render._draw_chip so a bet looks the same on both tables."""

import io
import os

from PIL import Image, ImageDraw, ImageFont

import craps
from roulette_render import _draw_chip

WIDTH = 1100
HEIGHT = 516
BORDER = 26
PAD = 20

WOOD = (92, 51, 23, 255)
WOOD_HIGHLIGHT = (150, 100, 55, 255)
WOOD_SHADOW = (55, 28, 10, 255)
FELT = (13, 82, 46, 255)
LINE = (235, 230, 210, 255)
GOLD = (255, 205, 60, 255)
TEXT = (240, 240, 235, 255)
DIM_TEXT = (190, 205, 195, 255)
RED_TEXT = (235, 90, 90, 255)
PUCK_ON = (245, 245, 245, 255)
PUCK_OFF = (30, 30, 30, 255)

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_big_font = ImageFont.truetype(_FONT_PATH, 34)
_label_font = ImageFont.truetype(_FONT_PATH, 20)
_small_font = ImageFont.truetype(_FONT_PATH, 13)
_puck_font = ImageFont.truetype(_FONT_PATH, 15)

# Left side: place numbers, field, don't pass, pass line. Right side: the proposition box.
LEFT_X0 = BORDER + PAD
LEFT_X1 = 700
RIGHT_X0 = 720
RIGHT_X1 = WIDTH - BORDER - PAD
PLACE_Y = (BORDER + PAD, 156)
FIELD_Y = (170, 270)
DONTPASS_Y = (284, 354)
PASS_Y = (368, HEIGHT - BORDER - PAD)
ANY7_Y = (BORDER + PAD, 96)
HARD_Y = (106, 306)
HORN_Y = (316, 396)
ANYCRAPS_Y = (406, HEIGHT - BORDER - PAD)

HARD_ORDER = (6, 10, 8, 4)  # 2x2 grid, read left-to-right, top-to-bottom
HORN_ORDER = ("snake", "yo", "boxcars")

ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "craps")
_background_path = os.path.join(ASSET_DIR, "craps_table.png")


def _place_rect(n: int) -> tuple[int, int, int, int]:
    w = (LEFT_X1 - LEFT_X0) / len(craps.POINT_NUMBERS)
    i = craps.POINT_NUMBERS.index(n)
    return (int(LEFT_X0 + i * w), PLACE_Y[0], int(LEFT_X0 + (i + 1) * w), PLACE_Y[1])


def _hard_rect(n: int) -> tuple[int, int, int, int]:
    i = HARD_ORDER.index(n)
    w = (RIGHT_X1 - RIGHT_X0) / 2
    h = (HARD_Y[1] - HARD_Y[0]) / 2
    x0 = RIGHT_X0 + (i % 2) * w
    y0 = HARD_Y[0] + (i // 2) * h
    return (int(x0), int(y0), int(x0 + w), int(y0 + h))


def _horn_rect(kind: str) -> tuple[int, int, int, int]:
    i = HORN_ORDER.index(kind)
    w = (RIGHT_X1 - RIGHT_X0) / 3
    return (int(RIGHT_X0 + i * w), HORN_Y[0], int(RIGHT_X0 + (i + 1) * w), HORN_Y[1])


def _bet_rect(kind: str, value: int | None) -> tuple[int, int, int, int]:
    if kind == "place":
        return _place_rect(value)
    if kind == "hard":
        return _hard_rect(value)
    if kind in HORN_ORDER:
        return _horn_rect(kind)
    return {
        "field": (LEFT_X0, FIELD_Y[0], LEFT_X1, FIELD_Y[1]),
        "dontpass": (LEFT_X0, DONTPASS_Y[0], LEFT_X1, DONTPASS_Y[1]),
        "pass": (LEFT_X0, PASS_Y[0], LEFT_X1, PASS_Y[1]),
        "any7": (RIGHT_X0, ANY7_Y[0], RIGHT_X1, ANY7_Y[1]),
        "anycraps": (RIGHT_X0, ANYCRAPS_Y[0], RIGHT_X1, ANYCRAPS_Y[1]),
    }[kind]


def _centered_text(draw: ImageDraw.ImageDraw, cx: float, cy: float, text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def _box(draw: ImageDraw.ImageDraw, rect, title: str, subtitle: str | None = None, *, font=None, color=TEXT):
    draw.rectangle(rect, outline=LINE, width=3)
    x0, y0, x1, y1 = rect
    cx = (x0 + x1) / 2
    if subtitle:
        _centered_text(draw, cx, y0 + (y1 - y0) * 0.38, title, font or _label_font, color)
        _centered_text(draw, cx, y0 + (y1 - y0) * 0.75, subtitle, _small_font, DIM_TEXT)
    else:
        _centered_text(draw, cx, (y0 + y1) / 2, title, font or _label_font, color)


def _procedural_table() -> Image.Image:
    img = Image.new("RGBA", (WIDTH, HEIGHT), WOOD)
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, WIDTH - 3, HEIGHT - 3], outline=WOOD_HIGHLIGHT, width=2)
    draw.rectangle([BORDER - 7, BORDER - 7, WIDTH - BORDER + 6, HEIGHT - BORDER + 6], outline=WOOD_SHADOW, width=3)
    draw.rounded_rectangle([BORDER, BORDER, WIDTH - BORDER, HEIGHT - BORDER], radius=30, fill=FELT)

    for n in craps.POINT_NUMBERS:
        num, den = craps.PLACE_PAYOUT[n]
        _box(draw, _place_rect(n), "SIX" if n == 6 else ("NINE" if n == 9 else str(n)), f"PAYS {num} TO {den}", font=_big_font)
    _box(draw, _bet_rect("field", None), "FIELD   2 · 3 · 4 · 9 · 10 · 11 · 12", "2 PAYS DOUBLE   ·   12 PAYS TRIPLE", color=GOLD)
    _box(draw, _bet_rect("dontpass", None), "DON'T PASS BAR", "BAR 12   ·   PAYS 1 TO 1")
    _box(draw, _bet_rect("pass", None), "PASS LINE", "PAYS 1 TO 1", font=_big_font)

    _box(draw, _bet_rect("any7", None), "SEVEN   PAYS 4 TO 1", color=RED_TEXT)
    for n in HARD_ORDER:
        num, _ = craps.HARD_PAYOUT[n]
        h = n // 2
        _box(draw, _hard_rect(n), f"HARD {n}", f"{h}-{h}   PAYS {num} TO 1")
    _box(draw, _horn_rect("snake"), "1-1", "30 TO 1")
    _box(draw, _horn_rect("yo"), "YO 11", "15 TO 1")
    _box(draw, _horn_rect("boxcars"), "6-6", "30 TO 1")
    _box(draw, _bet_rect("anycraps", None), "ANY CRAPS   PAYS 7 TO 1", color=RED_TEXT)
    return img


_table_background = (
    Image.open(_background_path).convert("RGBA").resize((WIDTH, HEIGHT))
    if os.path.exists(_background_path)
    else None
)


def _base_table() -> Image.Image:
    return (_table_background or _procedural_table()).copy()


def _draw_puck(draw: ImageDraw.ImageDraw, point: int | None):
    """The ON puck sits on the point's place box; with no point, OFF sits in the corner of the
    Pass Line so everyone can see it's a come-out roll."""
    r = 20
    if point is None:
        cx, cy = LEFT_X1 - 40, PASS_Y[0] + 30
        fill, text_fill, label = PUCK_OFF, PUCK_ON, "OFF"
    else:
        x0, y0, x1, _ = _place_rect(point)
        cx, cy = x1 - 26, y0 + 24
        fill, text_fill, label = PUCK_ON, PUCK_OFF, "ON"
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=GOLD, width=3)
    _centered_text(draw, cx, cy, label, _puck_font, text_fill)


def _winning_rects(dice: tuple[int, int], point_before: int | None) -> list[tuple[int, int, int, int]]:
    """Every area on the felt this roll paid, whether or not anyone had a chip there."""
    d1, d2 = dice
    areas = [("pass", None), ("dontpass", None), ("field", None), ("any7", None), ("anycraps", None)]
    areas += [(k, None) for k in HORN_ORDER]
    areas += [("place", n) for n in craps.POINT_NUMBERS]
    areas += [("hard", n) for n in craps.HARD_NUMBERS]
    rects = []
    for kind, value in areas:
        paid = craps.settle(kind, value, 10, d1, d2, point_before)
        if paid and paid > 10:
            rects.append(_bet_rect(kind, value))
    return rects


def render_table(
    bets: list[dict], point: int | None, dice: tuple[int, int] | None = None, point_before: int | None = None,
) -> io.BytesIO:
    """The felt with everyone's chips and the point puck. With `dice`, also outlines every area
    that roll paid (judged against `point_before`, the point going into the roll)."""
    img = _base_table()
    draw = ImageDraw.Draw(img)

    if dice is not None:
        for rect in _winning_rects(dice, point_before):
            draw.rectangle(rect, outline=GOLD, width=6)

    _draw_puck(draw, point)

    grouped: dict[tuple, list[dict]] = {}
    for bet in bets:
        grouped.setdefault((bet["kind"], bet["value"]), []).append(bet)

    offsets = [(0, 0), (-22, 0), (22, 0), (-44, 0), (44, 0), (-66, 0), (66, 0)]
    for (kind, value), group in grouped.items():
        x0, y0, x1, y1 = _bet_rect(kind, value)
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        shown = group[: len(offsets)]
        for i, bet in enumerate(shown):
            dx, dy = offsets[i]
            _draw_chip(draw, int(cx + dx), int(cy + dy), bet["display_name"][:2].upper())
        if len(group) > len(shown):
            _draw_chip(draw, int(cx), int(cy - 22), f"+{len(group) - len(shown)}")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


DIE_SIZE = 150
DICE_W = 440
DICE_H = 240
DIE_FILL = (200, 25, 35, 255)
DIE_EDGE = (110, 10, 18, 255)
PIP = (250, 250, 245, 255)

_PIP_LAYOUT = {
    1: [(0.5, 0.5)],
    2: [(0.27, 0.27), (0.73, 0.73)],
    3: [(0.27, 0.27), (0.5, 0.5), (0.73, 0.73)],
    4: [(0.27, 0.27), (0.73, 0.27), (0.27, 0.73), (0.73, 0.73)],
    5: [(0.27, 0.27), (0.73, 0.27), (0.5, 0.5), (0.27, 0.73), (0.73, 0.73)],
    6: [(0.27, 0.25), (0.73, 0.25), (0.27, 0.5), (0.73, 0.5), (0.27, 0.75), (0.73, 0.75)],
}


def _draw_die(draw: ImageDraw.ImageDraw, x0: int, y0: int, face: int):
    draw.rounded_rectangle([x0, y0, x0 + DIE_SIZE, y0 + DIE_SIZE], radius=22, fill=DIE_FILL, outline=DIE_EDGE, width=4)
    pr = DIE_SIZE * 0.075
    for fx, fy in _PIP_LAYOUT[face]:
        px, py = x0 + fx * DIE_SIZE, y0 + fy * DIE_SIZE
        draw.ellipse([px - pr, py - pr, px + pr, py + pr], fill=PIP)


def render_dice(d1: int, d2: int) -> io.BytesIO:
    """The roll result, roulette's wheel-image equivalent: two dice on a strip of felt."""
    img = Image.new("RGBA", (DICE_W, DICE_H), WOOD)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([12, 12, DICE_W - 12, DICE_H - 12], radius=24, fill=FELT, outline=WOOD_SHADOW, width=3)
    gap = (DICE_W - 2 * DIE_SIZE) / 3
    y0 = int((DICE_H - DIE_SIZE) / 2)
    _draw_die(draw, int(gap), y0, d1)
    _draw_die(draw, int(2 * gap + DIE_SIZE), y0, d2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
