"""Shared NPC dialogue rendering -- one speech-bubble renderer reused by every NPC, replacing the
three near-duplicate implementations that used to live one per hub (dungeon_render's
render_mondor_dialogue/render_dungeon_banner, ranch_render's render_kel_dialogue,
casino_render's render_roy_greeting). Each hub still owns its own banner image
(assets/dungeon_banner.png, assets/ranch_banner.png, assets/casino_banner.png) -- these functions
take that banner path as a parameter instead of hardcoding one, which was the only thing that
actually differed between the three originals; the bubble geometry, font, and sprite-compositing
logic were otherwise identical.
"""

import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_BUBBLE_FONT = ImageFont.truetype(_FONT_PATH, 13)
_WRAP_WIDTH = 32
_BUBBLE_FILL = (255, 255, 245, 235)
_BUBBLE_OUTLINE = (30, 20, 15, 255)
_BUBBLE_TEXT_COLOR = (20, 15, 10, 255)


def render_npc_dialogue(banner_path: str, text: str, sprite_path: str | None = None) -> io.BytesIO:
    """Composites a comic-style speech bubble with arbitrary NPC dialogue over `banner_path`,
    positioned clear of the NPC's face (they stand in the left half/third of the image) with a
    tail pointing back to them. Bubble size is measured from the actual wrapped text rather than
    guessed, so it stays legible regardless of the line's length. sprite_path, if given, is
    composited onto the banner first (e.g. a quest-complete reveal sprite) so the bubble still
    draws on top and stays legible over it."""
    base = Image.open(banner_path).convert("RGBA")
    if sprite_path:
        sprite = Image.open(sprite_path).convert("RGBA")
        base.alpha_composite(sprite)
    draw = ImageDraw.Draw(base)

    wrapped = textwrap.fill(text, width=_WRAP_WIDTH)
    padding = 10
    text_bbox = draw.multiline_textbbox((0, 0), wrapped, font=_BUBBLE_FONT, spacing=4)
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
    draw.line(
        [(bubble_x + 12, bubble_y + bubble_h - 2), (bubble_x + bubble_w - 12, bubble_y + bubble_h - 2)],
        fill=_BUBBLE_FILL, width=3,
    )

    draw.multiline_text(
        (bubble_x + padding, bubble_y + padding), wrapped, font=_BUBBLE_FONT, fill=_BUBBLE_TEXT_COLOR, spacing=4,
    )

    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_room_banner(banner_path: str, sprite_paths: list[str] = ()) -> io.BytesIO:
    """The plain banner, with every currently-present NPC's portrait sprite (if any) composited on
    in order -- no dialogue bubble, unlike render_npc_dialogue. Used for a room's resting/default
    state so e.g. a completed quest's reveal NPC (the Greasy Princess) stays visible every time the
    room is opened, not just mid-conversation. Composites a *list* rather than a single optional
    sprite so this works uniformly for any room regardless of how many present NPCs have a
    sprite_path -- room_view.py is what actually decides who's present and passes their paths in,
    this function has no idea what a "princess" or a "quest" is."""
    base = Image.open(banner_path).convert("RGBA")
    for sprite_path in sprite_paths:
        sprite = Image.open(sprite_path).convert("RGBA")
        base.alpha_composite(sprite)
    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
