import io

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

CABINET = (64, 18, 22, 255)
CABINET_DARK = (44, 10, 14, 255)
TRIM = (230, 180, 60, 255)
TRIM_DARK = (150, 110, 30, 255)
WINDOW_BG = (12, 12, 16, 255)
CELL_BACK = (244, 238, 222, 255)
DIVIDER = (25, 25, 30, 255)

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_title_font = ImageFont.truetype(_FONT_PATH, 22)
_seven_font = ImageFont.truetype(_FONT_PATH, int(CELL * 0.5))
_placeholder_font = ImageFont.truetype(_FONT_PATH, int(CELL * 0.4))
_paytable_header_font = ImageFont.truetype(_FONT_PATH, 15)
_paytable_row_font = ImageFont.truetype(_FONT_PATH, 17)
_paytable_note_font = ImageFont.truetype(_FONT_PATH, 12)

# Highest payout first, like the paytable glass on a real cabinet.
PAYTABLE_ROWS = ["7️⃣", "💎", "🍀", "🔔", "🍋", "🍒"]

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


def _draw_cherries(draw: ImageDraw.ImageDraw, cx, cy, r):
    stem_color = (90, 60, 20, 255)
    leaf_color = (45, 150, 65, 255)
    ball_color = (190, 20, 30, 255)
    ball_r = r * 0.4
    top = (cx, cy - r * 0.55)
    left_c = (cx - r * 0.32, cy + r * 0.25)
    right_c = (cx + r * 0.32, cy + r * 0.25)
    draw.line([top, left_c], fill=stem_color, width=3)
    draw.line([top, right_c], fill=stem_color, width=3)
    draw.ellipse([top[0] - r * 0.05, top[1] - r * 0.32, top[0] + r * 0.35, top[1] + r * 0.05], fill=leaf_color)
    for c in (left_c, right_c):
        draw.ellipse(
            [c[0] - ball_r, c[1] - ball_r, c[0] + ball_r, c[1] + ball_r],
            fill=ball_color, outline=(110, 10, 15, 255), width=2,
        )
        hl = ball_r * 0.3
        hx, hy = c[0] - ball_r * 0.35, c[1] - ball_r * 0.35
        draw.ellipse([hx - hl, hy - hl, hx + hl, hy + hl], fill=(255, 255, 255, 110))


def _draw_lemon(draw: ImageDraw.ImageDraw, cx, cy, r):
    w, h = r * 0.95, r * 1.2
    draw.ellipse([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], fill=(235, 205, 40, 255), outline=(170, 140, 10, 255), width=2)
    draw.ellipse([cx - w * 0.1, cy - h / 2 - 3, cx + w * 0.1, cy - h / 2 + 7], fill=(180, 150, 10, 255))
    draw.ellipse([cx - w * 0.1, cy + h / 2 - 7, cx + w * 0.1, cy + h / 2 + 3], fill=(180, 150, 10, 255))
    draw.line([(cx - w * 0.18, cy - h * 0.1), (cx + w * 0.1, cy - h * 0.3)], fill=(250, 235, 150, 255), width=2)


def _draw_bell(draw: ImageDraw.ImageDraw, cx, cy, r):
    gold, outline = (225, 175, 40, 255), (120, 80, 10, 255)
    top = cy - r * 0.55
    draw.pieslice([cx - r * 0.5, top - r * 0.05, cx + r * 0.5, top + r * 0.65], 180, 360, fill=gold, outline=outline, width=2)
    draw.polygon(
        [(cx - r * 0.5, top + r * 0.3), (cx + r * 0.5, top + r * 0.3), (cx + r * 0.68, cy + r * 0.3), (cx - r * 0.68, cy + r * 0.3)],
        fill=gold, outline=outline,
    )
    draw.rectangle([cx - r * 0.78, cy + r * 0.3, cx + r * 0.78, cy + r * 0.4], fill=outline)
    draw.ellipse([cx - r * 0.1, cy + r * 0.42, cx + r * 0.1, cy + r * 0.62], fill=outline)
    draw.ellipse([cx - r * 0.09, top - r * 0.2, cx + r * 0.09, top - r * 0.04], outline=outline, width=2)


def _draw_clover(draw: ImageDraw.ImageDraw, cx, cy, r):
    green, outline = (42, 150, 72, 255), (20, 90, 40, 255)
    lobe_r = r * 0.34
    for ox, oy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
        lc = (cx + ox * lobe_r * 0.85, cy + oy * lobe_r * 0.85)
        draw.ellipse([lc[0] - lobe_r, lc[1] - lobe_r, lc[0] + lobe_r, lc[1] + lobe_r], fill=green, outline=outline, width=2)
    draw.line([(cx, cy + lobe_r * 0.5), (cx, cy + r * 0.65)], fill=(90, 60, 20, 255), width=3)


def _draw_diamond(draw: ImageDraw.ImageDraw, cx, cy, r):
    cyan, outline, light = (70, 190, 225, 255), (20, 100, 130, 255), (215, 245, 250, 255)
    pts = [(cx, cy - r * 0.62), (cx + r * 0.5, cy - r * 0.08), (cx, cy + r * 0.62), (cx - r * 0.5, cy - r * 0.08)]
    draw.polygon(pts, fill=cyan, outline=outline)
    draw.line([pts[3], pts[0]], fill=light, width=2)
    draw.line([pts[0], pts[1]], fill=light, width=2)


def _draw_seven(draw: ImageDraw.ImageDraw, cx, cy, r):
    draw.ellipse([cx - r * 0.65, cy - r * 0.65, cx + r * 0.65, cy + r * 0.65], fill=(250, 250, 245, 255), outline=(205, 40, 40, 255), width=3)
    _centered_text(draw, cx, cy - r * 0.03, "7", _seven_font, (210, 30, 30, 255))


SYMBOL_DRAWERS = {
    "🍒": _draw_cherries,
    "🍋": _draw_lemon,
    "🔔": _draw_bell,
    "🍀": _draw_clover,
    "💎": _draw_diamond,
    "7️⃣": _draw_seven,
}


def _draw_paytable(draw: ImageDraw.ImageDraw):
    x0, y0 = FRAME, WINDOW_Y0 - 6
    x1, y1 = FRAME + PAYTABLE_W - 10, WINDOW_Y0 + WINDOW_H + 6
    draw.rectangle([x0, y0, x1, y1], fill=CABINET_DARK, outline=TRIM_DARK, width=2)

    header_h = 22
    _centered_text(draw, (x0 + x1) / 2, y0 + header_h / 2 + 2, "PAYOUTS", _paytable_header_font, TRIM)
    draw.line([(x0 + 8, y0 + header_h), (x1 - 8, y0 + header_h)], fill=TRIM_DARK, width=1)

    note_h = 38
    rows_top = y0 + header_h + 4
    row_h = (y1 - note_h - rows_top) / len(PAYTABLE_ROWS)
    icon_r = min(row_h, 34) * 0.36
    for i, symbol in enumerate(PAYTABLE_ROWS):
        cy = rows_top + row_h * (i + 0.5)
        icon_cx = x0 + 22
        SYMBOL_DRAWERS[symbol](draw, icon_cx, cy, icon_r)
        _centered_text(draw, x0 + 22 + PAYTABLE_W / 2 - 8, cy, f"x{int(slots.SYMBOLS[symbol]['triple'])}", _paytable_row_font, TRIM)

    note_y = y1 - note_h
    draw.line([(x0 + 8, note_y), (x1 - 8, note_y)], fill=TRIM_DARK, width=1)
    pair_icon_cy = note_y + note_h * 0.32
    _draw_cherries(draw, x0 + 20, pair_icon_cy, icon_r * 0.8)
    _draw_cherries(draw, x0 + 36, pair_icon_cy, icon_r * 0.8)
    _centered_text(draw, x0 + 22 + PAYTABLE_W / 2 - 8, pair_icon_cy, f"x{slots.CHERRY_PAIR_PAYOUT}", _paytable_row_font, TRIM)
    _centered_text(draw, (x0 + x1) / 2, note_y + note_h * 0.8, "(any 2 cherries)", _paytable_note_font, (220, 210, 190, 255))


def _draw_cabinet(draw: ImageDraw.ImageDraw):
    draw.rectangle([0, 0, IMG_W - 1, IMG_H - 1], fill=CABINET)
    draw.rectangle([4, 4, IMG_W - 5, IMG_H - 5], outline=TRIM, width=4)
    _centered_text(draw, IMG_W / 2, HEADER_H / 2 + 2, "DA CASINO SLOTS", _title_font, TRIM)

    window_rect = [WINDOW_X0 - 6, WINDOW_Y0 - 6, WINDOW_X0 + WINDOW_W + 6, WINDOW_Y0 + WINDOW_H + 6]
    draw.rectangle(window_rect, fill=WINDOW_BG, outline=TRIM_DARK, width=3)
    _draw_paytable(draw)

    for cx, cy in [(16, 16), (IMG_W - 16, 16), (16, IMG_H - 16), (IMG_W - 16, IMG_H - 16)]:
        draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=TRIM, outline=TRIM_DARK, width=1)

    lever_x = IMG_W - LEVER_W / 2 - 6
    draw.rectangle([lever_x - 5, HEADER_H, lever_x + 5, HEADER_H + WINDOW_H * 0.55], fill=(180, 180, 185, 255), outline=(60, 60, 65, 255), width=2)
    knob_y = HEADER_H + WINDOW_H * 0.55
    draw.ellipse([lever_x - 14, knob_y - 14, lever_x + 14, knob_y + 14], fill=(190, 20, 30, 255), outline=(100, 10, 15, 255), width=2)


def render_reels(grid: list[list[str]] | None, winning_lines: list | None = None) -> io.BytesIO:
    """Draws the slot machine cabinet with the given grid (or a placeholder if None),
    highlighting any winning paylines. winning_lines is a list of (line_index, symbols, payout)."""
    img = Image.new("RGBA", (IMG_W, IMG_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_cabinet(draw)

    r = CELL / 2 - 12
    for row in range(ROWS):
        for col in range(COLS):
            cx, cy = _cell_center(row, col)
            draw.ellipse([cx - CELL / 2 + 4, cy - CELL / 2 + 4, cx + CELL / 2 - 4, cy + CELL / 2 - 4], fill=CELL_BACK)
            if grid is None:
                _centered_text(draw, cx, cy, "?", _placeholder_font, (170, 170, 175, 255))
            else:
                SYMBOL_DRAWERS[grid[row][col]](draw, cx, cy, r)

    for col in range(1, COLS):
        x = WINDOW_X0 + col * (CELL + GAP) - GAP / 2
        draw.line([(x, WINDOW_Y0), (x, WINDOW_Y0 + WINDOW_H)], fill=DIVIDER, width=2)

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
