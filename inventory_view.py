"""!inventory (read-only) and !equipment (manage gear) -- and the ephemeral popups the hub
shortcut buttons open for the same two views, via hub_ui.InventoryButton/EquipmentButton.

Covers quest items and crafting materials (both quests.py's/dungeon.py's rows in the same
generic `inventory` table -- see _inventory_sections below for how one row is told from the
other) and dungeon gear (dungeon.EQUIPMENT, currently equipped or sitting in equipment_inventory
-- see db.equip_item_smart for why non-upgrade drops/rewards land there instead of being
discarded).
"""

import asyncio

import discord

import db
import dungeon
import horse_clothes
import quests

MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options


def _stat_bonus_text(item: dict) -> str:
    """e.g. 'ATK +3' or 'HP +2 / DEF +1' -- the compact form used everywhere an item's constant
    power needs to be legible at a glance (embed lines and Select option descriptions alike).
    Constant-only on purpose: on_use/on_hit effects go through _dynamic_effect_lines instead,
    never here -- this text also feeds a Select option's char-capped description (see
    _equipment_summary), which a proc's description could overflow."""
    order = ("hp", "atk", "def", "spatk", "spdef")
    bonuses = dungeon.constant_stat_bonuses(item)
    parts = [f"{stat.upper()} +{bonuses[stat]}" for stat in order if bonuses.get(stat)]
    return " / ".join(parts)


def _equipment_summary(item: dict) -> str:
    """'ATK +3 · rare' -- used in Select option descriptions, where space is tight. An item with
    no constant effects at all (pure on_use/on_hit) just omits the stat segment rather than
    leaving a dangling '· rare' separator."""
    stat_text = _stat_bonus_text(item)
    return f"{stat_text} · {item['rarity']}" if stat_text else item["rarity"]


def _effect_phrase(effect: dict) -> str:
    """A short player-facing description of one on_use/on_hit effect -- e.g. '1.5x damage' or
    'enemy DEF −3' -- used by _dynamic_effect_lines. Deliberately terser than
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
        return f"SpAtk +{v}"
    if t == "spdef_buff":
        return f"SpDef +{v}"
    if t == "hp_buff":
        return f"Max HP +{v}"
    if t == "atk_debuff":
        return f"enemy ATK −{v}"
    if t == "spatk_debuff":
        return f"enemy SpAtk −{v}"
    if t == "spdef_debuff":
        return f"enemy SpDef −{v}"
    if t == "dodge_buff":
        return f"Dodge +{v * 100:.0f}% for {effect['duration']} round(s)"
    if t == "resist_buff":
        return f"Resist +{v * 100:.0f}% for {effect['duration']} round(s)"
    if t == "dot":
        return f"{v} dmg/round for {effect['duration']} round(s)"
    if t == "hot":
        return f"heal {v * 100:.0f}% HP/round for {effect['duration']} round(s)"
    return t


def _dynamic_effect_lines(item: dict) -> list[str]:
    """One line per on_use/on_hit effect on `item` -- constant effects are already covered by
    _stat_bonus_text, this is what makes the other two trigger kinds visible to a player at all."""
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
    stat_text = _stat_bonus_text(item)
    stat_suffix = f" — {stat_text}" if stat_text else ""
    lines = [f"**{item['name']}**{qty_suffix}{stat_suffix} · *{item['rarity']}*"]
    lines.extend(_dynamic_effect_lines(item))
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


def _horse_clothes_line(item_id: str, qty: int) -> str:
    item = horse_clothes.HORSE_CLOTHES[item_id]
    qty_suffix = f" x{qty}" if qty > 1 else ""
    return f"👒 **{item['name']}**{qty_suffix} · *{item['slot']}*\n> {item['flavor']}"


def _inventory_sections(
    held: dict[str, int]
) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
    """The generic `inventory` table holds several kinds of item (quest items, crafting
    materials, consumables, horse clothes) told apart only by which content registry recognizes
    the id -- there's no type column. Splits `held` into (quest_items, materials, consumables,
    horse_clothes); an id recognized by none of them is a genuine content bug (e.g. two JSON
    files picking the same id, or a kind this function hasn't been taught about yet) and is
    reported loudly rather than silently dropped, since that's exactly the class of bug this
    split exists to catch."""
    quest_item_ids = {item_id: qty for item_id, qty in held.items() if item_id in quests.QUEST_ITEMS}
    material_ids = {item_id: qty for item_id, qty in held.items() if item_id in dungeon.MATERIALS}
    consumable_ids = {item_id: qty for item_id, qty in held.items() if item_id in dungeon.CONSUMABLES}
    horse_clothes_ids = {item_id: qty for item_id, qty in held.items() if item_id in horse_clothes.HORSE_CLOTHES}
    unrecognized = (
        held.keys() - quest_item_ids.keys() - material_ids.keys() - consumable_ids.keys() - horse_clothes_ids.keys()
    )
    if unrecognized:
        raise ValueError(f"inventory has unrecognized item id(s): {sorted(unrecognized)}")
    return quest_item_ids, material_ids, consumable_ids, horse_clothes_ids


async def build_inventory_embed(guild_id: int, user_id: int) -> discord.Embed:
    held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
    equipped = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
    stored = _stored_excluding_equipped(equipped, await asyncio.to_thread(db.get_equipment_inventory, guild_id, user_id))
    quest_items, materials, consumables, horse_clothes_held = _inventory_sections(held)

    embed = discord.Embed(title="🎒 Inventory", color=discord.Color.blurple())

    if quest_items:
        lines = [_quest_item_line(item_id, qty) for item_id, qty in quest_items.items()]
        embed.add_field(name="Quest Items", value="\n\n".join(lines), inline=False)
    else:
        embed.add_field(name="Quest Items", value="None yet.", inline=False)

    if materials:
        lines = [_material_line(item_id, qty) for item_id, qty in materials.items()]
        embed.add_field(name="Materials", value="\n\n".join(lines), inline=False)
    else:
        embed.add_field(name="Materials", value="None yet.", inline=False)

    if consumables:
        lines = [_consumable_line(item_id, qty) for item_id, qty in consumables.items()]
        embed.add_field(name="Consumables", value="\n\n".join(lines), inline=False)
    else:
        embed.add_field(name="Consumables", value="None yet.", inline=False)

    if horse_clothes_held:
        lines = [_horse_clothes_line(item_id, qty) for item_id, qty in horse_clothes_held.items()]
        embed.add_field(name="Horse Clothes", value="\n\n".join(lines), inline=False)
    else:
        embed.add_field(name="Horse Clothes", value="None yet.", inline=False)

    embed.add_field(name="Equipped", value=_equipped_lines(equipped), inline=False)

    if stored:
        lines = [_equipment_line(item_id, qty) for item_id, qty in stored.items()]
        embed.add_field(name="Stored Equipment", value="\n\n".join(lines), inline=False)
    else:
        embed.add_field(name="Stored Equipment", value="None yet.", inline=False)

    embed.set_footer(text="Manage your gear with !equipment, craft more with !craft.")
    return embed


def _equipped_lines(equipped: dict[str, str]) -> str:
    lines = []
    for slot in dungeon.EQUIPMENT_SLOTS:
        item_id = equipped.get(slot)
        if item_id is None:
            lines.append(f"**{slot.title()}**: *empty*")
        else:
            item = dungeon.EQUIPMENT[item_id]
            lines.append(
                f"**{slot.title()}**: {item['name']} — {_stat_bonus_text(item)} · *{item['rarity']}*\n> {item['flavor']}"
            )
    return "\n".join(lines)


async def build_equipment_display(guild_id: int, user_id: int) -> tuple[discord.Embed, "EquipmentView"]:
    equipped = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
    stored = _stored_excluding_equipped(equipped, await asyncio.to_thread(db.get_equipment_inventory, guild_id, user_id))

    embed = discord.Embed(
        title="⚔️ Equipment",
        description="Pick a replacement (or unequip) per slot below.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Equipped", value=_equipped_lines(equipped), inline=False)
    if stored:
        lines = [_equipment_line(item_id, qty) for item_id, qty in stored.items()]
        embed.add_field(name="Stored", value="\n\n".join(lines), inline=False)
    else:
        embed.add_field(name="Stored", value="None yet — extra gear you find gets stored here instead of discarded.", inline=False)

    view = EquipmentView(guild_id, user_id, equipped, stored)
    return embed, view


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
                description=_equipment_summary(item),
            ))
        for item_id in stored_ids[: MAX_SELECT_OPTIONS - len(options)]:
            item = dungeon.EQUIPMENT[item_id]
            options.append(discord.SelectOption(label=item["name"], value=item_id, description=_equipment_summary(item)))
        super().__init__(placeholder=f"{slot.title()}...", options=options, row=row)

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        value = self.values[0]
        if value == self._UNEQUIP_VALUE:
            await asyncio.to_thread(db.unequip_item, guild_id, user_id, self.slot)
        else:
            await asyncio.to_thread(db.equip_item_smart, guild_id, user_id, self.slot, value)
        embed, view = await build_equipment_display(guild_id, user_id)
        await interaction.response.edit_message(embed=embed, view=view)
