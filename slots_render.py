import io
import os

from PIL import Image, ImageDraw, ImageFont

import slots

CELL = 110
GAP = 12
ROWS = slots.GRID_ROWS
COLS = slots.GRID_COLS
FRAME = 26
HEADER_H = 52
BASE_H = 34
LEVER_W = 46
PAYTABLE_W = 122

WINDOW_W = COLS * CELL + (COLS - 1) * GAP
WINDOW_H = ROWS * CELL + (ROWS - 1) * GAP
IMG_W = PAYTABLE_W + WINDOW_W + 2 * FRAME + LEVER_W
IMG_H = HEADER_H + WINDOW_H + 2 * FRAME + BASE_H
WINDOW_X0 = FRAME + PAYTABLE_W
WINDOW_Y0 = HEADER_H + FRAME

# Cabinet art (frame, lever, paytable glass) and the six reel symbols are real images now --
# assets/slots/cabinet_background.jpg is pre-authored at exactly IMG_W x IMG_H so the reel window
# drawn below lines up with its glass cutout, and each symbol PNG is pre-authored near CELL size.
ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "slots")

SYMBOL_ASSET_NAMES = {
    "🍒": "cherries", "🍋": "lemon", "🔔": "bell",
    "🍀": "clover", "💎": "diamond", "7️⃣": "seven",
}

_cabinet_background = Image.open(os.path.join(ASSET_DIR, "cabinet_background.jpg")).convert("RGBA").resize((IMG_W, IMG_H))
SYMBOL_IMAGES = {
    emoji: Image.open(os.path.join(ASSET_DIR, f"{name}.png")).convert("RGBA")
    for emoji, name in SYMBOL_ASSET_NAMES.items()
}

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_placeholder_font = ImageFont.truetype(_FONT_PATH, int(CELL * 0.4))

LINE_COLORS = [
    (255, 210, 60, 235),   # line 1 (middle) — gold
    (80, 200, 255, 235),   # line 2 (top) — cyan
    (255, 90, 190, 235),   # line 3 (bottom) — magenta
    (255, 140, 40, 235),   # line 4 (diagonal down)
    (140, 240, 90, 235),   # line 5 (diagonal up)
]


def _cell_center(row: int, col: int) -> tuple[float, float]:
    cx = WINDOW_X0 + col * (CELL + GAP) + CELL / 2
    cy = WINDOW_Y0 + row * (CELL + GAP) + CELL / 2
    return cx, cy


def _centered_text(draw: ImageDraw.ImageDraw, cx, cy, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def render_reels(grid: list[list[str]] | None, winning_lines: list | None = None) -> io.BytesIO:
    """Draws the slot machine cabinet with the given grid (or a placeholder if None),
    highlighting any winning paylines. winning_lines is a list of (line_index, symbols, payout)."""
    img = _cabinet_background.copy()
    draw = ImageDraw.Draw(img)

    for row in range(ROWS):
        for col in range(COLS):
            cx, cy = _cell_center(row, col)
            if grid is None:
                draw.ellipse([cx - 42, cy - 42, cx + 42, cy + 42], fill=(20, 10, 15, 160))
                _centered_text(draw, cx, cy, "?", _placeholder_font, (230, 210, 180, 255))
            else:
                symbol = SYMBOL_IMAGES[grid[row][col]]
                dest = (int(cx - symbol.width / 2), int(cy - symbol.height / 2))
                img.alpha_composite(symbol, dest)

    if winning_lines:
        overlay = Image.new("RGBA", (IMG_W, IMG_H), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        for line_index, _symbols, _payout in winning_lines:
            color = LINE_COLORS[line_index % len(LINE_COLORS)]
            points = [_cell_center(row, col) for col, row in enumerate(slots.PAYLINES[line_index])]
            overlay_draw.line(points, fill=color, width=6, joint="curve")
            for px, py in points:
                overlay_draw.ellipse([px - 7, py - 7, px + 7, py + 7], fill=color)
        img = Image.alpha_composite(img, overlay)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
