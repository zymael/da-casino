"""/play's Casino destination: a game picker plus one-click buttons for every economy command, so
players don't have to memorize the ! commands (which still work standalone, unchanged).

Neither the Select nor the buttons duplicate any game logic -- they invoke the *exact same*
command callback bot.py already registers via @bot.command, by wrapping the click/modal-submit
interaction in a minimal ctx-alike (every one of those callbacks only ever touches
ctx.guild/.channel/.author/.send). Command callbacks are passed in from bot.py at
hub-construction time (not imported here), since bot.py is what imports this module -- importing
back would be circular.
"""

import asyncio

import discord

import casino_render
import hub_ui

CASINO_BANNER_PATH = "assets/casino_banner.png"

# Every game, collapsed into one Select instead of one button each -- "amount" games open
# hub_ui.AmountModal first (same bet-collection convention as blackjack's Join/Set Bet modal
# elsewhere in this codebase), the rest just run. closes_hub=True is implicit for every game here
# (see GameSelect.callback) -- once a game actually starts (its own message posted below), the
# hub's job is done, so it disappears rather than sitting there idle behind the game.
GAMES = [
    {"key": "blackjack", "label": "🃏 Blackjack", "amount": True, "modal_title": "Blackjack Bet", "input_label": "Bet amount"},
    {"key": "slots", "label": "🎰 Slots", "amount": False},
    {"key": "roulette", "label": "🎡 Roulette", "amount": False},
    {"key": "holdem", "label": "♠️ Hold'em", "amount": True, "modal_title": "Hold'em Buy-in", "input_label": "Buy-in (blank = default)", "required": False},
    {"key": "videopoker", "label": "🎴 Video Poker", "amount": True, "modal_title": "Video Poker Bet", "input_label": "Bet amount"},
    {"key": "deuceswild", "label": "🎴 Deuces Wild", "amount": True, "modal_title": "Deuces Wild Bet", "input_label": "Bet amount"},
    {"key": "horserace", "label": "🐎 Horse Race", "amount": False},
]
GAMES_BY_KEY = {spec["key"]: spec for spec in GAMES}


class GameSelect(discord.ui.Select):
    def __init__(self, commands: dict, row: int):
        self.commands = commands
        options = [discord.SelectOption(label=spec["label"], value=spec["key"]) for spec in GAMES]
        super().__init__(placeholder="🎮 Pick a game...", options=options, row=row)

    async def callback(self, interaction: discord.Interaction):
        spec = GAMES_BY_KEY[self.values[0]]
        command_callback = self.commands[spec["key"]]
        if spec["amount"]:
            await interaction.response.send_modal(
                hub_ui.AmountModal(
                    spec["modal_title"], command_callback, spec["input_label"],
                    spec.get("required", True), closes_hub=True,
                )
            )
        else:
            await interaction.response.defer()
            await command_callback(hub_ui.InteractionContext(interaction))
            await hub_ui.close_hub(interaction)


async def build_casino_display(commands: dict, session: hub_ui.HubSession, go_home) -> tuple[discord.Embed, "CasinoView"]:
    embed = discord.Embed(
        title="🎰 Casino Hub",
        description="Every game and shortcut in one place — the `!` commands still work too.",
        color=discord.Color.gold(),
    )
    embed.set_image(url="attachment://casino_banner.png")
    view = CasinoView(commands, session, go_home)
    return embed, view


class CasinoView(discord.ui.View):
    """Rebuilt fresh on every render (see build_casino_display) rather than a static persistent
    panel, now that it lives inside /play's single ephemeral message alongside the other hubs --
    same "state-aware dashboard" idea as ranch_view.RanchView."""

    def __init__(self, commands: dict, session: hub_ui.HubSession, go_home):
        super().__init__(timeout=300)
        self.session = session
        c = commands

        # Row 0: every game, one Select instead of 7 buttons across 2 uneven rows.
        self.add_item(GameSelect(commands, row=0))

        # Row 1: economy quick actions -- instant, no ongoing session, so the hub stays open.
        self.add_item(hub_ui.NoArgButton("💰 Balance", discord.ButtonStyle.secondary, 1, c["balance"]))
        self.add_item(hub_ui.NoArgButton("😴 Rest", discord.ButtonStyle.secondary, 1, c["rest"]))
        self.add_item(hub_ui.NoArgButton("⛏️ Mine", discord.ButtonStyle.secondary, 1, c["mine"]))
        self.add_item(hub_ui.NoArgButton("🍕 Pizza", discord.ButtonStyle.secondary, 1, c["pizza"]))
        self.add_item(hub_ui.NoArgButton("🏆 Leaderboard", discord.ButtonStyle.secondary, 1, c["leaderboard"]))

        # Row 2: info + navigation.
        self.add_item(hub_ui.NoArgButton("📊 Stats", discord.ButtonStyle.secondary, 2, c["stats"]))
        self.add_item(hub_ui.NoArgButton("🏅 Achievements", discord.ButtonStyle.secondary, 2, c["achievements"]))
        self.add_item(hub_ui.InventoryButton(row=2))
        self.add_item(hub_ui.EquipmentButton(row=2))
        self.add_item(hub_ui.TownSquareButton(go_home, row=2))

        # Row 3: flavor.
        self.add_item(RoyButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        self.session.touch(interaction)
        return True


class RoyButton(discord.ui.Button):
    """Deliberately not a text command -- the only way to meet Roy is to click this. Unlike
    Kel's ranch-hub equivalent, this one has no achievement tied to it (not asked for) -- it just
    swaps the hub's banner for Roy's "LET IT RIDE" greeting, replaying every time it's clicked."""

    def __init__(self):
        super().__init__(label="👹 Meet Roy", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction):
        buf = await asyncio.to_thread(casino_render.render_roy_greeting)
        file = discord.File(buf, filename="roy_greeting.png")
        embed = interaction.message.embeds[0]
        embed.set_image(url="attachment://roy_greeting.png")
        await interaction.response.edit_message(embed=embed, attachments=[file], view=self.view)
