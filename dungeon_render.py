import io
import math
import os

from PIL import Image, ImageDraw, ImageFont

# Room background is a real image (assets/dungeon/dungeon1.png), sized to WIDTH x HEIGHT so it
# drops in with no scaling. Monsters with a `sprite_path` in dungeon_monsters.json get that art
# composited in; monsters without one (or a missing file) fall back to the placeholder
# shape/color, so new monster JSON entries work immediately without needing art first.
WIDTH, HEIGHT = 500, 350
ROOM_BG_PATH = "assets/dungeon/dungeon1.png"

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_label_font = ImageFont.truetype(_FONT_PATH, 20)

# The dungeon-entrance banner -- a separate photo banner, not the room background above.
# Dialogue rendering itself now lives in npc_render.py (shared by every NPC/hub); this module just
# owns the path, same as ranch_render.BANNER_PATH/casino_render.BANNER_PATH.
BANNER_PATH = "assets/dungeon_banner.png"


def _parse_color(hex_color: str) -> tuple[int, int, int, int]:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return (r, g, b, 255)


def _regular_polygon(cx: float, cy: float, radius: float, sides: int) -> list[tuple[float, float]]:
    rotation = -math.pi / 2  # point one vertex straight up
    return [
        (cx + radius * math.cos(rotation + i * (2 * math.pi / sides)),
         cy + radius * math.sin(rotation + i * (2 * math.pi / sides)))
        for i in range(sides)
    ]


_SHAPE_SIDES = {"triangle": 3, "pentagon": 5, "hexagon": 6}


def _draw_monster_shape(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float, shape: str, color: tuple):
    if shape == "circle":
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=color, outline=(0, 0, 0, 180), width=3)
        return
    sides = _SHAPE_SIDES.get(shape, 8)  # unrecognized shapes fall back to an octagon, not an error
    draw.polygon(_regular_polygon(cx, cy, radius, sides), fill=color, outline=(0, 0, 0, 180), width=3)


# Pixel-art sprites all get scaled to this height regardless of their source size, so mismatched
# source resolutions (e.g. a 60px vs a 132px export) still read as the same in-world scale.
# NEAREST keeps upscaling blocky/crisp instead of blurring the pixel art.
SPRITE_HEIGHT = 140


def _load_monster_sprite(sprite_path: str) -> Image.Image | None:
    if not sprite_path or not os.path.exists(sprite_path):
        return None
    sprite = Image.open(sprite_path).convert("RGBA")
    scale = SPRITE_HEIGHT / sprite.height
    new_size = (max(1, round(sprite.width * scale)), SPRITE_HEIGHT)
    return sprite.resize(new_size, Image.NEAREST)


def _load_background(background_path: str | None) -> Image.Image:
    """The delve-specific background if it's set and the file actually exists, else the default
    -- same forgiving fallback as _load_monster_sprite, so a delve JSON entry with no
    background_path (or a since-deleted file) still renders instead of erroring mid-delve.
    Uploaded images won't generally already be exactly WIDTH x HEIGHT the way the hand-placed
    default is, so this cover-fits: scale to fill the frame, then center-crop, rather than
    stretching (which would distort) or leaving the frame partially empty."""
    path = background_path if background_path and os.path.exists(background_path) else ROOM_BG_PATH
    img = Image.open(path).convert("RGBA")
    if img.size != (WIDTH, HEIGHT):
        scale = max(WIDTH / img.width, HEIGHT / img.height)
        new_size = (round(img.width * scale), round(img.height * scale))
        img = img.resize(new_size, Image.LANCZOS)
        left = (img.width - WIDTH) // 2
        top = (img.height - HEIGHT) // 2
        img = img.crop((left, top, left + WIDTH, top + HEIGHT))
    return img


def render_room(visited_count: int, monster: dict | None, background_path: str | None = None) -> io.BytesIO:
    """Renders the corridor view for one dungeon room -- with its monster standing at the far end
    if there is one (combat rooms), or just the empty scene if not (choice rooms have no monster
    to draw). Combat HP/stats are shown as embed text by the caller, not baked into this image --
    this only draws the scene. `visited_count` labels the room ("Room N") with no denominator,
    since a branching delve graph has no single well-defined total room count the way a flat list
    did -- a fork's two paths can have different lengths, and a room can even be revisited via a
    dead-end self-loop. Returns a ready-to-attach BytesIO."""
    img = _load_background(background_path)

    if monster is not None:
        cx, cy = WIDTH / 2, HEIGHT / 2 - 10
        sprite = _load_monster_sprite(monster.get("sprite_path"))
        if sprite:
            pos = (round(cx - sprite.width / 2), round(cy + 75 - sprite.height))
            img.alpha_composite(sprite, pos)
        else:
            draw = ImageDraw.Draw(img)
            _draw_monster_shape(draw, cx, cy, 60, monster["shape"], _parse_color(monster["color"]))

    draw = ImageDraw.Draw(img)
    draw.text((16, HEIGHT - 32), f"Room {visited_count}", font=_label_font, fill=(200, 200, 210, 255))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
