"""!bake -- the oven: station crafting for recipes marked "station": "oven" (dungeon.RECIPE_STATIONS),
opened by bot.py's bake_cmd (typed, or the Bake button on the !house screen) for anyone who owns a
housing item with that station (housing.owned_station -- the Necronomicrust).

Unlike !craft (pure discovery, no shortcut for known recipes), the oven is a menu: every pizza
you've already discovered is one pick away. Finding new ones still goes through the Experiment
screen, which is the same discovery idea as crafting_view.CombineView (crafting.combine with
station="oven") but built as "add a leftover" one at a time, since oven recipes can use more
ingredients (and more copies of one) than !craft's three fixed slots allow.

Nothing here names a pizza or a leftover: the menu is whatever oven recipes you've discovered,
and the experiment picker is whatever materials any oven recipe uses.
"""

import asyncio

import discord

import crafting
import db
import dungeon

STATION = "oven"
MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options
MAX_INGREDIENTS = 8


def _ingredient_ids() -> set[str]:
    """Every material some oven recipe uses -- the only things worth putting in the oven."""
    return {m_id for r in dungeon.station_recipes(STATION) for m_id in r["materials"]}


async def _held_ingredients(guild_id: int, user_id: int) -> dict[str, int]:
    held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
    usable = _ingredient_ids()
    return {m_id: qty for m_id, qty in held.items() if m_id in usable and m_id in dungeon.MATERIALS and qty > 0}


async def _known_pizzas(guild_id: int, user_id: int) -> list[dict]:
    discovered = await asyncio.to_thread(db.get_discovered_recipes, guild_id, user_id)
    return sorted(
        (r for r in dungeon.station_recipes(STATION) if r["id"] in discovered), key=lambda r: r["name"]
    )


def _bakeable_count(recipe: dict, held: dict[str, int]) -> int:
    return min(held.get(m_id, 0) // qty for m_id, qty in recipe["materials"].items())


def _recipe_line(recipe: dict) -> str:
    return ", ".join(f"{qty} {dungeon.MATERIALS[m_id]['name']}" for m_id, qty in recipe["materials"].items())


def _held_field(embed: discord.Embed, held: dict[str, int]):
    if held:
        lines = [f"{dungeon.MATERIALS[m_id]['name']}: {qty}" for m_id, qty in sorted(held.items())]
        embed.add_field(name="Leftovers on hand", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Leftovers on hand", value="None. Order a `!pizza` and keep what's left.", inline=False)


async def build_oven_display(guild_id: int, user_id: int, oven: dict) -> tuple[discord.Embed, "OvenMenuView"]:
    held = await _held_ingredients(guild_id, user_id)
    known = await _known_pizzas(guild_id, user_id)
    embed = discord.Embed(
        title=f"{oven['emoji']} {oven['name']}",
        description="Pick something off the menu, or experiment with your leftovers to find something new.",
        color=discord.Color.dark_purple(),
    )
    if known:
        lines = [f"**{r['name']}**: {_recipe_line(r)} *(can bake {_bakeable_count(r, held)})*" for r in known]
        embed.add_field(name="Menu", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Menu", value="Nothing yet. Experiment to find your first pizza.", inline=False)
    _held_field(embed, held)
    return embed, OvenMenuView(guild_id, user_id, oven, known, held)


class _OwnerOnlyView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("That's not your oven.", ephemeral=True)
            return False
        return True


def _bake_result_text(result: dict) -> str | None:
    """The ephemeral message for a crafting.craft/combine result, or None for a no_match (the
    caller words that one itself)."""
    if result["status"] == "no_match":
        return None
    if not result["success"]:
        return (
            "You don't have enough leftovers for that." if result["status"] == "insufficient_materials"
            else "You can't afford that."
        )
    text = f"🍕 Baked a **{result['output_item']['name']}**. It's in your `!inventory`."
    if result.get("newly_discovered"):
        text = f"🎉 New pizza on the menu!\n{text}"
    return text


class MenuSelect(discord.ui.Select):
    """Picking a pizza bakes one right away -- the menu's whole point is no re-guessing."""

    def __init__(self, known: list[dict], held: dict[str, int]):
        options = [
            discord.SelectOption(
                label=r["name"][:100], value=r["id"],
                description=(f"Can bake {n}" if (n := _bakeable_count(r, held)) else "Not enough leftovers"),
            )
            for r in known[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(
            placeholder="🔥 Bake something off the menu..." if options else "Nothing on the menu yet",
            options=options or [discord.SelectOption(label="(nothing yet)", value="_none")],
            disabled=not options, row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: OvenMenuView = self.view
        result = await crafting.craft(view.guild_id, view.user_id, self.values[0])
        embed, new_view = await build_oven_display(view.guild_id, view.user_id, view.oven)
        await interaction.response.edit_message(embed=embed, view=new_view)
        await interaction.followup.send(_bake_result_text(result), ephemeral=True)


class OvenMenuView(_OwnerOnlyView):
    def __init__(self, guild_id: int, user_id: int, oven: dict, known: list[dict], held: dict[str, int]):
        super().__init__(user_id)
        self.guild_id = guild_id
        self.oven = oven
        self.add_item(MenuSelect(known, held))

    @discord.ui.button(label="🧪 Experiment", style=discord.ButtonStyle.primary, row=1)
    async def experiment_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        held = await _held_ingredients(self.guild_id, self.user_id)
        view = ExperimentView(self.guild_id, self.user_id, self.oven, held, [])
        await interaction.response.edit_message(embed=view.embed(), view=view)


class IngredientSelect(discord.ui.Select):
    def __init__(self, held: dict[str, int], picks: list[str]):
        remaining = {m_id: qty - picks.count(m_id) for m_id, qty in held.items()}
        options = [
            discord.SelectOption(label=dungeon.MATERIALS[m_id]["name"][:100], value=m_id, description=f"{qty} left")
            for m_id, qty in sorted(remaining.items()) if qty > 0
        ][:MAX_SELECT_OPTIONS]
        full = len(picks) >= MAX_INGREDIENTS
        super().__init__(
            placeholder="The oven is full" if full else "Put a leftover in...",
            options=options or [discord.SelectOption(label="(nothing left to add)", value="_none")],
            disabled=full or not options, row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: ExperimentView = self.view
        picks = view.picks + [self.values[0]]
        new_view = ExperimentView(view.guild_id, view.user_id, view.oven, view.held, picks)
        await interaction.response.edit_message(embed=new_view.embed(), view=new_view)


class ExperimentView(_OwnerOnlyView):
    def __init__(self, guild_id: int, user_id: int, oven: dict, held: dict[str, int], picks: list[str]):
        super().__init__(user_id)
        self.guild_id = guild_id
        self.oven = oven
        self.held = held
        self.picks = picks
        self.add_item(IngredientSelect(held, picks))
        self.bake_button.disabled = not picks
        self.undo_button.disabled = not picks

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"{self.oven['emoji']} {self.oven['name']}: Experiment",
            description="Put leftovers in, then bake. If it isn't a real pizza, you get them back.",
            color=discord.Color.dark_purple(),
        )
        names = "  +  ".join(dungeon.MATERIALS[p]["name"] for p in self.picks) or "*(empty)*"
        embed.add_field(name="In the oven", value=names, inline=False)
        _held_field(embed, self.held)
        return embed

    async def _refresh(self, interaction: discord.Interaction, picks: list[str]):
        held = await _held_ingredients(self.guild_id, self.user_id)
        view = ExperimentView(self.guild_id, self.user_id, self.oven, held, picks)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="🔥 Bake", style=discord.ButtonStyle.success, row=1)
    async def bake_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        result = await crafting.combine(self.guild_id, self.user_id, self.picks, station=STATION)
        if result["status"] == "no_match":
            await interaction.response.send_message(
                "The oven rumbles for a while, then pushes your leftovers back out. That isn't a pizza.",
                ephemeral=True,
            )
            return
        await self._refresh(interaction, [] if result["success"] else self.picks)
        await interaction.followup.send(_bake_result_text(result), ephemeral=True)

    @discord.ui.button(label="↩ Take one out", style=discord.ButtonStyle.secondary, row=1)
    async def undo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._refresh(interaction, self.picks[:-1])

    @discord.ui.button(label="📖 Menu", style=discord.ButtonStyle.secondary, row=1)
    async def menu_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = await build_oven_display(self.guild_id, self.user_id, self.oven)
        await interaction.response.edit_message(embed=embed, view=view)
