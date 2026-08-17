import io

from PIL import Image, ImageDraw, ImageFont

from game import Card

CARD_WIDTH = 100
CARD_HEIGHT = 140
CARD_GAP = 12
CORNER_RADIUS = 10

_RED_SUITS = {"♥", "♦"}
_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_rank_font = ImageFont.truetype(_FONT_PATH, 22)
_suit_font_small = ImageFont.truetype(_FONT_PATH, 18)
_suit_font_big = ImageFont.truetype(_FONT_PATH, 46)

_card_cache: dict[str, Image.Image] = {}


def _draw_card_face(rank: str, suit: str) -> Image.Image:
    img = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (200, 30, 30) if suit in _RED_SUITS else (20, 20, 20)

    draw.rounded_rectangle(
        [(0, 0), (CARD_WIDTH - 1, CARD_HEIGHT - 1)],
        radius=CORNER_RADIUS,
        fill=(255, 255, 255, 255),
        outline=(60, 60, 60, 255),
        width=2,
    )

    draw.text((8, 6), rank, font=_rank_font, fill=color)
    draw.text((8, 30), suit, font=_suit_font_small, fill=color)

    bbox = draw.textbbox((0, 0), suit, font=_suit_font_big)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((CARD_WIDTH - w) / 2 - bbox[0], (CARD_HEIGHT - h) / 2 - bbox[1]),
        suit,
        font=_suit_font_big,
        fill=color,
    )

    # Build the bottom-right corner mark by drawing the same top-left glyphs on a
    # blank layer and rotating the whole layer 180°, so it lands mirrored at the
    # opposite corner instead of colliding with the top-left text.
    corner = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
    corner_draw = ImageDraw.Draw(corner)
    corner_draw.text((8, 6), rank, font=_rank_font, fill=color)
    corner_draw.text((8, 30), suit, font=_suit_font_small, fill=color)
    corner = corner.rotate(180)
    img = Image.alpha_composite(img, corner)

    return img


def _draw_card_back() -> Image.Image:
    img = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [(0, 0), (CARD_WIDTH - 1, CARD_HEIGHT - 1)],
        radius=CORNER_RADIUS,
        fill=(30, 60, 130, 255),
        outline=(15, 30, 70, 255),
        width=2,
    )
    margin = 10
    draw.rounded_rectangle(
        [(margin, margin), (CARD_WIDTH - 1 - margin, CARD_HEIGHT - 1 - margin)],
        radius=6,
        outline=(220, 220, 240, 255),
        width=2,
    )
    step = 14
    for x in range(-CARD_HEIGHT, CARD_WIDTH, step):
        draw.line([(x, CARD_HEIGHT), (x + CARD_HEIGHT, 0)], fill=(60, 90, 160, 255), width=2)
    draw.rounded_rectangle(
        [(0, 0), (CARD_WIDTH - 1, CARD_HEIGHT - 1)],
        radius=CORNER_RADIUS,
        outline=(15, 30, 70, 255),
        width=2,
    )
    return img


def _card_image(card: Card) -> Image.Image:
    key = f"{card.rank}{card.suit}"
    if key not in _card_cache:
        _card_cache[key] = _draw_card_face(card.rank, card.suit)
    return _card_cache[key]


def _back_image() -> Image.Image:
    if "back" not in _card_cache:
        _card_cache["back"] = _draw_card_back()
    return _card_cache["back"]


def render_hand(cards: list[Card], hide_first: bool = False) -> io.BytesIO:
    """Renders a hand as a single side-by-side PNG. Returns a ready-to-attach BytesIO."""
    n = len(cards)
    width = n * CARD_WIDTH + (n - 1) * CARD_GAP
    sheet = Image.new("RGBA", (width, CARD_HEIGHT), (0, 0, 0, 0))
    for i, card in enumerate(cards):
        face = _back_image() if (hide_first and i == 0) else _card_image(card)
        sheet.paste(face, (i * (CARD_WIDTH + CARD_GAP), 0), face)

    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    buf.seek(0)
    return buf
