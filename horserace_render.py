import io
import math

from PIL import Image, ImageDraw, ImageFont

LEGEND_W = 190
TRACK_MARGIN = 30
TRACK_W = 480
TRACK_H = 280
IMG_W = LEGEND_W + TRACK_W
IMG_H = TRACK_H + 2 * TRACK_MARGIN

# Stadium (rounded-rectangle) track geometry, in track-area-local coordinates. The straight
# length and turn radius are picked so the loop fills TRACK_W x TRACK_H once TRACK_MARGIN is
# subtracted on every side.
TURN_RADIUS = (TRACK_H - 2 * TRACK_MARGIN) / 2
STRAIGHT_LEN = TRACK_W - 2 * TRACK_MARGIN - 2 * TURN_RADIUS

# Horses are spread into concentric "lanes" around the loop by a small perpendicular offset
# from the centerline. RING_HALF_WIDTH is how far the drawn dirt band extends either side of
# the centerline; LANE_GAP must keep every horse's marker (LANE_GAP * up to ~3.5 lanes either
# way, for a full RACE_FIELD_SIZE=8 field, plus its own radius) within that band.
RING_HALF_WIDTH = 55
LANE_GAP = 11

# Horses run START_T -> FINISH_T (increasing t, wrapping past 1.0) -- a bit less than a full
# lap, so the race visibly sweeps around most of the oval before hitting the home stretch.
FINISH_T = 0.0
ARC_FRACTION = 0.85
START_T = (FINISH_T - ARC_FRACTION) % 1.0

# How far past the finish line (as a fraction of the loop) the top-3 markers are pushed on the
# final frame, so 1st/2nd/3rd read clearly as *past* the line and in the right order, rather
# than clustered right at it. RANK_LANE_GAP staggers them perpendicular to the direction of
# travel too (independent of each horse's normal racing lane), so the three form a clean
# diagonal line instead of scattering across whatever lanes they happened to be running in.
PAST_FINISH_T = {0: 0.05, 1: 0.032, 2: 0.016}
RANK_LANE_GAP = 16

DIRT = (150, 105, 65, 255)
RAIL = (230, 225, 210, 255)
INFIELD = (60, 120, 65, 255)
FINISH_LIGHT = (255, 255, 255, 255)
FINISH_DARK = (30, 30, 30, 255)
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


def _stadium_point(t: float, offset: float = 0.0) -> tuple[float, float]:
    """Point on the stadium perimeter at fraction t in [0, 1), traversed clockwise starting at
    the left end of the top straight. `offset` shifts the point perpendicular to the direction
    of travel (positive = outward), used to spread horses into concentric lanes around the loop.
    """
    L, R = STRAIGHT_LEN, TURN_RADIUS
    cap_len = math.pi * R
    perim = 2 * L + 2 * cap_len
    s = (t % 1.0) * perim

    if s < L:
        return R + s, -offset
    s -= L
    if s < cap_len:
        theta = -math.pi / 2 + (s / cap_len) * math.pi
        cx, cy, r = L + R, R, R + offset
        return cx + r * math.cos(theta), cy + r * math.sin(theta)
    s -= cap_len
    if s < L:
        return L + R - s, 2 * R + offset
    s -= L
    theta = math.pi / 2 + (s / cap_len) * math.pi
    cx, cy, r = R, R, R + offset
    return cx + r * math.cos(theta), cy + r * math.sin(theta)


def _draw_oval(draw: ImageDraw.ImageDraw, ox: float, oy: float):
    """Draws the dirt track, grass infield, rail, and finish line at origin (ox, oy)."""
    outer = [(ox + x, oy + y) for x, y in (_stadium_point(t / 200, offset=RING_HALF_WIDTH) for t in range(201))]
    inner = [(ox + x, oy + y) for x, y in (_stadium_point(t / 200, offset=-RING_HALF_WIDTH) for t in range(201))]
    draw.polygon(outer, fill=DIRT)
    draw.polygon(inner, fill=INFIELD)
    draw.line(outer + [outer[0]], fill=RAIL, width=2)
    draw.line(inner + [inner[0]], fill=RAIL, width=2)

    fx, fy = _stadium_point(FINISH_T)
    square = 8
    for row in range(-3, 4):
        for col in range(2):
            shade = FINISH_LIGHT if (row + col) % 2 == 0 else FINISH_DARK
            x0 = ox + fx + col * square
            y0 = oy + fy + row * square
            draw.rectangle([x0, y0, x0 + square, y0 + square], fill=shade)


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


def _draw_horse_marker(draw: ImageDraw.ImageDraw, x: float, y: float, color, label: str, rank: int | None):
    r = 12
    if rank == 0:
        draw.polygon(
            [(x - r * 0.7, y - r - 2), (x - r * 0.35, y - r - 12), (x, y - r - 4),
             (x + r * 0.35, y - r - 12), (x + r * 0.7, y - r - 2)],
            fill=CROWN, outline=(30, 30, 30, 255),
        )
        _centered_text(draw, x, y - r - 20, "1st", _rank_font, TEXT_COLOR)
    elif rank is not None:
        badge = RANK_BADGE_COLORS[rank]
        _centered_text(draw, x + r + 12, y, {1: "2nd", 2: "3rd"}[rank], _rank_font, badge)
    draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline=(20, 20, 20, 255), width=2)
    bbox = draw.textbbox((0, 0), label, font=_marker_font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x - w / 2 - bbox[0], y - h / 2 - bbox[1]), label, font=_marker_font, fill=(255, 255, 255, 255))


def render_track(
    names: list[str],
    colors: list[tuple[int, int, int, int]],
    odds_labels: list[str],
    positions: list[float] | None = None,
    final_max: float | None = None,
    finish_order: list[int] | None = None,
) -> io.BytesIO:
    """Draws an oval track with every horse's number/color/name/odds in a legend on the left
    (not tied to lane position -- an oval doesn't have straight parallel lanes to line text up
    against) and a numbered, colored marker per horse placed by its progress around the loop.

    `positions` is per-horse cumulative distance (None = everyone still at the gate).
    `final_max` normalizes the scale across every frame of a race so markers only ever move
    forward as legs are drawn; without it, positions are normalized to their own max.
    `finish_order` (only passed for the final frame) is every racing horse's position-list
    index, ranked 1st-first; the top 3 are pushed just past the finish line, staggered in that
    order, with a place badge, rather than plotted at their raw (near-identical) distances.
    """
    n = len(names)
    img = Image.new("RGBA", (IMG_W, IMG_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    ox, oy = LEGEND_W, TRACK_MARGIN
    _draw_oval(draw, ox, oy)
    _draw_legend(draw, names, colors, odds_labels)

    if positions is None:
        positions = [0.0] * n
    scale = final_max if final_max else (max(positions) or 1.0)
    rank_of_position = {pos: rank for rank, pos in enumerate(finish_order[:3])} if finish_order else {}

    for i in range(n):
        rank = rank_of_position.get(i)
        if rank is not None:
            # Placed in a dedicated staircase by rank alone -- not this horse's usual lane
            # offset, which would scatter 1st/2nd/3rd across unrelated lanes and obscure the
            # order the whole point of this is to show.
            t = (FINISH_T + PAST_FINISH_T[rank]) % 1.0
            offset = (1 - rank) * RANK_LANE_GAP
        else:
            frac = min(positions[i] / scale, 1.0) if scale else 0.0
            t = (START_T + frac * ARC_FRACTION) % 1.0
            offset = (i - (n - 1) / 2) * LANE_GAP
        x, y = _stadium_point(t, offset=offset)
        _draw_horse_marker(draw, ox + x, oy + y, colors[i], str(i + 1), rank)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
