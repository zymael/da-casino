import asyncio

import discord

import db
import horserace
import hub_ui

MAX_RANCH_HORSES = 10  # Discord embed field values cap at 1024 chars -- same cap style as !stats
MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options


class TrainHorseSelect(discord.ui.Select):
    """Presented by !train's own response when it's called with no horse number (see bot.py's
    train_cmd) -- NOT constructed by room_view.py, deliberately: the Ranch room's Train button is
    a plain zero-arg command wrapper like every other room button, and stays that way regardless
    of how the command resolves "which horse" internally. Picking an option here calls
    `train_horse` (bot.py's shared _train_horse helper, passed in by whoever builds this) for that
    horse -- this class only collects the argument, it doesn't contain any training logic itself."""

    def __init__(self, owned: list[dict], train_horse):
        options = [
            discord.SelectOption(label=f"{h['horse_index'] + 1}. {h['name']}"[:100], value=str(h["horse_index"]))
            for h in owned[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(placeholder="Choose a horse to train...", options=options)
        self.train_horse = train_horse

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.train_horse(hub_ui.InteractionContext(interaction), int(self.values[0]))


def build_train_horse_picker(owned: list[dict], train_horse) -> discord.ui.View:
    """A one-off View wrapping TrainHorseSelect -- what !train sends back when called with no
    number. Lives here (not in bot.py) because bot.py never constructs UI components directly,
    only invokes commands that view modules wrap; train_cmd just asks for this and attaches it to
    its own response."""
    view = discord.ui.View(timeout=300)
    view.add_item(TrainHorseSelect(owned, train_horse))
    return view


async def extra_embed_fields(guild_id: int, user_id: int) -> list[tuple[str, str, bool]]:
    """room_view.py's ranch specialization hook -- the facility status + horse roster, both live
    per-player DB reads a generic room embed has no way to know about on its own. Returns
    (name, value, inline) tuples, embed.add_field'd in order by the caller."""
    currency = db.get_currency_name(guild_id)
    owned = await asyncio.to_thread(db.get_ranch_horses, guild_id, user_id)
    tier = await asyncio.to_thread(db.get_facility_tier, guild_id, user_id)

    fields = []
    if tier > 0:
        facility = horserace.FACILITY_TIERS[tier - 1]
        facility_text = f"**{facility['name']}** (Tier {tier}) — +{int(facility['bonus'] * 100)}% training gains"
    else:
        facility_text = "None yet"
    fields.append(("🏗️ Facility", facility_text, False))

    if owned:
        lines = []
        for horse in owned[:MAX_RANCH_HORSES]:
            sex_symbol = horserace.SEX_SYMBOLS.get(horse["sex"], "")
            kind = "🐣" if horse["is_foal"] else "🏆"
            record = (
                f"{horse['wins']}W-{horse['places']}P-{horse['shows']}S ({horse['races']} starts)"
                if horse["races"] else "unraced"
            )
            boost_text = f" — 🧪 {horse['pending_boost_stat']} boost queued" if horse["pending_boost_stat"] else ""
            lines.append(
                f"{kind} **{horse['horse_index'] + 1}. {horse['name']}** {sex_symbol} {horse['coat']} — "
                f"Age {horse['age']}\nSPD {horse['speed']:.0f} / END {horse['endurance']:.0f} / "
                f"SPI {horse['spirit']:.0f} — {record}{boost_text}"
            )
        if len(owned) > MAX_RANCH_HORSES:
            lines.append(f"...and {len(owned) - MAX_RANCH_HORSES} more — see `!horses`.")
        fields.append((f"Your Horses ({len(owned)})", "\n\n".join(lines), False))
        footer = f"Train/Boost ask which horse when clicked. Boost items cost {horserace.ITEM_COST} {currency} each."
    else:
        fields.append(("Your Horses", "None yet — try `!buyhorse` or `!buyfoal`.", False))
        footer = "Buy a horse to unlock Train/Boost — Upgrade Facility is still available."
    fields.append(("​", footer, False))
    return fields
