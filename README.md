# da-casino

A Discord casino bot built with [discord.py](https://discordpy.readthedocs.io/). Members earn virtual credits and play blackjack, slots, roulette, and Texas hold'em against each other, all restricted to a single `#da-casino` channel.

## Features

- **Balances & economy** — daily credit drops, peer-to-peer transfers, and a leaderboard.
- **Blackjack** (`!blackjack` / `!bj`) — classic dealer-vs-player blackjack with betting.
- **Slots** (`!slots` / `!slot`) — spin-based slot machine with configurable max bet.
- **Roulette** (`!roulette` / `!rl`) — table roulette with rendered board images.
- **Texas Hold'em** (`!holdem` / `!poker`) — multiplayer poker tables with buy-ins.
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
| `!slots bet` | `!slot` | Spin the slot machine |
| `!roulette` | `!rl` | Open a roulette round |
| `!holdem buy_in` | `!poker` | Start/join a hold'em table |
| `!pizza` | | Buy a pizza slice (cooldown-limited) |

All commands only work inside a channel literally named `da-casino`.

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

Player balances, stats, and cooldowns are persisted to a local SQLite database (`casino.db`, managed by `db.py`).

## Project layout

- `bot.py` — command registration, economy commands, bot startup
- `db.py` — SQLite persistence layer
- `game.py` — shared game/card utilities
- `views.py`, `holdem_view.py`, `roulette_view.py`, `slots_view.py` — Discord UI views per game
- `cards_render.py`, `roulette_render.py` — image rendering for card hands and the roulette board
- `poker.py`, `roulette.py`, `slots.py` — core game logic
