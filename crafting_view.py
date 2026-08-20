"""!craft -- browse dungeon.RECIPES, pick one, and craft it if you're holding enough materials
(and currency). Same two-step "pick from a list, preview, confirm" flow as dungeon_view.py's
ClassPickerView, and the same "rebuild the whole display from fresh DB state" idea as
ranch_view/dungeon_view's hub displays, so the listing always reflects what you're actually
holding right now.
"""

import asyncio

import discord

import crafting
import db
import dungeon

MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options


def _recipe_output_name(recipe: dict) -> str:
    registry = dungeon.EQUIPMENT if recipe["output_kind"] == "equipment" else dungeon.CONSUMABLES
    return registry[recipe["output_id"]]["name"]


def _material_status_line(material_id: str, needed: int, held: int) -> str:
    material = dungeon.MATERIALS[material_id]
    check = "✅" if held >= needed else "❌"
    return f"{check} {material['name']} {held}/{needed}"


def _recipe_field_value(recipe: dict, held: dict[str, int]) -> str:
    lines = [_material_status_line(m_id, qty, held.get(m_id, 0)) for m_id, qty in recipe["materials"].items()]
    cost = recipe.get("currency_cost", 0)
    if cost:
        lines.append(f"💰 {cost}")
    return f"→ {_recipe_output_name(recipe)}\n" + "\n".join(lines)


def _recipe_option_description(recipe: dict) -> str:
    cost = recipe.get("currency_cost", 0)
    cost_part = f"{cost} coins + " if cost else ""
    count = len(recipe["materials"])
    return f"{cost_part}{count} material kind{'s' if count != 1 else ''}"


async def build_craft_display(guild_id: int, user_id: int) -> tuple[discord.Embed, "CraftPickerView"]:
    """Lists every recipe -- not just ones you can currently afford -- so you can see what to
    farm for, same spirit as EquipmentView showing both equipped and stored gear."""
    held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)

    embed = discord.Embed(
        title="🛠️ Crafting",
        description="Pick a recipe below to see full details and craft it.",
        color=discord.Color.blurple(),
    )
    for recipe in dungeon.RECIPES.values():
        embed.add_field(name=recipe["name"], value=_recipe_field_value(recipe, held), inline=False)

    return embed, CraftPickerView()


class RecipeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=recipe["name"], value=recipe_id, description=_recipe_option_description(recipe))
            for recipe_id, recipe in list(dungeon.RECIPES.items())[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(placeholder="Choose a recipe...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        recipe = dungeon.RECIPES[self.values[0]]
        embed, view = await build_craft_confirm_display(interaction.guild.id, interaction.user.id, recipe)
        await interaction.response.edit_message(embed=embed, view=view)


class CraftPickerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(RecipeSelect())


async def build_craft_confirm_display(guild_id: int, user_id: int, recipe: dict) -> tuple[discord.Embed, "CraftConfirmView"]:
    held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
    balance = await asyncio.to_thread(db.get_balance, guild_id, user_id)

    embed = discord.Embed(
        title=f"🛠️ Craft: {recipe['name']}", description=recipe.get("flavor", ""), color=discord.Color.blurple(),
    )
    embed.add_field(name="Produces", value=_recipe_output_name(recipe), inline=False)
    lines = [_material_status_line(m_id, qty, held.get(m_id, 0)) for m_id, qty in recipe["materials"].items()]
    embed.add_field(name="Materials", value="\n".join(lines), inline=False)
    cost = recipe.get("currency_cost", 0)
    if cost:
        check = "✅" if balance >= cost else "❌"
        embed.add_field(name="Cost", value=f"{check} {cost} (you have {balance})", inline=False)

    return embed, CraftConfirmView(recipe["id"])


class CraftConfirmView(discord.ui.View):
    def __init__(self, recipe_id: str):
        super().__init__(timeout=180)
        self.recipe_id = recipe_id

    @discord.ui.button(label="Craft", style=discord.ButtonStyle.success, row=0)
    async def craft_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        result = await crafting.craft(interaction.guild.id, interaction.user.id, self.recipe_id)
        if not result["success"]:
            reason = (
                "You're missing materials for this." if result["status"] == "insufficient_materials"
                else "You can't afford this."
            )
            await interaction.response.send_message(reason, ephemeral=True)
            return

        item = result["output_item"]
        if result["output_kind"] == "equipment":
            if result["equipped"]:
                status_text = f"⚔️ Crafted **{item['name']}** — equipped!"
            else:
                status_text = f"⚔️ Crafted **{item['name']}**, but your current gear is better — stored in `!equipment`."
        else:
            status_text = f"🧪 Crafted **{item['name']}** — check `!inventory`."

        embed, view = await build_craft_display(interaction.guild.id, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(status_text, ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=0)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = await build_craft_display(interaction.guild.id, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)
