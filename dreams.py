"""Registry of admin-authored "dream" messages -- content-as-data like quest_items.json/
horse_clothes.json, but with one constraint no other registry has: at most one entry may be
`active` at a time (checked in _load_dreams itself, so a save that would leave two dreams active
at once is rejected the same way any other bad edit is, no extra_validators/deferred cross-module
check needed -- this is a purely self-contained rule within the one JSON file).

A player who successfully !rest's while a dream is active gets its message DM'd once, ever, per
(guild, dream) -- see try_deliver_dream. Claim tracking is a flag in db.py's generic `flags` table
(dream_claimed:<id>), not a new table -- same "it's just a flag key" idea quest-stage/NPC-presence
state already relies on. Keying by the dream's own id (not one global "has dreamed" flag) means a
*new* dream, once activated, reaches every player again.

A dream can optionally grant one item alongside its message (item_kind + item_id, both optional --
set together or not at all) -- same six kinds and store/add_inventory_item grant logic as shop.py's
REGISTRIES/buy() (equipment is always stored, never auto-equipped -- the player equips manually via
!equipment), just triggered by a dream instead of a purchase. quests.py
and housing.py are both importable here (unlike npcs.py, which can't -- see its own SHOP_KINDS
comment -- neither quests.py nor housing.py import dreams.py, so there's no cycle), so every kind's
item ids are validated directly, no deferred cross-module check needed for any of them.
"""

import asyncio
import json
import os

import discord

import db
import dungeon
import horse_clothes
import housing
import quests

_DREAMS_PATH = os.path.join(os.path.dirname(__file__), "dreams.json")
_REQUIRED_DREAM_FIELDS = {"id", "name", "message"}

# Values are zero-arg getters, not the registries themselves -- see npcs.SHOP_KINDS' own comment
# for why a captured `dungeon.MATERIALS` etc would silently go stale the moment a content edit
# landed through the admin panel with no restart.
REGISTRIES = {
    "equipment": lambda: dungeon.EQUIPMENT,
    "material": lambda: dungeon.MATERIALS,
    "consumable": lambda: dungeon.CONSUMABLES,
    "quest_item": lambda: quests.QUEST_ITEMS,
    "horse_clothes": lambda: horse_clothes.HORSE_CLOTHES,
    "housing_item": lambda: housing.HOUSING_ITEMS,
}


def _load_dreams(path: str = _DREAMS_PATH) -> dict[str, dict]:
    with open(path) as f:
        raw = json.load(f)
    dreams: dict[str, dict] = {}
    active_count = 0
    for entry in raw:
        entry_id = entry.get("id", "?")
        missing = _REQUIRED_DREAM_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"dreams.json: dream {entry_id!r} missing field(s): {sorted(missing)}")
        if entry_id in dreams:
            raise ValueError(f"dreams.json: duplicate dream id {entry_id!r}")
        item_kind, item_id = entry.get("item_kind"), entry.get("item_id")
        if bool(item_kind) != bool(item_id):
            raise ValueError(
                f"dreams.json: dream {entry_id!r} must set both item_kind and item_id together, or neither"
            )
        if item_kind is not None:
            if item_kind not in REGISTRIES:
                raise ValueError(f"dreams.json: dream {entry_id!r} has unknown item_kind {item_kind!r}")
            if item_id not in REGISTRIES[item_kind]():
                raise ValueError(
                    f"dreams.json: dream {entry_id!r} item_id {item_id!r} not found in {item_kind} registry"
                )
        if entry.get("active"):
            active_count += 1
        dreams[entry_id] = entry
    if active_count > 1:
        raise ValueError(f"dreams.json: only one dream can be active at a time, found {active_count}")
    return dreams


DREAMS = _load_dreams()


def active_dream() -> dict | None:
    """The one currently-active dream, or None if no dream is active right now -- relies on
    _load_dreams' at-most-one-active guarantee, so there's never more than one match here."""
    return next((d for d in DREAMS.values() if d.get("active")), None)


async def try_deliver_dream(dm_send, guild_id: int, user_id: int) -> bool:
    """Attempts to DM the currently active dream (if any) to this player, once ever per (guild,
    dream) -- granting its optional item alongside it. `dm_send` is an async callable like
    discord.Member.send/discord.User.send -- mirrors achievements.try_award_many's own `send`
    param, same "caller supplies the Discord-facing bit, this module stays decoupled from needing a
    real discord.Member" reasoning. Returns whether it was actually delivered -- False if there's
    no active dream right now, or this player already claimed it.

    Claims the flag BEFORE sending (atomically, via db.set_flag_if_zero) so a doubled-up call can't
    send twice. The item grant itself happens AFTER a successful send, not before -- only the
    (pure, side-effect-free) lookup of what the item is happens early, so the DM text can describe
    it. If the DM fails (discord.Forbidden -- the player has DMs off), the flag is rolled back and
    nothing is granted, so a retry after they fix their DM settings grants it exactly once, not
    twice -- same "lost a race, give it back" shape quests.turn_in already uses for a stale
    double-click on an inventory item."""
    dream = active_dream()
    if dream is None:
        return False
    flag_key = f"dream_claimed:{dream['id']}"
    claimed = await asyncio.to_thread(db.set_flag_if_zero, guild_id, user_id, flag_key, 1)
    if not claimed:
        return False

    item_kind = dream.get("item_kind")
    item = REGISTRIES[item_kind]()[dream["item_id"]] if item_kind else None

    description = dream["message"]
    if item is not None:
        if item_kind == "equipment":
            description += f"\n\n⚔️ You wake up holding **{item['name']}** — stored in `!equipment`."
        else:
            description += f"\n\n🎁 You wake up holding **{item['name']}**! Check `!inventory`."

    try:
        embed = discord.Embed(title="💭 A Dream", description=description, color=discord.Color.purple())
        await dm_send(embed=embed)
    except discord.Forbidden:
        await asyncio.to_thread(db.set_flag, guild_id, user_id, flag_key, 0)
        return False

    if item is not None:
        if item_kind == "equipment":
            await asyncio.to_thread(db.store_equipment_item, guild_id, user_id, item["id"])
        else:
            await asyncio.to_thread(db.add_inventory_item, guild_id, user_id, item["id"], 1)

    return True
