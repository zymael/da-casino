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
import random
import textwrap

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageFont

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_BUBBLE_FONT = ImageFont.truetype(_FONT_PATH, 13)
_WRAP_WIDTH = 32
_BUBBLE_FILL = (255, 255, 245, 235)
_BUBBLE_OUTLINE = (30, 20, 15, 255)
_BUBBLE_TEXT_COLOR = (20, 15, 10, 255)


def _irradiate(base: Image.Image) -> Image.Image:
    """The Mane-hay-tan Fallout look (see manhattan.py): every color pushed toward radioactive
    green with a soft glow, then TV static on top -- grain, scan lines, and bright specks. The
    static is re-rolled on every render, so it shimmers between room visits."""
    img = base.convert("RGB")
    w, h = img.size
    tinted = ImageOps.colorize(ImageOps.grayscale(img), black=(5, 25, 5), mid=(60, 160, 40), white=(210, 255, 140))
    img = Image.blend(img, tinted, 0.65)
    glow = img.filter(ImageFilter.GaussianBlur(6))
    img = ImageChops.screen(img, Image.blend(glow, Image.new("RGB", (w, h)), 0.45))
    grain = Image.effect_noise((w, h), 64).convert("RGB")
    img = Image.blend(img, ImageChops.overlay(img, grain), 0.55)
    lines = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(lines)
    for y in range(0, h, 3):
        draw.line([(0, y), (w, y)], fill=(170, 190, 170))
    img = ImageChops.multiply(img, lines)
    draw = ImageDraw.Draw(img)
    for _ in range(w * h // 400):
        draw.point((random.randrange(w), random.randrange(h)), fill=(200, 255, 170))
    return img.convert("RGBA")


def render_npc_dialogue(
    banner_path: str, text: str, sprite_path: str | None = None, irradiated: bool = False,
) -> io.BytesIO:
    """Composites a comic-style speech bubble with arbitrary NPC dialogue over `banner_path`,
    positioned clear of the NPC's face (they stand in the left half/third of the image) with a
    tail pointing back to them. Bubble size is measured from the actual wrapped text rather than
    guessed, so it stays legible regardless of the line's length. sprite_path, if given, is
    composited onto the banner first (e.g. a quest-complete reveal sprite) so the bubble still
    draws on top and stays legible over it. `irradiated` applies the Fallout filter to the scene
    but not the bubble, which stays readable."""
    base = Image.open(banner_path).convert("RGBA")
    if sprite_path:
        sprite = Image.open(sprite_path).convert("RGBA")
        base.alpha_composite(sprite)
    if irradiated:
        base = _irradiate(base)
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


# Ghost-photo look for a haunting (necromancy.py): the overlay is cropped to its visible pixels,
# then scaled, placed, mirrored, faded and blurred at random each time, so no two sightings match.
# Depth is faked from size -- a smaller ghost stands higher in the frame (further back) and comes
# out blurrier, the way a real long-exposure "ghost" does.
GHOST_HEIGHT_RANGE = (0.25, 0.65)  # fraction of the banner's height
GHOST_OPACITY_RANGE = (0.15, 0.45)
GHOST_MAX_BLUR = 2.0  # px, applied in full to the smallest ghost


def _composite_ghost(base: Image.Image, ghost_path: str) -> Image.Image:
    ghost = Image.open(ghost_path).convert("RGBA")
    bbox = ghost.getbbox()
    if bbox:
        ghost = ghost.crop(bbox)
    low, high = GHOST_HEIGHT_RANGE
    height_frac = random.uniform(low, high)
    depth = (height_frac - low) / (high - low)  # 0 = far away, 1 = right up close
    height = max(1, int(base.height * height_frac))
    width = max(1, int(ghost.width * height / ghost.height))
    ghost = ghost.resize((width, height), Image.LANCZOS)
    if random.random() < 0.5:
        ghost = ImageOps.mirror(ghost)
    blur = GHOST_MAX_BLUR * (1 - depth)
    if blur > 0.2:
        ghost = ghost.filter(ImageFilter.GaussianBlur(blur))
    opacity = random.uniform(*GHOST_OPACITY_RANGE)
    alpha = ghost.getchannel("A").point(lambda a: int(a * opacity))
    ghost.putalpha(alpha)
    # Feet land lower the closer it is: from ~70% down the frame (far) to the bottom edge (close).
    feet_y = int(base.height * (0.7 + 0.3 * depth))
    y = max(0, min(base.height - height, feet_y - height))
    x = random.randint(min(0, base.width - width), max(0, base.width - width))
    # A plain paste onto the empty layer: passing the ghost as its own mask too would apply its alpha
    # twice (opacity squared -- a 30% ghost came out ~9%, invisible once blurred into a busy banner).
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.paste(ghost, (x, y))
    return Image.alpha_composite(base, layer)


def render_room_banner(
    banner_path: str, sprite_paths: list[str] = (), irradiated: bool = False, ghost_path: str | None = None,
) -> io.BytesIO:
    """The plain banner, with every currently-present NPC's portrait sprite (if any) composited on
    in order -- no dialogue bubble, unlike render_npc_dialogue. Used for a room's resting/default
    state so e.g. a completed quest's reveal NPC (the Greasy Princess) stays visible every time the
    room is opened, not just mid-conversation. Composites a *list* rather than a single optional
    sprite so this works uniformly for any room regardless of how many present NPCs have a
    sprite_path -- room_view.py is what actually decides who's present and passes their paths in,
    this function has no idea what a "princess" or a "quest" is. `ghost_path`, when given, adds a
    haunting's ghost on top (see _composite_ghost) -- room_view decides when, this just draws it."""
    base = Image.open(banner_path).convert("RGBA")
    for sprite_path in sprite_paths:
        sprite = Image.open(sprite_path).convert("RGBA")
        base.alpha_composite(sprite)
    if ghost_path:
        base = _composite_ghost(base, ghost_path)
    if irradiated:
        base = _irradiate(base)
    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
