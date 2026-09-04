"""!inventory (read-only) and !equipment (manage gear) -- and the ephemeral popups the hub
shortcut buttons open for the same two views, via hub_ui.InventoryButton/EquipmentButton.

Covers quest items, crafting materials, consumables, horse clothes, and housing items (all five
sharing the same generic `inventory` table -- see _inventory_sections below for how one row is
told from another) and dungeon gear (dungeon.EQUIPMENT, currently equipped or sitting in
equipment_inventory -- see db.equip_item_smart for why non-upgrade drops/rewards land there
instead of being discarded).
"""

import asyncio

import discord

import db
import dungeon
import horse_clothes
import housing
import quests

MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options
EMBED_FIELD_LIMIT = 1024  # Discord's hard cap on one embed field's value length
EMBED_TOTAL_LIMIT = 6000  # Discord's hard cap on the summed length of every embed in one message


def _chunked_field_pairs(name: str, lines: list[str], empty_text: str) -> list[tuple[str, str]]:
    """Splits `lines` (each a pre-built multi-line block, e.g. _material_line's output) into as
    many (field_name, value) pairs as needed to keep every value under EMBED_FIELD_LIMIT -- Discord
    caps one field's value at that, which a long enough item list blows past (this is exactly what
    broke !equipment's "Stored" field for a player with enough gear: HTTPException 400, "Must be
    1024 or fewer in length"). Packs lines greedily into as few fields as fit, naming every field
    after the first "<name> (cont.)", "<name> (cont. 2)", etc. Returns a single (name, empty_text)
    pair when `lines` is empty -- the "None yet." style message every call site already had.
    Returned as pairs rather than added straight to an embed so _pack_into_embeds can decide which
    *message* each field ends up in -- see its docstring for why that split exists."""
    if not lines:
        return [(name, empty_text)]
    chunks: list[str] = []
    current = ""
    for line in lines:
        # Defensive: a single line longer than the whole field limit gets truncated on its own --
        # never actually expected from authored content, just keeps this robust either way.
        if len(line) > EMBED_FIELD_LIMIT:
            line = line[: EMBED_FIELD_LIMIT - 1] + "…"
        candidate = f"{current}\n\n{line}" if current else line
        if len(candidate) > EMBED_FIELD_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [
        (name if i == 0 else (f"{name} (cont.)" if i == 1 else f"{name} (cont. {i})"), chunk)
        for i, chunk in enumerate(chunks)
    ]


def _pack_into_embeds(
    title: str, color: discord.Color, footer_text: str | None, field_pairs: list[tuple[str, str]],
    *, description: str | None = None,
) -> list[discord.Embed]:
    """Distributes `field_pairs` (each already under the per-field EMBED_FIELD_LIMIT -- see
    _chunked_field_pairs) across as many embeds as needed to respect EMBED_TOTAL_LIMIT, Discord's
    cap on the *summed* length of every embed in one message. That cap is separate from -- and not
    fixed by -- the per-field chunking above: a player with enough gear/materials/etc. across all
    of !inventory's sections can have every individual field comfortably under 1024 chars while the
    sections still add up past 6000 total, which is exactly what started silently breaking
    !inventory once a content update grew the average loadout (HTTPException 400, "Embed size
    exceeds maximum size of 6000"). Each embed this returns is meant to be sent as its own message
    by the caller (a second embed can't just be appended to the same message -- Discord's 6000 cap
    applies across all embeds *in one message*, not per embed). Only the first carries the
    title/color/footer; follow-ons are titled "<title> (cont.)" so a multi-message reply still
    reads as one continuous thing."""
    embeds = [discord.Embed(title=title, color=color, description=description or None)]
    if footer_text:
        embeds[0].set_footer(text=footer_text)
    for name, value in field_pairs:
        embeds[-1].add_field(name=name, value=value, inline=False)
        if len(embeds[-1]) > EMBED_TOTAL_LIMIT:
            embeds[-1].remove_field(len(embeds[-1].fields) - 1)
            embeds.append(discord.Embed(title=f"{title} (cont.)", color=color))
            embeds[-1].add_field(name=name, value=value, inline=False)
    return embeds


def stat_bonus_text(item: dict) -> str:
    """e.g. 'ATK +3' or 'HP +2 / DEF +1' -- the compact form used everywhere an item's constant
    power needs to be legible at a glance (embed lines and Select option descriptions alike).
    Constant-only on purpose: on_use/on_hit effects go through dynamic_effect_lines instead,
    never here -- this text also feeds a Select option's char-capped description (see
    _equipment_summary), which a proc's description could overflow."""
    order = ("hp", "atk", "def", "spatk")
    bonuses = dungeon.constant_stat_bonuses(item)
    parts = [f"{dungeon.stat_label(stat)} +{bonuses[stat]}" for stat in order if bonuses.get(stat)]
    return " / ".join(parts)


def _equipment_summary(item: dict) -> str:
    """'ATK +3 · rare' -- used in Select option descriptions, where space is tight. An item with
    no constant effects at all (pure on_use/on_hit) just omits the stat segment rather than
    leaving a dangling '· rare' separator."""
    stat_text = stat_bonus_text(item)
    return f"{stat_text} · {item['rarity']}" if stat_text else item["rarity"]


def _effect_phrase(effect: dict) -> str:
    """A short player-facing description of one on_use/on_hit effect -- e.g. '1.5x damage' or
    'enemy DEF −3' -- used by dynamic_effect_lines. Deliberately terser than
    admin_schemas.EFFECT_TYPE_HINTS, which is authoring-oriented text for someone filling in a
    form, not a player reading their own gear."""
    t, v = effect["type"], effect.get("value")
    if t == "damage_multiplier":
        return f"{v}x damage"
    if t == "heal_fraction":
        return f"heal {v * 100:.0f}% HP"
    if t == "guard":
        return f"block {effect['reduction'] * 100:.0f}% of the next hit"
    if t == "lifesteal_fraction":
        return f"drain {v * 100:.0f}% of the damage as HP"
    if t == "def_shred":
        return f"enemy DEF −{v}"
    if t == "extra_attack":
        return f"bonus attack (x{effect.get('multiplier', 1.0)})"
    if t == "atk_buff":
        return f"ATK +{v}"
    if t == "def_buff":
        return f"DEF +{v}"
    if t == "spatk_buff":
        return f"{dungeon.stat_label('spatk')} +{v}"
    if t == "hp_buff":
        return f"Max HP +{v}"
    if t == "chip_gain":
        return f"Chips +{v}"
    if t == "atk_debuff":
        return f"enemy ATK −{v}"
    if t == "spatk_debuff":
        return f"enemy {dungeon.stat_label('spatk')} −{v}"
    if t == "dodge_buff":
        return f"Dodge +{v * 100:.0f}% for {effect['duration']} round(s)"
    if t == "resist_buff":
        return f"Resist +{v * 100:.0f}% for {effect['duration']} round(s)"
    if t == "dot":
        return f"{v} dmg/round for {effect['duration']} round(s)"
    if t == "hot":
        return f"heal {v * 100:.0f}% HP/round for {effect['duration']} round(s)"
    return t


def dynamic_effect_lines(item: dict) -> list[str]:
    """One line per on_use/on_hit effect on `item` -- constant effects are already covered by
    stat_bonus_text, this is what makes the other two trigger kinds visible to a player at all."""
    lines = []
    for effect in item["effects"]:
        if effect["trigger"] == "on_use":
            lines.append(f"✨ On Use: {_effect_phrase(effect)}")
        elif effect["trigger"] == "on_hit":
            pct = round(effect["chance"] * 100)
            lines.append(f"⚡ {pct}% on hit: {_effect_phrase(effect)}")
    return lines


def _equipment_line(item_id: str, qty: int | None = None) -> str:
    """A full detail line for an embed field: name, stats, rarity, qty, dynamic effects, and
    flavor text on its own line beneath -- used for both the Equipped and Stored sections."""
    item = dungeon.EQUIPMENT[item_id]
    qty_suffix = f" x{qty}" if qty and qty > 1 else ""
    stat_text = stat_bonus_text(item)
    stat_suffix = f" — {stat_text}" if stat_text else ""
    rarity_dot = dungeon.RARITY_EMOJI[item["rarity"]]
    lines = [f"{rarity_dot} **{item['name']}**{qty_suffix}{stat_suffix} · *{item['rarity']}*"]
    lines.extend(dynamic_effect_lines(item))
    lines.append(f"> {item['flavor']}")
    return "\n".join(lines)


def _quest_item_line(item_id: str, qty: int) -> str:
    item = quests.QUEST_ITEMS[item_id]
    qty_suffix = f" x{qty}" if qty > 1 else ""
    return f"{item['emoji']} **{item['name']}**{qty_suffix}\n> {item['description']}"


def _material_line(item_id: str, qty: int) -> str:
    item = dungeon.MATERIALS[item_id]
    qty_suffix = f" x{qty}" if qty > 1 else ""
    return f"⛏️ **{item['name']}**{qty_suffix} · *{item['rarity']}*\n> {item['flavor']}"


def _consumable_line(item_id: str, qty: int) -> str:
    item = dungeon.CONSUMABLES[item_id]
    qty_suffix = f" x{qty}" if qty > 1 else ""
    return f"🧪 **{item['name']}**{qty_suffix}\n> {item['flavor']}"


def _stored_excluding_equipped(equipped: dict[str, str], stored: dict[str, int]) -> dict[str, int]:
    """db.get_equipment_inventory's raw {item_id: qty} can end up ALSO containing whatever's
    currently equipped -- db.store_equipment_item (a monster drop that ties the equipped item's
    own power rather than beating it, or an admin "Give Item" grant of something already worn)
    doesn't know equip_item_smart's own invariant that the equipped copy is never also counted in
    storage. Filtered out wherever stored items are shown or offered, regardless of how the
    overlap arose -- letting the same item_id appear as both an "equipped" option AND a "stored"
    option in EquipmentSlotSelect is what Discord rejects as a duplicate option value (400 Invalid
    Form Body), and even where it wouldn't crash (the plain Stored Equipment listing), showing an
    item as both equipped and stored is just confusing."""
    equipped_ids = set(equipped.values())
    return {item_id: qty for item_id, qty in stored.items() if item_id not in equipped_ids}


def _housing_item_line(item_id: str, qty: int) -> str:
    item = housing.HOUSING_ITEMS[item_id]
    qty_suffix = f" x{qty}" if qty > 1 else ""
    return f"{item['emoji']} **{item['name']}**{qty_suffix}\n> {item['description']}"


def _horse_clothes_line(item_id: str, qty: int) -> str:
    item = horse_clothes.HORSE_CLOTHES[item_id]
    qty_suffix = f" x{qty}" if qty > 1 else ""
    return f"👒 **{item['name']}**{qty_suffix} · *{item['slot']}*\n> {item['flavor']}"


def _inventory_sections(
    held: dict[str, int]
) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
    """The generic `inventory` table holds several kinds of item (quest items, crafting
    materials, consumables, horse clothes, housing items) told apart only by which content
    registry recognizes the id -- there's no type column. Splits `held` into (quest_items,
    materials, consumables, horse_clothes, housing_items); an id recognized by none of them is a
    genuine content bug (e.g. two JSON files picking the same id, or a kind this function hasn't
    been taught about yet) and is reported loudly rather than silently dropped, since that's
    exactly the class of bug this split exists to catch."""
    quest_item_ids = {item_id: qty for item_id, qty in held.items() if item_id in quests.QUEST_ITEMS}
    material_ids = {item_id: qty for item_id, qty in held.items() if item_id in dungeon.MATERIALS}
    consumable_ids = {item_id: qty for item_id, qty in held.items() if item_id in dungeon.CONSUMABLES}
    horse_clothes_ids = {item_id: qty for item_id, qty in held.items() if item_id in horse_clothes.HORSE_CLOTHES}
    housing_item_ids = {item_id: qty for item_id, qty in held.items() if item_id in housing.HOUSING_ITEMS}
    unrecognized = (
        held.keys() - quest_item_ids.keys() - material_ids.keys() - consumable_ids.keys()
        - horse_clothes_ids.keys() - housing_item_ids.keys()
    )
    if unrecognized:
        raise ValueError(f"inventory has unrecognized item id(s): {sorted(unrecognized)}")
    return quest_item_ids, material_ids, consumable_ids, horse_clothes_ids, housing_item_ids


async def build_inventory_display(guild_id: int, user_id: int) -> tuple[list[discord.Embed], "InventoryView"]:
    """Returns (embeds, view) -- embeds is almost always length 1, but see _pack_into_embeds:
    a large enough inventory needs more than one *message* to stay under Discord's total embed
    size cap. Every caller must send embeds[0] (with `view` attached) and then, if there's more,
    send embeds[1:] as follow-up messages of their own -- see inventory_cmd/InventoryButton for
    the shape."""
    held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
    equipped = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
    stored = _stored_excluding_equipped(equipped, await asyncio.to_thread(db.get_equipment_inventory, guild_id, user_id))
    quest_items, materials, consumables, horse_clothes_held, housing_items_held = _inventory_sections(held)

    if any(dungeon.usable_outside_combat(dungeon.CONSUMABLES[item_id]) for item_id in consumables):
        footer_text = "Manage your gear with !equipment, craft more with !craft. Use energy/healing items below."
    else:
        footer_text = "Manage your gear with !equipment, craft more with !craft."

    field_pairs = [
        *_chunked_field_pairs("Quest Items", [_quest_item_line(item_id, qty) for item_id, qty in quest_items.items()], "None yet."),
        *_chunked_field_pairs("Materials", [_material_line(item_id, qty) for item_id, qty in materials.items()], "None yet."),
        *_chunked_field_pairs("Consumables", [_consumable_line(item_id, qty) for item_id, qty in consumables.items()], "None yet."),
        *_chunked_field_pairs("Horse Clothes", [_horse_clothes_line(item_id, qty) for item_id, qty in horse_clothes_held.items()], "None yet."),
        *_chunked_field_pairs("Housing Items", [_housing_item_line(item_id, qty) for item_id, qty in housing_items_held.items()], "None yet -- see !house."),
        ("Equipped", _equipped_lines(equipped)),
        *_chunked_field_pairs("Stored Equipment", [_equipment_line(item_id, qty) for item_id, qty in stored.items()], "None yet."),
    ]
    embeds = _pack_into_embeds("🎒 Inventory", discord.Color.blurple(), footer_text, field_pairs)

    view = InventoryView(guild_id, user_id, consumables, housing_items_held)
    return embeds, view


class InventoryView(discord.ui.View):
    """One button per currently-owned consumable usable outside combat (dungeon.
    usable_outside_combat), plus one per currently-owned housing item with a use_label set (see
    housing.py's "usable by field presence" comment) -- energy-restoring items, heal_fraction
    potions, and flavor-only "use" items like the Turrón alike. Only ever built by
    build_inventory_display, right alongside the embed it's attached to."""

    def __init__(self, guild_id: int, user_id: int, consumables: dict[str, int], housing_items_held: dict[str, int]):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        row = 0
        in_row = 0

        def _add(button: discord.ui.Button):
            nonlocal row, in_row
            self.add_item(button)
            in_row += 1
            if in_row >= 5:  # Discord's own per-row button cap
                in_row = 0
                row += 1

        for item_id in consumables:
            item = dungeon.CONSUMABLES[item_id]
            if not dungeon.usable_outside_combat(item):
                continue
            _add(UseConsumableButton(item_id, item, row))
        for item_id in housing_items_held:
            item = housing.HOUSING_ITEMS[item_id]
            if not item.get("use_label"):
                continue
            _add(UseHousingItemButton(item_id, item, row))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your inventory.", ephemeral=True)
            return False
        return True


class UseConsumableButton(discord.ui.Button):
    """Uses one item_id outside combat -- db.use_energy_item for an energy_restore item,
    db.use_healing_item for a heal_fraction one (dungeon.usable_outside_combat already guarantees
    an owned, qualifying item is exactly one or the other). The button's verb is the item's own
    optional use_label ("Snort" for the bag of powder), defaulting to "Drink" -- purely cosmetic,
    the underlying db calls are the same regardless of wording. Same "*_view.py button calls db.*
    directly, then rebuilds+edits the display" shape EquipmentSlotSelect's own callback already
    uses."""

    def __init__(self, item_id: str, item: dict, row: int):
        self.item_id = item_id
        self.is_energy = bool(item.get("energy_restore"))
        emoji = "⚡" if self.is_energy else "❤️"
        verb = item.get("use_label", "Drink")
        super().__init__(label=f"{emoji} {verb} {item['name']}"[:80], style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        item = dungeon.CONSUMABLES[self.item_id]

        if self.is_energy:
            status, value = await asyncio.to_thread(
                db.use_energy_item, guild_id, user_id, self.item_id, item["energy_restore"]
            )
        else:
            heal_fraction = next(e["value"] for e in item["effects"] if e["type"] == "heal_fraction")
            status, value = await asyncio.to_thread(
                db.use_healing_item, guild_id, user_id, self.item_id, heal_fraction
            )

        if status == "cooldown":
            hours, minutes = int(value // 3600), int((value % 3600) // 60)
            await interaction.response.send_message(
                f"You've already used an energy item today — try again in {hours}h {minutes}m.",
                ephemeral=True,
            )
            return
        if status == "no_character":
            await interaction.response.send_message("You don't have a dungeon character to heal yet.", ephemeral=True)
            return
        if status == "full":
            await interaction.response.send_message("You're already at full HP.", ephemeral=True)
            return
        if status == "no_item":
            await interaction.response.send_message("You don't have one of those anymore.", ephemeral=True)
            return

        if self.is_energy:
            result_text = f"⚡ Drank **{item['name']}** — energy is now **{value}**/{db.ENERGY_CAP}."
        else:
            result_text = f"❤️ Drank **{item['name']}** — HP is now **{value}**."
        embeds, view = await build_inventory_display(guild_id, user_id)
        await interaction.response.edit_message(embed=embeds[0], view=view)
        is_ephemeral = bool(interaction.message and interaction.message.flags.ephemeral)
        for extra in embeds[1:]:
            await interaction.followup.send(embed=extra, ephemeral=is_ephemeral)
        await interaction.followup.send(result_text, ephemeral=True)


class UseHousingItemButton(discord.ui.Button):
    """Shown for any owned housing item with a use_label set (see housing.py's "usable by field
    presence" comment) -- just sends its use_message ephemerally, no game-state change. Doesn't
    edit/rebuild the inventory display afterward the way UseConsumableButton does, since nothing
    about the item or its owner's state actually changed."""

    def __init__(self, item_id: str, item: dict, row: int):
        self.use_message = item["use_message"]
        super().__init__(
            label=f"{item['emoji']} {item['use_label']} {item['name']}"[:80], style=discord.ButtonStyle.secondary, row=row
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(self.use_message, ephemeral=True)


def _equipped_lines(equipped: dict[str, str]) -> str:
    lines = []
    for slot in dungeon.EQUIPMENT_SLOTS:
        item_id = equipped.get(slot)
        if item_id is None:
            lines.append(f"**{slot.title()}**: *empty*")
            continue
        item = dungeon.EQUIPMENT[item_id]
        rarity_dot = dungeon.RARITY_EMOJI[item["rarity"]]
        entry_lines = [
            f"**{slot.title()}**: {rarity_dot} {item['name']} — {stat_bonus_text(item)} · *{item['rarity']}*"
        ]
        entry_lines.extend(dynamic_effect_lines(item))
        entry_lines.append(f"> {item['flavor']}")
        lines.append("\n".join(entry_lines))
    return "\n".join(lines)


async def build_equipment_display(guild_id: int, user_id: int) -> tuple[list[discord.Embed], "EquipmentView"]:
    """Returns (embeds, view) -- see build_inventory_display's docstring, same shape and same
    reason: a big enough Stored list needs more than one message to stay under Discord's total
    embed size cap."""
    equipped = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
    stored = _stored_excluding_equipped(equipped, await asyncio.to_thread(db.get_equipment_inventory, guild_id, user_id))

    field_pairs = [
        ("Equipped", _equipped_lines(equipped)),
        *_chunked_field_pairs(
            "Stored", [_equipment_line(item_id, qty) for item_id, qty in stored.items()],
            "None yet — extra gear you find gets stored here instead of discarded.",
        ),
    ]
    embeds = _pack_into_embeds(
        "⚔️ Equipment", discord.Color.blurple(), None, field_pairs,
        description="Pick a replacement (or unequip) per slot below.",
    )

    view = EquipmentView(guild_id, user_id, equipped, stored)
    return embeds, view


class EquipmentView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, equipped: dict[str, str], stored: dict[str, int]):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        for row, slot in enumerate(dungeon.EQUIPMENT_SLOTS):
            equipped_id = equipped.get(slot)
            stored_ids = [item_id for item_id in stored if dungeon.EQUIPMENT[item_id]["slot"] == slot]
            if equipped_id is None and not stored_ids:
                continue  # nothing to manage for this slot -- no Select for it
            self.add_item(EquipmentSlotSelect(slot, equipped_id, stored_ids, row))


class EquipmentSlotSelect(discord.ui.Select):
    _UNEQUIP_VALUE = "__unequip__"

    def __init__(self, slot: str, equipped_id: str | None, stored_ids: list[str], row: int):
        self.slot = slot
        options = [discord.SelectOption(label="— Unequip —", value=self._UNEQUIP_VALUE, default=equipped_id is None)]
        if equipped_id is not None:
            item = dungeon.EQUIPMENT[equipped_id]
            options.append(discord.SelectOption(
                label=f"{item['name']} (equipped)", value=equipped_id, default=True,
                description=_equipment_summary(item), emoji=dungeon.RARITY_EMOJI[item["rarity"]],
            ))
        for item_id in stored_ids[: MAX_SELECT_OPTIONS - len(options)]:
            item = dungeon.EQUIPMENT[item_id]
            options.append(discord.SelectOption(
                label=item["name"], value=item_id, description=_equipment_summary(item),
                emoji=dungeon.RARITY_EMOJI[item["rarity"]],
            ))
        super().__init__(placeholder=f"{slot.title()}...", options=options, row=row)

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        value = self.values[0]
        if value == self._UNEQUIP_VALUE:
            await asyncio.to_thread(db.unequip_item, guild_id, user_id, self.slot)
        else:
            await asyncio.to_thread(db.equip_item_smart, guild_id, user_id, self.slot, value)
        embeds, view = await build_equipment_display(guild_id, user_id)
        await interaction.response.edit_message(embed=embeds[0], view=view)
        is_ephemeral = bool(interaction.message and interaction.message.flags.ephemeral)
        for extra in embeds[1:]:
            await interaction.followup.send(embed=extra, ephemeral=is_ephemeral)
