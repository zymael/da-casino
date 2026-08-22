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
    """e.g. 'ATK +3' or 'HP +2 / DEF +1' -- the compact form used everywhere an item's power
    needs to be legible at a glance (embed lines and Select option descriptions alike)."""
    order = ("hp", "atk", "def", "spatk", "spdef")
    parts = [f"{stat.upper()} +{item['stat_bonuses'][stat]}" for stat in order if item["stat_bonuses"].get(stat)]
    return " / ".join(parts)


def _equipment_summary(item: dict) -> str:
    """'ATK +3 · rare' -- used in Select option descriptions, where space is tight."""
    return f"{_stat_bonus_text(item)} · {item['rarity']}"


def _equipment_line(item_id: str, qty: int | None = None) -> str:
    """A full detail line for an embed field: name, stats, rarity, qty, and flavor text on its
    own line beneath -- used for both the Equipped and Stored sections."""
    item = dungeon.EQUIPMENT[item_id]
    qty_suffix = f" x{qty}" if qty and qty > 1 else ""
    return f"**{item['name']}**{qty_suffix} — {_stat_bonus_text(item)} · *{item['rarity']}*\n> {item['flavor']}"


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
    stored = await asyncio.to_thread(db.get_equipment_inventory, guild_id, user_id)
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
    stored = await asyncio.to_thread(db.get_equipment_inventory, guild_id, user_id)

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
