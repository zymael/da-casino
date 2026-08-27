import io
import math
import os

from PIL import Image, ImageDraw, ImageFont

import cards_render
from game import hand_value

MAX_SEATS = 4

WIDTH = 1050
HEIGHT = 780

# Wood rail trim -- same beveled-frame technique roulette_render.py uses, for a consistent look
# across the casino's rendered tables.
BORDER = 28
WOOD = (92, 51, 23, 255)
WOOD_HIGHLIGHT = (150, 100, 55, 255)
WOOD_SHADOW = (55, 28, 10, 255)
FELT = (13, 82, 46, 255)
FELT_EDGE = (200, 175, 130, 200)
GOLD = (255, 205, 60, 255)
TEXT = (240, 240, 235, 255)
DIM_TEXT = (190, 205, 195, 255)
WIN_COLOR = (110, 230, 130, 255)
LOSE_COLOR = (235, 90, 90, 255)
PUSH_COLOR = (200, 200, 200, 255)

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_dealer_label_font = ImageFont.truetype(_FONT_PATH, 22)
_name_font = ImageFont.truetype(_FONT_PATH, 19)
_value_font = ImageFont.truetype(_FONT_PATH, 15)
_bet_font = ImageFont.truetype(_FONT_PATH, 14)
_result_font = ImageFont.truetype(_FONT_PATH, 15)
_felt_text_font = ImageFont.truetype(_FONT_PATH, 17)
_felt_subtext_font = ImageFont.truetype(_FONT_PATH, 13)

# Cards are drawn smaller here than cards_render's native 100x140 -- up to 4 seats (each possibly
# showing 2 side-by-side hands after a split) plus the dealer all have to fit on one scene, unlike
# any single-hand view that only ever draws one hand at native size.
DEALER_SCALE = 0.8
SEAT_SCALE = 0.55
DEALER_CARD_W = int(cards_render.CARD_WIDTH * DEALER_SCALE)
DEALER_CARD_H = int(cards_render.CARD_HEIGHT * DEALER_SCALE)
SEAT_CARD_W = int(cards_render.CARD_WIDTH * SEAT_SCALE)
SEAT_CARD_H = int(cards_render.CARD_HEIGHT * SEAT_SCALE)
CARD_GAP = 8  # gap between cards within one hand, at seat scale
HAND_GAP = 22  # gap between two hands side by side at one seat, after a split

DEALER_CENTER = (WIDTH / 2, 150)

# Seat slot centers -- fixed positions (not recomputed from however many are actually seated, the
# way uno_render spaces its seats), arranged along a downward arc below the dealer the same way a
# real table's curved rail seats players: real casino seats don't slide over when someone leaves,
# so slot i always corresponds to table.seats[i] (join order = physical seat), never to whichever
# hands happen to be in round.hands this round.
_ARC_CENTER = (WIDTH / 2, 40)
_ARC_R = 520
_SEAT_ANGLES = (-50, -16, 16, 50)  # degrees either side of straight down
SEAT_CENTERS = [
    (
        _ARC_CENTER[0] + _ARC_R * math.sin(math.radians(a)),
        _ARC_CENTER[1] + _ARC_R * math.cos(math.radians(a)),
    )
    for a in _SEAT_ANGLES
]

ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "blackjack")
_background_path = os.path.join(ASSET_DIR, "table_background.png")


def _centered_text(draw: ImageDraw.ImageDraw, cx: float, cy: float, text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def _base_table() -> Image.Image:
    """Procedural placeholder -- swapped for real painted art (assets/blackjack/
    table_background.png) the moment that file exists, same as roulette_render.py's table. Draws
    the actual shape of a real table (a "D": dealer along the flat top edge, players around the
    curved edge) rather than a plain rectangle, so the template is a meaningful paint-over
    reference, not just a placeholder color."""
    if os.path.exists(_background_path):
        return Image.open(_background_path).convert("RGBA").resize((WIDTH, HEIGHT))

    img = Image.new("RGBA", (WIDTH, HEIGHT), WOOD)
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, WIDTH - 3, HEIGHT - 3], outline=WOOD_HIGHLIGHT, width=2)
    draw.rectangle(
        [BORDER - 7, BORDER - 7, WIDTH - BORDER + 6, HEIGHT - BORDER + 6],
        outline=WOOD_SHADOW, width=3,
    )

    # The felt "D" -- a big circle centered well above the canvas, clipped by the wood rail, so
    # only its lower arc shows: flat-ish near the top (dealer's edge), curving out toward the
    # bottom (the players' edge). Radius chosen so the curve passes just inside the seat arc above.
    felt_center = (WIDTH / 2, _ARC_CENTER[1] - 60)
    felt_r = _ARC_R + 150
    felt_box = [
        felt_center[0] - felt_r, felt_center[1] - felt_r,
        felt_center[0] + felt_r, felt_center[1] + felt_r,
    ]
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse(felt_box, fill=255)
    mask_draw.rectangle([0, 0, WIDTH, BORDER + 40], fill=0)  # keep the top corners wood, not felt
    felt_layer = Image.new("RGBA", (WIDTH, HEIGHT), FELT)
    img.paste(felt_layer, (0, 0), mask)
    draw.ellipse(felt_box, outline=FELT_EDGE, width=3)

    _centered_text(draw, WIDTH / 2, BORDER + 30, "BLACKJACK PAYS 3 TO 2", _felt_text_font, GOLD)
    _centered_text(
        draw, WIDTH / 2, BORDER + 54,
        "DEALER MUST STAND ON 17 AND DRAW TO 16", _felt_subtext_font, DIM_TEXT,
    )
    return img


def _draw_hand(
    img: Image.Image, draw: ImageDraw.ImageDraw, cards: list, cx: float, top_y: float,
    *, card_w: int, card_h: int, hide_first: bool,
) -> float:
    """Draws one hand centered on cx starting at top_y, at the given card size. Returns the y just
    below the drawn cards, for whatever gets placed underneath (value/bet/result text)."""
    n = max(len(cards), 1)
    width = n * card_w + (n - 1) * CARD_GAP
    x0 = cx - width / 2
    for i, card in enumerate(cards):
        face = cards_render._back_image() if (hide_first and i == 0) else cards_render._card_image(card)
        face = face.resize((card_w, card_h), Image.LANCZOS)
        img.alpha_composite(face, (int(x0 + i * (card_w + CARD_GAP)), int(top_y)))
    return top_y + card_h


def _outcome_color(outcome: str) -> tuple:
    if outcome in ("win", "blackjack"):
        return WIN_COLOR
    if outcome == "push":
        return PUSH_COLOR
    return LOSE_COLOR


def render_table(
    table: "blackjack_view.BlackjackTable", round_state: "blackjack_view.RoundState | None",
) -> io.BytesIO:
    """Draws the whole table in one scene: the dealer's hand at the top, and up to MAX_SEATS
    player slots (fixed positions -- see SEAT_CENTERS) below, all visible simultaneously for the
    round's entire life -- unlike the old per-turn embed, every seated player's hand (and a split
    hand's two halves side by side) stays on screen throughout, with whichever hand is currently
    acting highlighted, the same "always show the whole table, highlight whoever's turn it is"
    idea uno_render.render_table already uses for its seats."""
    img = _base_table()
    draw = ImageDraw.Draw(img)

    if round_state is not None:
        hide_hole_card = round_state.phase == "playing"
        dealer_value = "?" if hide_hole_card else str(hand_value(round_state.dealer))
        bottom = _draw_hand(
            img, draw, round_state.dealer, DEALER_CENTER[0], DEALER_CENTER[1],
            card_w=DEALER_CARD_W, card_h=DEALER_CARD_H, hide_first=hide_hole_card,
        )
        _centered_text(draw, DEALER_CENTER[0], DEALER_CENTER[1] - 26, "DEALER", _dealer_label_font, GOLD)
        _centered_text(draw, DEALER_CENTER[0], bottom + 14, f"Value: {dealer_value}", _value_font, TEXT)

        active_hand = (
            round_state.hands[round_state.active_hand_index]
            if round_state.active_hand_index is not None
            and 0 <= round_state.active_hand_index < len(round_state.hands)
            else None
        )

        for i, seat in enumerate(table.seats[:MAX_SEATS]):
            cx, cy = SEAT_CENTERS[i]
            seat_hands = [h for h in round_state.hands if h.member.id == seat.member.id]
            is_active_seat = active_hand is not None and active_hand in seat_hands
            name_color = GOLD if is_active_seat else TEXT
            name_label = f"➤ {seat.member.display_name}" if is_active_seat else seat.member.display_name
            _centered_text(draw, cx, cy - 18, name_label, _name_font, name_color)

            if not seat_hands:
                continue

            hand_widths = [
                len(h.cards) * SEAT_CARD_W + (len(h.cards) - 1) * CARD_GAP for h in seat_hands
            ]
            total_w = sum(hand_widths) + HAND_GAP * (len(seat_hands) - 1)
            hand_x = cx - total_w / 2
            for h, hw in zip(seat_hands, hand_widths):
                hand_cx = hand_x + hw / 2
                bottom = _draw_hand(
                    img, draw, h.cards, hand_cx, cy,
                    card_w=SEAT_CARD_W, card_h=SEAT_CARD_H, hide_first=False,
                )
                if h is active_hand:
                    box = [hand_x - 4, cy - 4, hand_x + hw + 4, bottom + 4]
                    draw.rounded_rectangle(box, radius=8, outline=GOLD, width=3)
                label_y = bottom + 12
                if h.label:
                    _centered_text(draw, hand_cx, label_y, h.label, _bet_font, DIM_TEXT)
                    label_y += 15
                _centered_text(draw, hand_cx, label_y, f"Value: {hand_value(h.cards)}", _value_font, TEXT)
                label_y += 17
                _centered_text(draw, hand_cx, label_y, f"Bet: {h.bet}", _bet_font, DIM_TEXT)
                if h.outcome is not None:
                    label_y += 17
                    sign = "+" if h.net >= 0 else ""
                    _centered_text(
                        draw, hand_cx, label_y, f"{h.outcome.upper()} ({sign}{h.net})",
                        _result_font, _outcome_color(h.outcome),
                    )
                elif h.busted:
                    # Busted but not yet settled (settlement only happens once every hand at the
                    # table has acted) -- shown immediately rather than waiting for the round to
                    # end, so a busted hand doesn't just sit there looking live for other players.
                    label_y += 17
                    _centered_text(draw, hand_cx, label_y, "BUSTED", _result_font, LOSE_COLOR)
                hand_x += hw + HAND_GAP
    else:
        for i, seat in enumerate(table.seats[:MAX_SEATS]):
            cx, cy = SEAT_CENTERS[i]
            _centered_text(draw, cx, cy - 18, seat.member.display_name, _name_font, TEXT)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
