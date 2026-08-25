"""Composites a player's placed housing items onto the house background for !house -- the PIL
rendering half of the housing system, split out from housing_view.py the same way
dungeon_render.py/npc_render.py are split from dungeon_view.py/room_view.py (this module builds
plain PIL images, never discord.ui components; housing_view.py owns the embed/picker UI).

BACKGROUND_PATH/FLOOR_START/CELL describe assets/housing/grid_template.png's layout: a 640x640
image, floor grid starting at (FLOOR_START, FLOOR_START), CELL px per cell, 3x3. Swapping in real
house art later just means replacing that file (or repointing BACKGROUND_PATH) -- as long as the
new art keeps the same floor-grid geometry, nothing here needs to change.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont

BACKGROUND_PATH = "assets/housing/grid_template.png"
FLOOR_START = 95
CELL = 150
ART_PADDING = 12  # an item's own art is scaled to fit within (CELL - 2*ART_PADDING), so it never touches the grid lines

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_INITIAL_FONT = ImageFont.truetype(_FONT_PATH, 64)

# Cycled by item id (not random) so the same item always gets the same placeholder color across
# renders -- deliberately not emoji-based: Pillow's default font support has no color-emoji glyphs
# (DejaVuSans just draws a blank .notdef box for one), so a letter-in-a-circle is the reliable
# "no art yet" placeholder, same role dungeon_render's shape/color fallback plays for sprite-less
# monsters.
_PLACEHOLDER_COLORS = [
    (200, 90, 90, 255), (90, 150, 200, 255), (200, 160, 70, 255),
    (110, 170, 110, 255), (160, 100, 190, 255), (90, 180, 170, 255),
    (210, 120, 160, 255), (150, 140, 90, 255),
]


def _load_item_art(item: dict, box: int) -> Image.Image:
    """The item's own image_path, scaled to fit within a box x box square (preserving aspect
    ratio) -- or, for an item with no art yet (or a missing/deleted file), a colored circle with
    its name's first letter, so new housing_items.json content works immediately without needing
    art first."""
    image_path = item.get("image_path")
    if image_path and os.path.exists(image_path):
        art = Image.open(image_path).convert("RGBA")
        scale = min(box / art.width, box / art.height)
        new_size = (max(1, round(art.width * scale)), max(1, round(art.height * scale)))
        return art.resize(new_size, Image.LANCZOS)

    art = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    draw = ImageDraw.Draw(art)
    color = _PLACEHOLDER_COLORS[hash(item["id"]) % len(_PLACEHOLDER_COLORS)]
    draw.ellipse([2, 2, box - 2, box - 2], fill=color, outline=(0, 0, 0, 160), width=3)
    letter = item["name"][0].upper() if item.get("name") else "?"
    bbox = draw.textbbox((0, 0), letter, font=_INITIAL_FONT)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        (box / 2 - w / 2 - bbox[0], box / 2 - h / 2 - bbox[1]), letter, font=_INITIAL_FONT,
        fill=(255, 255, 255, 255),
    )
    return art


def render_house(placements: dict[int, str], housing_items: dict[str, dict]) -> io.BytesIO:
    """Composites every placed item's own art (or placeholder) onto the house background at its
    grid slot's fixed pixel position. `placements` is {slot: item_id} (db.get_house_placements'
    own shape); an item id with no match in `housing_items` (e.g. removed from content after being
    placed) is skipped rather than crashing."""
    base = Image.open(BACKGROUND_PATH).convert("RGBA")
    box = CELL - 2 * ART_PADDING
    for slot, item_id in placements.items():
        item = housing_items.get(item_id)
        if item is None:
            continue
        row, col = divmod(slot, 3)
        cell_x = FLOOR_START + col * CELL
        cell_y = FLOOR_START + row * CELL
        art = _load_item_art(item, box)
        paste_x = cell_x + (CELL - art.width) // 2
        paste_y = cell_y + (CELL - art.height) // 2
        base.alpha_composite(art, (paste_x, paste_y))

    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
