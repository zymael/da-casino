"""One-off dev utility: exports every hand-drawn PIL visual element from the casino games
(roulette, slots, cards, connect4, mancala, uno, icebreak) as an individual PNG template, sized at
its real render dimensions, so each can be repainted into real art and later swapped in as an
image asset (same pattern as assets/npcs, assets/horses, assets/dungeon).

Not wired into the bot or admin panel -- run manually with `python3 export_art_templates.py`.
Output goes to assets/templates/<game>/*.png, all on transparent backgrounds unless the element is
itself a background panel (in which case its native fill color is the point of the template).
"""

import os

from PIL import Image, ImageDraw

OUT_ROOT = os.path.join(os.path.dirname(__file__), "assets", "templates")


def _save(img: Image.Image, game: str, name: str) -> None:
    game_dir = os.path.join(OUT_ROOT, game)
    os.makedirs(game_dir, exist_ok=True)
    path = os.path.join(game_dir, f"{name}.png")
    img.save(path, format="PNG")
    print(f"wrote {path}  ({img.width}x{img.height})")


def _transparent(w: int, h: int) -> Image.Image:
    return Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))


# ---------------------------------------------------------------------------
# roulette
# ---------------------------------------------------------------------------
def export_roulette():
    import roulette_render as rr

    r = 15
    pad = 6
    img = _transparent(2 * (r + pad), 2 * (r + pad))
    draw = ImageDraw.Draw(img)
    rr._draw_chip(draw, r + pad, r + pad, "10")
    _save(img, "roulette", "chip")

    ball_r = 9
    pad = 4
    img = _transparent(2 * (ball_r + pad), 2 * (ball_r + pad))
    draw = ImageDraw.Draw(img)
    cx = cy = ball_r + pad
    draw.ellipse([cx - ball_r, cy - ball_r, cx + ball_r, cy + ball_r], fill=(255, 255, 255, 255), outline=(30, 30, 30, 255), width=2)
    _save(img, "roulette", "ball")

    hub_r = rr.WHEEL_HUB_R
    pad = 6
    img = _transparent(2 * (hub_r + pad), 2 * (hub_r + pad))
    draw = ImageDraw.Draw(img)
    cx = cy = hub_r + pad
    draw.ellipse([cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r], fill=(40, 25, 10, 255), outline=rr.GOLD, width=3)
    _save(img, "roulette", "wheel_hub")

    _save(rr._base_table(), "roulette", "table_background")

    buf_img = Image.open(rr.render_wheel(winning_number=None))
    _save(buf_img, "roulette", "wheel_background")


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------
def export_cards():
    import cards_render as cr

    _save(cr._draw_card_back(), "cards", "card_back")

    for suit in ("♠", "♥", "♦", "♣"):
        img = _transparent(cr.CARD_WIDTH, cr.CARD_HEIGHT)
        draw = ImageDraw.Draw(img)
        color = (200, 30, 30) if suit in cr._RED_SUITS else (20, 20, 20)
        draw.rounded_rectangle(
            [(0, 0), (cr.CARD_WIDTH - 1, cr.CARD_HEIGHT - 1)],
            radius=cr.CORNER_RADIUS, fill=(255, 255, 255, 255), outline=(60, 60, 60, 255), width=2,
        )
        bbox = draw.textbbox((0, 0), suit, font=cr._suit_font_big)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(
            ((cr.CARD_WIDTH - w) / 2 - bbox[0], (cr.CARD_HEIGHT - h) / 2 - bbox[1]),
            suit, font=cr._suit_font_big, fill=color,
        )
        suit_name = {"♠": "spades", "♥": "hearts", "♦": "diamonds", "♣": "clubs"}[suit]
        _save(img, "cards", f"card_blank_{suit_name}")


# ---------------------------------------------------------------------------
# connect4
# ---------------------------------------------------------------------------
def export_connect4():
    import connect4_render as c4r

    r = c4r.CELL / 2 - c4r.PAD / 2
    pad = 6
    size = 2 * (r + pad)

    def disc(fill, outline):
        img = _transparent(size, size)
        draw = ImageDraw.Draw(img)
        cx = cy = size / 2
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=outline, width=3)
        return img

    _save(disc(c4r.CHALLENGER_COLOR, c4r.CHALLENGER_EDGE), "connect4", "disc_challenger")
    _save(disc(c4r.OPPONENT_COLOR, c4r.OPPONENT_EDGE), "connect4", "disc_opponent")
    _save(disc(c4r.SLOT_EMPTY, c4r.BOARD_EDGE), "connect4", "disc_empty")

    board_box = [c4r.MARGIN - 8, c4r.TOP_MARGIN - 8, c4r.WIDTH - c4r.MARGIN + 8, c4r.TOP_MARGIN + c4r.connect4.ROWS * c4r.CELL + 8]
    img = Image.new("RGBA", (c4r.WIDTH, c4r.TOP_MARGIN + c4r.connect4.ROWS * c4r.CELL + 8), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(board_box, radius=18, fill=c4r.BOARD_BLUE, outline=c4r.BOARD_EDGE, width=4)
    for row in range(c4r.connect4.ROWS):
        for col in range(c4r.connect4.COLS):
            cx, cy = c4r._slot_center(row, col)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c4r.SLOT_EMPTY, outline=c4r.BOARD_EDGE, width=3)
    _save(img, "connect4", "board_background")


# ---------------------------------------------------------------------------
# mancala
# ---------------------------------------------------------------------------
def export_mancala():
    import mancala_render as mr

    pad = 6
    size = 2 * (mr.PIT_R + pad)
    img = _transparent(size, size)
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2
    draw.ellipse([cx - mr.PIT_R, cy - mr.PIT_R, cx + mr.PIT_R, cy + mr.PIT_R], fill=mr.PIT_WOOD, outline=mr.PIT_EDGE, width=3)
    _save(img, "mancala", "pit_empty")

    stone_r = 4
    img = _transparent(2 * (stone_r + 2), 2 * (stone_r + 2))
    draw = ImageDraw.Draw(img)
    c = stone_r + 2
    draw.ellipse([c - stone_r, c - stone_r, c + stone_r, c + stone_r], fill=mr.STONE, outline=mr.STONE_EDGE)
    _save(img, "mancala", "stone")

    img = Image.new("RGBA", (mr.WIDTH, mr.HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    board_box = [mr.MARGIN - 10, 20, mr.WIDTH - mr.MARGIN + 10, mr.HEIGHT - 10]
    draw.rounded_rectangle(board_box, radius=24, fill=mr.BOARD_WOOD, outline=mr.BOARD_WOOD_EDGE, width=4)
    for col in range(6):
        draw.ellipse(
            [mr._col_x[col] - mr.PIT_R, mr.BOTTOM_Y - mr.PIT_R, mr._col_x[col] + mr.PIT_R, mr.BOTTOM_Y + mr.PIT_R],
            fill=mr.PIT_WOOD, outline=mr.PIT_EDGE, width=3,
        )
        draw.ellipse(
            [mr._col_x[col] - mr.PIT_R, mr.TOP_Y - mr.PIT_R, mr._col_x[col] + mr.PIT_R, mr.TOP_Y + mr.PIT_R],
            fill=mr.PIT_WOOD, outline=mr.PIT_EDGE, width=3,
        )
    for cx in (mr.CHALLENGER_STORE_X, mr.OPPONENT_STORE_X):
        box = [cx - mr.STORE_W / 2, mr.STORE_CY - mr.STORE_H / 2, cx + mr.STORE_W / 2, mr.STORE_CY + mr.STORE_H / 2]
        draw.rounded_rectangle(box, radius=mr.STORE_W / 2, fill=mr.PIT_WOOD, outline=mr.PIT_EDGE, width=3)
    _save(img, "mancala", "board_template")


# ---------------------------------------------------------------------------
# uno
# ---------------------------------------------------------------------------
def export_uno():
    import uno_render as ur

    _save(ur._card_back(), "uno", "card_back")

    for color in ("red", "yellow", "green", "blue"):
        img = _transparent(ur.CARD_W, ur.CARD_H)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([1, 1, ur.CARD_W - 2, ur.CARD_H - 2], radius=10, fill=ur.CARD_COLORS[color], outline=ur.CARD_EDGE, width=2)
        _save(img, "uno", f"card_blank_{color}")

    icon_size = 60
    img = _transparent(icon_size, icon_size)
    draw = ImageDraw.Draw(img)
    ur._draw_wild_swatch(draw, icon_size / 2, icon_size / 2, 44)
    _save(img, "uno", "icon_wild")

    img = _transparent(icon_size, icon_size)
    draw = ImageDraw.Draw(img)
    ur._draw_skip_icon(draw, icon_size / 2, icon_size / 2, 24)
    _save(img, "uno", "icon_skip")

    img = _transparent(icon_size, icon_size)
    draw = ImageDraw.Draw(img)
    ur._draw_reverse_icon(draw, icon_size / 2, icon_size / 2, 40)
    _save(img, "uno", "icon_reverse")


# ---------------------------------------------------------------------------
# icebreak
# ---------------------------------------------------------------------------
def export_icebreak():
    import icebreak_render as ir

    pad = 6
    size = ir.CELL + 2 * pad

    for state, name in ((True, "penguin_standing"), (False, "penguin_fallen")):
        img = _transparent(size, size)
        draw = ImageDraw.Draw(img)
        ir._draw_penguin(draw, size / 2, size / 2, ir.CELL * 0.85, fallen=not state)
        _save(img, "icebreak", name)

    for color, edge, name in ((ir.ICE, ir.ICE_EDGE, "cell_ice"), (ir.WATER, ir.WATER_EDGE, "cell_water")):
        img = _transparent(ir.CELL, ir.CELL)
        draw = ImageDraw.Draw(img)
        box = [4, 4, ir.CELL - 4, ir.CELL - 4]
        draw.rounded_rectangle(box, radius=8, fill=color, outline=edge, width=2)
        if name == "cell_water":
            cy = (box[1] + box[3]) / 2
            for dy in (-6, 5):
                draw.arc([box[0] + 6, cy + dy - 5, box[2] - 6, cy + dy + 5], start=200, end=340, fill=ir.WAVE, width=2)
        _save(img, "icebreak", name)

    img = Image.new(
        "RGBA",
        (ir.WIDTH, ir.TOP_MARGIN + ir.COORD_GUTTER + ir.GRID_H + 10),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(img)
    frame_box = [ir.GRID_X0 - 10, ir.GRID_Y0 - 10, ir.GRID_X0 + ir.GRID_W + 10, ir.GRID_Y0 + ir.GRID_H + 10]
    draw.rounded_rectangle(frame_box, radius=14, fill=ir.FRAME, outline=ir.FRAME_EDGE, width=4)
    for row in range(ir.icebreak.ROWS):
        for col in range(ir.icebreak.COLS):
            box = ir._cell_box(row, col)
            draw.rounded_rectangle(box, radius=8, fill=ir.ICE, outline=ir.ICE_EDGE, width=2)
    _save(img, "icebreak", "board_frame")


if __name__ == "__main__":
    export_roulette()
    export_cards()
    export_connect4()
    export_mancala()
    export_uno()
    export_icebreak()
    print(f"\nDone. Templates written under {OUT_ROOT}/<game>/")
