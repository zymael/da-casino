"""The sell popup -- one Select listing every item this player currently owns that sell.py
recognizes as sellable (25-max per Discord Select, same limit shop_view.py already works around),
opened via npc_view.SellButton for any NPC with npcs.json's "buys_items" checked. Same ephemeral-
popup-that-redraws-itself shape as shop_view.py; selling re-renders the same popup in place so a
player can sell more than one thing without reopening it.
"""

import asyncio

import discord

import db
import dungeon
import npcs
import sell

_KIND_EMOJI = {
    "equipment": "⚔️", "material": "⛏️", "consumable": "🧪", "horse_clothes": "👒", "housing_item": "🛋️",
}
MAX_SELECT_OPTIONS = 25


def _item_blurb(item: dict) -> str:
    """Same field-name reconciliation as shop_view._item_blurb -- most content uses "flavor",
    housing items use "description"."""
    return item.get("flavor") or item.get("description") or ""


async def build_sell_display(guild_id: int, user_id: int, npc_id: str) -> tuple[discord.Embed, "SellView"]:
    npc = npcs.NPCS[npc_id]
    held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
    stored_equipment = await asyncio.to_thread(db.get_equipment_inventory, guild_id, user_id)
    horse_clothes_in_use = await asyncio.to_thread(sell.horse_clothes_in_use, guild_id, user_id)
    holdings = sell.sellable_holdings(held, stored_equipment, horse_clothes_in_use)
    balance = await asyncio.to_thread(db.get_balance, guild_id, user_id)
    currency = db.get_currency_name(guild_id)

    embed = discord.Embed(title=f"💰 Sell to {npc['name']}", color=discord.Color.dark_gold())
    if holdings:
        lines = []
        for kind, item_id, qty in holdings:
            item = sell.SELLABLE_REGISTRIES[kind]()[item_id]
            price = sell.sell_price(item)
            qty_suffix = f" x{qty}" if qty > 1 else ""
            lines.append(f"{_KIND_EMOJI[kind]} **{item['name']}**{qty_suffix} — {price} {currency}")
        embed.description = "\n".join(lines)
    else:
        embed.description = "You don't have anything to sell right now."
    embed.set_footer(text=f"You have {balance} {currency}.")

    view = SellView(guild_id, user_id, npc_id, holdings, currency)
    return embed, view


class SellView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, npc_id: str, holdings: list[tuple[str, str, int]], currency: str):
        super().__init__(timeout=300)
        if holdings:  # a Select needs at least one option -- nothing to add if they own nothing sellable
            self.add_item(SellSelect(guild_id, user_id, npc_id, holdings, currency))


class SellSelect(discord.ui.Select):
    def __init__(
        self, guild_id: int, user_id: int, npc_id: str, holdings: list[tuple[str, str, int]], currency: str,
    ):
        self.guild_id = guild_id
        self.user_id = user_id
        self.npc_id = npc_id
        self.currency = currency
        self.holdings = holdings
        options = []
        for i, (kind, item_id, qty) in enumerate(holdings[:MAX_SELECT_OPTIONS]):
            item = sell.SELLABLE_REGISTRIES[kind]()[item_id]
            price = sell.sell_price(item)
            rarity_emoji = dungeon.RARITY_EMOJI[item["rarity"]] if kind == "equipment" else None
            qty_suffix = f" x{qty}" if qty > 1 else ""
            options.append(discord.SelectOption(
                label=f"{item['name']}{qty_suffix} — {price} {currency}"[:100],
                value=str(i),
                description=_item_blurb(item)[:100] or None,
                emoji=rarity_emoji,
            ))
        super().__init__(placeholder="Sell something...", options=options)

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        kind, item_id, _ = self.holdings[index]
        result = await sell.sell(self.guild_id, self.user_id, kind, item_id)
        if not result["success"]:
            await interaction.response.send_message("You don't have that anymore.", ephemeral=True)
            return

        item = result["item"]
        sale_text = f"💰 Sold **{item['name']}** for {result['price']} {self.currency}."

        embed, view = await build_sell_display(self.guild_id, self.user_id, self.npc_id)
        embed.add_field(name="Sold", value=sale_text, inline=False)
        await interaction.response.edit_message(embed=embed, view=view)
