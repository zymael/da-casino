import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

# Never mutated on disk -- every call re-composites a fresh copy over the original banner, same
# as every other dynamically-rendered image in this bot (cards_render.py etc).
BANNER_PATH = "assets/ranch_banner.png"

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_font = ImageFont.truetype(_FONT_PATH, 12)

KEL_INTRO_TEXT = (
    "Yes, I am Kel. Your assistant and a proud breeder of horses. You may look to me "
    "whenever you need help tending to the ranch, or whenever you need a horse bred."
)

_BUBBLE_FILL = (255, 255, 245, 235)
_BUBBLE_OUTLINE = (30, 20, 15, 255)
_TEXT_COLOR = (20, 15, 10, 255)


def render_kel_introduction() -> io.BytesIO:
    """Composites a comic-style speech bubble with Kel's introduction over the ranch banner,
    positioned clear of his face (left third of the image) with a tail pointing back to him.
    Bubble size is measured from the actual wrapped text rather than guessed, so it stays
    legible if the intro line is ever edited."""
    base = Image.open(BANNER_PATH).convert("RGBA")
    draw = ImageDraw.Draw(base)

    wrapped = textwrap.fill(KEL_INTRO_TEXT, width=40)
    padding = 10
    text_bbox = draw.multiline_textbbox((0, 0), wrapped, font=_font, spacing=4)
    text_w, text_h = text_bbox[2] - text_bbox[0], text_bbox[3] - text_bbox[1]

    bubble_w, bubble_h = text_w + padding * 2, text_h + padding * 2
    bubble_x = max(base.width - bubble_w - 10, base.width // 3)
    bubble_y = 8

    draw.rounded_rectangle(
        [bubble_x, bubble_y, bubble_x + bubble_w, bubble_y + bubble_h],
        radius=12, fill=_BUBBLE_FILL, outline=_BUBBLE_OUTLINE, width=2,
    )
    tail = [
        (bubble_x + 30, bubble_y + bubble_h - 2),
        (bubble_x + 10, bubble_y + bubble_h + 22),
        (bubble_x + 55, bubble_y + bubble_h - 2),
    ]
    draw.polygon(tail, fill=_BUBBLE_FILL, outline=_BUBBLE_OUTLINE)
    # redraw the bubble's bottom edge over the tail's top seam so the join looks clean
    draw.line([(bubble_x + 12, bubble_y + bubble_h - 2), (bubble_x + bubble_w - 12, bubble_y + bubble_h - 2)], fill=_BUBBLE_FILL, width=3)

    draw.multiline_text((bubble_x + padding, bubble_y + padding), wrapped, font=_font, fill=_TEXT_COLOR, spacing=4)

    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
