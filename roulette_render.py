import io
import math
import os

from PIL import Image, ImageDraw, ImageFont

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

GOLD = (255, 200, 40, 255)
CHIP_FILL = (250, 240, 200, 255)
CHIP_OUTLINE = (60, 40, 10, 255)

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_chip_font = ImageFont.truetype(_FONT_PATH, 13)

OUTSIDE_BOXES = ["low", "even", "red", "black", "odd", "high"]

# The table's whole felt/number-grid/label layer is real painted art now (assets/roulette/
# roulette.jpg, repainted 1:1 over an exported template at exactly IMG_WIDTH x IMG_HEIGHT -- see
# export_art_templates.py) -- _base_table below just loads it, same "load once, .copy() per render"
# pattern slots_render.py's cabinet background already uses. BORDER and every _*_rect function
# above/below are unchanged and still load-bearing: they're what lines up a bet chip or the
# winning-number highlight against this art's grid at render time.
ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "roulette")
_table_background = Image.open(os.path.join(ASSET_DIR, "roulette.jpg")).convert("RGBA").resize((IMG_WIDTH, IMG_HEIGHT))

# Standard European single-zero wheel pocket order, reading around the rim.
WHEEL_ORDER = [
    0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5,
    24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26,
]

WHEEL_SIZE = 440
WHEEL_CENTER = WHEEL_SIZE / 2
WHEEL_OUTER_R = 210
WHEEL_HUB_R = 80  # still used by export_art_templates.py's standalone hub template export

# Real painted art (assets/roulette/) for both wheel layers -- see render_wheel's own docstring for
# why this is two composited pieces rather than one flat image.
_wheel_background = Image.open(os.path.join(ASSET_DIR, "roulette_wheel.png")).convert("RGBA").resize((WHEEL_SIZE, WHEEL_SIZE))
_wheel_hub = Image.open(os.path.join(ASSET_DIR, "roulette_Base.png")).convert("RGBA")


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


def _base_table() -> Image.Image:
    return _table_background.copy()


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
    """Draws the wheel (real art, pockets/numbers already painted in -- assets/roulette/
    roulette_wheel.png, plus the hub medallion assets/roulette/roulette_Base.png composited on top
    of it as its own layer, mirroring the two-piece wheel_background/wheel_hub split
    export_art_templates.py originally exported) plus the one dynamic per-spin overlay: the
    winning pocket's gold highlight and the ball, drawn only once a result is known."""
    img = _wheel_background.copy()
    img.alpha_composite(
        _wheel_hub,
        (int(WHEEL_CENTER - _wheel_hub.width / 2), int(WHEEL_CENTER - _wheel_hub.height / 2)),
    )
    draw = ImageDraw.Draw(img)

    if winning_number is not None:
        step = 360 / len(WHEEL_ORDER)
        idx = WHEEL_ORDER.index(winning_number)
        mid = -90 + idx * step
        bbox_outer = [
            WHEEL_CENTER - WHEEL_OUTER_R, WHEEL_CENTER - WHEEL_OUTER_R,
            WHEEL_CENTER + WHEEL_OUTER_R, WHEEL_CENTER + WHEEL_OUTER_R,
        ]
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
