import io

from PIL import Image, ImageDraw, ImageFont

# Never mutated on disk -- every call re-composites a fresh copy, same pattern as
# ranch_render.py/cards_render.py etc.
BANNER_PATH = "assets/casino_banner.png"

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_TEXT = "LET IT RIDE"
_TEXT_COLOR = (220, 20, 20, 255)
_OUTLINE_COLOR = (20, 5, 5, 255)

# Roy's doodle occupies roughly the left quarter of the banner -- keep the text clear of him
# rather than centering over his face.
_CLEAR_ZONE_X = 130


def render_roy_greeting() -> io.BytesIO:
    """Composites Roy's big red "LET IT RIDE" over the casino banner, sized to fit the space to
    the right of his doodle and outlined for legibility against the busy photo behind it."""
    base = Image.open(BANNER_PATH).convert("RGBA")
    draw = ImageDraw.Draw(base)

    available_width = base.width - _CLEAR_ZONE_X - 20
    font_size = 44
    font = ImageFont.truetype(_FONT_PATH, font_size)
    bbox = draw.textbbox((0, 0), _TEXT, font=font)
    while (bbox[2] - bbox[0]) > available_width and font_size > 16:
        font_size -= 2
        font = ImageFont.truetype(_FONT_PATH, font_size)
        bbox = draw.textbbox((0, 0), _TEXT, font=font)

    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = _CLEAR_ZONE_X + (available_width - text_w) / 2 - bbox[0]
    y = (base.height - text_h) / 2 - bbox[1]

    outline_width = max(2, font_size // 14)
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx or dy:
                draw.text((x + dx, y + dy), _TEXT, font=font, fill=_OUTLINE_COLOR)
    draw.text((x, y), _TEXT, font=font, fill=_TEXT_COLOR)

    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
