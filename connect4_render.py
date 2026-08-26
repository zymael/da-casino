import io

from PIL import Image, ImageDraw, ImageFont

import connect4

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_name_font = ImageFont.truetype(_FONT_PATH, 20)

FELT = (24, 92, 58, 255)
BOARD_BLUE = (36, 78, 168, 255)
BOARD_EDGE = (20, 46, 100, 255)
SLOT_EMPTY = (18, 30, 60, 255)
CHALLENGER_COLOR = (214, 48, 49, 255)   # red
CHALLENGER_EDGE = (140, 25, 25, 255)
OPPONENT_COLOR = (240, 196, 25, 255)    # yellow
OPPONENT_EDGE = (170, 130, 10, 255)
TEXT = (255, 250, 235, 255)
WIN_RING = (255, 255, 255, 255)

CELL = 56
PAD = 14
MARGIN = 24
TOP_MARGIN = 56
BOTTOM_MARGIN = 44

WIDTH = MARGIN * 2 + connect4.COLS * CELL
HEIGHT = TOP_MARGIN + connect4.ROWS * CELL + BOTTOM_MARGIN


def _slot_center(row: int, col: int) -> tuple[float, float]:
    cx = MARGIN + col * CELL + CELL / 2
    cy = TOP_MARGIN + row * CELL + CELL / 2
    return cx, cy


def _centered_text(draw: ImageDraw.ImageDraw, cx: float, cy: float, text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def render_board(
    board: list[list[int]], challenger_name: str, opponent_name: str, challenger_turn: bool,
    win_line: list[tuple[int, int]] | None = None,
) -> io.BytesIO:
    img = Image.new("RGBA", (WIDTH, HEIGHT), FELT)
    draw = ImageDraw.Draw(img)

    # Plain text -- DejaVuSans-Bold (the only font this image-drawing pipeline uses, same as
    # mancala_render.py/dungeon_render.py) has no color emoji glyphs, so disc color is conveyed by
    # the discs themselves and the turn indicator below, not by emoji baked into this title.
    _centered_text(draw, WIDTH / 2, 22, f"{challenger_name}  vs  {opponent_name}", _name_font, TEXT)

    board_box = [MARGIN - 8, TOP_MARGIN - 8, WIDTH - MARGIN + 8, TOP_MARGIN + connect4.ROWS * CELL + 8]
    draw.rounded_rectangle(board_box, radius=18, fill=BOARD_BLUE, outline=BOARD_EDGE, width=4)

    win_cells = set(win_line or [])
    r = CELL / 2 - PAD / 2
    for row in range(connect4.ROWS):
        for col in range(connect4.COLS):
            cx, cy = _slot_center(row, col)
            piece = board[row][col]
            if piece == connect4.CHALLENGER:
                fill, outline = CHALLENGER_COLOR, CHALLENGER_EDGE
            elif piece == connect4.OPPONENT:
                fill, outline = OPPONENT_COLOR, OPPONENT_EDGE
            else:
                fill, outline = SLOT_EMPTY, BOARD_EDGE
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=outline, width=3)
            if (row, col) in win_cells:
                draw.ellipse([cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4], outline=WIN_RING, width=4)

    turn_color = CHALLENGER_COLOR if challenger_turn else OPPONENT_COLOR
    turn_name = challenger_name if challenger_turn else opponent_name
    turn_y = TOP_MARGIN + connect4.ROWS * CELL + BOTTOM_MARGIN / 2
    draw.ellipse([MARGIN, turn_y - 8, MARGIN + 16, turn_y + 8], fill=turn_color, outline=BOARD_EDGE, width=2)
    draw.text((MARGIN + 24, turn_y - 12), f"{turn_name}'s turn", font=_name_font, fill=TEXT)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
