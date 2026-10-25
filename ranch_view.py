import asyncio
from datetime import datetime, timedelta

import discord

import db
import horse_clothes
import horserace
import hub_ui
import manhattan

MAX_RANCH_HORSES = 10  # Discord embed field values cap at 1024 chars -- same cap style as !stats
MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options
_ROSTER_FIELD_BUDGET = 1024 - 60  # Discord's field value cap, minus room for the "...and N more" line


class HorsePickerSelect(discord.ui.Select):
    """Presented by a horse command's own response when it's called with no horse number (see
    bot.py's train_cmd/horseequip_cmd) -- NOT constructed by room_view.py, deliberately: the
    Ranch room's buttons are plain zero-arg command wrappers like every other room button, and
    stay that way regardless of how the command resolves "which horse" internally. Picking an
    option here calls `on_pick` (a caller-supplied async (ctx, horse_index) callback) -- this
    class only collects the argument, it doesn't contain any command logic itself."""

    def __init__(self, owned: list[dict], on_pick, placeholder: str):
        options = [
            discord.SelectOption(label=f"{h['horse_index'] + 1}. {h['name']}"[:100], value=str(h["horse_index"]))
            for h in owned[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(placeholder=placeholder, options=options)
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.on_pick(hub_ui.InteractionContext(interaction), int(self.values[0]))


def build_horse_picker(owned: list[dict], on_pick, placeholder: str = "Choose a horse...") -> discord.ui.View:
    """A one-off View wrapping HorsePickerSelect -- what a horse command sends back when called
    with no number. Lives here (not in bot.py) because bot.py never constructs UI components
    directly, only invokes commands that view modules wrap; the command just asks for this and
    attaches it to its own response."""
    view = discord.ui.View(timeout=300)
    view.add_item(HorsePickerSelect(owned, on_pick, placeholder))
    return view


STABLE_PAGE_SIZE = 10  # keeps one page well under the 4096-char embed description cap


HIDDEN_STATS = "SPD ?? / END ?? / SPI ??"
HIDDEN_STATS_NOTE = "Stats are kept close. Send a spy for a look."


def _stable_entry(horse: dict, worn: dict[str, str], irradiated: bool, show_stats: bool = True) -> str:
    """One horse's full card in !stable -- everything a player wants before racing, training, or
    sending it to war. show_stats=False is someone else's stable without a spy's intel: the stat
    line reads HIDDEN_STATS."""
    sex_symbol = horserace.SEX_SYMBOLS.get(horse["sex"], "")
    kind = "🐣 Foal" if horse["is_foal"] else "🏆 Legend"
    record = (
        f"{horse['wins']}W-{horse['places']}P-{horse['shows']}S ({horse['races']} starts)"
        if horse["races"] else "unraced"
    )
    if horserace.is_injured(horse):
        healed_at = int(datetime.fromisoformat(horse["injured_until"]).timestamp())
        health = f"🤕 Injured, back <t:{healed_at}:R>"
    else:
        health = "❤️ Healthy"
    if horse["age"] < horserace.MIN_RACING_AGE:
        racing = f"🌱 Growing (age {horse['age']}/{horserace.MIN_RACING_AGE})"
    else:
        racing = "🏁 Race-ready"
    if horse["last_trained"]:
        ready_at = datetime.fromisoformat(horse["last_trained"]) + timedelta(hours=db.REFRESH_HOURS)
        training = "🏋️ Train ready" if ready_at <= datetime.now() else f"🏋️ Trains again <t:{int(ready_at.timestamp())}:R>"
    else:
        training = "🏋️ Train ready"
    status = [health, racing, training]
    if irradiated:
        status.append(f"☢️ Stats cap {manhattan.FALLOUT_STAT_CAP}")
    if horse["pending_boost_stat"]:
        status.append(f"🧪 {horse['pending_boost_stat']} boost queued")
    if worn:
        status.append("👒 " + ", ".join(horse_clothes.HORSE_CLOTHES[item_id]["name"] for item_id in worn.values()
                                        if item_id in horse_clothes.HORSE_CLOTHES))
    return (
        f"**{horse['horse_index'] + 1}. {horse['name']}** {sex_symbol} {horse['coat']} · {kind} · Age {horse['age']}\n"
        + (f"SPD {horse['speed']:.0f} / END {horse['endurance']:.0f} / SPI {horse['spirit']:.0f}" if show_stats else HIDDEN_STATS)
        + f" · {record}\n"
        + " · ".join(status)
    )


def build_stable_embed(
    owner_name: str, horses: list[dict], clothes: dict[int, dict[str, str]], page: int, irradiated: set[int],
    show_stats: bool = True,
) -> discord.Embed:
    pages = hub_ui.page_count(len(horses), STABLE_PAGE_SIZE)
    shown = horses[page * STABLE_PAGE_SIZE:(page + 1) * STABLE_PAGE_SIZE]
    healthy = sum(not horserace.is_injured(h) for h in horses)
    embed = discord.Embed(
        title=f"🐴 {owner_name}'s Stable",
        description="\n\n".join(
            _stable_entry(h, clothes.get(h["horse_index"], {}), h["horse_index"] in irradiated, show_stats)
            for h in shown
        )
        or "No horses yet. Try `!buyfoal` or `!buyhorse`.",
        color=discord.Color.dark_gold(),
    )
    footer = f"{len(horses)}/{horserace.MAX_HORSES_PER_PLAYER} horses · {healthy} healthy"
    if pages > 1:
        footer += f" · Page {page + 1}/{pages}"
    if not show_stats and horses:
        footer += f" · {HIDDEN_STATS_NOTE}"
    embed.set_footer(text=footer)
    return embed


async def build_stable_display(
    guild_id: int, user_id: int, owner_name: str, show_stats: bool = True,
) -> tuple[discord.Embed, discord.ui.View | None]:
    """!stable's embed (first page) plus paging buttons only when there's more than one page."""
    horses = await asyncio.to_thread(db.get_ranch_horses, guild_id, user_id)
    clothes = await asyncio.to_thread(db.get_guild_horse_clothes, guild_id)
    irradiated_flags = await asyncio.to_thread(
        db.get_flags, guild_id, db.GUILD_FLAG_USER, [db.irradiated_flag(h["horse_index"]) for h in horses],
    )
    irradiated = {h["horse_index"] for h in horses if irradiated_flags[db.irradiated_flag(h["horse_index"])]}
    pages = hub_ui.page_count(len(horses), STABLE_PAGE_SIZE)

    def build_page(page: int) -> discord.Embed:
        return build_stable_embed(owner_name, horses, clothes, page, irradiated, show_stats)

    return build_page(0), (hub_ui.PagedEmbedView(build_page, pages) if pages > 1 else None)


class FoalNameModal(discord.ui.Modal, title="Name your foal"):
    """Collects !buyfoal's free-text name -- the one argument a room button can't (rooms only
    collect zero args or one integer), so the command asks for it itself, same as the pickers here."""

    def __init__(self, on_submit_name):
        super().__init__()
        self.on_submit_name = on_submit_name
        self.name_input = discord.ui.TextInput(
            label="Name", placeholder="e.g. Lightning", max_length=horserace.MAX_HORSE_NAME_LEN,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.on_submit_name(hub_ui.InteractionContext(interaction), self.name_input.value)


class FoalNameView(discord.ui.View):
    def __init__(self, on_submit_name):
        super().__init__(timeout=300)
        self.on_submit_name = on_submit_name

    @discord.ui.button(label="🐣 Name your foal", style=discord.ButtonStyle.primary)
    async def name_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FoalNameModal(self.on_submit_name))


def build_foal_name_prompt(on_submit_name) -> discord.ui.View:
    """What a bare !buyfoal sends back. `on_submit_name` is an async (ctx, name) callback; whoever
    clicks is the buyer, so there's no owner check -- anyone can use it to buy their own foal."""
    return FoalNameView(on_submit_name)


class PlayerPickerSelect(discord.ui.UserSelect):
    """Discord's native user picker for a horse command that needs a recipient (bot.py's
    givehorse_cmd), same reasoning as dungeon_view.DuelTargetSelect -- there's no fixed short list
    of "other players" to enumerate. `on_pick` is an async (ctx, member) callback."""

    def __init__(self, on_pick, placeholder: str):
        super().__init__(placeholder=placeholder)
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.on_pick(hub_ui.InteractionContext(interaction), self.values[0])


def build_player_picker(owner_id: int, on_pick, placeholder: str) -> discord.ui.View:
    """Only owner_id can use it -- anyone else picking would run the command as themselves."""
    view = _OwnerOnlyView(owner_id)
    view.add_item(PlayerPickerSelect(on_pick, placeholder))
    return view


class _OwnerOnlyView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't yours to pick.", ephemeral=True)
            return False
        return True


async def extra_embed_fields(guild_id: int, user_id: int) -> list[tuple[str, str, bool]]:
    """room_view.py's ranch specialization hook -- the facility status + horse roster, both live
    per-player DB reads a generic room embed has no way to know about on its own. Returns
    (name, value, inline) tuples, embed.add_field'd in order by the caller."""
    currency = db.get_currency_name(guild_id)
    owned = await asyncio.to_thread(db.get_ranch_horses, guild_id, user_id)
    tier = await asyncio.to_thread(db.get_facility_tier, guild_id, user_id)
    equipped_clothes = await asyncio.to_thread(db.get_guild_horse_clothes, guild_id)

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
            if horserace.is_injured(horse):
                healed_at = int(datetime.fromisoformat(horse["injured_until"]).timestamp())
                boost_text += f" — 🤕 injured, back <t:{healed_at}:R>"
            worn = equipped_clothes.get(horse["horse_index"], {})
            clothes_text = (
                " — " + ", ".join(horse_clothes.HORSE_CLOTHES[item_id]["name"] for item_id in worn.values()
                                  if item_id in horse_clothes.HORSE_CLOTHES)
                if worn else ""
            )
            line = (
                f"{kind} **{horse['horse_index'] + 1}. {horse['name']}** {sex_symbol} {horse['coat']} — "
                f"Age {horse['age']}\nSPD {horse['speed']:.0f} / END {horse['endurance']:.0f} / "
                f"SPI {horse['spirit']:.0f} — {record}{boost_text}{clothes_text}"
            )
            # Injuries, boosts and gear make a line long enough that 10 horses can overflow
            # Discord's 1024-char field cap, which fails the whole Ranch embed -- stop early instead,
            # keeping room for the "...and N more" line.
            if len("\n\n".join(lines + [line])) > _ROSTER_FIELD_BUDGET:
                break
            lines.append(line)
        if len(owned) > len(lines):
            lines.append(f"...and {len(owned) - len(lines)} more — see `!stable`.")
        fields.append((f"Your Horses ({len(owned)})", "\n\n".join(lines), False))
        footer = (
            f"Train/Boost ask which horse when clicked. Boost items cost {horserace.ITEM_COST} {currency} each. "
            f"Dress a horse up with `!horseequip`."
        )
    else:
        fields.append(("Your Horses", "None yet — try `!buyhorse` or `!buyfoal`.", False))
        footer = "Buy a horse to unlock Train/Boost — Upgrade Facility is still available."
    fields.append(("​", footer, False))
    return fields
