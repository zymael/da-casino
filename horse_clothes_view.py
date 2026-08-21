"""!horseequip's per-horse cosmetic picker -- the second step after picking which horse (see
ranch_view.build_horse_picker), shown by bot.py's horseequip_cmd. Same shape as
inventory_view.EquipmentView/EquipmentSlotSelect (one Select per slot, "Unequip" plus whatever's
owned), but keyed by (guild_id, horse_index) rather than (guild_id, user_id) -- horse clothes are
a reusable wardrobe (db.equip_horse_clothes never consumes anything from `inventory`), so the same
owned item can show up as an option for every horse the equipping user owns at once.
"""

import asyncio

import discord

import db
import horse_clothes

MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options


async def build_horse_equip_display(
    guild_id: int, user_id: int, horse_index: int
) -> tuple[discord.Embed, "HorseClothesView"]:
    horses = await asyncio.to_thread(db.get_guild_horses, guild_id)
    horse = horses[horse_index]
    equipped = (await asyncio.to_thread(db.get_guild_horse_clothes, guild_id)).get(horse_index, {})
    held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
    owned = {item_id: qty for item_id, qty in held.items() if item_id in horse_clothes.HORSE_CLOTHES}

    embed = discord.Embed(
        title=f"👒 Dress Up {horse['name']}",
        description="Pick a cosmetic (or unequip) per slot below -- purely for looks, no stat effect.",
        color=discord.Color.blurple(),
    )
    equipped_lines = []
    for slot in horse_clothes.CLOTHES_SLOTS:
        item_id = equipped.get(slot)
        if item_id is None:
            equipped_lines.append(f"**{slot.title()}**: *empty*")
        else:
            item = horse_clothes.HORSE_CLOTHES[item_id]
            equipped_lines.append(f"**{slot.title()}**: {item['name']}\n> {item['flavor']}")
    embed.add_field(name="Equipped", value="\n".join(equipped_lines), inline=False)

    if owned:
        owned_lines = [
            f"👒 **{horse_clothes.HORSE_CLOTHES[item_id]['name']}**{f' x{qty}' if qty > 1 else ''} "
            f"· *{horse_clothes.HORSE_CLOTHES[item_id]['slot']}*"
            for item_id, qty in owned.items()
        ]
        embed.add_field(name="Your Wardrobe", value="\n".join(owned_lines), inline=False)
    else:
        embed.add_field(name="Your Wardrobe", value="None yet — buy some from an NPC shop.", inline=False)

    view = HorseClothesView(horse_index, equipped, owned)
    return embed, view


class HorseClothesView(discord.ui.View):
    def __init__(self, horse_index: int, equipped: dict[str, str], owned: dict[str, int]):
        super().__init__(timeout=300)
        for row, slot in enumerate(horse_clothes.CLOTHES_SLOTS):
            equipped_id = equipped.get(slot)
            owned_ids = [item_id for item_id in owned if horse_clothes.HORSE_CLOTHES[item_id]["slot"] == slot]
            if equipped_id is None and not owned_ids:
                continue  # nothing to manage for this slot -- no Select for it
            self.add_item(HorseClothesSlotSelect(horse_index, slot, equipped_id, owned_ids, row))


class HorseClothesSlotSelect(discord.ui.Select):
    _UNEQUIP_VALUE = "__unequip__"

    def __init__(self, horse_index: int, slot: str, equipped_id: str | None, owned_ids: list[str], row: int):
        self.horse_index = horse_index
        self.slot = slot
        options = [discord.SelectOption(label="— Unequip —", value=self._UNEQUIP_VALUE, default=equipped_id is None)]
        if equipped_id is not None and equipped_id not in owned_ids:
            # Defensive: the wardrobe model never consumes ownership on equip, so this shouldn't
            # normally happen -- kept in case a future feature ever trades/removes an owned item
            # out from under something still wearing it, same guard EquipmentSlotSelect has.
            item = horse_clothes.HORSE_CLOTHES[equipped_id]
            options.append(discord.SelectOption(
                label=f"{item['name']} (equipped)", value=equipped_id, default=True, description=item["flavor"][:100],
            ))
        for item_id in owned_ids[: MAX_SELECT_OPTIONS - len(options)]:
            item = horse_clothes.HORSE_CLOTHES[item_id]
            options.append(discord.SelectOption(
                label=f"{item['name']}{' (equipped)' if item_id == equipped_id else ''}",
                value=item_id, default=item_id == equipped_id, description=item["flavor"][:100],
            ))
        super().__init__(placeholder=f"{slot.title()}...", options=options, row=row)

    async def callback(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        value = self.values[0]
        if value == self._UNEQUIP_VALUE:
            await asyncio.to_thread(db.unequip_horse_clothes, guild_id, user_id, self.horse_index, self.slot)
        else:
            await asyncio.to_thread(db.equip_horse_clothes, guild_id, user_id, self.horse_index, self.slot, value)
        embed, view = await build_horse_equip_display(guild_id, user_id, self.horse_index)
        await interaction.response.edit_message(embed=embed, view=view)
