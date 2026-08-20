"""Framework for multi-stage NPC storylines: an achievement unlock starts a quest, a dungeon
delve can drop the item a stage is waiting on, and turning that item in to the right NPC advances
the stage. New quests are added as data in QUESTS below (mirroring achievements.py's ACHIEVEMENTS
and dungeon.py's EQUIPMENT registry) -- no new plumbing needed per quest.

Stages currently advance by item turn-in only. A stage with no "turn_in_item" is a dialogue-only
endpoint (the quest's current end, until more stages are appended).
"""

import asyncio
import json
import os
import random

import db
import dungeon

_QUEST_ITEMS_PATH = os.path.join(os.path.dirname(__file__), "quest_items.json")
_REQUIRED_ITEM_FIELDS = {"id", "name", "emoji", "description"}

# Shown once every stage is turned in, for a quest with no quest-level "complete_message" of its
# own -- most quests won't bother writing one until/unless there's a reason to (a follow-up
# stage, a reveal, ...).
DEFAULT_COMPLETE_MESSAGE = "\"...\" (There's nothing more for now -- come back later.)"

QUEST_ITEM_DROP_CHANCE = 1.0  # guaranteed -- a quest stage's own monster/inventory gating (see
# roll_item_drop) already controls when an item is even eligible to drop; stacking a random
# chance on top of that just meant grinding the right monster repeatedly for no story reason


def _load_quest_items(path: str = _QUEST_ITEMS_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    items: dict[str, dict] = {}
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_ITEM_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"quest_items.json: item {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in items:
            raise ValueError(f"quest_items.json: duplicate item id {entry_id!r}")
        items[entry_id] = entry
    return items


QUEST_ITEMS = _load_quest_items()

# Each stage: "prompt" (NPC dialogue shown while this stage is active), "turn_in_item" (item id
# that advances it, or absent for a dialogue-only endpoint), "on_complete_message", and either a
# currency "reward" or an equipment "reward_item" (id into dungeon.EQUIPMENT -- equipped via the
# same is_upgrade rule as ordinary loot, see turn_in). "drop_monster" optionally restricts which
# dungeon.MONSTERS id can drop turn_in_item (absent = any monster, see roll_item_drop). Later
# stages are just appended to a quest's list.
QUESTS = [
    {
        "id": "kel_romance",
        "npc": "kel",
        "start_achievement": "love_in_bloom",
        "stages": [
            {
                "prompt": (
                    "Kel scuffs his boot in the dirt. \"...I used to whittle, you know. Haven't "
                    "picked it back up in years. Lost the last thing I ever carved, out past the "
                    "training grounds somewhere. Silly to miss a piece of wood this much.\""
                ),
                "turn_in_item": "wooden_horse_carving",
                "on_complete_message": (
                    "...You're giving this to me? It's beautiful.\" "
                    "\"Thank you. Really.\""
                ),
                "reward": 0,
            },
        ],
    },
    {
        "id": "mondor_goblin_chieftain",
        "npc": "mondor",
        "start_achievement": "dared_by_mondor",
        "complete_message": "You have returned the \"Princess\" to her rightful place at Mondor's side.",
        "stages": [
            {
                "prompt": (
                    "THE GOBLIN CHIEFTAIN HAS KIDNAPPED THE PRINCESS THAT IS NOT THREE RATS IN A "
                    "TRENCHCOAT. SAVE HER BRAVE ADVENTURER AND YOU WILL BE REWARDED HANDSOMELY."
                ),
                "turn_in_item": "three_rats_in_a_trenchcoat",
                "drop_monster": "goblin_chief",
                "on_complete_message": (
                    "YOU HAVE DONE WELL ADVENTURER. PLEASE TAKE THIS SLIPPERY PENCIL AND MY "
                    "THANKS. OW SHE IS BITING ME JESUS CHRIST."
                ),
                "reward_item": "greasy_pencil",
            },
        ],
    },
]

BY_ID = {quest["id"]: quest for quest in QUESTS}
BY_NPC: dict[str, list[dict]] = {}
for _quest in QUESTS:
    BY_NPC.setdefault(_quest["npc"], []).append(_quest)


async def maybe_start_quests(guild_id: int, user_id: int, unlocked_kinds: list[str]):
    """Starts every quest whose start_achievement is in unlocked_kinds. Meant to be called right
    after achievements.try_award_many computes what a user just unlocked, so quest-starts work
    from every existing achievement call site without each one needing to know about quests."""
    for quest in QUESTS:
        if quest["start_achievement"] in unlocked_kinds:
            await asyncio.to_thread(db.start_quest, guild_id, user_id, quest["id"])


async def quest_log(guild_id: int, user_id: int) -> list[dict]:
    """Every quest this player has started (in QUESTS order), each as {"quest_id", "npc",
    "stage_index", "total_stages", "complete", "prompt"} -- prompt is the current stage's own
    text (or None once complete). Backs !quests; deliberately doesn't invent quest titles or any
    other copy -- npc id and each stage's existing prompt are the only text surfaced."""
    progress = await asyncio.to_thread(db.get_all_quest_progress, guild_id, user_id)
    entries = []
    for quest in QUESTS:
        stage_index = progress.get(quest["id"])
        if stage_index is None:
            continue
        complete = stage_index >= len(quest["stages"])
        if complete:
            prompt = quest.get("complete_message", DEFAULT_COMPLETE_MESSAGE)
        else:
            prompt = quest["stages"][stage_index]["prompt"]
        entries.append({
            "quest_id": quest["id"],
            "npc": quest["npc"],
            "stage_index": stage_index,
            "total_stages": len(quest["stages"]),
            "complete": complete,
            "prompt": prompt,
        })
    return entries


async def roll_item_drop(guild_id: int, user_id: int, room_index: int, monster_id: str) -> dict | None:
    """None most of the time. When it hits, adds one quest item to the player's inventory and
    returns it -- but only an item their *current* stage on some in-progress quest is actually
    waiting on, so players not on that quest never see unrelated flavor items. A stage with
    "drop_monster" set only offers its item after killing that specific dungeon.MONSTERS id;
    room_index is accepted for symmetry with dungeon.roll_equipment_drop's call site (no quest
    item is tier-gated, only monster-gated)."""
    candidates = []
    for quest in QUESTS:
        stage_index = await asyncio.to_thread(db.get_quest_stage, guild_id, user_id, quest["id"])
        if stage_index is None or stage_index >= len(quest["stages"]):
            continue
        stage = quest["stages"][stage_index]
        item_id = stage.get("turn_in_item")
        if item_id is None:
            continue
        required_monster = stage.get("drop_monster")
        if required_monster is not None and required_monster != monster_id:
            continue
        held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
        if held.get(item_id, 0) > 0:
            continue  # already holding it, waiting on the turn-in itself
        candidates.append(item_id)

    if not candidates or random.random() > QUEST_ITEM_DROP_CHANCE:
        return None

    item_id = random.choice(candidates)
    await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, item_id)
    return QUEST_ITEMS[item_id]


async def talk_to_npc(guild_id: int, user_id: int, npc_id: str) -> dict:
    """Returns {"active", "prompt", "can_turn_in", "item", "complete_quest_id"} describing
    whatever quest+stage the player is on with this NPC. "active" is False if they have no quest
    started with this NPC at all (nothing to show). A stage with no turn-in requirement
    (dialogue-only endpoint) reports can_turn_in False. complete_quest_id is the id of whichever
    quest just reported its complete_message (None otherwise) -- lets a caller key a one-off
    visual (e.g. a reveal sprite) to one specific quest rather than "any NPC quest is done".

    Self-healing: if a quest's start_achievement is already earned but the quest was never
    started (e.g. the achievement was claimed before this quest existed in QUESTS, or any other
    gap in maybe_start_quests firing at unlock time), starts it here rather than reporting
    "nothing to show" for someone who's actually eligible."""
    result = {"active": False, "prompt": None, "can_turn_in": False, "item": None, "complete_quest_id": None}
    earned = None
    for quest in BY_NPC.get(npc_id, []):
        stage_index = await asyncio.to_thread(db.get_quest_stage, guild_id, user_id, quest["id"])
        if stage_index is None:
            if earned is None:
                earned = await asyncio.to_thread(db.get_user_personal_achievements, guild_id, user_id)
            if quest["start_achievement"] not in earned:
                continue
            await asyncio.to_thread(db.start_quest, guild_id, user_id, quest["id"])
            stage_index = 0
        result["active"] = True
        if stage_index >= len(quest["stages"]):
            result["prompt"] = quest.get("complete_message", DEFAULT_COMPLETE_MESSAGE)
            result["complete_quest_id"] = quest["id"]
            continue
        stage = quest["stages"][stage_index]
        result["prompt"] = stage["prompt"]
        item_id = stage.get("turn_in_item")
        if item_id is None:
            continue
        held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
        if held.get(item_id, 0) > 0:
            result["can_turn_in"] = True
            result["item"] = QUEST_ITEMS[item_id]
    return result


async def turn_in(guild_id: int, user_id: int, npc_id: str) -> dict:
    """Resolves whatever quest+stage the player is on with this NPC and, if they're holding the
    item that stage wants, consumes it and advances the stage. Returns {"success", "message",
    "reward", "reward_item", "equipped", "quest_complete"} -- success is False (everything else
    None/0/False) if there's nothing to turn in. reward_item is the dungeon.EQUIPMENT dict if
    this stage grants one (None otherwise); equipped says whether it actually got equipped (same
    is_upgrade rule as ordinary loot) -- if not, it's stored in equipment_inventory instead, swappable later via
    !equipment rather than lost."""
    for quest in BY_NPC.get(npc_id, []):
        stage_index = await asyncio.to_thread(db.get_quest_stage, guild_id, user_id, quest["id"])
        if stage_index is None or stage_index >= len(quest["stages"]):
            continue
        stage = quest["stages"][stage_index]
        item_id = stage.get("turn_in_item")
        if item_id is None:
            continue
        consumed = await asyncio.to_thread(db.consume_inventory_item, guild_id, user_id, item_id)
        if not consumed:
            continue
        advanced = await asyncio.to_thread(db.advance_quest_stage, guild_id, user_id, quest["id"], stage_index)
        if not advanced:
            # Lost a race (e.g. a double-clicked button) -- give the item back rather than eat it.
            await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, item_id)
            continue

        reward = stage.get("reward", 0)
        if reward:
            await asyncio.to_thread(db.update_balance, guild_id, user_id, reward)

        reward_item, equipped = None, False
        reward_item_id = stage.get("reward_item")
        if reward_item_id:
            reward_item = dungeon.EQUIPMENT[reward_item_id]
            equipped_items = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
            slot = reward_item["slot"]
            if dungeon.is_upgrade(equipped_items.get(slot), reward_item):
                await asyncio.to_thread(db.equip_item_smart, guild_id, user_id, slot, reward_item_id)
                equipped = True
            else:
                await asyncio.to_thread(db.store_equipment_item, guild_id, user_id, reward_item_id)

        return {
            "success": True, "message": stage.get("on_complete_message"), "reward": reward,
            "reward_item": reward_item, "equipped": equipped,
            "quest_complete": stage_index + 1 >= len(quest["stages"]),
        }

    return {
        "success": False, "message": None, "reward": 0, "reward_item": None, "equipped": False,
        "quest_complete": False,
    }
