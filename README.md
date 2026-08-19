# da-casino

A Discord casino bot built with [discord.py](https://discordpy.readthedocs.io/). Members earn virtual credits and play blackjack, slots, roulette, video poker, Texas hold'em, and horse racing against each other — plus a dungeon RPG side game — all restricted to a single `#da-casino` channel (or a channel of the server's choosing).

## Features

- **Balances & economy** — daily credit drops, a two-step mining minigame, peer-to-peer tips/transfers, and leaderboards.
- **Blackjack** (`!blackjack` / `!bj`) — persistent multi-round tables: join, bet, and the table deals back-to-back hands, checking in with everyone between rounds to keep or change their bet.
- **Slots** (`!slots` / `!slot`) — 3x3 slot machine with up to 5 selectable paylines and a bet multiplier, all configured interactively via dropdowns before each spin.
- **Roulette** (`!roulette` / `!rl`) — table roulette with rendered board/wheel images, covering straight, split, column, dozen, and multi-number bets, plus a one-click Repeat Bets option.
- **Texas Hold'em** (`!holdem` / `!poker`) — multiplayer poker tables with buy-ins.
- **Video Poker** (`!videopoker` / `!vp`) — 5-card draw, Jacks or Better paytable. Hold cards, then draw.
- **Deuces Wild** (`!deuceswild` / `!dw`) — 5-card draw with wild deuces and its own paytable.
- **Horse Racing** (`!horserace` / `!horses`) — bet on races between a fixed roster of legend horses, or buy in as an owner (legends or cheap unraced foals you train up yourself) to earn a cut whenever your horse wins.
- **Dungeon RPG** (`!class`, `!delve`) — pick a permanent character (a class from face cards, a subclass from suits — 16 builds total) and delve a small daily dungeon for a class-biased, push-your-luck payout: bank your loot after any room, or push deeper for a bigger — riskier — haul.
- **Achievements** (`!achievements`) — one-off "first on the server" achievements plus tiered win/loss-count achievements per game, each with a small credit reward.
- **Stats** (`!stats`) — a personal rundown: balance, class, per-game win/loss record, lifetime winnings, and horses owned.
- **Pizza** (`!pizza`) — a silly cooldown-gated side game with its own leaderboard.

## Commands

| Command | Aliases | Description |
|---|---|---|
| `!ping` | | Health check (bot owner only) |
| `!help [command]` | | List every command by category, or details on one |
| `!balance` | `!bal`, `!credits` | Show your credit balance |
| `!stats` | | Personal rundown: balance, class, win/loss record, lifetime winnings, horses owned |
| `!leaderboard` | `!lb`, `!top` | Top credit holders, top pizza buyers, biggest single-bet win/loss |
| `!daily` | | Claim your daily credits |
| `!mine` | | Two-step dig: start, then collect ~10 minutes later (cooldown after collecting) |
| `!tip @user` | | Tip another user credits generated fresh, once a day |
| `!transfer @user amount` | `!give`, `!pay` | Send credits to another player |
| `!blackjack bet` | `!bj` | Open/join a blackjack table |
| `!slots` | `!slot` | Open the slot machine — pick paylines/multiplier, then spin |
| `!roulette` | `!rl` | Open a roulette round |
| `!holdem buy_in` | `!poker` | Start/join a hold'em table |
| `!videopoker bet` | `!vp` | Play 5-card draw video poker (Jacks or Better) |
| `!deuceswild bet` | `!dw` | Play 5-card draw video poker with wild deuces |
| `!horserace` | `!horse`, `!race` | Open a horse race others can bet on before it runs |
| `!horses` | `!stable` | List every horse, its odds/price/record, and its owner |
| `!buyhorse <number>` | | Buy an unowned legend horse |
| `!buyfoal <name>` | | Buy and name a cheap, unraced foal |
| `!renamehorse <number> <name>` | | Rename a horse you own |
| `!train <number>` | | Train a horse you own once a day |
| `!class` | | Pick your permanent dungeon class/subclass, or check your current one |
| `!delve` | `!dungeon` | Delve today's dungeon level for a class-biased, push-your-luck payout |
| `!achievements` | `!achievement` | Show the achievements you've earned |
| `!pizza` | | Buy a pizza slice (cooldown-limited) |
| `!setcasino [#channel]` | | **Manage Server** permission required. Restrict casino commands to a channel |
| `!setcurrency <name>` | | **Manage Server** permission required. Rename this server's currency |

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
- `achievements.py` — the achievement registry (one-off and tiered) and award/announce logic
- `blackjack_view.py`, `holdem_view.py`, `roulette_view.py`, `slots_view.py`, `video_poker_view.py`, `horserace_view.py`, `dungeon_view.py` — Discord UI views per game
- `cards_render.py`, `roulette_render.py`, `slots_render.py`, `horserace_render.py`, `dungeon_render.py` — image rendering per game (card hands, the roulette board, the slot cabinet, the race track, the dungeon corridor)
- `poker.py`, `roulette.py`, `slots.py`, `video_poker.py`, `horserace.py`, `dungeon.py` — core game logic
- `dungeon_monsters.json` — dungeon monster content (stats, loot, placeholder art), loaded and validated at startup — add a monster by adding an entry, no code change needed
