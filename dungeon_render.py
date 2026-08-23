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
_card_rank_font = ImageFont.truetype(_FONT_PATH, 17)
_card_suit_font = ImageFont.truetype(_FONT_PATH, 13)
_card_strip_label_font = ImageFont.truetype(_FONT_PATH, 14)

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


def _load_monster_sprite(sprite_path: str, target_height: int = SPRITE_HEIGHT) -> Image.Image | None:
    if not sprite_path or not os.path.exists(sprite_path):
        return None
    sprite = Image.open(sprite_path).convert("RGBA")
    scale = target_height / sprite.height
    new_size = (max(1, round(sprite.width * scale)), target_height)
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


# x-offsets from center for each living monster, keyed by how many are being drawn -- count 1 is
# byte-identical to the old single-monster centering (offset 0). Beyond 2, sprites also shrink
# (see _sprite_height_for) so a 4-wide group doesn't overlap at WIDTH=500.
_GROUP_X_OFFSETS = {
    1: [0],
    2: [-90, 90],
    3: [-150, 0, 150],
    4: [-180, -60, 60, 180],
}


def _sprite_height_for(count: int) -> int:
    return SPRITE_HEIGHT if count <= 2 else round(SPRITE_HEIGHT * 0.75)


# FFX-style turn-order card strip, composited below the room scene (see dungeon.preview_next_turns
# for the schedule these cards visualize). Fixed at a size that comfortably fits up to 10 cards --
# the largest count any call site passes -- within WIDTH, so there's no dynamic per-card resizing
# to keep in sync with font sizes.
CARD_WIDTH, CARD_HEIGHT, CARD_GAP = 42, 58, 4
CARD_STRIP_HEIGHT = CARD_HEIGHT + 28  # card row + a "Next up:" label row above it

_RED_SUITS = {"♥", "♦"}


def _draw_player_card(draw: ImageDraw.ImageDraw, x: float, y: float, rank: str, suit: str) -> None:
    """A player/party-member card: existing rank letter (A/K/Q/J, dungeon.CLASSES[...]["rank"]) +
    suit symbol (♠♥♦♣, dungeon.SUIT_SYMBOLS[...]), suit-colored red/black per standard card
    convention -- zero new art, reuses the exact pairing that already names every build."""
    color = (190, 25, 25, 255) if suit in _RED_SUITS else (20, 20, 20, 255)
    draw.rounded_rectangle(
        [x, y, x + CARD_WIDTH, y + CARD_HEIGHT], radius=5, fill=(245, 242, 230, 255), outline=(0, 0, 0, 255), width=2
    )
    draw.text((x + 5, y + 3), rank, font=_card_rank_font, fill=color)
    suit_box = draw.textbbox((0, 0), suit, font=_card_suit_font)
    sw, sh = suit_box[2] - suit_box[0], suit_box[3] - suit_box[1]
    draw.text((x + CARD_WIDTH - sw - 6, y + CARD_HEIGHT - sh - 7), suit, font=_card_suit_font, fill=color)


def _draw_monster_card(draw: ImageDraw.ImageDraw, x: float, y: float, shape: str, color_hex: str) -> None:
    """A monster card: reuses _draw_monster_shape's own shape/color, drawn small and centered on a
    dark card outline -- the same placeholder identity already used for the room scene itself."""
    draw.rounded_rectangle(
        [x, y, x + CARD_WIDTH, y + CARD_HEIGHT], radius=5, fill=(35, 18, 18, 255), outline=(0, 0, 0, 255), width=2
    )
    cx, cy = x + CARD_WIDTH / 2, y + CARD_HEIGHT / 2 + 2
    _draw_monster_shape(draw, cx, cy, CARD_WIDTH * 0.34, shape, _parse_color(color_hex))


def _composite_turn_order_strip(img: Image.Image, turn_order: list[dict]) -> Image.Image:
    """Grows the room scene downward to fit a horizontal strip of playing-card-style icons for the
    next several turns (dungeon.preview_next_turns) -- purely cosmetic FFX flavor. Each entry is
    `{"kind": "player", "rank": ..., "suit": ...}` or `{"kind": "monster", "shape": ..., "color":
    ...}`; the same combatant can (and does) appear on multiple cards when they're fast enough to
    act again before others get a turn -- intended, matches FFX's own UI. Returns a new taller
    image; doesn't mutate `img`."""
    strip = Image.new("RGBA", (WIDTH, CARD_STRIP_HEIGHT), (15, 15, 20, 255))
    draw = ImageDraw.Draw(strip)
    draw.text((8, 3), "Next up:", font=_card_strip_label_font, fill=(190, 190, 200, 255))
    n = len(turn_order)
    total_width = n * CARD_WIDTH + max(0, n - 1) * CARD_GAP
    start_x = max(6, (WIDTH - total_width) // 2)
    y = CARD_STRIP_HEIGHT - CARD_HEIGHT - 5
    for i, card in enumerate(turn_order):
        x = start_x + i * (CARD_WIDTH + CARD_GAP)
        if card["kind"] == "player":
            _draw_player_card(draw, x, y, card["rank"], card["suit"])
        else:
            _draw_monster_card(draw, x, y, card["shape"], card["color"])
    combined = Image.new("RGBA", (WIDTH, HEIGHT + CARD_STRIP_HEIGHT), (0, 0, 0, 255))
    combined.alpha_composite(img, (0, 0))
    combined.alpha_composite(strip, (0, HEIGHT))
    return combined


def render_room(
    visited_count: int, monsters: list[dict], background_path: str | None = None,
    turn_order: list[dict] | None = None,
) -> io.BytesIO:
    """Renders the corridor view for one dungeon room -- with its living monster(s) standing at the
    far end if there are any (combat rooms), or just the empty scene if not (choice rooms, or a
    combat room whose group has been fully cleared, pass an empty list). Combat HP/stats are shown
    as embed text by the caller, not baked into this image -- this only draws the scene.
    `visited_count` labels the room ("Room N") with no denominator, since a branching delve graph
    has no single well-defined total room count the way a flat list did -- a fork's two paths can
    have different lengths, and a room can even be revisited via a dead-end self-loop. `turn_order`
    (optional, only ever passed for combat rooms with someone still alive to schedule) grows the
    image downward to add the FFX-style turn-order card strip -- see _composite_turn_order_strip
    for its shape. Returns a ready-to-attach BytesIO."""
    img = _load_background(background_path)

    cx, cy = WIDTH / 2, HEIGHT / 2 - 10
    count = len(monsters)
    sprite_height = _sprite_height_for(count)
    offsets = _GROUP_X_OFFSETS.get(count, _GROUP_X_OFFSETS[4])
    for monster, x_offset in zip(monsters, offsets):
        mx = cx + x_offset
        sprite = _load_monster_sprite(monster.get("sprite_path"), sprite_height)
        if sprite:
            pos = (round(mx - sprite.width / 2), round(cy + 75 - sprite.height))
            img.alpha_composite(sprite, pos)
        else:
            draw = ImageDraw.Draw(img)
            radius = 60 if count <= 2 else 45
            _draw_monster_shape(draw, mx, cy, radius, monster["shape"], _parse_color(monster["color"]))

    draw = ImageDraw.Draw(img)
    draw.text((16, HEIGHT - 32), f"Room {visited_count}", font=_label_font, fill=(200, 200, 210, 255))

    if turn_order:
        img = _composite_turn_order_strip(img, turn_order)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
