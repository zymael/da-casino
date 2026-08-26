import io
import math

from PIL import Image, ImageDraw, ImageFont

import uno

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_big_font = ImageFont.truetype(_FONT_PATH, 34)
_small_font = ImageFont.truetype(_FONT_PATH, 16)
_name_font = ImageFont.truetype(_FONT_PATH, 18)
_count_font = ImageFont.truetype(_FONT_PATH, 16)

FELT = (24, 92, 58, 255)
WHITE = (250, 250, 250, 255)
BLACK_TEXT = (25, 25, 28, 255)
TEXT = (255, 250, 235, 255)

CARD_COLORS = {
    "red": (211, 47, 47, 255),
    "yellow": (245, 190, 30, 255),
    "green": (56, 142, 60, 255),
    "blue": (25, 118, 210, 255),
    "wild": (32, 32, 36, 255),
}
CARD_EDGE = (255, 255, 255, 255)
BACK_COLOR = (150, 20, 30, 255)
BACK_EDGE = (235, 200, 60, 255)

CARD_W, CARD_H = 70, 100
HAND_OVERLAP = 34  # px each subsequent card shifts right by, in the fanned hand image
STACK_OVERLAP = 10  # much tighter overlap for the public face-down stacks
STACK_MAX_SHOWN = 8  # decorative cap -- exact count is always printed as text alongside


def _rounded_card_box(w: int, h: int) -> Image.Image:
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def _centered_text(draw: ImageDraw.ImageDraw, cx: float, cy: float, text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def _draw_wild_swatch(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float) -> None:
    half = size / 2
    quadrants = [
        ("red", (cx - half, cy - half, cx, cy)),
        ("yellow", (cx, cy - half, cx + half, cy)),
        ("green", (cx - half, cy, cx, cy + half)),
        ("blue", (cx, cy, cx + half, cy + half)),
    ]
    for color, box in quadrants:
        draw.rectangle(box, fill=CARD_COLORS[color])
    draw.rectangle([cx - half, cy - half, cx + half, cy + half], outline=WHITE, width=1)


def _draw_skip_icon(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=WHITE, width=5)
    angle = math.radians(45)
    dx, dy = r * math.cos(angle), r * math.sin(angle)
    draw.line([cx - dx, cy - dy, cx + dx, cy + dy], fill=WHITE, width=5)


def _draw_reverse_icon(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float) -> None:
    w = size * 0.8
    for dy, point_right in ((-size * 0.35, True), (size * 0.35, False)):
        y = cy + dy
        if point_right:
            pts = [(cx - w / 2, y), (cx + w / 2, y), (cx + w / 2 - 8, y - 8), (cx + w / 2, y), (cx + w / 2 - 8, y + 8)]
        else:
            pts = [(cx + w / 2, y), (cx - w / 2, y), (cx - w / 2 + 8, y - 8), (cx - w / 2, y), (cx - w / 2 + 8, y + 8)]
        draw.line(pts, fill=WHITE, width=5, joint="curve")


def _draw_card_face(color: str, kind: str) -> Image.Image:
    img = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([1, 1, CARD_W - 2, CARD_H - 2], radius=10, fill=CARD_COLORS[color], outline=CARD_EDGE, width=2)

    cx, cy = CARD_W / 2, CARD_H / 2
    if kind.isdigit():
        draw.ellipse([cx - 22, cy - 28, cx + 22, cy + 28], fill=WHITE)
        _centered_text(draw, cx, cy, kind, _big_font, CARD_COLORS[color])
        _centered_text(draw, 14, 16, kind, _small_font, WHITE)
    elif kind == "skip":
        _draw_skip_icon(draw, cx, cy, 20)
        _centered_text(draw, 14, 16, "S", _small_font, WHITE)
    elif kind == "reverse":
        _draw_reverse_icon(draw, cx, cy, 34)
        _centered_text(draw, 14, 16, "R", _small_font, WHITE)
    elif kind == "draw_two":
        draw.ellipse([cx - 24, cy - 28, cx + 24, cy + 28], fill=WHITE)
        _centered_text(draw, cx, cy, "+2", _big_font, CARD_COLORS[color])
        _centered_text(draw, 14, 16, "+2", _small_font, WHITE)
    elif kind == "wild":
        draw.ellipse([cx - 24, cy - 24, cx + 24, cy + 24], fill=WHITE)
        _draw_wild_swatch(draw, cx, cy, 30)
    elif kind == "wild_draw_four":
        draw.ellipse([cx - 26, cy - 30, cx + 26, cy + 12], fill=WHITE)
        _draw_wild_swatch(draw, cx, cy - 10, 26)
        _centered_text(draw, cx, cy + 26, "+4", _small_font, WHITE)
    return img


_FACE_CACHE: dict[tuple, Image.Image] = {}


def _card_image(card: "uno.Card") -> Image.Image:
    key = (card.color, card.kind)
    if key not in _FACE_CACHE:
        _FACE_CACHE[key] = _draw_card_face(*key)
    return _FACE_CACHE[key]


_BACK_CACHE: Image.Image | None = None


def _card_back() -> Image.Image:
    global _BACK_CACHE
    if _BACK_CACHE is None:
        img = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([1, 1, CARD_W - 2, CARD_H - 2], radius=10, fill=BACK_COLOR, outline=BACK_EDGE, width=2)
        draw.ellipse([CARD_W / 2 - 20, CARD_H / 2 - 14, CARD_W / 2 + 20, CARD_H / 2 + 14], outline=BACK_EDGE, width=3)
        _BACK_CACHE = img
    return _BACK_CACHE


def render_hand(cards: list["uno.Card"]) -> io.BytesIO:
    """A player's own hand, sorted (uno.sorted_hand) and fanned left-to-right -- card N in this
    image is always card N in the ephemeral view's button row below it."""
    cards = uno.sorted_hand(cards)
    n = max(len(cards), 1)
    width = CARD_W + (n - 1) * HAND_OVERLAP + 8
    height = CARD_H + 8
    img = Image.new("RGBA", (width, height), FELT)
    for i, card in enumerate(cards):
        img.alpha_composite(_card_image(card), (4 + i * HAND_OVERLAP, 4))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _draw_seat_stack(img: Image.Image, x: int, y: int, count: int) -> None:
    shown = min(count, STACK_MAX_SHOWN)
    for i in range(shown):
        img.alpha_composite(_card_back(), (x + i * STACK_OVERLAP, y))


def render_table(game: "uno.UnoGame", pot: int = 0) -> io.BytesIO:
    width, height = 620, 400
    img = Image.new("RGBA", (width, height), FELT)
    draw = ImageDraw.Draw(img)

    arrow = "↻ clockwise" if game.direction == 1 else "↺ counterclockwise"
    _centered_text(draw, width / 2, 14, arrow, _small_font, TEXT)

    # Discard pile + active color swatch
    top = game.top_card()
    top_x, top_y = width / 2 - CARD_W / 2, 34
    color_box = [top_x - 10, top_y - 10, top_x + CARD_W + 10, top_y + CARD_H + 10]
    draw.rounded_rectangle(color_box, radius=14, fill=CARD_COLORS[game.current_color], outline=WHITE, width=3)
    img.alpha_composite(_card_image(top), (int(top_x), int(top_y)))
    _centered_text(draw, width / 2, top_y + CARD_H + 24, f"Color in play: {game.current_color}", _small_font, TEXT)
    if pot:
        _centered_text(draw, width / 2, top_y + CARD_H + 44, f"Pot: {pot}", _small_font, TEXT)

    # Seats along the bottom -- name, a fanned face-down stack, then the exact count as text
    n = len(game.seats)
    seat_w = width / n
    name_y = height - 150
    stack_y = name_y + 18
    count_y = stack_y + CARD_H + 16
    for i, seat in enumerate(game.seats):
        cx = seat_w * i + seat_w / 2
        active = i == game.current_index
        name_color = (255, 221, 90, 255) if active else TEXT
        name_label = f"➤ {seat.name}" if active else seat.name
        _centered_text(draw, cx, name_y, name_label, _name_font, name_color)
        shown = min(len(seat.hand), STACK_MAX_SHOWN)
        stack_x = int(cx - (CARD_W + (shown - 1) * STACK_OVERLAP) / 2)
        _draw_seat_stack(img, stack_x, int(stack_y), len(seat.hand))
        _centered_text(
            draw, cx, count_y, f"{len(seat.hand)} card{'s' if len(seat.hand) != 1 else ''}", _count_font, TEXT,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
