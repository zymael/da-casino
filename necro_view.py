"""Discord UI for horse necromancy (rules: necromancy.py; command: bot.py's !glue). Like every
*_view.py module, this only collects arguments and hands them to a caller-supplied callback --
bot.py never builds discord.ui components itself, and the sanctuary room's button stays a plain
NoArgButton (see CLAUDE.md's "rooms are thin command wrappers")."""

import asyncio
import random
import time

import discord

import db
import hub_ui
import necromancy


class HauntingView(discord.ui.View):
    """One-button dismissal for a haunting popup -- purely cosmetic, same shape as roulette_view's
    SicklyVictorianDaughtersView. Anyone can clear it; looking away is a group effort."""

    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Pretend you didn't see it", style=discord.ButtonStyle.secondary)
    async def look_away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass


async def maybe_haunt_winners(send, guild_id: int, net_by_user: dict[int, int]) -> None:
    """Called once a casino game's results are out. Every player who came out ahead and has glued a
    horse rolls necromancy.HAUNT_WIN_CHANCE for one of their ghosts (not prayed to peace) to show
    up. `send` is the game's own channel send (ctx.send / interaction.followup.send / ...)."""
    now = int(time.time())
    for user_id, net in net_by_user.items():
        if net <= 0 or random.random() >= necromancy.HAUNT_WIN_CHANCE:
            continue
        ghosts = await asyncio.to_thread(
            db.get_haunting_horses, guild_id, user_id, now, necromancy.peace_flag(""),
        )
        if not ghosts:
            continue
        name = random.choice(ghosts)
        embed = discord.Embed(
            title="👻 You're Being Haunted",
            description=random.choice(necromancy.HAUNT_WIN_LINES).format(name=name),
            color=discord.Color.dark_gray(),
        )
        file = discord.File(necromancy.HAUNT_IMAGE_PATH, filename="ghost_horse.png")
        embed.set_image(url="attachment://ghost_horse.png")
        await send(f"<@{user_id}>", embed=embed, file=file, view=HauntingView())


class GlueConfirmView(discord.ui.View):
    """The "this is forever" confirmation !glue shows before rendering a horse. Only the player who
    asked can press it -- anyone else would just get not_owner from db.glue_horse, but there's no
    reason to let them try. `on_confirm` is an async (ctx, horse_index) callback."""

    def __init__(self, user_id: int, horse_index: int, on_confirm):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.horse_index = horse_index
        self.on_confirm = on_confirm

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("That isn't your horse to lay to rest.", ephemeral=True)
            return False
        return True

    def _disable_all(self):
        for item in self.children:
            item.disabled = True
        self.stop()

    @discord.ui.button(label="Lay it to rest", emoji="⚱️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self._disable_all()
        await interaction.response.edit_message(view=self)
        await self.on_confirm(hub_ui.InteractionContext(interaction), self.horse_index)

    @discord.ui.button(label="Not today", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self._disable_all()
        await interaction.response.edit_message(content="The horse lives to gallop another day.", view=self)


def build_glue_confirm(user_id: int, horse_index: int, on_confirm) -> discord.ui.View:
    return GlueConfirmView(user_id, horse_index, on_confirm)


class _OwnerOnlyView(discord.ui.View):
    def __init__(self, user_id: int, reject_message: str = "Get your own Ectoglue."):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.reject_message = reject_message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(self.reject_message, ephemeral=True)
            return False
        return True


class _CallbackSelect(discord.ui.Select):
    """A Select that hands its one picked value to `on_pick(ctx, value)` and disables itself."""

    def __init__(self, options: list[discord.SelectOption], on_pick, placeholder: str):
        super().__init__(placeholder=placeholder, options=options[:25])
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        self.disabled = True
        await interaction.response.edit_message(view=self.view)
        self.view.stop()
        await self.on_pick(hub_ui.InteractionContext(interaction), self.values[0])


def build_spell_picker(user_id: int, spells: list[tuple[dict, str]], on_pick) -> discord.ui.View:
    """spells: (spell dict, one-line summary) per option. on_pick(ctx, spell_id)."""
    options = [
        discord.SelectOption(
            label=f"{spell['name']} ({spell['cost']} Ectoglue)"[:100], value=spell["id"],
            emoji=spell["emoji"], description=summary[:100],
        )
        for spell, summary in spells
    ]
    view = _OwnerOnlyView(user_id)
    view.add_item(_CallbackSelect(options, on_pick, "Choose a spell to prepare..."))
    return view


def build_grave_picker(
    user_id: int, entries: list[tuple[int, str, str]], on_pick, placeholder: str, emoji: str = "🪦",
    reject_message: str = "Get your own Ectoglue.",
) -> discord.ui.View:
    """A Select over graveyard plots -- shared by a summon spell's "whose grave do you disturb" and
    !pray. entries: (horse_index, name, description) per grave, already sorted. on_pick(ctx, horse_index)."""
    options = [
        discord.SelectOption(label=name[:100], value=str(horse_index), emoji=emoji, description=description[:100])
        for horse_index, name, description in entries
    ]

    async def picked(ctx, value: str):
        await on_pick(ctx, int(value))

    view = _OwnerOnlyView(user_id, reject_message)
    view.add_item(_CallbackSelect(options, picked, placeholder))
    return view


def build_grade_picker(user_id: int, grades: list[tuple[int, dict, int, str]], on_pick) -> discord.ui.View:
    """grades: (grade index, quest item dict, qty held, effect summary at that grade) for every grade
    the player can afford. on_pick(ctx, grade_index)."""
    options = [
        discord.SelectOption(
            label=f"{item['name']} (you have {held})"[:100], value=str(grade),
            emoji=item["emoji"], description=summary[:100],
        )
        for grade, item, held, summary in grades
    ]

    async def picked(ctx, value: str):
        await on_pick(ctx, int(value))

    view = _OwnerOnlyView(user_id)
    view.add_item(_CallbackSelect(options, picked, "Pay with which grade?"))
    return view
