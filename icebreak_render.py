import io

from PIL import Image, ImageDraw, ImageFont

import icebreak

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_name_font = ImageFont.truetype(_FONT_PATH, 20)
_coord_font = ImageFont.truetype(_FONT_PATH, 14)

FELT = (24, 92, 58, 255)
FRAME = (150, 110, 60, 255)      # the sturdy wooden frame -- never breaks
FRAME_EDGE = (100, 72, 38, 255)
ICE = (198, 232, 240, 255)       # intact cell
ICE_EDGE = (140, 190, 205, 255)
HOLE = (14, 34, 58, 255)         # broken cell -- the water underneath
HOLE_EDGE = (8, 20, 36, 255)
TEXT = (255, 250, 235, 255)
COORD_TEXT = (230, 240, 235, 255)

# Colors for highlighting each separate piece at the moment the sheet fractures -- up to a
# handful of components are plausible on a 5x5 board (never remotely close to running out).
COMPONENT_COLORS = [
    (231, 76, 60, 255), (241, 196, 15, 255), (46, 204, 113, 255), (155, 89, 182, 255), (52, 152, 219, 255),
]

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
    components: list[list[tuple[int, int]]] | None = None,
) -> io.BytesIO:
    """`components` -- pass icebreak.MoveResult.components only once the sheet has actually
    fractured (2+ pieces), to color each piece separately and make the break visible; omit it
    (None) during normal play, when there's always exactly one piece and nothing to distinguish."""
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

    component_color = {}
    if components and len(components) > 1:
        for i, comp in enumerate(components):
            color = COMPONENT_COLORS[i % len(COMPONENT_COLORS)]
            for cell in comp:
                component_color[cell] = color

    for row in range(icebreak.ROWS):
        for col in range(icebreak.COLS):
            box = _cell_box(row, col)
            if board[row][col] == 1:
                highlight = component_color.get((row, col))
                fill = highlight if highlight else ICE
                draw.rounded_rectangle(box, radius=8, fill=fill, outline=ICE_EDGE, width=2)
            else:
                draw.rounded_rectangle(box, radius=8, fill=HOLE, outline=HOLE_EDGE, width=2)

    turn_color = (231, 76, 60, 255) if challenger_turn else (52, 152, 219, 255)
    turn_name = challenger_name if challenger_turn else opponent_name
    turn_y = GRID_Y0 + GRID_H + BOTTOM_MARGIN / 2
    draw.ellipse([MARGIN, turn_y - 8, MARGIN + 16, turn_y + 8], fill=turn_color, outline=FRAME_EDGE, width=2)
    draw.text((MARGIN + 24, turn_y - 12), f"{turn_name}'s turn", font=_name_font, fill=TEXT)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
