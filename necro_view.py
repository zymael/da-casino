"""Discord UI for horse necromancy (rules: necromancy.py; command: bot.py's !glue). Like every
*_view.py module, this only collects arguments and hands them to a caller-supplied callback --
bot.py never builds discord.ui components itself, and the sanctuary room's button stays a plain
NoArgButton (see CLAUDE.md's "rooms are thin command wrappers")."""

import discord

import hub_ui


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
