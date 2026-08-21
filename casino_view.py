"""Casino-room specialization: a game picker, on top of whatever generic room content
room_view.py already renders (background, NPCs, the plain `commands` button list). `GameSelect` is
a documented exception to "rooms only ever invoke a command with 0-or-1 generically-collected
args" (see CLAUDE.md) -- it's *one* Select representing 7 different possible commands, not one
button collecting one argument, so it can't be expressed as a `rooms.json` commands[] entry the
way every other button can. It still only ever invokes an existing command per option, same as
everything else; it's the "one Select, many commands" *shape* that doesn't fit the schema, not a
bespoke-argument-collection violation of that rule.

Neither the Select nor the buttons duplicate any game logic -- they invoke the *exact same*
command callback bot.py already registers via @bot.command, by wrapping the click/modal-submit
interaction in a minimal ctx-alike (every one of those callbacks only ever touches
ctx.guild/.channel/.author/.send).
"""

import discord

import hub_ui

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


def extra_items(commands: dict) -> list[discord.ui.Item]:
    """room_view.py's casino specialization hook -- see module docstring for why GameSelect can't
    just be a rooms.json commands[] entry like everything else."""
    return [GameSelect(commands, row=0)]
