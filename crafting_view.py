"""!craft -- discovery-based crafting: pick two or three materials you're holding and find out what
they make, rather than choosing from a list of recipes you already know by name. A successful combo
crafts the item (same consumption/grant logic as before, see crafting.combine) and remembers it
as "discovered" so a Known Recipes reference list can jog your memory later -- but re-crafting
something you already know still goes through the same pick-materials flow, not a shortcut.
"""

import asyncio

import discord

import crafting
import db
import dungeon

MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options

# output_kind -> emoji shown in the "Crafted X" status message for non-equipment kinds, same icon
# each kind already uses elsewhere (inventory_view.py's per-kind listing) -- quest_item is the one
# kind with a per-item icon (quest_items.json's own "emoji" field) rather than one fixed for the
# whole kind.
_INVENTORY_KIND_EMOJI = {
    "consumable": lambda item: "🧪",
    "quest_item": lambda item: item["emoji"],
    "horse_clothes": lambda item: "👒",
}


def _material_option(material_id: str, held_qty: int) -> discord.SelectOption:
    material = dungeon.MATERIALS[material_id]
    return discord.SelectOption(
        label=material["name"], value=material_id, description=f"You have {held_qty}"
    )


async def build_craft_display(guild_id: int, user_id: int) -> tuple[discord.Embed, "CombineView"]:
    held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
    held_materials = {m_id: qty for m_id, qty in held.items() if m_id in dungeon.MATERIALS and qty > 0}

    embed = _combine_embed(held_materials, [None, None, None])
    view = CombineView(guild_id, user_id, held_materials)
    return embed, view


def _combine_embed(held_materials: dict[str, int], picks: list[str | None]) -> discord.Embed:
    embed = discord.Embed(
        title="🛠️ Crafting",
        description="Pick materials to combine. If it's a real recipe, you'll find out.",
        color=discord.Color.blurple(),
    )
    if held_materials:
        lines = [f"{dungeon.MATERIALS[m_id]['name']} — {qty}" for m_id, qty in sorted(held_materials.items())]
        embed.add_field(name="Materials on hand", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Materials on hand", value="None yet -- go delve and find some.", inline=False)

    names = [dungeon.MATERIALS[p]["name"] if p else "*(pick one)*" for p in picks]
    embed.add_field(name="Combining", value="  +  ".join(names), inline=False)
    return embed


class MaterialSelect(discord.ui.Select):
    """One of the three material pickers on a CombineView -- `slot` (0, 1, or 2) says which of the
    view's picks this one sets. The third slot is optional (a 2-material recipe just leaves it
    unset -- see combine_button), unlike the first two. Options and the currently-chosen value come
    from the parent view, so every select can show the same material as a valid choice in each
    (picking Stick in two slots is how a "2 Stick" combo gets made)."""

    def __init__(self, view: "CombineView", slot: int):
        self.slot = slot
        options = [
            _material_option(m_id, qty) for m_id, qty in list(view.held_materials.items())[:MAX_SELECT_OPTIONS]
        ]
        current = view.picks[slot]
        for option in options:
            option.default = option.value == current
        if slot == 2:
            options = [discord.SelectOption(label="(none)", value="_none", default=current is None)] + options
        placeholder = f"Choose material {slot + 1}..." if slot < 2 else "Choose material 3 (optional)..."
        super().__init__(
            placeholder=placeholder,
            options=options or [discord.SelectOption(label="(nothing held)", value="_none", default=True)],
            disabled=not options,
            row=slot,
        )

    async def callback(self, interaction: discord.Interaction):
        view: "CombineView" = self.view
        view.picks[self.slot] = self.values[0] if self.values[0] != "_none" else None
        embed = _combine_embed(view.held_materials, view.picks)
        await interaction.response.edit_message(embed=embed, view=view.rebuilt())


class CombineView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, held_materials: dict[str, int], picks: list[str | None] = None):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.held_materials = held_materials
        self.picks: list[str | None] = picks if picks is not None else [None, None, None]
        for slot in range(3):
            self.add_item(MaterialSelect(self, slot))

    def rebuilt(self) -> "CombineView":
        """A fresh view carrying forward the same picks -- MaterialSelect.options bake in
        `option.default` at construction time, so the simplest way to reflect a new selection is
        a new view/select pair rather than mutating options on the existing ones in place."""
        return CombineView(self.guild_id, self.user_id, self.held_materials, list(self.picks))

    @discord.ui.button(label="Combine", style=discord.ButtonStyle.success, row=3)
    async def combine_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.picks[0] or not self.picks[1]:
            await interaction.response.send_message("Pick at least two materials first.", ephemeral=True)
            return

        materials = [p for p in self.picks if p is not None]
        result = await crafting.combine(self.guild_id, self.user_id, materials)

        if result["status"] == "no_match":
            await interaction.response.send_message("💨 Nothing happens.", ephemeral=True)
            return
        if not result["success"]:
            reason = (
                "You're missing materials for that." if result["status"] == "insufficient_materials"
                else "You can't afford that."
            )
            await interaction.response.send_message(reason, ephemeral=True)
            return

        item = result["output_item"]
        if result["output_kind"] == "equipment":
            status_text = f"⚔️ Crafted **{item['name']}** — stored in `!equipment`."
        else:
            emoji = _INVENTORY_KIND_EMOJI[result["output_kind"]](item)
            status_text = f"{emoji} Crafted **{item['name']}** — check `!inventory`."
        if result["newly_discovered"]:
            status_text = f"🎉 New recipe discovered!\n{status_text}"

        embed, view = await build_craft_display(self.guild_id, self.user_id)
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(status_text, ephemeral=True)

    @discord.ui.button(label="📖 Known Recipes", style=discord.ButtonStyle.secondary, row=3)
    async def known_recipes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = await build_known_recipes_display(self.guild_id, self.user_id)
        await interaction.response.edit_message(embed=embed, view=view)


def _known_recipe_line(recipe: dict) -> str:
    materials = ", ".join(
        f"{qty} {dungeon.MATERIALS[m_id]['name']}" for m_id, qty in recipe["materials"].items()
    )
    return f"**{recipe['name']}** — {materials}"


async def build_known_recipes_display(guild_id: int, user_id: int) -> tuple[discord.Embed, "KnownRecipesView"]:
    discovered_ids = await asyncio.to_thread(db.get_discovered_recipes, guild_id, user_id)
    embed = discord.Embed(title="📖 Known Recipes", color=discord.Color.blurple())
    if not discovered_ids:
        embed.description = "You haven't discovered any recipes yet -- try combining materials and see what happens."
    else:
        recipes = sorted((dungeon.RECIPES[r_id] for r_id in discovered_ids), key=lambda r: r["name"])
        embed.description = "\n".join(_known_recipe_line(r) for r in recipes)
    return embed, KnownRecipesView()


class KnownRecipesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=0)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = await build_craft_display(interaction.guild.id, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)
