# da-casino

A Discord casino bot built with [discord.py](https://discordpy.readthedocs.io/). Members earn virtual credits and play blackjack, slots, roulette, and Texas hold'em against each other, all restricted to a single `#da-casino` channel.

## Features

- **Balances & economy** — daily credit drops, peer-to-peer transfers, and a leaderboard.
- **Blackjack** (`!blackjack` / `!bj`) — classic dealer-vs-player blackjack with betting.
- **Slots** (`!slots` / `!slot`) — 3x3 slot machine with up to 5 selectable paylines and a bet multiplier, all configured interactively via dropdowns before each spin.
- **Roulette** (`!roulette` / `!rl`) — table roulette with rendered board images.
- **Texas Hold'em** (`!holdem` / `!poker`) — multiplayer poker tables with buy-ins.
- **Video Poker** (`!videopoker` / `!vp`) — 5-card draw, Jacks or Better paytable. Hold cards, then draw.
- **Deuces Wild** (`!deuceswild` / `!dw`) — 5-card draw with wild deuces and its own paytable.
- **Pizza** (`!pizza`) — a silly cooldown-gated side game with its own leaderboard.

## Commands

| Command | Aliases | Description |
|---|---|---|
| `!ping` | | Health check |
| `!balance` | `!bal`, `!credits` | Show your credit balance |
| `!leaderboard` | `!lb`, `!top` | Show the credits leaderboard |
| `!daily` | | Claim your daily credits |
| `!transfer @user amount` | `!give`, `!pay` | Send credits to another player |
| `!blackjack bet` | `!bj` | Start a blackjack hand |
| `!slots` | `!slot` | Open the slot machine — pick paylines/multiplier, then spin |
| `!roulette` | `!rl` | Open a roulette round |
| `!holdem buy_in` | `!poker` | Start/join a hold'em table |
| `!videopoker bet` | `!vp` | Play 5-card draw video poker (Jacks or Better) |
| `!deuceswild bet` | `!dw` | Play 5-card draw video poker with wild deuces |
| `!pizza` | | Buy a pizza slice (cooldown-limited) |
| `!setcasino [#channel]` | | **Manage Server** permission required. Restrict casino commands to a channel |

By default, casino commands only work in a channel literally named `da-casino`. Server admins can run `!setcasino` (optionally with `#channel`) to point the bot at a different channel instead — this is per-server and stored in the database, so it doesn't affect other servers the bot is in.

## Setup

1. Create a Python virtual environment and install dependencies:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
2. Create a `.env` file in the project root with your bot token:
   ```
   DISCORD_TOKEN=your-discord-bot-token
   ```
3. Run the bot:
   ```bash
   python bot.py
   ```

Player balances, stats, and cooldowns are persisted to a local SQLite database (`casino.db`, managed by `db.py`) and are scoped per-server — the same player has an independent balance in each server the bot is in.

## Project layout

- `bot.py` — command registration, economy commands, bot startup
- `db.py` — SQLite persistence layer
- `game.py` — shared game/card utilities
- `views.py`, `holdem_view.py`, `roulette_view.py`, `slots_view.py`, `video_poker_view.py` — Discord UI views per game
- `cards_render.py`, `roulette_render.py`, `slots_render.py` — image rendering for card hands, the roulette board, and the slot machine cabinet
- `poker.py`, `roulette.py`, `slots.py`, `video_poker.py` — core game logic
