import io

from PIL import Image, ImageDraw, ImageFont

import horserace

LANE_H = 46
LABEL_W = 150
TRACK_MARGIN_R = 60
TRACK_W = 420
IMG_W = LABEL_W + TRACK_W + TRACK_MARGIN_R
IMG_H = LANE_H * len(horserace.HORSES) + 20

DIRT = (150, 105, 65, 255)
RAIL = (230, 225, 210, 255)
LANE_LINE = (120, 82, 48, 255)
GATE_COLOR = (60, 60, 65, 255)
FINISH_LIGHT = (255, 255, 255, 255)
FINISH_DARK = (30, 30, 30, 255)
TEXT_COLOR = (255, 255, 245, 255)
CROWN = (255, 210, 60, 255)

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_name_font = ImageFont.truetype(_FONT_PATH, 15)
_marker_font = ImageFont.truetype(_FONT_PATH, 14)


def _lane_center_y(i: int) -> float:
    return 10 + LANE_H * i + LANE_H / 2


def _vcentered_text(draw: ImageDraw.ImageDraw, x, cy, text, font, fill, anchor_left=True):
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x if anchor_left else x - w
    draw.text((tx - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def _draw_track(draw: ImageDraw.ImageDraw, names: list[str]):
    n = len(horserace.HORSES)
    draw.rectangle([LABEL_W, 0, IMG_W, IMG_H], fill=DIRT)
    for i in range(n + 1):
        y = 10 + LANE_H * i
        draw.line([(LABEL_W, y), (IMG_W, y)], fill=LANE_LINE, width=1)

    draw.line([(LABEL_W, 0), (LABEL_W, IMG_H)], fill=GATE_COLOR, width=6)

    finish_x = IMG_W - TRACK_MARGIN_R
    square = 8
    for i in range(n):
        y0 = 10 + LANE_H * i
        for row in range((LANE_H // square) + 1):
            for col in range(2):
                if (row + col) % 2 == 0:
                    x0 = finish_x + col * square
                    y1 = y0 + row * square
                    draw.rectangle([x0, y1, x0 + square, min(y1 + square, y0 + LANE_H)], fill=FINISH_LIGHT)
                else:
                    x0 = finish_x + col * square
                    y1 = y0 + row * square
                    draw.rectangle([x0, y1, x0 + square, min(y1 + square, y0 + LANE_H)], fill=FINISH_DARK)

    for i in range(n):
        cy = _lane_center_y(i)
        _vcentered_text(draw, 8, cy - 8, names[i], _name_font, TEXT_COLOR)
        _vcentered_text(draw, 8, cy + 9, horserace.describe_odds(i), _marker_font, (225, 210, 180, 255))


def _draw_horse_marker(draw: ImageDraw.ImageDraw, x: float, y: float, color, label: str, crowned: bool):
    r = 13
    if crowned:
        draw.polygon(
            [(x - r * 0.7, y - r - 2), (x - r * 0.35, y - r - 12), (x, y - r - 4),
             (x + r * 0.35, y - r - 12), (x + r * 0.7, y - r - 2)],
            fill=CROWN, outline=(150, 110, 20, 255),
        )
    draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline=(20, 20, 20, 255), width=2)
    bbox = draw.textbbox((0, 0), label, font=_marker_font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x - w / 2 - bbox[0], y - h / 2 - bbox[1]), label, font=_marker_font, fill=(255, 255, 255, 255))


def render_track(
    positions: list[float] | None = None,
    final_max: float | None = None,
    winner: int | None = None,
    names: list[str] | None = None,
) -> io.BytesIO:
    """Draws the track with each horse's marker placed by its progress.

    `positions` is per-horse cumulative distance (None = everyone still at the gate).
    `final_max` normalizes the scale across every frame of a race so markers only ever
    move rightward as legs are drawn; without it, positions are normalized to their own max.
    `winner` (only known on the final frame) gets a little crown over its marker.
    `names` overrides the default horse names (e.g. with an owner's custom name).
    """
    img = Image.new("RGBA", (IMG_W, IMG_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    names = names or [h["name"] for h in horserace.HORSES]
    _draw_track(draw, names)

    n = len(horserace.HORSES)
    lane_x0 = LABEL_W + 14
    lane_x1 = IMG_W - TRACK_MARGIN_R - 14
    span = lane_x1 - lane_x0

    if positions is None:
        positions = [0.0] * n
    scale = final_max if final_max else (max(positions) or 1.0)

    for i, horse in enumerate(horserace.HORSES):
        frac = min(positions[i] / scale, 1.0) if scale else 0.0
        x = lane_x0 + span * frac
        y = _lane_center_y(i)
        label = str(i + 1)
        _draw_horse_marker(draw, x, y, horse["color"], label, crowned=(winner == i))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
