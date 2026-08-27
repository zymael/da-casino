import io
import math

from PIL import Image, ImageDraw, ImageFont

import roulette

CELL = 60
ZERO_WIDTH = 60
OUTSIDE_HEIGHT = 74
COLUMN_BOX_WIDTH = 50
DOZEN_HEIGHT = 36
GRID_WIDTH = ZERO_WIDTH + 12 * CELL
GRID_HEIGHT = 3 * CELL
# Wood trim border around the felt playing surface -- baked into the 4 primitive rect functions
# below (_cell_rect/_outside_rect/_dozen_rect/_column_box_rect all add BORDER to their return
# coordinates) rather than into _base_table alone, so every caller that positions something on the
# table (render_table's chip placement, the winning-number highlight, _combo_rect) is automatically
# correct without needing to know a border exists at all.
BORDER = 26
GRID_TABLE_WIDTH = GRID_WIDTH + COLUMN_BOX_WIDTH
GRID_TABLE_HEIGHT = GRID_HEIGHT + DOZEN_HEIGHT + OUTSIDE_HEIGHT
IMG_WIDTH = GRID_TABLE_WIDTH + 2 * BORDER
IMG_HEIGHT = GRID_TABLE_HEIGHT + 2 * BORDER

FELT = (10, 90, 40, 255)
RED = (176, 30, 30, 255)
BLACK = (25, 25, 25, 255)
GREEN = (20, 130, 60, 255)
LINE = (230, 230, 220, 255)
GOLD = (255, 200, 40, 255)
CHIP_FILL = (250, 240, 200, 255)
CHIP_OUTLINE = (60, 40, 10, 255)
# Wood trim -- a solid walnut base plus a lighter outer bevel and darker inner bevel, the same
# simple raised-frame trick real picture frames/table rims use, rather than an attempted procedural
# grain texture (this is a template meant to be repainted into real art anyway, per
# export_art_templates.py's own docstring -- it only needs to clearly read as "wood trim here").
WOOD = (92, 51, 23, 255)
WOOD_HIGHLIGHT = (150, 100, 55, 255)
WOOD_SHADOW = (55, 28, 10, 255)

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_number_font = ImageFont.truetype(_FONT_PATH, 20)
_label_font = ImageFont.truetype(_FONT_PATH, 16)
_chip_font = ImageFont.truetype(_FONT_PATH, 13)

OUTSIDE_BOXES = ["low", "even", "red", "black", "odd", "high"]
OUTSIDE_LABELS = {"low": "1-18", "even": "EVEN", "red": "RED", "black": "BLACK", "odd": "ODD", "high": "19-36"}
DOZEN_LABELS = {1: "1st12", 2: "2nd12", 3: "3rd12"}

# Standard European single-zero wheel pocket order, reading around the rim.
WHEEL_ORDER = [
    0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5,
    24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26,
]

WHEEL_SIZE = 440
WHEEL_CENTER = WHEEL_SIZE / 2
WHEEL_OUTER_R = 210
WHEEL_INNER_R = 145
WHEEL_HUB_R = 80
_wheel_number_font = ImageFont.truetype(_FONT_PATH, 17)


def _cell_rect(number: int) -> tuple[int, int, int, int]:
    """Pixel (x0, y0, x1, y1) for a number's cell, offset by BORDER (see its own comment). Number 0
    spans all 3 rows on the left."""
    if number == 0:
        return (BORDER, BORDER, BORDER + ZERO_WIDTH, BORDER + GRID_HEIGHT)
    col = (number - 1) // 3
    row_in_col = (number - 1) % 3  # 0 = bottom, 2 = top
    visual_row = 2 - row_in_col
    x0 = BORDER + ZERO_WIDTH + col * CELL
    y0 = BORDER + visual_row * CELL
    return (x0, y0, x0 + CELL, y0 + CELL)


def _outside_rect(kind: str) -> tuple[int, int, int, int]:
    idx = OUTSIDE_BOXES.index(kind)
    box_w = GRID_TABLE_WIDTH / len(OUTSIDE_BOXES)
    x0 = BORDER + idx * box_w
    y0 = BORDER + GRID_HEIGHT + DOZEN_HEIGHT
    return (int(x0), y0, int(x0 + box_w), y0 + OUTSIDE_HEIGHT)


def _dozen_rect(value: int) -> tuple[int, int, int, int]:
    """value is 1, 2, or 3 — the three dozen boxes sit under the number grid (not the column boxes)."""
    box_w = (GRID_WIDTH - ZERO_WIDTH) / 3
    x0 = BORDER + ZERO_WIDTH + (value - 1) * box_w
    y0 = BORDER + GRID_HEIGHT
    return (int(x0), y0, int(x0 + box_w), y0 + DOZEN_HEIGHT)


def _column_box_rect(value: int) -> tuple[int, int, int, int]:
    """value is 1, 2, or 3 — the "2 to 1" boxes to the right of the grid, aligned with that column's row."""
    visual_row = 3 - value
    x0 = BORDER + GRID_WIDTH
    y0 = BORDER + visual_row * CELL
    return (x0, y0, x0 + COLUMN_BOX_WIDTH, y0 + CELL)


def _centered_text(draw: ImageDraw.ImageDraw, rect, text, font, fill):
    x0, y0, x1, y1 = rect
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((x0 + x1 - w) / 2 - bbox[0], (y0 + y1 - h) / 2 - bbox[1]), text, font=font, fill=fill)


def _base_table() -> Image.Image:
    img = Image.new("RGBA", (IMG_WIDTH, IMG_HEIGHT), WOOD)
    draw = ImageDraw.Draw(img)
    # A simple raised-bevel look: a lighter highlight near the outer edge, a darker shadow near the
    # inner edge (where the trim meets the felt) -- reads as a routed wooden rim without needing an
    # actual grain texture (see BORDER's own comment for why that's fine here).
    draw.rectangle([2, 2, IMG_WIDTH - 3, IMG_HEIGHT - 3], outline=WOOD_HIGHLIGHT, width=2)
    draw.rectangle(
        [BORDER - 7, BORDER - 7, IMG_WIDTH - BORDER + 6, IMG_HEIGHT - BORDER + 6],
        outline=WOOD_SHADOW, width=3,
    )
    # Felt playing surface, inset by BORDER on every side -- everything below this still fills in
    # its own cell/box color, but the dozen row has small gaps beside the "0" column and beside the
    # column boxes (see _dozen_rect/_outside_rect) that only this base fill covers.
    draw.rectangle([BORDER, BORDER, IMG_WIDTH - BORDER, IMG_HEIGHT - BORDER], fill=FELT)

    for n in range(37):
        rect = _cell_rect(n)
        color = GREEN if n == 0 else (RED if n in roulette.RED_NUMBERS else BLACK)
        draw.rectangle(rect, fill=color, outline=LINE, width=1)
        _centered_text(draw, rect, str(n), _number_font, (255, 255, 255, 255))

    for kind in OUTSIDE_BOXES:
        rect = _outside_rect(kind)
        color = RED if kind == "red" else BLACK if kind == "black" else FELT
        draw.rectangle(rect, fill=color, outline=LINE, width=1)
        label_rect = (rect[0], rect[1], rect[2], rect[1] + 24)
        _centered_text(draw, label_rect, OUTSIDE_LABELS[kind], _label_font, (255, 255, 255, 255))

    for value in (1, 2, 3):
        rect = _dozen_rect(value)
        draw.rectangle(rect, fill=FELT, outline=LINE, width=1)
        _centered_text(draw, rect, DOZEN_LABELS[value], _label_font, (255, 255, 255, 255))

    for value in (1, 2, 3):
        rect = _column_box_rect(value)
        draw.rectangle(rect, fill=FELT, outline=LINE, width=1)
        _centered_text(draw, rect, "2:1", _label_font, (255, 255, 255, 255))

    return img


def _combo_rect(numbers) -> tuple[int, int, int, int]:
    """A small rect centered on the average position of the given numbers' cells."""
    centers = [_cell_rect(n) for n in numbers]
    cx = sum((r[0] + r[2]) / 2 for r in centers) / len(centers)
    cy = sum((r[1] + r[3]) / 2 for r in centers) / len(centers)
    half = CELL / 4
    return (int(cx - half), int(cy - half), int(cx + half), int(cy + half))


def _bet_cell_rect(bet: dict) -> tuple[int, int, int, int]:
    kind = bet["kind"]
    if kind == "number":
        return _cell_rect(bet["value"])
    if kind == "column":
        return _column_box_rect(bet["value"])
    if kind == "dozen":
        return _dozen_rect(bet["value"])
    if kind == "combo":
        return _combo_rect(bet["value"])
    return _outside_rect(kind)


def _draw_chip(draw: ImageDraw.ImageDraw, cx: int, cy: int, label: str):
    r = 15
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=CHIP_FILL, outline=CHIP_OUTLINE, width=2)
    for i in range(6):
        angle = math.pi / 3 * i
        dot_x = cx + (r - 4) * math.cos(angle)
        dot_y = cy + (r - 4) * math.sin(angle)
        draw.ellipse([dot_x - 2, dot_y - 2, dot_x + 2, dot_y + 2], fill=CHIP_OUTLINE)
    bbox = draw.textbbox((0, 0), label, font=_chip_font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), label, font=_chip_font, fill=CHIP_OUTLINE)


def render_table(bets: list[dict], winning_number: int | None = None) -> io.BytesIO:
    """Draws the roulette table with a chip per bet, optionally highlighting the winning cell."""
    img = _base_table()
    draw = ImageDraw.Draw(img)

    if winning_number is not None:
        rect = _cell_rect(winning_number)
        draw.rectangle(rect, outline=GOLD, width=5)

    grouped: dict[tuple, list[dict]] = {}
    for bet in bets:
        key = (bet["kind"], bet["value"])
        grouped.setdefault(key, []).append(bet)

    OFFSETS = [(0, 0), (-12, -10), (12, -10), (-12, 10), (12, 10)]
    for key, group in grouped.items():
        x0, y0, x1, y1 = _bet_cell_rect(group[0])
        cx = (x0 + x1) / 2
        # Outside boxes (red/black/odd/even/low/high) have a label at the top, so chips sit below
        # it. Everything else (number/column/dozen/combo cells) is small enough that the chip
        # sits dead-center, covering the label/number, like a real table.
        cy = y0 + 24 + (y1 - y0 - 24) / 2 if group[0]["kind"] in OUTSIDE_BOXES else (y0 + y1) / 2
        shown = group[:5]
        for i, bet in enumerate(shown):
            dx, dy = OFFSETS[i]
            label = bet["display_name"][:2].upper()
            _draw_chip(draw, int(cx + dx), int(cy + dy), label)
        if len(group) > len(shown):
            _draw_chip(draw, int(cx), int(cy - 16), f"+{len(group) - len(shown)}")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_wheel(winning_number: int | None = None) -> io.BytesIO:
    """Draws the wheel with pockets in real wheel order, optionally highlighting the result."""
    img = Image.new("RGBA", (WHEEL_SIZE, WHEEL_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    step = 360 / len(WHEEL_ORDER)
    bbox_outer = [
        WHEEL_CENTER - WHEEL_OUTER_R, WHEEL_CENTER - WHEEL_OUTER_R,
        WHEEL_CENTER + WHEEL_OUTER_R, WHEEL_CENTER + WHEEL_OUTER_R,
    ]

    for i, number in enumerate(WHEEL_ORDER):
        mid = -90 + i * step
        color = GREEN if number == 0 else (RED if number in roulette.RED_NUMBERS else BLACK)
        draw.pieslice(bbox_outer, mid - step / 2, mid + step / 2, fill=color, outline=LINE, width=1)

    bbox_inner = [
        WHEEL_CENTER - WHEEL_INNER_R, WHEEL_CENTER - WHEEL_INNER_R,
        WHEEL_CENTER + WHEEL_INNER_R, WHEEL_CENTER + WHEEL_INNER_R,
    ]
    draw.ellipse(bbox_inner, fill=FELT, outline=LINE, width=2)

    bbox_hub = [
        WHEEL_CENTER - WHEEL_HUB_R, WHEEL_CENTER - WHEEL_HUB_R,
        WHEEL_CENTER + WHEEL_HUB_R, WHEEL_CENTER + WHEEL_HUB_R,
    ]
    draw.ellipse(bbox_hub, fill=(40, 25, 10, 255), outline=GOLD, width=3)

    ring_r = (WHEEL_OUTER_R + WHEEL_INNER_R) / 2
    for i, number in enumerate(WHEEL_ORDER):
        mid = -90 + i * step
        label = Image.new("RGBA", (40, 24), (0, 0, 0, 0))
        label_draw = ImageDraw.Draw(label)
        text = str(number)
        bbox = label_draw.textbbox((0, 0), text, font=_wheel_number_font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        label_draw.text(((40 - w) / 2 - bbox[0], (24 - h) / 2 - bbox[1]), text, font=_wheel_number_font, fill=(255, 255, 255, 255))
        rotated = label.rotate(-(mid + 90), expand=True)
        rx = WHEEL_CENTER + ring_r * math.cos(math.radians(mid))
        ry = WHEEL_CENTER + ring_r * math.sin(math.radians(mid))
        img.alpha_composite(rotated, (int(rx - rotated.width / 2), int(ry - rotated.height / 2)))

    draw.ellipse(bbox_outer, outline=(200, 170, 90, 255), width=4)

    if winning_number is not None:
        idx = WHEEL_ORDER.index(winning_number)
        mid = -90 + idx * step
        draw.pieslice(bbox_outer, mid - step / 2, mid + step / 2, outline=GOLD, width=6)
        ball_r = WHEEL_OUTER_R - 14
        bx = WHEEL_CENTER + ball_r * math.cos(math.radians(mid))
        by = WHEEL_CENTER + ball_r * math.sin(math.radians(mid))
        ball_size = 9
        draw.ellipse(
            [bx - ball_size, by - ball_size, bx + ball_size, by + ball_size],
            fill=(255, 255, 255, 255), outline=(30, 30, 30, 255), width=2,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
