"""Housing: the 3x3 grid embed + the slot-picker -> item-picker chain a bare `!house` invocation
sends back (see bot.py's house_cmd/_place_house_item/_remove_house_item). Follows ranch_view.py's
build_horse_picker shape exactly -- per CLAUDE.md, a room's House button stays a plain zero-arg
command wrapper regardless of this; all picker UI here is built by the command itself (bot.py),
never by room_view.py. Unlike a horse (a single typed number), placing an item needs *two*
arguments -- which slot, which item -- so there's no single-typed-int fast path; bare `!house`
always opens this same picker chain.
"""
import discord

import housing
import hub_ui

MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options
GRID_SIZE = 9
REMOVE_VALUE = "__remove__"
IMAGE_FILENAME = "house.png"


def _bonus_lines(bonuses: dict) -> list[str]:
    lines = []
    for effect_type, value in bonuses.items():
        if effect_type == "stat_bonus":
            for stat, stat_value in value.items():
                lines.append(f"+{stat_value} {stat}")
            continue
        unit = "%" if housing.HOUSING_EFFECT_TYPES[effect_type]["value_kind"] == "percent" else ""
        lines.append(f"+{value}{unit} {effect_type.replace('_', ' ')}")
    return lines


def build_house_embed(display_name: str, placements: dict[int, str], bonuses: dict) -> discord.Embed:
    """Renders the text half of !house's response -- a field per filled slot and a summary of the
    aggregate passive bonuses currently active. The grid itself is the composited image bot.py
    attaches separately (housing_render.render_house) and sets via embed.set_image(url=
    f"attachment://{IMAGE_FILENAME}"); this function doesn't touch the image at all, so it stays a
    plain data-in-data-out builder like build_slot_picker/build_item_picker below (placements and
    bonuses are pre-fetched by the caller, not queried here)."""
    embed = discord.Embed(title=f"🏠 {display_name}'s House", color=discord.Color.gold())
    for slot in range(GRID_SIZE):
        item = housing.HOUSING_ITEMS.get(placements.get(slot))
        if item:
            embed.add_field(name=f"Slot {slot + 1}", value=f"{item['emoji']} {item['name']}", inline=True)

    bonus_lines = _bonus_lines(bonuses)
    embed.add_field(
        name="Active Bonuses",
        value="\n".join(bonus_lines) if bonus_lines else "None yet -- place an item!",
        inline=False,
    )
    return embed


class HouseSlotSelect(discord.ui.Select):
    """First step of the placement flow: pick which of the 9 grid slots to edit. Selecting one
    calls `on_pick` (an async (ctx, slot) callback) -- this class only collects the argument, it
    doesn't contain any command logic itself, same split as ranch_view.HorsePickerSelect."""

    def __init__(self, placements: dict[int, str], on_pick):
        options = []
        for slot in range(GRID_SIZE):
            item = housing.HOUSING_ITEMS.get(placements.get(slot))
            label = f"Slot {slot + 1}: {item['name']}" if item else f"Slot {slot + 1}: (empty)"
            options.append(discord.SelectOption(label=label[:100], value=str(slot)))
        super().__init__(placeholder="Choose a slot...", options=options)
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.on_pick(hub_ui.InteractionContext(interaction), int(self.values[0]))


def build_slot_picker(placements: dict[int, str], on_pick) -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    view.add_item(HouseSlotSelect(placements, on_pick))
    return view


class HouseItemSelect(discord.ui.Select):
    """Second step: pick which owned housing item to place into the already-chosen slot, or (if
    the slot's occupied) remove what's there instead. Selecting the remove option calls
    `on_remove`; selecting an item calls `on_place` (an async (ctx, item_id) callback) -- again,
    this class only collects the argument."""

    def __init__(self, owned_item_ids: list[str], occupant_item_id: str | None, on_place, on_remove):
        options = []
        occupant = housing.HOUSING_ITEMS.get(occupant_item_id)
        if occupant:
            options.append(discord.SelectOption(label=f"✖ Remove {occupant['name']}"[:100], value=REMOVE_VALUE))
        for item_id in owned_item_ids[:MAX_SELECT_OPTIONS]:
            item = housing.HOUSING_ITEMS.get(item_id)
            if item:
                options.append(discord.SelectOption(label=f"{item['emoji']} {item['name']}"[:100], value=item_id))
        super().__init__(placeholder="Choose an item...", options=options)
        self.on_place = on_place
        self.on_remove = on_remove

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ctx = hub_ui.InteractionContext(interaction)
        if self.values[0] == REMOVE_VALUE:
            await self.on_remove(ctx)
        else:
            await self.on_place(ctx, self.values[0])


def build_item_picker(
    owned_item_ids: list[str], occupant_item_id: str | None, on_place, on_remove
) -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    view.add_item(HouseItemSelect(owned_item_ids, occupant_item_id, on_place, on_remove))
    return view
