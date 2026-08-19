import io
import math

from PIL import Image, ImageDraw, ImageFont

# Simple old-school wireframe corridor -- placeholder art only, no AI generation, ever. A
# monster's JSON entry drives its shape/color here; a future `sprite_path` field on a monster
# would let render_room load and composite real art instead, with no change needed at any call
# site -- see the shape-drawing fallback below for where that swap would happen.
WIDTH, HEIGHT = 500, 350
BG_COLOR = (20, 18, 22, 255)
WALL_COLOR = (60, 55, 65, 255)
WALL_OUTLINE = (100, 95, 110, 255)
FLOOR_COLOR = (35, 32, 38, 255)
CEILING_COLOR = (28, 26, 32, 255)
BACK_WALL_COLOR = (45, 42, 50, 255)

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_label_font = ImageFont.truetype(_FONT_PATH, 20)


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


def _draw_corridor(draw: ImageDraw.ImageDraw):
    cx, cy = WIDTH / 2, HEIGHT / 2 - 10
    half_w, half_h = 60, 60
    draw.polygon([(0, HEIGHT), (WIDTH, HEIGHT), (cx + half_w, cy + half_h), (cx - half_w, cy + half_h)], fill=FLOOR_COLOR)
    draw.polygon([(0, 0), (WIDTH, 0), (cx + half_w, cy - half_h), (cx - half_w, cy - half_h)], fill=CEILING_COLOR)
    draw.polygon(
        [(0, 0), (0, HEIGHT), (cx - half_w, cy + half_h), (cx - half_w, cy - half_h)],
        fill=WALL_COLOR, outline=WALL_OUTLINE, width=2,
    )
    draw.polygon(
        [(WIDTH, 0), (WIDTH, HEIGHT), (cx + half_w, cy + half_h), (cx + half_w, cy - half_h)],
        fill=WALL_COLOR, outline=WALL_OUTLINE, width=2,
    )
    draw.rectangle([cx - half_w, cy - half_h, cx + half_w, cy + half_h], fill=BACK_WALL_COLOR, outline=WALL_OUTLINE, width=2)


def render_room(room_index: int, total_rooms: int, monster: dict) -> io.BytesIO:
    """Renders the corridor view for one dungeon room, with its monster standing at the far end.
    Combat HP/stats are shown as embed text by the caller, not baked into this image -- this
    only draws the scene. Returns a ready-to-attach BytesIO."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)
    _draw_corridor(draw)

    cx, cy = WIDTH / 2, HEIGHT / 2 - 10
    _draw_monster_shape(draw, cx, cy, 60, monster["shape"], _parse_color(monster["color"]))

    draw.text((16, HEIGHT - 32), f"Room {room_index + 1} / {total_rooms}", font=_label_font, fill=(200, 200, 210, 255))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
