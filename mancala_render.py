import io
import math

from PIL import Image, ImageDraw, ImageFont

import mancala

# Physical layout: bottom row (left->right) is the challenger's pits 0-5, ending at their store on
# the right; top row (left->right) is the opponent's pits 12,11,...,7, ending at their store on the
# left -- this is the real board's shape, where sowing flows counterclockwise (challenger's row
# right into their own store, up into the opponent's row, left into the opponent's store, then
# wraps back down into the challenger's row) and each column holds a pair of mancala.opposite_pit
# partners lined up vertically, same as a physical board.
WIDTH, HEIGHT = 700, 300

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_name_font = ImageFont.truetype(_FONT_PATH, 20)
_count_font = ImageFont.truetype(_FONT_PATH, 22)
_store_count_font = ImageFont.truetype(_FONT_PATH, 26)

FELT = (24, 92, 58, 255)
BOARD_WOOD = (121, 79, 45, 255)
BOARD_WOOD_EDGE = (86, 55, 30, 255)
PIT_WOOD = (94, 60, 33, 255)
PIT_EDGE = (60, 38, 20, 255)
STONE = (240, 225, 190, 255)
STONE_EDGE = (150, 120, 70, 255)
TEXT = (255, 250, 235, 255)
HIGHLIGHT = (255, 205, 60, 140)

PIT_R = 32
STORE_W, STORE_H = 76, 210
COL_GAP = 16
MARGIN = 24
TOP_Y = 95
BOTTOM_Y = 205

_col_x = [MARGIN + STORE_W + COL_GAP + PIT_R + i * (2 * PIT_R + COL_GAP) for i in range(6)]
CHALLENGER_STORE_X = _col_x[-1] + PIT_R + COL_GAP + STORE_W // 2
OPPONENT_STORE_X = _col_x[0] - PIT_R - COL_GAP - STORE_W // 2
STORE_CY = (TOP_Y + BOTTOM_Y) // 2


def _stone_dots(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float, count: int) -> None:
    """Decorative -- a handful of stones scattered inside a pit/store, capped well below the real
    count (which is always rendered separately as an exact number) so it never looks cluttered."""
    shown = min(count, 9)
    if shown <= 0:
        return
    dot_r = 4
    ring_r = radius * 0.55
    for i in range(shown):
        angle = (2 * math.pi * i / shown) + 0.3
        jitter = ring_r * (0.5 if shown == 1 else 1.0)
        x = cx + jitter * math.cos(angle) * 0.8
        y = cy + jitter * math.sin(angle) * 0.8
        draw.ellipse([x - dot_r, y - dot_r, x + dot_r, y + dot_r], fill=STONE, outline=STONE_EDGE)


def _centered_text(draw: ImageDraw.ImageDraw, cx: float, cy: float, text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def _draw_pit(draw: ImageDraw.ImageDraw, cx: float, cy: float, count: int, active: bool) -> None:
    if active:
        draw.ellipse([cx - PIT_R - 6, cy - PIT_R - 6, cx + PIT_R + 6, cy + PIT_R + 6], fill=HIGHLIGHT)
    draw.ellipse([cx - PIT_R, cy - PIT_R, cx + PIT_R, cy + PIT_R], fill=PIT_WOOD, outline=PIT_EDGE, width=3)
    _stone_dots(draw, cx, cy, PIT_R, count)
    _centered_text(draw, cx, cy + PIT_R + 16, str(count), _count_font, TEXT)


def _draw_store(draw: ImageDraw.ImageDraw, cx: float, count: int, active: bool) -> None:
    box = [cx - STORE_W / 2, STORE_CY - STORE_H / 2, cx + STORE_W / 2, STORE_CY + STORE_H / 2]
    if active:
        pad = 6
        draw.rounded_rectangle(
            [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad], radius=STORE_W / 2 + pad, fill=HIGHLIGHT,
        )
    draw.rounded_rectangle(box, radius=STORE_W / 2, fill=PIT_WOOD, outline=PIT_EDGE, width=3)
    _stone_dots(draw, cx, STORE_CY, STORE_W / 2, count)
    _centered_text(draw, cx, STORE_CY + STORE_H / 2 + 20, str(count), _store_count_font, TEXT)


def render_board(board: list[int], challenger_name: str, opponent_name: str, challenger_turn: bool) -> io.BytesIO:
    img = Image.new("RGBA", (WIDTH, HEIGHT), FELT)
    draw = ImageDraw.Draw(img)

    board_box = [MARGIN - 10, 20, WIDTH - MARGIN + 10, HEIGHT - 10]
    draw.rounded_rectangle(board_box, radius=24, fill=BOARD_WOOD, outline=BOARD_WOOD_EDGE, width=4)

    _centered_text(draw, WIDTH / 2, 34, f"{opponent_name}'s side", _name_font, TEXT)
    _centered_text(draw, WIDTH / 2, HEIGHT - 24, f"{challenger_name}'s side", _name_font, TEXT)

    for col in range(6):
        challenger_pit = col
        opponent_pit = mancala.opposite_pit(col)
        _draw_pit(draw, _col_x[col], BOTTOM_Y, board[challenger_pit], active=challenger_turn)
        _draw_pit(draw, _col_x[col], TOP_Y, board[opponent_pit], active=not challenger_turn)

    _draw_store(draw, CHALLENGER_STORE_X, board[mancala.CHALLENGER_STORE], active=challenger_turn)
    _draw_store(draw, OPPONENT_STORE_X, board[mancala.OPPONENT_STORE], active=not challenger_turn)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
