# da-casino

A Discord casino bot built with [discord.py](https://discordpy.readthedocs.io/). Members earn virtual credits and play blackjack, slots, roulette, video poker, Texas hold'em, and horse racing against each other — plus a dungeon RPG and horse ranch side game with crafting, quests, and NPCs — all restricted to a single `#da-casino` channel (or a channel of the server's choosing).

Most of the game's *content* — monsters, equipment, materials, recipes, skills, delves, quest items, quests, NPCs, and rooms — lives in JSON files and is editable through a built-in, password-gated web content editor, without touching Python or restarting the bot.

## Features

- **Balances & economy** — daily credit claim (`!rest`, alongside energy refill), a two-step mining minigame, peer-to-peer tips/transfers, and leaderboards.
- **Blackjack** (`!blackjack` / `!bj`) — persistent multi-round tables: join, bet, and the table deals back-to-back hands, checking in with everyone between rounds to keep or change their bet.
- **Slots** (`!slots` / `!slot`) — 3x3 slot machine with up to 5 selectable paylines and a bet multiplier, all configured interactively via dropdowns before each spin.
- **Roulette** (`!roulette` / `!rl`) — table roulette with rendered board/wheel images, covering straight, split, column, dozen, and multi-number bets, plus a one-click Repeat Bets option.
- **Texas Hold'em** (`!holdem` / `!poker`) — multiplayer poker tables with buy-ins.
- **Video Poker** (`!videopoker` / `!vp`) — 5-card draw, Jacks or Better paytable. Hold cards, then draw.
- **Deuces Wild** (`!deuceswild` / `!dw`) — 5-card draw with wild deuces and its own paytable.
- **Horse Racing & Ranch** (`!horserace` / `!horses`, `!train`, `!boost`, `!facility`) — bet on races between a fixed roster of legend horses, or buy in as an owner (legends or cheap unraced foals you train up yourself), upgrade a permanent training facility, and queue stat-boost items to earn a cut whenever your horse wins.
- **Dungeon RPG** (`!class`, `!delve`, `!craft`) — pick a permanent character (a class from face cards, a subclass from suits — 16 builds total) and delve a small daily dungeon for a class-biased, push-your-luck payout: bank your loot after any room, or push deeper for a bigger — riskier — haul. Craft gear/consumables from materials you find, and manage your inventory/equipment.
- **Rooms, NPCs & Quests** (`/play`, `!quests`) — a private, navigable hub (Town Square ↔ Casino/Ranch/Dungeon) with NPCs to talk to and Morrowind-style multi-stage quests to complete by turning in items, hitting kill/craft counts, or other conditions — entirely data-driven (see below).
- **Achievements** (`!achievements`) — one-off "first on the server" achievements plus tiered win/loss-count achievements per game, each with a small credit reward.
- **Stats** (`!stats`) — a personal rundown: balance, class, per-game win/loss record, lifetime winnings, and horses owned.
- **Pizza** (`!pizza`) — a silly cooldown-gated side game with its own leaderboard.
- **Rub for luck** (`!rub`) — a once-daily cosmetic Luck stat nudge: you get luckier, someone else (weighted-random) gets less lucky.

## The `/play` hub and the room/NPC/quest system

`/play` opens a private, ephemeral menu the player navigates like a tiny top-down game: Town Square connects out to the Casino, Ranch, and Dungeon, each rendered from a banner image with buttons for that room's commands, any NPCs currently present, and exits to other rooms. Every room is authored as a `rooms.json` entry (background image, exits, which commands live there); every NPC is a `npcs.json` entry (which room they're in, their greeting, an optional achievement, and an optional visibility condition — e.g. an NPC who only appears once a quest is complete).

Quests (`quests.json`) work like Morrowind's journal: each is a numbered sequence of stages tied to one NPC, and a stage advances when its trigger condition is met — turning in a quest item, reaching a kill or craft count, having already earned an achievement, completing another quest, or a generic "flag at least N" escape hatch. Talking to an NPC shows whatever's currently relevant (their static greeting, or the current stage of any active quest with them); a "turn in" button appears once a stage's condition is satisfied.

## Content editor

A built-in web UI (password-gated, served on the same process as the bot) lets you add or edit almost every piece of game content — monsters, equipment, materials, consumables, recipes, skills, delves, quest items, quests, NPCs, and rooms — without writing code or restarting the bot. Every content type reuses its own module's real loader as the save-time validator (write to a temp file, try loading it for real, only replace the live file and hot-reload if that succeeds), so a bad edit can't corrupt content that's currently working. A code change (editing `.py` files) still needs a bot restart to take effect; JSON content edits made through this panel apply immediately to the live bot.

## Commands

| Command | Aliases | Description |
|---|---|---|
| `!ping` | | Health check (bot owner only) |
| `!help [command]` | | List every command by category, or details on one |
| `/play` | | Open the private room hub (Casino/Ranch/Dungeon, NPCs, quests) |
| `!balance` | `!bal`, `!credits` | Show your credit balance |
| `!stats` | | Personal rundown: balance, class, win/loss record, lifetime winnings, horses owned |
| `!leaderboard` | `!lb`, `!top` | Top credit holders, top pizza buyers, luckiest, biggest single-bet win/loss |
| `!rest` | | Claim your daily credits and refill your dungeon energy (once per day) |
| `!mine` | | Two-step dig: start, then collect ~10 minutes later (cooldown after collecting) |
| `!tip @user` | | Tip another user credits generated fresh, once a day |
| `!transfer @user amount` | `!give`, `!pay` | Send credits to another player |
| `!rub` | | Rub your belly for luck (once per day) — you get luckier, someone else gets less lucky |
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
| `!train [number]` | | Train a horse you own once a day — blank picks from a dropdown of your horses |
| `!boost <number> <stat>` | | Buy a training-boost item for a horse, queued for its next training (`speed`/`endurance`/`spirit`) |
| `!facility [buy]` | | Check or upgrade your ranch's permanent training facility |
| `!class` | | Pick your permanent dungeon class/subclass, or check your current one |
| `!delve` | `!dungeon` | Delve today's dungeon level for a class-biased, push-your-luck payout (costs 1 ⚡ energy) |
| `!craft` | | Craft gear or consumables from materials you've found |
| `!inventory` | | See your quest items and dungeon gear (equipped + stored) |
| `!equipment` | | Equip, unequip, or swap in stored dungeon gear per slot |
| `!quests` | | See your active and completed quests |
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
2. Create a `.env` file in the project root:
   ```
   DISCORD_TOKEN=your-discord-bot-token
   ADMIN_PANEL_PASSWORD=some-password-for-the-content-editor
   ACTIVITY_SERVER_PORT=8787
   ```
   `ADMIN_PANEL_PASSWORD` gates the content editor; `ACTIVITY_SERVER_PORT` is optional (defaults to `8787`).
3. Run the bot:
   ```bash
   python bot.py
   ```
   This also starts the content editor (on `ACTIVITY_SERVER_PORT`) on the same process/event loop.

Player balances, stats, cooldowns, and per-player flags (quest progress, etc.) are persisted to a local SQLite database (`casino.db`, managed by `db.py`) and are scoped per-server — the same player has an independent balance in each server the bot is in.

In production this repo runs under systemd as `da-casino-bot.service`; deploying a code change is `sudo systemctl restart da-casino-bot`. Content edits made through the web editor apply live and never require a restart.

## Project layout

- `bot.py` — command registration, economy commands, bot startup, admin server bring-up
- `db.py` — SQLite persistence layer, including the generic per-player `flags` table backing quest progress and other counted state
- `game.py` — shared game/card utilities
- `achievements.py` — the achievement registry (one-off and tiered) and award/announce logic
- `quests.py` / `quests.json` / `quest_items.json` — the quest/trigger system (Morrowind-style journal stages) and quest item registry
- `npcs.py` / `npcs.json`, `npc_view.py`, `npc_render.py` — the NPC registry, generic talk/turn-in buttons, and shared dialogue-bubble rendering
- `rooms.py` / `rooms.json`, `room_view.py`, `room_commands.py` — the room registry (background, exits, commands) and the generic `/play` hub renderer/navigation
- `casino_view.py`, `ranch_view.py`, `dungeon_view.py` — each room's `specialization` hook (extra embed fields / extra components a generic room can't derive on its own, e.g. Casino's game picker, Ranch's horse roster)
- `crafting.py` / `crafting_view.py`, `dungeon_recipes.json`, `dungeon_materials.json`, `dungeon_consumables.json` — the crafting system and its content registries
- `admin_server.py` / `admin_schemas.py` — the password-gated web content editor (generic CRUD over every JSON-backed content type)
- `blackjack_view.py`, `holdem_view.py`, `roulette_view.py`, `slots_view.py`, `video_poker_view.py`, `horserace_view.py` — Discord UI views per game
- `cards_render.py`, `roulette_render.py`, `slots_render.py`, `horserace_render.py`, `dungeon_render.py` — image rendering per game (card hands, the roulette board, the slot cabinet, the race track, the dungeon corridor)
- `poker.py`, `roulette.py`, `slots.py`, `video_poker.py`, `horserace.py`, `dungeon.py` — core game logic
- `dungeon_monsters.json`, `dungeon_equipment.json`, `dungeon_skills.json`, `dungeon_delves.json` — dungeon content (monster stats/loot, equipment, skills, delve room layouts), loaded and validated at startup — add an entry via the content editor, no code change needed

See `CLAUDE.md` for the architectural conventions and design decisions behind the content-is-data system, the trigger/flag system, and the rules around what a room's view is and isn't allowed to do.
