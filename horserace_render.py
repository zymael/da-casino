import io
import os
import random

from PIL import Image, ImageDraw, ImageFont

import horse_clothes
import horserace

# The track is rendered as a series of side-view "photos" rather than an overhead oval: a fixed
# 160x120 pixel-art backdrop (assets/horses/bgs/racebg.png), upscaled by PHOTO_SCALE with NEAREST
# so it stays crisp like the horse sprites rather than blurring like a real photo would.
LEGEND_W = 190
PHOTO_SCALE = 3
SRC_W, SRC_H = 160, 120
PHOTO_W, PHOTO_H = SRC_W * PHOTO_SCALE, SRC_H * PHOTO_SCALE
IMG_W = LEGEND_W + PHOTO_W
IMG_H = PHOTO_H

HORSE_DIR = "assets/horses"
BG_DIR = "assets/horses/bgs"

# racebg.png's horizon (sky/grass boundary) sits at source y=47, with the grass band running to
# the bottom of the canvas -- verified by inspecting the source art, not guessed. tree.png,
# kelbreeder.png, and stands.png are all authored bottom-aligned to that same y=47, so they read
# as trackside scenery standing on the horizon once composited at the same scale.
_HORIZON_SRC_Y = 47
HORIZON_Y = _HORIZON_SRC_Y * PHOTO_SCALE
GRASS_TOP = 48 * PHOTO_SCALE
GRASS_BOTTOM = SRC_H * PHOTO_SCALE
GRASS_CENTER_Y = (GRASS_TOP + GRASS_BOTTOM) / 2

# Horses run left -> right, gate at TRACK_LEFT, finish line at FINISH_X. PAST_FINISH_PUSH_X pushes
# the top 3 further right (staggered by finishing order) on the final frame, same idea as the old
# oval renderer's PAST_FINISH_T -- "clearly past the line, in the right order" -- just along a
# straight line instead of around a loop. LANE_GAP_Y/RANK_LANE_GAP_Y are the equivalent vertical
# spread (perpendicular to the direction of travel).
TRACK_LEFT = 30
FINISH_X = PHOTO_W - 110
PAST_FINISH_PUSH_X = {0: 55, 1: 35, 2: 16}
RANK_LANE_GAP_Y = 30
LANE_GAP_Y = 22

# A horse more than this fraction of the track behind the leader just isn't in frame this shot --
# "not every horse need be in the image if they are VERY far back."
CAMERA_CUTOFF = 0.28

HORSE_DISPLAY_HEIGHT = 46

TEXT_COLOR = (255, 255, 245, 255)
CROWN = (255, 210, 60, 255)
RANK_BADGE_COLORS = {0: (255, 210, 60, 255), 1: (200, 200, 210, 255), 2: (200, 140, 70, 255)}

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_legend_name_font = ImageFont.truetype(_FONT_PATH, 15)
_legend_odds_font = ImageFont.truetype(_FONT_PATH, 13)
_marker_font = ImageFont.truetype(_FONT_PATH, 13)
_rank_font = ImageFont.truetype(_FONT_PATH, 12)


def _vcentered_text(draw: ImageDraw.ImageDraw, x, cy, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def _centered_text(draw: ImageDraw.ImageDraw, cx, cy, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def _draw_legend(draw: ImageDraw.ImageDraw, names: list[str], colors: list, odds_labels: list[str]):
    swatch = 12
    row_h = 34
    for i, name in enumerate(names):
        y = 10 + row_h * i
        draw.rectangle([10, y + 3, 10 + swatch, y + 3 + swatch], fill=colors[i], outline=(20, 20, 20, 255))
        _vcentered_text(draw, 10 + swatch + 8, y + 9, f"{i + 1}. {name}", _legend_name_font, TEXT_COLOR)
        _vcentered_text(
            draw, 10 + swatch + 8, y + 25, odds_labels[i], _legend_odds_font, (225, 210, 180, 255)
        )


def _load_scaled(path: str) -> Image.Image:
    """Loads a source pixel-art asset and upscales it by PHOTO_SCALE with NEAREST -- an integer
    scale factor, so this stays perfectly crisp rather than blurring the flat-color pixel art the
    way a photographic resize (LANCZOS) would."""
    img = Image.open(path).convert("RGBA")
    return img.resize((img.width * PHOTO_SCALE, img.height * PHOTO_SCALE), Image.NEAREST)


def _content_crop(img: Image.Image) -> Image.Image:
    """Crops to just the non-transparent content -- tree/stands/kelbreeder/finish are all full
    160x120 canvases with their actual art in one small corner, so this is what lets them be
    repositioned as standalone sprites instead of full-canvas overlays."""
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


_racebg_img = _load_scaled(f"{BG_DIR}/racebg.png")

# tree/kelbreeder/stands sum to ~154 of the source canvas's 160px width -- they were clearly
# authored to span the whole horizon edge-to-edge. Each frame shuffles their left-to-right order
# and jitters the (small) leftover slack between them, so consecutive photos don't look identical,
# while laying them out strictly left-to-right guarantees they never overlap.
_SCENERY_NAMES = ["tree", "kelbreeder", "stands"]
_scenery_sprites = {name: _content_crop(_load_scaled(f"{BG_DIR}/{name}.png")) for name in _SCENERY_NAMES}

_finish_sprite = _content_crop(_load_scaled(f"{BG_DIR}/finish.png"))

_horse_sprite_cache: dict[tuple[str, tuple[str, ...]], Image.Image | None] = {}
_clothes_image_cache: dict[str, Image.Image | None] = {}


def _coat_asset_filename(coat: str) -> str:
    """assets/horses/*.png filenames are the coat name lowercased/underscored, except two
    irregular ones: the file is "palamino.png" (a typo in the art, not in horserace.COAT_COLORS'
    "Palomino"), and the one-off SPECIAL_COAT_RED_SPOTTED foal uses the hand-drawn pizza_face.png
    rather than a coat-named file."""
    overrides = {"Palomino": "palamino.png", horserace.SPECIAL_COAT_RED_SPOTTED: "pizza_face.png"}
    return overrides.get(coat, coat.lower().replace(" ", "_") + ".png")


def _clothes_image(item_id: str) -> Image.Image | None:
    """None (rather than raising) for a missing/bad image_path, so a content-editor mistake
    degrades to "cosmetic just doesn't show" instead of breaking every race image."""
    if item_id not in _clothes_image_cache:
        item = horse_clothes.HORSE_CLOTHES.get(item_id)
        path = item["image_path"] if item else None
        _clothes_image_cache[item_id] = Image.open(path).convert("RGBA") if path and os.path.exists(path) else None
    return _clothes_image_cache[item_id]


def _horse_sprite(coat: str, clothes_ids: tuple[str, ...] = ()) -> Image.Image | None:
    """None (rather than raising) for a coat with no matching file, so a typo'd or future coat
    name degrades to the plain color-blob fallback in _paste_horse instead of breaking the race.
    clothes_ids (equipped cosmetics, if any) are composited onto the coat's native-resolution
    image before the shared upscale below -- assets/horses/horse_clothes/*.png is pre-authored on
    the exact same canvas size as assets/horses/*.png specifically so this needs no per-item
    positioning, just alpha_composite at (0, 0)."""
    cache_key = (coat, clothes_ids)
    if cache_key not in _horse_sprite_cache:
        path = os.path.join(HORSE_DIR, _coat_asset_filename(coat))
        if os.path.exists(path):
            img = Image.open(path).convert("RGBA")
            for item_id in clothes_ids:
                clothes_img = _clothes_image(item_id)
                if clothes_img is not None and clothes_img.size == img.size:
                    img.alpha_composite(clothes_img)
            scale = HORSE_DISPLAY_HEIGHT / img.height
            new_size = (max(1, round(img.width * scale)), HORSE_DISPLAY_HEIGHT)
            _horse_sprite_cache[cache_key] = img.resize(new_size, Image.NEAREST)
        else:
            _horse_sprite_cache[cache_key] = None
    return _horse_sprite_cache[cache_key]


def _place_scenery(photo: Image.Image):
    order = list(_SCENERY_NAMES)
    random.shuffle(order)
    sprites = [_scenery_sprites[name] for name in order]
    slack = max(PHOTO_W - sum(s.width for s in sprites), 0)
    weights = [random.random() for _ in range(len(sprites) + 1)]
    wsum = sum(weights) or 1.0
    gaps = [w / wsum * slack for w in weights]
    x = gaps[0]
    for sprite, gap in zip(sprites, gaps[1:]):
        photo.alpha_composite(sprite, (round(x), HORIZON_Y - sprite.height))
        x += sprite.width + gap


def _base_photo() -> Image.Image:
    """One race photo's backdrop: the bg layer plus that frame's own randomized scenery
    placement -- called fresh per rendered frame so consecutive photos don't look identical."""
    photo = _racebg_img.copy()
    _place_scenery(photo)
    return photo


def _paste_finish_line(photo: Image.Image):
    x = round(FINISH_X - _finish_sprite.width / 2)
    photo.alpha_composite(_finish_sprite, (x, GRASS_TOP))


def _paste_horse(
    photo: Image.Image, draw: ImageDraw.ImageDraw, x: float, y: float, coat: str, clothes_ids: tuple[str, ...],
    color, label: str, rank: int | None,
):
    sprite = _horse_sprite(coat, clothes_ids)
    half_h = HORSE_DISPLAY_HEIGHT / 2
    if sprite is not None:
        photo.alpha_composite(sprite, (round(x - sprite.width / 2), round(y - sprite.height / 2)))
    else:
        draw.ellipse([x - half_h, y - half_h, x + half_h, y + half_h], fill=color, outline=(20, 20, 20, 255), width=2)

    badge_r = 9
    bx, by = x, y - half_h - badge_r - 2
    if rank == 0:
        draw.polygon(
            [(bx - badge_r * 0.7, by - badge_r - 2), (bx - badge_r * 0.35, by - badge_r - 10),
             (bx, by - badge_r - 3), (bx + badge_r * 0.35, by - badge_r - 10),
             (bx + badge_r * 0.7, by - badge_r - 2)],
            fill=CROWN, outline=(30, 30, 30, 255),
        )
        _centered_text(draw, bx, by - badge_r - 16, "1st", _rank_font, TEXT_COLOR)
    elif rank is not None:
        _centered_text(draw, bx + badge_r + 16, by, {1: "2nd", 2: "3rd"}[rank], _rank_font, RANK_BADGE_COLORS[rank])

    draw.ellipse([bx - badge_r, by - badge_r, bx + badge_r, by + badge_r], fill=color, outline=(20, 20, 20, 255), width=2)
    _centered_text(draw, bx, by, label, _marker_font, (255, 255, 255, 255))


def render_track(
    names: list[str],
    colors: list[tuple[int, int, int, int]],
    coats: list[str],
    odds_labels: list[str],
    positions: list[float] | None = None,
    final_max: float | None = None,
    finish_order: list[int] | None = None,
    clothes: list[tuple[str, ...]] | None = None,
) -> io.BytesIO:
    """Draws a legend (name/color/odds per horse, unchanged from the old oval renderer) plus one
    "photo" of the race: a randomized scenery backdrop with each horse's own coat sprite (plus any
    equipped cosmetics from `clothes`, parallel to `coats`; None/omitted means nobody's dressed up)
    placed along a straight left-to-right track by progress, numbered/colored to match the legend.

    `positions` is per-horse cumulative distance (None = everyone still at the gate). `final_max`
    normalizes the scale across every frame of a race so markers only move forward. `finish_order`
    (only passed for the final frame) ranks every racing position 1st-first; the top 3 are pushed
    past the finish line, staggered in order. The finish line itself only appears on this final
    frame and on whichever earlier frame already has the leader at frac 1.0 (the last leg, before
    the ranked push) -- "one before, one after" the same way the old renderer showed it. A horse
    more than CAMERA_CUTOFF behind the leader is left out of the shot entirely.
    """
    n = len(names)
    clothes = clothes if clothes is not None else [()] * n
    img = Image.new("RGBA", (IMG_W, IMG_H), (0, 0, 0, 0))
    _draw_legend(ImageDraw.Draw(img), names, colors, odds_labels)

    photo = _base_photo()

    if positions is None:
        positions = [0.0] * n
    scale = final_max if final_max else (max(positions) or 1.0)
    fracs = [min(p / scale, 1.0) if scale else 0.0 for p in positions]
    leader_frac = max(fracs) if fracs else 0.0

    if finish_order is not None or leader_frac >= 0.999:
        _paste_finish_line(photo)

    draw = ImageDraw.Draw(photo)
    rank_of_position = {pos: rank for rank, pos in enumerate(finish_order[:3])} if finish_order else {}

    to_draw = []
    for i in range(n):
        rank = rank_of_position.get(i)
        if rank is not None:
            x = FINISH_X + PAST_FINISH_PUSH_X[rank]
            y = GRASS_CENTER_Y + (1 - rank) * RANK_LANE_GAP_Y
        else:
            if fracs[i] < leader_frac - CAMERA_CUTOFF:
                continue
            x = TRACK_LEFT + fracs[i] * (FINISH_X - TRACK_LEFT)
            y = GRASS_CENTER_Y + (i - (n - 1) / 2) * LANE_GAP_Y
        to_draw.append((i, x, y, rank))

    # Farther-back lanes (lower on screen = higher y = closer to camera) must be painted last so
    # they occlude horses standing behind them -- sorting by progress instead left horses looking
    # like they floated in front of/behind the wrong lane-mates.
    for i, x, y, rank in sorted(to_draw, key=lambda t: t[2]):
        _paste_horse(photo, draw, x, y, coats[i], clothes[i], colors[i], str(i + 1), rank)

    img.alpha_composite(photo, (LEGEND_W, 0))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
