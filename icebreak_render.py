import io
import math

from PIL import Image, ImageDraw, ImageFont

import icebreak

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_name_font = ImageFont.truetype(_FONT_PATH, 20)
_coord_font = ImageFont.truetype(_FONT_PATH, 14)

FELT = (24, 92, 58, 255)
FRAME = (150, 110, 60, 255)      # the sturdy wooden frame -- never breaks
FRAME_EDGE = (100, 72, 38, 255)
ICE = (198, 232, 240, 255)       # intact cell, still holding a chain back to a wall
ICE_EDGE = (140, 190, 205, 255)
WATER = (32, 110, 168, 255)      # broken cell -- open water, whether hammered directly or fallen
WATER_EDGE = (18, 70, 112, 255) # through in a cascade once its own support gave out
WAVE = (110, 175, 220, 255)
TEXT = (255, 250, 235, 255)
COORD_TEXT = (230, 240, 235, 255)

CELL = 56
MARGIN = 24
COORD_GUTTER = 22
TOP_MARGIN = 56
BOTTOM_MARGIN = 44

GRID_W = icebreak.COLS * CELL
GRID_H = icebreak.ROWS * CELL
WIDTH = MARGIN * 2 + COORD_GUTTER + GRID_W
HEIGHT = TOP_MARGIN + COORD_GUTTER + GRID_H + BOTTOM_MARGIN

GRID_X0 = MARGIN + COORD_GUTTER
GRID_Y0 = TOP_MARGIN + COORD_GUTTER

COL_LETTERS = "ABCDE"

PENGUIN_BLACK = (30, 32, 38, 255)
PENGUIN_WHITE = (250, 250, 250, 255)
PENGUIN_BEAK = (240, 140, 20, 255)
SPLASH = (210, 235, 245, 255)

# The penguin always stands dead center -- he's on a rigid platform spanning the whole sheet, not
# tied to any one cell, so his position never depends on which cell actually gets clicked.
_PENGUIN_ROW = icebreak.ROWS // 2
_PENGUIN_COL = icebreak.COLS // 2


def _draw_penguin(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float, fallen: bool) -> None:
    if not fallen:
        body_w, body_h = size * 0.7, size * 1.0
        draw.ellipse([cx - body_w / 2, cy - body_h / 2, cx + body_w / 2, cy + body_h / 2], fill=PENGUIN_BLACK)
        draw.ellipse(
            [cx - body_w * 0.28, cy - body_h * 0.1, cx + body_w * 0.28, cy + body_h * 0.48], fill=PENGUIN_WHITE,
        )
        head_r = size * 0.28
        head_cy = cy - body_h * 0.55
        draw.ellipse([cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r], fill=PENGUIN_BLACK)
        draw.polygon(
            [(cx, head_cy + head_r * 0.1), (cx + head_r, head_cy + head_r * 0.35), (cx, head_cy + head_r * 0.6)],
            fill=PENGUIN_BEAK,
        )
        for sign in (-1, 1):
            ex = cx + sign * head_r * 0.4
            draw.ellipse([ex - 3, head_cy - head_r * 0.3 - 3, ex + 3, head_cy - head_r * 0.3 + 3], fill=PENGUIN_WHITE)
            draw.ellipse([ex - 1.5, head_cy - head_r * 0.3 - 1.5, ex + 1.5, head_cy - head_r * 0.3 + 1.5], fill=(0, 0, 0, 255))
        foot_y = cy + body_h / 2
        for sign in (-1, 1):
            fx = cx + sign * body_w * 0.22
            draw.ellipse([fx - 6, foot_y - 3, fx + 6, foot_y + 5], fill=PENGUIN_BEAK)
        for sign in (-1, 1):
            fx = cx + sign * body_w * 0.46
            draw.ellipse([fx - 5, cy - body_h * 0.1, fx + 5, cy + body_h * 0.35], fill=PENGUIN_BLACK)
    else:
        # Toppled on his side in the water -- flattened body, dizzy X eyes, a splash ring around him.
        for i in range(6):
            angle = i * (2 * math.pi / 6)
            sx, sy = cx + 22 * math.cos(angle), cy + 10 * math.sin(angle)
            draw.ellipse([sx - 6, sy - 6, sx + 6, sy + 6], outline=SPLASH, width=2)
        body_w, body_h = size * 1.0, size * 0.6
        draw.ellipse([cx - body_w / 2, cy - body_h / 2, cx + body_w / 2, cy + body_h / 2], fill=PENGUIN_BLACK)
        draw.ellipse(
            [cx - body_w * 0.1, cy - body_h * 0.32, cx + body_w * 0.48, cy + body_h * 0.3], fill=PENGUIN_WHITE,
        )
        head_r = size * 0.26
        head_cx = cx - body_w * 0.42
        draw.ellipse([head_cx - head_r, cy - head_r, head_cx + head_r, cy + head_r], fill=PENGUIN_BLACK)
        draw.polygon(
            [
                (head_cx - head_r * 0.9, cy + head_r * 0.05),
                (head_cx - head_r * 1.5, cy + head_r * 0.25),
                (head_cx - head_r * 0.9, cy + head_r * 0.45),
            ],
            fill=PENGUIN_BEAK,
        )
        for sign in (-1, 1):
            ex, ey = head_cx + sign * head_r * 0.3, cy - head_r * 0.15
            draw.line([ex - 3, ey - 3, ex + 3, ey + 3], fill=PENGUIN_WHITE, width=2)
            draw.line([ex - 3, ey + 3, ex + 3, ey - 3], fill=PENGUIN_WHITE, width=2)


def _cell_box(row: int, col: int, pad: int = 4) -> list[float]:
    x0 = GRID_X0 + col * CELL + pad
    y0 = GRID_Y0 + row * CELL + pad
    return [x0, y0, x0 + CELL - 2 * pad, y0 + CELL - 2 * pad]


def _centered_text(draw: ImageDraw.ImageDraw, cx: float, cy: float, text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def render_board(
    board: list[list[int]], challenger_name: str, opponent_name: str, challenger_turn: bool,
    game_over: bool = False,
) -> io.BytesIO:
    """`board` is authoritative -- every cell that's lost its support is already water by the time
    this is called (icebreak.apply_move cascades that itself), so there's no separate "doomed but
    still standing" state left to render. `game_over` swaps the penguin from standing on the
    platform to toppled into the water -- that's the entire point of the game (don't be the one
    who dunks him)."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), FELT)
    draw = ImageDraw.Draw(img)

    _centered_text(draw, WIDTH / 2, 22, f"{challenger_name}  vs  {opponent_name}", _name_font, TEXT)

    frame_box = [GRID_X0 - 10, GRID_Y0 - 10, GRID_X0 + GRID_W + 10, GRID_Y0 + GRID_H + 10]
    draw.rounded_rectangle(frame_box, radius=14, fill=FRAME, outline=FRAME_EDGE, width=4)

    for c in range(icebreak.COLS):
        cx = GRID_X0 + c * CELL + CELL / 2
        _centered_text(draw, cx, GRID_Y0 - COORD_GUTTER / 2, str(c + 1), _coord_font, COORD_TEXT)
    for r in range(icebreak.ROWS):
        cy = GRID_Y0 + r * CELL + CELL / 2
        _centered_text(draw, GRID_X0 - COORD_GUTTER / 2, cy, COL_LETTERS[r], _coord_font, COORD_TEXT)

    for row in range(icebreak.ROWS):
        for col in range(icebreak.COLS):
            box = _cell_box(row, col)
            if board[row][col] == 0:
                draw.rounded_rectangle(box, radius=8, fill=WATER, outline=WATER_EDGE, width=2)
                cy = (box[1] + box[3]) / 2
                for dy in (-6, 5):
                    draw.arc([box[0] + 6, cy + dy - 5, box[2] - 6, cy + dy + 5], start=200, end=340, fill=WAVE, width=2)
            else:
                draw.rounded_rectangle(box, radius=8, fill=ICE, outline=ICE_EDGE, width=2)

    penguin_cx = GRID_X0 + _PENGUIN_COL * CELL + CELL / 2
    penguin_cy = GRID_Y0 + _PENGUIN_ROW * CELL + CELL / 2
    _draw_penguin(draw, penguin_cx, penguin_cy, CELL * 0.85, fallen=game_over)

    turn_color = (231, 76, 60, 255) if challenger_turn else (52, 152, 219, 255)
    turn_name = challenger_name if challenger_turn else opponent_name
    turn_y = GRID_Y0 + GRID_H + BOTTOM_MARGIN / 2
    draw.ellipse([MARGIN, turn_y - 8, MARGIN + 16, turn_y + 8], fill=turn_color, outline=FRAME_EDGE, width=2)
    draw.text((MARGIN + 24, turn_y - 12), f"{turn_name}'s turn", font=_name_font, fill=TEXT)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
