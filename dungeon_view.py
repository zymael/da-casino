import asyncio
import random

import discord

import db
import dungeon
import dungeon_render
import hub_ui
import moon
import quests
from holdem_view import busy_players

# Secret moon nudge (see moon.py) applied to combat/loot rolls -- never surfaced to players.
DUNGEON_MOON_SHIFT = 0.08


def _moon_combat_multiplier(effect: str | None, favors: str) -> float:
    """1.0 normally; nudged by DUNGEON_MOON_SHIFT when tonight is dungeon's secret moon night and
    `favors` ("player" or "monster") is on the winning side of it."""
    if effect is None:
        return 1.0
    on_our_side = (effect == "player") == (favors == "player")
    return 1 + DUNGEON_MOON_SHIFT if on_our_side else 1 - DUNGEON_MOON_SHIFT

# user_id -> whatever delve-related thing currently has that player reserved, so a player can
# only be in one at a time (across any channel, matching how busy_players itself is unscoped by
# channel/guild). A party delve's PartyLobby/PartyDelveSession registers ALL its member ids here,
# each pointing at the same shared object -- see _cleanup, which releases every id an entry has
# at once regardless of which of the three types it is.
active_delves: dict[int, "DelveSession | PartyLobby | PartyDelveSession"] = {}

CLASS_OPTIONS = [
    ("fighter", "Fighter (Ace)", "Tank — high HP/DEF. Skill varies by subclass."),
    ("healer", "Healer (King)", "Support — balanced spread. Skill varies by subclass."),
    ("mage", "Mage (Queen)", "High ATK, fragile. Skill varies by subclass."),
    ("rogue", "Rogue (Jack)", "Balanced/quick. Skill varies by subclass."),
]
SUBCLASS_OPTIONS = [
    ("clubs", "♣ Brawler", "More HP/ATK."),
    ("spades", "♠ Lethal", "More ATK, less DEF."),
    ("hearts", "♥ Loyal", "More HP/DEF."),
    ("diamonds", "♦ Greedy", "Better loot rolls."),
]


class MonsterInstance:
    """One live monster within a rolled encounter group -- `slot` is this instance's stable index
    within the group it was rolled into (0..len(group)-1), used as the Target Select's option value
    and as the lookup key for "which monster is this", since a group's monster ids are not required
    to be unique (two goblins of the same kind is legal, see dungeon.py's module docstring)."""

    def __init__(self, monster: dict, slot: int):
        self.monster = monster
        self.hp = monster["hp"]
        self.max_hp = monster["hp"]
        # Real per-instance mutable combat stats, snapshotted from `monster` -- unlike hp/max_hp
        # (always instance-specific), atk/def/spatk/spdef used to be read straight off the shared
        # `monster` content dict everywhere, which can't be mutated (the same dict may be reused
        # across encounters). Giving a monster its own copy is what lets a monster's own skill use
        # atk_buff/def_buff/etc (previously impossible -- there was nothing to mutate), and lets a
        # player's def_shred/atk_debuff/etc land on a specific instance, not the shared content.
        self.atk = monster["atk"]
        self.def_ = monster["def"]
        self.spatk = monster["spatk"]
        self.spdef = monster["spdef"]
        self.speed = monster["spd"]
        self.atk_debuff = self.def_debuff = self.spatk_debuff = self.spdef_debuff = self.speed_debuff = 0
        # Temporary (N-round) effects -- dodge_buff/resist_buff/dot/hot -- each a
        # {"type", "value", "remaining"} dict, ticked by _tick_timed_effects. Unlike the debuffs
        # above (permanent for the fight), these expire and are refreshed-not-stacked (see
        # _apply_timed_effect).
        self.timed_effects: list[dict] = []
        # Turn-order scheduling state (dungeon.preview_next_turns) -- reset to 0 at the same point
        # Chips/used_item_effects already reset (fight start). guard_charge is a one-shot "absorb
        # the next hit" charge (set by _effect_guard, consumed and cleared wherever damage is next
        # applied to this entity) -- NOT a timed_effects duration entry, since Guard has no
        # duration, just "until the next hit lands."
        self.turn_clock = 0.0
        self.guard_charge: float | None = None
        self.slot = slot
        # Party-only threat table: {user_id: accumulated threat}, this monster's own view of who
        # it's most likely to attack (dungeon.pick_target_by_threat, used by _advance_party_turns).
        # A missing entry means "hasn't drawn any of this monster's aggro yet" (implicit 0), not an
        # error -- see dungeon.THREAT_PER_DAMAGE/taunt/lower_threat for what raises it. Freshly
        # empty every time a MonsterInstance is built (every combat room is a fresh fight), so
        # there's no separate per-room reset needed the way chips/turn_clock get elsewhere. Solo
        # never reads this (a monster always attacks the sole player directly there), so it just
        # sits inert for a solo MonsterInstance.
        self.threat: dict[int, float] = {}


def _roll_monster_instances(room: dict) -> list[MonsterInstance]:
    return [MonsterInstance(m, slot) for slot, m in enumerate(dungeon.monsters_for_room(room))]


class DelveSession:
    def __init__(self, guild_id: int, user_id: int, character: dict, equipped: dict[str, str], delve: dict):
        self.guild_id = guild_id
        self.user_id = user_id
        self.main_class = character["main_class"]
        self.subclass = character["subclass"]
        self.level = character["level"]
        self.equipped = equipped
        effective = dungeon.compute_effective_stats(character, equipped)
        self.max_hp = effective["hp"]
        self.atk = effective["atk"]
        self.def_ = effective["def"]
        self.spatk = effective["spatk"]
        self.spdef = effective["spdef"]
        self.speed = effective["speed"]
        # Symmetric to MonsterInstance's own debuff/timed_effects fields -- a player never had a
        # debuff before (only a monster's def_debuff existed), needed now that a monster's own
        # skill can weaken the player, same full parity as the buff side already had.
        self.atk_debuff = self.def_debuff = self.spatk_debuff = self.spdef_debuff = self.speed_debuff = 0
        self.timed_effects: list[dict] = []
        # Turn-order scheduling state -- see MonsterInstance's own fields for what these mean;
        # reset at the same points Chips/used_item_effects already reset.
        self.turn_clock = 0.0
        self.guard_charge: float | None = None
        # Equipped item ids whose on_use effect has already been cast this fight -- reset
        # alongside chips at every combat-room entry (see _goto_room), same "once per fight"
        # reset point.
        self.used_item_effects: set[str] = set()
        self.loot_mult = dungeon.SUBCLASSES[self.subclass]["loot_mult"]
        self.display_name = dungeon.display_name(self.main_class, self.subclass)
        # Level-1 skill is guaranteed to exist for every build (validated at import time in
        # dungeon.py) and is always sorted first, so unlocked_skills is never empty.
        self.unlocked_skills = dungeon.unlocked_skills(self.main_class, self.subclass, self.level)

        # Which delve this session is running -- a graph of rooms addressed by id (not a flat
        # sequence), see dungeon.DELVES/dungeon.rooms_by_id. current_room_id/rooms_visited replace
        # the old room_index/len(rooms) -- a branching graph has no single well-defined "room N of
        # Y" the way a flat list did (different forks can have different lengths, and a room can
        # even be revisited via a dead-end self-loop), so rooms_visited just counts transitions.
        self.delve = delve
        self.rooms_by_id = dungeon.rooms_by_id(delve)
        self.current_room_id = delve["start_room"]
        self.rooms_visited = 1

        # Starts wherever the character's last delve left off (db.set_current_hp), clamped to the
        # current max in case max_hp grew since -- HP no longer auto-refills to full just from
        # starting a fresh delve, only !rest or an in-combat heal raises it.
        self.hp = min(character["current_hp"], self.max_hp)
        self.loot_total = 0

        # monsters/current_target_slot/chips are combat-room-only state -- empty/0 if the start
        # room happens to be a non-combat "choice" room instead (see dungeon.py's room shape).
        # _goto_room (re)populates these on every transition into a combat room. max_chips is
        # derived live from main_class/subclass (dungeon.compute_stats), not persisted -- a
        # fight's Chips pool refills to max at the start of every fight, same reset points the old
        # once-per-fight ability_used flag used.
        start_room = self.rooms_by_id[self.current_room_id]
        self.monsters: list[MonsterInstance] = _roll_monster_instances(start_room) if start_room["type"] == "combat" else []
        self.current_target_slot = 0
        self.max_chips = dungeon.compute_stats(self.main_class, self.subclass)["chips"]
        self.chips = self.max_chips

        self.message: discord.Message | None = None
        # Which view is currently the "live" one attached to `message` -- lets a stale view's
        # on_timeout (from a room/fight the player has already moved past) recognize it's been
        # superseded and no-op, since there's no single coroutine blocking on each view in turn
        # here (unlike blackjack's play_round) to guarantee ordering.
        self.current_view: discord.ui.View | None = None

    def all_user_ids(self) -> list[int]:
        return [self.user_id]

    def living_monsters(self) -> list[MonsterInstance]:
        return [m for m in self.monsters if m.hp > 0]

    def current_target(self) -> MonsterInstance | None:
        """Self-healing lookup rather than a maintained pointer -- if current_target_slot doesn't
        resolve to a still-living monster (it died, or was never set for this group), falls back to
        the first living monster and updates current_target_slot to match. This is the entire
        mechanism behind "target auto-swaps once the current one dies" -- no explicit swap code is
        needed anywhere a monster gets killed. Returns None only once the whole group is dead, which
        callers never actually observe -- the room clears the moment living_monsters() empties."""
        living = self.living_monsters()
        if not living:
            return None
        for m in living:
            if m.slot == self.current_target_slot:
                return m
        self.current_target_slot = living[0].slot
        return living[0]


# --- Party delves -------------------------------------------------------------------------------
# A party lets other players join a delve before it starts, at no energy cost to them -- only the
# leader (whoever ran !delve) spends the charge, and only once they actually confirm Start Delve.
# Combat then resolves in full rounds: every living member acts once (in join order) before the
# monster gets its single counter-attack, rather than solo's "monster hits back after every
# action." See PARTY_LOBBY_TIMEOUT/PARTY_ACTION_TIMEOUT below and PartyLobby/PartyDelveSession.

PARTY_SIZE_CAP = 4  # keeps the roster/turn-order embed readable; matches party_hp_multiplier's natural 1-4 range
PARTY_LOBBY_TIMEOUT = 300  # 5 minutes to gather a party -- longer than DelvePickerView's 120s since it needs multiple humans, not just one player picking a dropdown
PARTY_ACTION_TIMEOUT = 150  # 2.5 minutes -- solo's 20-minute DELVE_ACTION_TIMEOUT only ever blocks the one player waiting on themselves; a party turn blocks everyone else too, so it has to be much tighter


class PartyMember:
    """One party delve participant -- the same per-user combat fields DelveSession carries for a
    solo player (hp/max_hp/atk/def_/loot_total/...), just pulled onto their own object, since a
    party's monster group state (each MonsterInstance's hp/def_debuff) is shared across members
    rather than living on each one individually (see PartyDelveSession, which owns that shared
    state instead)."""

    def __init__(self, guild_id: int, user_id: int, player_name: str, character: dict, equipped: dict[str, str], is_leader: bool):
        self.guild_id = guild_id
        self.user_id = user_id
        self.player_name = player_name  # snapshot of interaction.user.display_name at join time
        self.main_class = character["main_class"]
        self.subclass = character["subclass"]
        self.level = character["level"]
        self.equipped = equipped
        effective = dungeon.compute_effective_stats(character, equipped)
        self.max_hp = effective["hp"]
        self.atk = effective["atk"]
        self.def_ = effective["def"]
        self.spatk = effective["spatk"]
        self.spdef = effective["spdef"]
        self.speed = effective["speed"]
        self.atk_debuff = self.def_debuff = self.spatk_debuff = self.spdef_debuff = self.speed_debuff = 0
        self.timed_effects: list[dict] = []
        self.turn_clock = 0.0
        self.guard_charge: float | None = None
        self.used_item_effects: set[str] = set()
        # Same "start wherever the last delve left off" rule as DelveSession -- see its own hp
        # comment for why.
        self.hp = min(character["current_hp"], self.max_hp)
        self.loot_mult = dungeon.SUBCLASSES[self.subclass]["loot_mult"]
        self.build_name = dungeon.display_name(self.main_class, self.subclass)
        self.unlocked_skills = dungeon.unlocked_skills(self.main_class, self.subclass, self.level)
        self.is_leader = is_leader
        self.max_chips = dungeon.compute_stats(self.main_class, self.subclass)["chips"]
        self.chips = self.max_chips
        self.knocked_out = False
        self.loot_total = 0

    @property
    def label(self) -> str:
        return f"{self.player_name} ({self.build_name})"


class PartyLobby:
    """Pre-combat party-forming state, shown after the leader picks "Start Party" from
    DelveModeChoiceView. Deliberately a separate class from PartyDelveSession (rather than a
    started flag on one combined class) so combat-only fields (monster, turn_clock, ...) never
    exist in a half-initialized state during the join phase."""

    def __init__(self, guild_id: int, leader_id: int, leader_name: str, leader_character: dict, delve: dict):
        self.guild_id = guild_id
        self.leader_id = leader_id
        self.leader_character = leader_character
        self.delve = delve
        self.member_ids: list[int] = [leader_id]  # join order, leader always first
        self.member_names: dict[int, str] = {leader_id: leader_name}
        self.message: discord.Message | None = None
        self.current_view: discord.ui.View | None = None

    def all_user_ids(self) -> list[int]:
        return list(self.member_ids)


class PartyDelveSession:
    """Combat state for a party delve, built the moment the leader clicks Start Delve. members[0]
    is always the leader; turn order otherwise follows join order."""

    def __init__(self, guild_id: int, delve: dict, members: list[PartyMember]):
        self.guild_id = guild_id
        self.delve = delve
        self.rooms_by_id = dungeon.rooms_by_id(delve)
        self.members = members
        self.members_by_id = {m.user_id: m for m in members}
        self.monsters: list[MonsterInstance] = []
        # user_id -> the slot that member is currently targeting -- per-member rather than one
        # session-wide field, since each party member picks their own target independently. See
        # target_for's self-healing fallback, same idea as solo DelveSession.current_target.
        self.member_target_slots: dict[int, int] = {}
        self.rooms_visited = 1
        self._enter_room(delve["start_room"])

        self.message: discord.Message | None = None
        self.current_view: discord.ui.View | None = None

    def living_members(self) -> list[PartyMember]:
        return [m for m in self.members if not m.knocked_out]

    def all_user_ids(self) -> list[int]:
        return [m.user_id for m in self.members]

    def living_monsters(self) -> list[MonsterInstance]:
        return [m for m in self.monsters if m.hp > 0]

    def target_for(self, member: PartyMember) -> MonsterInstance | None:
        """Party sibling of solo DelveSession.current_target -- self-healing per-member lookup, not
        a maintained pointer. Falls back to (and records) the first living monster if this member's
        stored slot no longer resolves to one, which is the entire "auto-swap once the current
        target dies" mechanism."""
        living = self.living_monsters()
        if not living:
            return None
        slot = self.member_target_slots.get(member.user_id)
        for m in living:
            if m.slot == slot:
                return m
        self.member_target_slots[member.user_id] = living[0].slot
        return living[0]

    def _enter_room(self, room_id: str):
        """Moves into room_id -- rolls a fresh monster group and resets each member's turn_clock if
        it's a combat room (called on session init and on Push Deeper), or clears combat state if
        not. Each monster's HP is scaled by however many members are alive RIGHT NOW
        (party_hp_multiplier), not the party's original size, so a roster thinned by earlier
        knockouts faces a fight scaled to its current strength; the multiplier applies per monster,
        not again on top of a group's already-higher aggregate danger from every monster getting
        its own turn in the CTB schedule (see _advance_party_turns). Doesn't touch rooms_visited --
        callers bump that themselves, since __init__'s first call shouldn't double-count the
        starting room."""
        self.current_room_id = room_id
        room = self.rooms_by_id[room_id]
        self.member_target_slots = {}
        if room["type"] == "combat":
            mult = dungeon.party_hp_multiplier(len(self.living_members()))
            self.monsters = []
            for slot, monster in enumerate(dungeon.monsters_for_room(room)):
                instance = MonsterInstance(monster, slot)
                instance.max_hp = round(monster["hp"] * mult)
                instance.hp = instance.max_hp
                self.monsters.append(instance)
            for m in self.members:
                m.chips = m.max_chips
                m.used_item_effects = set()
                m.turn_clock = 0.0  # fresh fight -- same reset point as chips/used_item_effects
        else:
            self.monsters = []


def _room_background_path(delve: dict, room: dict) -> str | None:
    """A room's own background_path if it set one, else the delve's top-level default -- lets
    different rooms in the same delve look different without requiring every room to set one."""
    return room.get("background_path") or delve.get("background_path")


# Card-strip flavor for dungeon_render.render_room's turn_order param -- see dungeon.preview_next_turns
# for the schedule these visualize. TURN_ORDER_PREVIEW_COUNT of 8 comfortably fits the fixed card
# width dungeon_render.py sizes itself for (up to 10).
TURN_ORDER_PREVIEW_COUNT = 8


def _player_card(name: str, main_class: str, subclass: str) -> dict:
    """A player/party-member's card descriptor -- reuses the existing rank letter (A/K/Q/J) and
    suit symbol (♠♥♦♣) that already name every build, zero new content needed. `initial` is the
    first letter of the player's own name, printed under the card in the turn-order strip so
    party members (who share ranks/suits across builds) stay distinguishable at a glance."""
    return {
        "kind": "player",
        "rank": dungeon.CLASSES[main_class]["rank"],
        "suit": dungeon.SUIT_SYMBOLS[subclass],
        "initial": name[0].upper() if name else "?",
    }


def _monster_card(monster: dict) -> dict:
    """A monster's card descriptor -- reuses its existing shape/color fields, the same placeholder
    identity already used to draw it in the room scene."""
    return {
        "kind": "monster",
        "shape": monster["shape"],
        "color": monster["color"],
        "initial": monster["name"][0].upper() if monster.get("name") else "?",
    }


def _combat_intro_text(room: dict, monsters: list[dict]) -> str:
    """The description shown the moment a combat room is entered -- the room's own optional
    "prompt" (introducing the room itself, dungeon.py's module docstring) followed by every rolled
    monster's own flavor text. Only ever used at room-entry (_build_room_display/
    _build_party_room_display); a later combat-turn embed reuses _combat_embed/_party_combat_embed
    with the fight's actual log lines instead, so a room's intro is shown exactly once per visit,
    not repeated on every attack."""
    parts = [f"*{room['prompt']}*"] if room.get("prompt") else []
    parts.extend(f"*{m['flavor']}*" for m in monsters)
    return "\n\n".join(parts)


def _solo_turn_order_cards(session: DelveSession) -> list[dict]:
    living = session.living_monsters()
    combatants = [{"id": None, "speed": max(0, session.speed - session.speed_debuff), "clock": session.turn_clock}]
    combatants += [
        {"id": m.slot, "speed": max(0, m.speed - m.speed_debuff), "clock": m.turn_clock} for m in living
    ]
    monsters_by_slot = {m.slot: m for m in living}
    order = dungeon.preview_next_turns(combatants, TURN_ORDER_PREVIEW_COUNT)
    return [
        _player_card(session.display_name, session.main_class, session.subclass)
        if cid is None else _monster_card(monsters_by_slot[cid].monster)
        for cid in order
    ]


def _combat_embed(session: DelveSession, log_text: str) -> tuple[discord.Embed, discord.File]:
    living = session.living_monsters()
    title = f"🗡️ {living[0].monster['name']}" if len(living) == 1 else "🗡️ Combat"
    embed = discord.Embed(title=title, description=log_text, color=discord.Color.dark_red())
    embed.add_field(
        name=f"{session.display_name} (You)",
        value=f"❤️ HP {max(session.hp, 0)}/{session.max_hp}\n🪙 Chips {session.chips}/{session.max_chips}",
        inline=True,
    )
    target_slot = session.current_target().slot if living else None
    for m in living:
        marker = " ⬅️ target" if len(living) > 1 and m.slot == target_slot else ""
        embed.add_field(name=m.monster["name"], value=f"❤️ HP {max(m.hp, 0)}/{m.max_hp}{marker}", inline=True)
    room = session.rooms_by_id[session.current_room_id]
    buf = dungeon_render.render_room(
        session.rooms_visited, [m.monster for m in living], _room_background_path(session.delve, room),
        turn_order=_solo_turn_order_cards(session),
    )
    file = discord.File(buf, filename="room.png")
    embed.set_image(url="attachment://room.png")
    return embed, file


# --- Choice rooms -------------------------------------------------------------------------------
# A non-combat room: flavor text plus a menu of player-chosen actions (dungeon.py's room shape),
# each optionally gated (requires), costed, and/or skill-checked, with its own next-room outcome
# -- this is where a delve actually branches, not on combat rooms (which have at most one `next`).
# See dungeon.py's module docstring for the full action shape.

_CHECK_STAT_LABELS = {"atk": "ATK", "def": "DEF", "hp": "HP"}


def _stat_value_for_check(actor, stat: str) -> int:
    """The actor's own value for whichever stat a skill check rolls against -- "hp" reads max_hp,
    not current wounded HP, so a check's odds don't depend on unrelated earlier combat damage."""
    if stat == "hp":
        return actor.max_hp
    if stat == "def":
        return actor.def_
    return actor.atk


def _cost_item_registry(item_kind: str) -> dict:
    return {"material": dungeon.MATERIALS, "consumable": dungeon.CONSUMABLES, "quest_item": quests.QUEST_ITEMS}[item_kind]


def _cost_summary(cost: dict | None, currency_name: str) -> str | None:
    if not cost:
        return None
    parts = []
    if cost.get("currency"):
        parts.append(f"{cost['currency']} {currency_name}")
    if cost.get("item_id"):
        item = _cost_item_registry(cost["item_kind"]).get(cost["item_id"], {})
        qty = cost.get("item_qty", 1)
        parts.append(f"{qty}x {item.get('name', cost['item_id'])}")
    return "💰 " + ", ".join(parts)


def _action_summary_lines(action: dict, currency_name: str) -> list[str]:
    """Cost/check info shown on an action's button field, so a player knows what they're
    committing to before clicking it."""
    lines = []
    cost_line = _cost_summary(action.get("cost"), currency_name)
    if cost_line:
        lines.append(cost_line)
    check = action.get("check")
    if check:
        lines.append(f"🎲 {_CHECK_STAT_LABELS[check['stat']]} check (DC {check['dc']})")
    return lines


def _choice_embed(session: DelveSession, room: dict, description: str) -> tuple[discord.Embed, discord.File]:
    currency = db.get_currency_name(session.guild_id)
    embed = discord.Embed(title="🚪 A Choice", description=description, color=discord.Color.blurple())
    embed.add_field(name=f"{session.display_name} (You)", value=f"❤️ HP {max(session.hp, 0)}/{session.max_hp}", inline=False)
    for action in room["actions"]:
        lines = _action_summary_lines(action, currency)
        embed.add_field(name=action["label"], value="\n".join(lines) if lines else "—", inline=True)
    buf = dungeon_render.render_room(session.rooms_visited, [], _room_background_path(session.delve, room))
    file = discord.File(buf, filename="room.png")
    embed.set_image(url="attachment://room.png")
    return embed, file


async def _build_choice_room_view(session: DelveSession, room: dict) -> "ChoiceRoomView":
    """Pre-checks each action's `requires` (if any) so gated actions render disabled rather than
    hidden -- mirrors how an already-used skill button is shown-disabled in CombatView rather than
    removed, same "you can see what you can't do yet" idea."""
    character = {"main_class": session.main_class, "subclass": session.subclass}
    availability = []
    for action in room["actions"]:
        requires = action.get("requires")
        ok = True
        if requires is not None:
            ok = await quests.trigger_satisfied(session.guild_id, session.user_id, requires, character=character)
        availability.append(ok)
    return ChoiceRoomView(session, room, availability)


async def _build_room_display(interaction: discord.Interaction, session: DelveSession, intro_text: str = "") -> None:
    """Renders (and sends, via interaction.response.edit_message) whatever room a session is
    CURRENTLY sitting on -- used both for a fresh delve's very first room and, via _goto_room,
    every later transition, so there's one place that branches on room type rather than
    duplicating it at each call site. A combat room doesn't just show a static view -- entering
    one can resolve several automatic monster turns before the player ever gets to act (a fast
    enough monster group ambushes a slow player), so intro_text and the room's own flavor
    (_combat_intro_text) are seeded as the STARTING log lines and handed to _advance_solo_turns,
    which does the actual rendering/sending for combat."""
    room = session.rooms_by_id[session.current_room_id]
    if room["type"] == "combat":
        log_lines = [intro_text] if intro_text else []
        log_lines.append(_combat_intro_text(room, [m.monster for m in session.living_monsters()]))
        await _advance_solo_turns(interaction, session, log_lines)
    else:
        embed, file = _choice_embed(session, room, room["prompt"])
        if intro_text:
            embed.description = f"{intro_text}\n\n{embed.description}"
        view = await _build_choice_room_view(session, room)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)


async def _goto_room(interaction: discord.Interaction, session: DelveSession, room_id: str, intro_text: str = ""):
    """Transitions a solo session into room_id -- sets up a fresh fight if it's a combat room,
    clears combat state and renders the choice menu if not. Shared by Push Deeper (after a combat
    victory) and a choice action's own outcome (after resolving that action) -- either can now
    land on either room type, a direct consequence of rooms forming a graph instead of a flat
    sequence."""
    session.current_room_id = room_id
    session.rooms_visited += 1
    room = session.rooms_by_id[room_id]
    if room["type"] == "combat":
        session.monsters = _roll_monster_instances(room)
        session.current_target_slot = 0
        session.chips = session.max_chips
        session.used_item_effects = set()
        session.turn_clock = 0.0  # fresh fight -- same reset point as chips/used_item_effects
    else:
        session.monsters = []
    await _build_room_display(interaction, session, intro_text)


async def _handle_choice_action(
    interaction: discord.Interaction, session: DelveSession, room: dict, action: dict,
) -> bool:
    """Resolves one choice-room action: requires gate -> cost spend (atomic) -> skill check (if
    any) -> apply the chosen outcome (hp_delta, then a room transition, victory, or death). Same
    bool contract as _handle_action (False = rejected, view stays live and un-stopped; True =
    resolved, caller stop()s the view)."""
    requires = action.get("requires")
    if requires is not None:
        character = {"main_class": session.main_class, "subclass": session.subclass}
        satisfied = await quests.trigger_satisfied(session.guild_id, session.user_id, requires, character=character)
        if not satisfied:
            await interaction.response.send_message("You don't meet the requirements for that.", ephemeral=True)
            return False

    cost = action.get("cost")
    if cost is not None:
        materials = {cost["item_id"]: cost.get("item_qty", 1)} if cost.get("item_id") else {}
        status, _balance = await asyncio.to_thread(
            db.craft_item, session.guild_id, session.user_id, materials, cost.get("currency", 0)
        )
        if status != "ok":
            reason = "You can't afford that." if status == "broke" else "You don't have what that requires."
            await interaction.response.send_message(reason, ephemeral=True)
            return False

    log_lines = []
    check = action.get("check")
    if check is not None:
        stat_value = _stat_value_for_check(session, check["stat"])
        success, rolled = dungeon.roll_check(stat_value, check["dc"])
        stat_label = _CHECK_STAT_LABELS[check["stat"]]
        log_lines.append(
            f"🎲 {stat_label} check: rolled **{rolled}** vs DC **{check['dc']}** — "
            f"{'success!' if success else 'failure!'}"
        )
        outcome = action["on_success"] if success else action["on_fail"]
    else:
        outcome = action["on_success"]

    if outcome.get("message"):
        log_lines.append(outcome["message"])

    hp_delta = outcome.get("hp_delta", 0)
    if hp_delta:
        session.hp = min(session.max_hp, session.hp + hp_delta)
        verb = "recover" if hp_delta > 0 else "take"
        log_lines.append(f"You {verb} **{abs(hp_delta)}** HP.")

    if session.hp <= 0:
        currency = db.get_currency_name(session.guild_id)
        await _forfeit(session)
        embed = discord.Embed(
            title="💀 You Have Fallen",
            description="\n".join(log_lines) + f"\n\nYou're carried out of the dungeon empty-handed, losing this "
            f"delve's **{session.loot_total}** {currency} haul.",
            color=discord.Color.dark_red(),
        )
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        return True

    next_room = outcome.get("next")
    if next_room is None:
        currency = db.get_currency_name(session.guild_id)
        balance = await asyncio.to_thread(db.update_balance, session.guild_id, session.user_id, session.loot_total)
        await asyncio.to_thread(db.log_bet, session.guild_id, session.user_id, "dungeon", 0, session.loot_total)
        await asyncio.to_thread(db.set_current_hp, session.guild_id, session.user_id, session.hp)
        _cleanup(session)
        embed = discord.Embed(
            title="🏆 Victory!",
            description="\n".join(log_lines) + f"\n\nYou've cleared the dungeon! Balance: **{balance}** {currency}.",
            color=discord.Color.gold(),
        )
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        return True

    await _present_choice_outcome(interaction, session, next_room, log_lines)
    return True


class ChoiceActionButton(discord.ui.Button):
    def __init__(self, action: dict, row: int, disabled: bool):
        super().__init__(label=action["label"][:80], style=discord.ButtonStyle.primary, row=row, disabled=disabled)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        if await _handle_choice_action(interaction, self.view.session, self.view.room, self.action):
            self.view.stop()


class RetreatFromChoiceButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Retreat with Loot", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        embed = await _apply_retreat(self.view.session)
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        self.view.stop()


class ChoiceRoomView(discord.ui.View):
    """Every room-entry screen (this one and RoomResultView) offers Retreat -- a delve that routes
    straight into a choice room needs its own way to bank loot and leave, same as a combat victory
    already does, rather than forcing a commitment to one of this room's actions."""

    def __init__(self, session: DelveSession, room: dict, availability: list[bool]):
        super().__init__(timeout=DELVE_ACTION_TIMEOUT)
        self.session = session
        self.room = room
        for i, (action, ok) in enumerate(zip(room["actions"], availability)):
            self.add_item(ChoiceActionButton(action, row=min(3, i // 5), disabled=not ok))
        self.add_item(RetreatFromChoiceButton(row=4))
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message("This isn't your delve.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return
        if session.message is None:
            await _forfeit(session)
            return
        embed = await _apply_retreat(session)  # default to the safe choice if they don't respond
        try:
            await session.message.edit(embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            pass


class ChoiceOutcomeView(discord.ui.View):
    """Interstitial shown after a choice action resolves, before actually moving into whatever
    room its outcome points at -- without this, an outcome landing on a combat room reads as
    "you just got dropped straight into a fight" with no beat to register what happened first.
    Deliberately generic regardless of the destination room's type (a more tailored message per
    case can come later); mirrors RoomResultView's own shape (an embed of what just happened plus
    Continue/Retreat, same timeout-defaults-to-retreat safety net) since this is the same kind of
    room-boundary pause, just triggered by a choice outcome instead of a combat win."""

    def __init__(self, session: DelveSession, next_room_id: str):
        super().__init__(timeout=DELVE_ACTION_TIMEOUT)
        self.session = session
        self.next_room_id = next_room_id
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message("This isn't your delve.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _goto_room(interaction, self.session, self.next_room_id)
        self.stop()

    @discord.ui.button(label="Retreat with Loot", style=discord.ButtonStyle.secondary)
    async def retreat_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await _apply_retreat(self.session)
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        self.stop()

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return
        if session.message is None:
            await _forfeit(session)
            return
        embed = await _apply_retreat(session)  # default to the safe choice if they don't respond
        try:
            await session.message.edit(embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            pass


async def _present_choice_outcome(
    interaction: discord.Interaction, session: DelveSession, next_room_id: str, log_lines: list[str],
):
    embed = discord.Embed(title="🚪 Onward", description="\n".join(log_lines), color=discord.Color.blurple())
    embed.add_field(name=f"{session.display_name} (You)", value=f"❤️ HP {max(session.hp, 0)}/{session.max_hp}", inline=True)
    view = ChoiceOutcomeView(session, next_room_id)
    await interaction.response.edit_message(embed=embed, attachments=[], view=view)


def _cleanup(entity) -> None:
    """Shared end-of-session release for every active_delves entry type (solo DelveSession,
    PartyLobby, or PartyDelveSession) -- pops every one of its member user_ids from both
    active_delves and busy_players at once, via each type's own all_user_ids()."""
    for uid in entity.all_user_ids():
        active_delves.pop(uid, None)
        busy_players.discard(uid)


async def _apply_retreat(session: DelveSession) -> discord.Embed:
    currency = db.get_currency_name(session.guild_id)
    balance = await asyncio.to_thread(db.update_balance, session.guild_id, session.user_id, session.loot_total)
    await asyncio.to_thread(db.log_bet, session.guild_id, session.user_id, "dungeon", 0, session.loot_total)
    await asyncio.to_thread(db.set_current_hp, session.guild_id, session.user_id, session.hp)
    _cleanup(session)
    return discord.Embed(
        title="🏃 Retreated Safely",
        description=f"You make it back with **{session.loot_total}** {currency}. Balance: **{balance}** {currency}.",
        color=discord.Color.green(),
    )


async def _forfeit(session: DelveSession):
    """Ends the delve with no payout -- shared cleanup for death, abandonment (timeout), and
    anything else short of a deliberate retreat. Still persists current_hp (usually 0, on death)
    so the next delve picks up from there rather than silently starting back at full."""
    await asyncio.to_thread(db.set_current_hp, session.guild_id, session.user_id, session.hp)
    _cleanup(session)


DELVE_ACTION_TIMEOUT = 1200  # 20 minutes -- plenty of time to notice it's your turn and act


class AttackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Attack", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        # stop() cancels this view's own pending timeout once its turn is actually resolved --
        # without it, this view's on_timeout could still fire ~20 minutes later even after
        # combat has long since moved on to a new view (or ended), incorrectly overwriting an
        # already-concluded delve with a false "abandoned" message despite the real payout
        # having already landed.
        if await _handle_action(interaction, self.view.session, skill=None):
            self.view.stop()


class SkillButton(discord.ui.Button):
    """One button per skill the character has unlocked so far -- a build with only its level-1
    skill gets one, a higher-level build gets more. Each costs its own chip_cost out of
    session.chips (refilled to max_chips at the start of every fight), so unlocking more skills
    means more choices of what to spend a limited-but-refilling Chips pool on, not one shared
    once-per-fight charge -- disabled (not hidden) once its cost exceeds what's left this fight,
    same "show what you can't afford yet" idea an already-used skill used to convey."""

    def __init__(self, skill: dict, disabled: bool):
        super().__init__(label=skill["name"], style=discord.ButtonStyle.success, disabled=disabled, row=0)
        self.skill = skill

    async def callback(self, interaction: discord.Interaction):
        if await _handle_action(interaction, self.view.session, skill=self.skill):
            self.view.stop()


MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options


class UseItemButton(discord.ui.Button):
    """Shown instead of a Select when there's exactly one usable consumable -- one fewer click
    than opening a dropdown to pick from a list of one."""

    def __init__(self, item: dict):
        super().__init__(label=f"🧪 {item['name']}", style=discord.ButtonStyle.secondary, row=1)
        self.item = item

    async def callback(self, interaction: discord.Interaction):
        if await _handle_use_item(interaction, self.view.session, self.item):
            self.view.stop()


class UseItemSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        options = [
            discord.SelectOption(label=item["name"], value=item["id"], description=item["flavor"][:100])
            for item in items[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(placeholder="🧪 Use an item...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        item = dungeon.CONSUMABLES[self.values[0]]
        if await _handle_use_item(interaction, self.view.session, item):
            self.view.stop()


def castable_equipment(equipped: dict[str, str]) -> list[dict]:
    """Equipped items (weapon/armor/trinket, at most 3) carrying at least one on_use effect --
    shared by solo and party CastItemButton building. Unlike UseItemButton/Select's consumables,
    this never needs a DB re-fetch: equipment isn't consumed, and `equipped` already reflects a
    mid-fight auto-equip swap live (see _award_kill)."""
    items = (dungeon.EQUIPMENT[item_id] for item_id in equipped.values())
    return [item for item in items if any(e["trigger"] == "on_use" for e in item["effects"])]


class CastItemButton(discord.ui.Button):
    """One button per equipped item with an on_use effect (at most 3 -- weapon/armor/trinket) --
    disabled (not hidden), same "show what you can't do yet" convention SkillButton already uses,
    once its own id is in session.used_item_effects for this fight."""

    def __init__(self, item: dict, disabled: bool):
        super().__init__(label=f"✨ {item['name']}", style=discord.ButtonStyle.secondary, disabled=disabled, row=3)
        self.item = item

    async def callback(self, interaction: discord.Interaction):
        if await _handle_cast_item(interaction, self.view.session, self.item):
            self.view.stop()


class TargetSelect(discord.ui.Select):
    """Only added to CombatView when more than one monster in the current group is still alive --
    picking an option here just changes which monster Attack/Skill/Item resolve against (see
    DelveSession.current_target). Switching target costs no turn, so this rebuilds the same combat
    view in place rather than routing through _resolve_combat_turn."""

    def __init__(self, session: DelveSession):
        target = session.current_target()
        options = [
            discord.SelectOption(
                label=f"{m.monster['name']} ({m.hp}/{m.max_hp} HP)", value=str(m.slot),
                default=m.slot == target.slot,
            )
            for m in session.living_monsters()
        ]
        super().__init__(placeholder="🎯 Choose a target...", options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        session: DelveSession = self.view.session
        session.current_target_slot = int(self.values[0])
        log_text = interaction.message.embeds[0].description or ""
        embed, file = _combat_embed(session, log_text)
        view = await _build_combat_view(session)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
        self.view.stop()


class CombatView(discord.ui.View):
    def __init__(self, session: DelveSession, usable_items: list[dict] | None = None):
        super().__init__(timeout=DELVE_ACTION_TIMEOUT)
        self.session = session
        self.add_item(AttackButton())
        for skill in session.unlocked_skills:
            self.add_item(SkillButton(skill, disabled=skill["chip_cost"] > session.chips))
        usable_items = usable_items or []
        if len(usable_items) == 1:
            self.add_item(UseItemButton(usable_items[0]))
        elif len(usable_items) > 1:
            self.add_item(UseItemSelect(usable_items))
        for item in castable_equipment(session.equipped):
            self.add_item(CastItemButton(item, disabled=item["id"] in session.used_item_effects))
        if len(session.living_monsters()) > 1:
            self.add_item(TargetSelect(session))
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message("This isn't your delve.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return  # superseded -- the player already moved on through some other path
        await _forfeit(session)
        if session.message is None:
            return
        currency = db.get_currency_name(session.guild_id)
        embed = discord.Embed(
            title="⌛ Delve Abandoned",
            description=f"You hesitate too long and stumble out empty-handed, losing this delve's "
            f"**{session.loot_total}** {currency} haul.",
            color=discord.Color.dark_grey(),
        )
        try:
            await session.message.edit(embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            pass


class RoomResultView(discord.ui.View):
    def __init__(self, session: DelveSession):
        super().__init__(timeout=DELVE_ACTION_TIMEOUT)
        self.session = session
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message("This isn't your delve.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Retreat with Loot", style=discord.ButtonStyle.success)
    async def retreat_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await _apply_retreat(self.session)
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        self.stop()

    @discord.ui.button(label="Push Deeper", style=discord.ButtonStyle.danger)
    async def push_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = self.session
        room = session.rooms_by_id[session.current_room_id]
        # Guaranteed non-None -- this view is only ever shown when _present_room_result found a
        # "next" room to push into (see its is_last_room check).
        await _goto_room(interaction, session, room["next"], "You press deeper into the dungeon...")
        self.stop()

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return
        if session.message is None:
            await _forfeit(session)
            return
        embed = await _apply_retreat(session)  # default to the safe choice if they don't respond
        try:
            await session.message.edit(embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            pass


async def _award_kill(
    guild_id: int, monster: dict, actor, room_id: str, log_lines: list[str],
    *, loot_mult: float, chance_mult: float,
):
    """Shared kill-reward logic for a solo delve (actor=the DelveSession itself) or a party delve
    (actor=one PartyMember): credits, XP (applying any level-up's stat growth to `actor`
    *immediately*, not just the stored character row, so leveling up mid-delve actually helps you
    survive deeper right then), drops (auto-equipping an upgrade, storing otherwise), quest
    kill-progress, and a quest-item roll. `loot_mult` scales the credit roll (as it always has --
    subclass loot bonus folded in by the caller); `chance_mult` separately scales drop chances --
    both 1.0 for solo/a party leader, halved for a party joiner. Mutates `actor` in place and
    appends result lines to log_lines -- does NOT log "<monster> is defeated!" itself, since a
    party kill only says that once while every living member still gets their own reward lines;
    callers log that line themselves before calling this (once per kill, not once per actor)."""
    currency = db.get_currency_name(guild_id)
    loot = dungeon.roll_loot(monster, loot_mult)
    actor.loot_total += loot
    log_lines.append(f"You find **{loot}** {currency}.")

    xp_gain = dungeon.xp_for_monster(monster)
    level_result = await asyncio.to_thread(
        db.add_xp, guild_id, actor.user_id, xp_gain,
        dungeon.LEVEL_HP_GAIN, dungeon.LEVEL_ATK_GAIN, dungeon.LEVEL_DEF_GAIN,
        dungeon.LEVEL_SPATK_GAIN, dungeon.LEVEL_SPDEF_GAIN, dungeon.LEVEL_SPEED_GAIN,
    )
    log_lines.append(f"+{xp_gain} XP")
    if level_result["levels_gained"] > 0:
        actor.level = level_result["new_level"]
        hp_delta = dungeon.LEVEL_HP_GAIN * level_result["levels_gained"]
        atk_delta = dungeon.LEVEL_ATK_GAIN * level_result["levels_gained"]
        def_delta = dungeon.LEVEL_DEF_GAIN * level_result["levels_gained"]
        spatk_delta = dungeon.LEVEL_SPATK_GAIN * level_result["levels_gained"]
        spdef_delta = dungeon.LEVEL_SPDEF_GAIN * level_result["levels_gained"]
        speed_delta = dungeon.LEVEL_SPEED_GAIN * level_result["levels_gained"]
        actor.max_hp += hp_delta
        actor.hp += hp_delta
        actor.atk += atk_delta
        actor.def_ += def_delta
        actor.spatk += spatk_delta
        actor.spdef += spdef_delta
        actor.speed += speed_delta
        actor.unlocked_skills = dungeon.unlocked_skills(actor.main_class, actor.subclass, actor.level)
        plural = "s" if level_result["levels_gained"] > 1 else ""
        log_lines.append(f"🎉 Level up! Now level {actor.level} (+{level_result['levels_gained']} level{plural}).")

    for dropped in dungeon.roll_drops(monster, chance_mult):
        if dropped["_drop_kind"] == "material":
            await asyncio.to_thread(db.add_inventory_item, guild_id, actor.user_id, dropped["id"])
            log_lines.append(f"⛏️ You scavenge some **{dropped['name']}**.")
            continue

        slot = dropped["slot"]
        current_item_id = actor.equipped.get(slot)
        current_item = dungeon.EQUIPMENT.get(current_item_id) if current_item_id else None
        if dungeon.is_upgrade(current_item_id, dropped):
            await asyncio.to_thread(db.equip_item_smart, guild_id, actor.user_id, slot, dropped["id"])
            old_bonuses = dungeon.constant_stat_bonuses(current_item) if current_item else {}
            new_bonuses = dungeon.constant_stat_bonuses(dropped)
            actor.max_hp += new_bonuses.get("hp", 0) - old_bonuses.get("hp", 0)
            actor.hp += new_bonuses.get("hp", 0) - old_bonuses.get("hp", 0)
            actor.atk += new_bonuses.get("atk", 0) - old_bonuses.get("atk", 0)
            actor.def_ += new_bonuses.get("def", 0) - old_bonuses.get("def", 0)
            actor.spatk += new_bonuses.get("spatk", 0) - old_bonuses.get("spatk", 0)
            actor.spdef += new_bonuses.get("spdef", 0) - old_bonuses.get("spdef", 0)
            actor.speed += new_bonuses.get("speed", 0) - old_bonuses.get("speed", 0)
            actor.equipped[slot] = dropped["id"]
            if current_item:
                log_lines.append(f"⚔️ Found **{dropped['name']}**! Replaced {current_item['name']} — equipped (stored in `!equipment`).")
            else:
                log_lines.append(f"⚔️ Found **{dropped['name']}**! Equipped.")
        elif dropped["id"] == current_item_id:
            # A dead-even is_upgrade tie against the exact item already worn (not just something
            # of equal power) -- storing it would put the same item_id in equipment_inventory
            # while it's ALSO equipped, an invariant equip_item_smart otherwise always maintains
            # (see inventory_view._stored_excluding_equipped, added after this produced a live
            # crash: a duplicate Discord Select option value in EquipmentSlotSelect). Nothing to
            # gain from a spare of an already-worn item, so it's just not kept.
            log_lines.append(f"⚔️ Found another **{dropped['name']}** -- you're already wearing one.")
        else:
            await asyncio.to_thread(db.store_equipment_item, guild_id, actor.user_id, dropped["id"])
            log_lines.append(f"⚔️ Found **{dropped['name']}**, but your current {slot} is better — stored in `!equipment`.")

    await quests.record_progress(guild_id, actor.user_id, "kill_monster", monster_id=monster["id"])

    quest_item = await quests.roll_item_drop(guild_id, actor.user_id, room_id, monster["id"])
    if quest_item is not None:
        log_lines.append(f"{quest_item['emoji']} Found a **{quest_item['name']}**...")


# --- Effect dispatch --------------------------------------------------------------------------
# Interprets the `effects` list on a skill (dungeon.SKILLS) or, later, a consumable
# (dungeon.CONSUMABLES) -- one handler per primitive type in dungeon.EFFECT_PARAM_SCHEMAS, each
# mutating `actor`/`monster_state`/`mods` in place. Lives here rather than in dungeon.py because
# these handlers mutate Discord-layer session objects -- the same reason the ability logic this
# replaces already lived here rather than in dungeon.py.
#
# `actor` (hp/max_hp/atk/def_) and `monster_state` (the current target -- a MonsterInstance when a
# player is acting, or the acting monster's own current target -- a DelveSession/PartyMember --
# when a monster is acting, see _resolve_monster_attack) are split into two params rather than one
# "session" because a party delve's monster state is shared across every member, while hp/atk/
# def_ belong to whichever one PartyMember is acting. Every caller passes whichever entity this
# action's opponent-targeted effects (def_shred, the *_debuff family) should land on -- self-
# targeted effects (heal/guard/buffs/the timed dodge/resist/dot/hot ones) only ever touch `actor`
# and never read `monster_state` at all, so they behave identically regardless of who the opponent
# is.

# Effect types that make this action roll monster damage (as opposed to a pure utility action
# like Heal/Guard, which resolve entirely inside _apply_effects and skip the damage roll below).
DAMAGE_EFFECT_TYPES = {"damage_multiplier", "extra_attack"}


def _default_mods() -> dict:
    return {"multiplier": 1.0, "lifesteal_fraction": None, "extra_attack_multipliers": []}


def _actor_label(entity) -> str:
    """Subject-position label for a self-referring log line -- 'You' for the player's own
    session/member, or the monster's bolded name for a MonsterInstance. Grammatically this only
    ever precedes a base-form verb ('You attack') or a monster's own third-person one ('**Goblin**
    attacks') -- see _verb for picking the right form."""
    return "You" if not isinstance(entity, MonsterInstance) else f"**{entity.monster['name']}**"


def _possessive_label(entity) -> str:
    """Possessive-position label -- 'Your' / "**Goblin**'s" -- for log lines like "Your ATK rises"
    where the grammatical subject is the stat, not the entity itself, so no verb conjugation is
    needed regardless of which label this resolves to."""
    return "Your" if not isinstance(entity, MonsterInstance) else f"**{entity.monster['name']}**'s"


def _verb(entity, base: str) -> str:
    """Base form ('recover') for 'You', third-person -s form ('recovers') for a monster -- the
    one piece of English _actor_label alone can't paper over."""
    return base if not isinstance(entity, MonsterInstance) else base + "s"


def _combatant_name(entity) -> str:
    """Subject-position name for an entity in the monster_state/opponent role -- unlike
    _actor_label, this is never "You": a MonsterInstance's bolded name (PvE, the usual case) or a
    PartyMember's own bolded label (a duel opponent -- see the Dueling section below). Only ever
    used for monster_state-role references (enemy-shaped effect log lines, _resolve_player_action's
    own dodge/damage lines) -- an actor's own self-referential lines still go through
    _actor_label/_possessive_label ("You"/"Your") unchanged, and monster_state in every one of
    those call sites is guaranteed to be a MonsterInstance or a duel-opponent PartyMember, never a
    solo DelveSession (which has no .label), so this never needs to handle that case."""
    if isinstance(entity, MonsterInstance):
        return f"**{entity.monster['name']}**"
    return f"**{entity.label}**"


def _combatant_possessive(entity) -> str:
    return f"{_combatant_name(entity)}'s"


def _apply_timed_effect(actor, effect_type: str, value, duration: int, log_lines: list[str], message: str) -> None:
    """Shared by the four timed-effect handlers (dodge_buff/resist_buff/dot/hot): refreshes an
    existing entry of this type on `actor.timed_effects` in place rather than stacking a second
    one, or appends a fresh entry if there wasn't one."""
    existing = next((e for e in actor.timed_effects if e["type"] == effect_type), None)
    if existing is not None:
        existing["value"] = value
        existing["remaining"] = duration
    else:
        actor.timed_effects.append({"type": effect_type, "value": value, "remaining": duration})
    log_lines.append(message)


def _effect_damage_multiplier(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    mods["multiplier"] *= effect["value"]


def _effect_heal_fraction(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    healed = min(actor.max_hp, actor.hp + round(actor.max_hp * effect["value"])) - actor.hp
    actor.hp += healed
    log_lines.append(f"{_actor_label(actor)} {_verb(actor, 'recover')} **{healed}** HP.")


def _effect_guard(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    # A one-shot "absorb the next hit" charge (actor.guard_charge), NOT a mods-dict entry -- under
    # dynamic turn order, an arbitrary number of *other* combatants' turns can fall between "I
    # guard" and "something actually hits me," so there's no single next call frame (the old
    # same-call mods dict) to stash this in. Consumed by _consume_guard_charge, called wherever
    # damage is next applied to this entity, whoever that ends up being.
    actor.guard_charge = effect["reduction"]
    pronoun = "its" if isinstance(actor, MonsterInstance) else "your"
    log_lines.append(f"{_actor_label(actor)} {_verb(actor, 'raise')} {pronoun} guard, ready to blunt the next blow.")


def _consume_guard_charge(defender, dmg: int, log_lines: list[str]) -> int:
    """If `defender` has an active guard_charge, reduces `dmg` by it and clears the charge (spent
    on this hit, whichever hit that turns out to be -- could be the very next action, or several
    turns later if nothing else has hit them since). Returns dmg unchanged if there's nothing to
    consume. Called at every point damage is applied to any entity, player or monster alike (full
    parity -- a monster's own Guard-shaped skill works the same way a player's does)."""
    if defender.guard_charge is None:
        return dmg
    reduced = max(1, round(dmg * defender.guard_charge))
    defender.guard_charge = None
    log_lines.append(f"{_possessive_label(defender)} guard softens the blow — {reduced} damage instead of {dmg}.")
    return reduced


def _effect_lifesteal_fraction(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    mods["lifesteal_fraction"] = effect["value"]  # applied against total damage dealt, once known


def _effect_def_shred(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    monster_state.def_debuff += effect["value"]
    log_lines.append(f"{_combatant_possessive(monster_state)} defenses crumble by **{effect['value']}**.")


def _effect_extra_attack(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    mods["extra_attack_multipliers"].append(effect.get("multiplier", 1.0))


def _effect_atk_buff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    actor.atk += effect["value"]
    log_lines.append(f"{_possessive_label(actor)} ATK rises by **{effect['value']}** for the rest of the fight.")


def _effect_def_buff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    actor.def_ += effect["value"]
    log_lines.append(f"{_possessive_label(actor)} DEF rises by **{effect['value']}** for the rest of the fight.")


def _effect_spatk_buff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    actor.spatk += effect["value"]
    log_lines.append(f"{_possessive_label(actor)} SpAtk rises by **{effect['value']}** for the rest of the fight.")


def _effect_spdef_buff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    actor.spdef += effect["value"]
    log_lines.append(f"{_possessive_label(actor)} SpDef rises by **{effect['value']}** for the rest of the fight.")


def _effect_hp_buff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    actor.max_hp += effect["value"]
    actor.hp += effect["value"]
    log_lines.append(f"{_possessive_label(actor)} max HP rises by **{effect['value']}** for the rest of the fight.")


def _effect_speed_buff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    actor.speed += effect["value"]
    log_lines.append(f"{_possessive_label(actor)} Speed rises by **{effect['value']}** for the rest of the fight.")


def _effect_atk_debuff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    monster_state.atk_debuff += effect["value"]
    log_lines.append(f"{_combatant_possessive(monster_state)} ATK falls by **{effect['value']}** for the rest of the fight.")


def _effect_spatk_debuff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    monster_state.spatk_debuff += effect["value"]
    log_lines.append(f"{_combatant_possessive(monster_state)} SpAtk falls by **{effect['value']}** for the rest of the fight.")


def _effect_spdef_debuff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    monster_state.spdef_debuff += effect["value"]
    log_lines.append(f"{_combatant_possessive(monster_state)} SpDef falls by **{effect['value']}** for the rest of the fight.")


def _effect_speed_debuff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    # speed_debuff is read live at every turn-order scheduling point (dungeon.preview_next_turns/
    # turn_interval, called wherever a combatants list is built) -- never cached, so this lands on
    # the very next scheduling decision, not just "future fights."
    monster_state.speed_debuff += effect["value"]
    log_lines.append(
        f"{_combatant_possessive(monster_state)} Speed falls by **{effect['value']}** for the rest of the fight."
    )


def _effect_taunt(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    # A duel opponent (a PartyMember, not a MonsterInstance) has no .threat table -- that's a
    # monster-only mechanic (see dungeon_view's threat-mechanic section). Rather than crash, this
    # just fails harmlessly: nothing to sway when the "opponent" already knows exactly who to
    # attack (the only other player in the fight).
    if not hasattr(monster_state, "threat"):
        log_lines.append("Threat means nothing between two players.")
        return
    monster_state.threat[actor.user_id] = monster_state.threat.get(actor.user_id, 0) + effect["value"]
    log_lines.append(
        f"{_possessive_label(actor)} Threat against {_combatant_name(monster_state)} rises by **{effect['value']}** for the rest of the fight."
    )


def _effect_lower_threat(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    if not hasattr(monster_state, "threat"):
        log_lines.append("Threat means nothing between two players.")
        return
    monster_state.threat[actor.user_id] = monster_state.threat.get(actor.user_id, 0) - effect["value"]
    log_lines.append(
        f"{_possessive_label(actor)} Threat against {_combatant_name(monster_state)} falls by **{effect['value']}** for the rest of the fight."
    )


def _effect_dodge_buff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    _apply_timed_effect(
        actor, "dodge_buff", effect["value"], effect["duration"], log_lines,
        f"{_possessive_label(actor)} chance to dodge rises for **{effect['duration']}** more round(s).",
    )


def _effect_resist_buff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    _apply_timed_effect(
        actor, "resist_buff", effect["value"], effect["duration"], log_lines,
        f"{_possessive_label(actor)} chance to resist rises for **{effect['duration']}** more round(s).",
    )


def _effect_dot(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    _apply_timed_effect(
        actor, "dot", effect["value"], effect["duration"], log_lines,
        f"{_possessive_label(actor)} wounds will fester for **{effect['duration']}** more round(s).",
    )


def _effect_hot(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    _apply_timed_effect(
        actor, "hot", effect["value"], effect["duration"], log_lines,
        f"{_possessive_label(actor)} wounds will mend for **{effect['duration']}** more round(s).",
    )


EFFECT_HANDLERS = {
    "damage_multiplier": _effect_damage_multiplier,
    "heal_fraction": _effect_heal_fraction,
    "guard": _effect_guard,
    "lifesteal_fraction": _effect_lifesteal_fraction,
    "def_shred": _effect_def_shred,
    "extra_attack": _effect_extra_attack,
    "atk_buff": _effect_atk_buff,
    "def_buff": _effect_def_buff,
    "spatk_buff": _effect_spatk_buff,
    "spdef_buff": _effect_spdef_buff,
    "hp_buff": _effect_hp_buff,
    "speed_buff": _effect_speed_buff,
    "atk_debuff": _effect_atk_debuff,
    "spatk_debuff": _effect_spatk_debuff,
    "spdef_debuff": _effect_spdef_debuff,
    "speed_debuff": _effect_speed_debuff,
    "taunt": _effect_taunt,
    "lower_threat": _effect_lower_threat,
    "dodge_buff": _effect_dodge_buff,
    "resist_buff": _effect_resist_buff,
    "dot": _effect_dot,
    "hot": _effect_hot,
}


def _apply_effects(actor, monster_state, effects: list[dict], log_lines: list[str]) -> dict:
    """Simple single-target dispatch: every effect in `effects` runs once against this one
    `actor`/`monster_state` pair via EFFECT_HANDLERS. Used only by _resolve_monster_attack -- a
    monster's own skill never needs the multi-target per-effect "aoe" resolution
    _resolve_player_action implements below (taunt/lower_threat, the one type that used to need
    broadcasting to every monster, are validation-rejected on monster skills --
    dungeon._MONSTER_SKILL_EXCLUDED_EFFECT_TYPES -- so every monster-skill effect really is
    single-target by construction; this dispatch never needs to know about "aoe" at all).
    Returns this-action modifiers the caller still needs for the damage roll: multiplier,
    lifesteal_fraction (None if no lifesteal), and extra_attack_multipliers (one roll_damage call
    per entry, on top of the primary hit). Guard is NOT in this dict -- see _effect_guard/
    _consume_guard_charge for why it's a persistent per-entity field instead."""
    mods = _default_mods()
    for effect in effects:
        EFFECT_HANDLERS[effect["type"]](actor, monster_state, effect, log_lines, mods)
    return mods


def _resolve_player_action(
    actor, ally_pool: list, enemy_pool: list, current_target,
    effects: list[dict], special: bool, verb: str, subject_label: str, possessive_label: str, drain_verb: str,
    moon_mult: float, equipped_items: list[dict], threat_gain: bool, log_lines: list[str],
) -> list:
    """Resolves one player-cast action (skill/consumable/equipment on-use) -- the shared core of
    _resolve_combat_turn (solo), _resolve_party_turn (party), and _resolve_duel_turn (PvP). Those
    three differ only in `ally_pool` (solo/duel: always just [actor], nothing else to expand an
    ally-aoe effect to), `threat_gain` (party only -- a duel opponent has no .threat table, see
    _effect_taunt/_effect_lower_threat's own guard), the handful of solo/party/duel-phrased strings
    passed in, and their own kill-check/reward-loop shape once this returns. `enemy_pool`/
    `current_target` hold `MonsterInstance`s for solo/party (PvE) or a single-entry list containing
    the other duelist (a `PartyMember`) for a duel -- every access to them in here goes through
    _combatant_name (never a raw `.monster["name"]` read) specifically so this function doesn't
    need to know or care which kind it's holding.

    Each effect independently decides its own target set off its own "aoe" bool (dungeon.
    MODS_ONLY_EFFECT_TYPES / ENEMY_TARGETED_EFFECT_TYPES / ally-shaped-by-omission -- see those
    comments in dungeon.py):
      - mods-only effects (damage_multiplier/extra_attack/lifesteal_fraction) run once, configuring
        `mods` for the damage roll below -- damage_multiplier/extra_attack's own "aoe" (either one)
        decides whether the damage roll below targets `current_target` alone or every monster in
        `enemy_pool`.
      - ally-shaped effects apply to `[actor]`, or (per their own "aoe") `ally_pool` -- always
        unconditional, never gated by any monster's dodge (a self-heal was never really "the
        attack" that could be dodged).
      - enemy-shaped effects (taunt/lower_threat folded in here like any other, now that they're
        plain EFFECT_HANDLERS entries) each apply to `[current_target]`, or (per their own "aoe")
        `enemy_pool` -- gated by dodge.
      - dodge is rolled ONCE per monster touched by anything enemy-shaped or the damage roll this
        action (not once per effect) -- a dodging monster skips everything aimed at it this action;
        a different monster touched by a different, independently-"aoe"-flagged effect in the same
        action rolls its own dodge separately.

    Returns every monster that actually took damage this action (dodged/undamaged monsters
    excluded) -- the caller runs its own kill-check/reward loop against exactly that list."""
    mods_effects = [e for e in effects if e["type"] in dungeon.MODS_ONLY_EFFECT_TYPES]
    ally_effects = [
        e for e in effects
        if e["type"] not in dungeon.MODS_ONLY_EFFECT_TYPES and e["type"] not in dungeon.ENEMY_TARGETED_EFFECT_TYPES
    ]
    enemy_effects = [e for e in effects if e["type"] in dungeon.ENEMY_TARGETED_EFFECT_TYPES]
    is_damage_action = not effects or any(e["type"] in DAMAGE_EFFECT_TYPES for e in effects)

    mods = _default_mods()
    for e in mods_effects:
        EFFECT_HANDLERS[e["type"]](actor, None, e, log_lines, mods)

    for e in ally_effects:
        for t in (ally_pool if e.get("aoe") else [actor]):
            EFFECT_HANDLERS[e["type"]](t, None, e, log_lines, mods)

    attack_is_aoe = any(e.get("aoe") for e in mods_effects if e["type"] in DAMAGE_EFFECT_TYPES)
    damage_targets = (enemy_pool if attack_is_aoe else [current_target]) if is_damage_action else []
    enemy_effect_targets = {id(e): (enemy_pool if e.get("aoe") else [current_target]) for e in enemy_effects}

    touched = set(damage_targets)
    for t_list in enemy_effect_targets.values():
        touched.update(t_list)
    dodged = {}
    for monster in touched:
        eff_def = max(0, (monster.spdef - monster.spdef_debuff) if special else (monster.def_ - monster.def_debuff))
        dodged[monster] = random.random() < _defended_dodge_chance(monster, eff_def, special)

    for e in enemy_effects:
        for monster in enemy_effect_targets[id(e)]:
            if not dodged[monster]:
                EFFECT_HANDLERS[e["type"]](actor, monster, e, log_lines, mods)

    hit_monsters: list["MonsterInstance"] = []
    total_dmg = 0
    if is_damage_action:
        attacker_atk = (actor.spatk - actor.spatk_debuff) if special else (actor.atk - actor.atk_debuff)
        for monster in damage_targets:
            if dodged[monster]:
                log_lines.append(f"{_combatant_name(monster)} dodges {possessive_label} {verb}!")
                continue
            eff_def = max(0, (monster.spdef - monster.spdef_debuff) if special else (monster.def_ - monster.def_debuff))
            dmg = dungeon.roll_damage(attacker_atk, eff_def, mods["multiplier"] * moon_mult)
            for extra_multiplier in mods["extra_attack_multipliers"]:
                dmg += dungeon.roll_damage(attacker_atk, eff_def, extra_multiplier * moon_mult)
            # Guard reduction is consumed here (against the TARGET's own charge, if any -- a
            # monster can guard itself too, full parity) before lifesteal/on-hit procs read `dmg`.
            dmg = _consume_guard_charge(monster, dmg, log_lines)
            monster.hp -= dmg
            total_dmg += dmg
            hit_monsters.append(monster)
            log_lines.append(f"{subject_label} {verb} {_combatant_name(monster)} for **{dmg}** damage.")
            if threat_gain:
                monster.threat[actor.user_id] = monster.threat.get(actor.user_id, 0) + dmg * dungeon.THREAT_PER_DAMAGE
            _roll_on_hit_procs(actor, monster, equipped_items, dmg, log_lines)

    if mods["lifesteal_fraction"] and total_dmg:
        healed = min(actor.max_hp, actor.hp + round(total_dmg * mods["lifesteal_fraction"])) - actor.hp
        actor.hp += healed
        if healed:
            log_lines.append(f"{subject_label} {drain_verb} **{healed}** HP from the strike.")

    return hit_monsters


def _defended_dodge_chance(defender, defense: int, special: bool) -> float:
    """dungeon.dodge_chance's base roll (against `defense`, already debuff-adjusted by the caller)
    plus any active dodge_buff (Physical) / resist_buff (Special) bonus from the defender's own
    timed_effects -- still capped at dungeon.DODGE_CAP, the same hard ceiling that already bounds
    DEF/SpDef stacking alone (see dungeon.py's comment on DODGE_CAP)."""
    buff_type = "resist_buff" if special else "dodge_buff"
    bonus = next((e["value"] for e in defender.timed_effects if e["type"] == buff_type), 0)
    return min(dungeon.DODGE_CAP, dungeon.dodge_chance(defense) + bonus)


def _tick_timed_effects(entities: list, log_lines: list[str]) -> None:
    """One round's worth of DoT/HoT ticks + duration countdown, for every entity still in the
    fight (players and monsters alike -- both now carry timed_effects). A freshly-applied effect
    doesn't tick the round it's applied (that round's "hit" was the skill's own visible effect, if
    any) -- this is only ever called once per round, after that round's actions/counter-attacks
    are already resolved, so the first tick naturally lands the following round. Doesn't handle
    death/knockout itself from a DoT tick -- callers already check hp<=0 right after this runs."""
    for entity in entities:
        for eff in entity.timed_effects:
            if eff["type"] == "dot":
                dmg = eff["value"]
                entity.hp -= dmg
                log_lines.append(f"{_actor_label(entity)} {_verb(entity, 'take')} **{dmg}** damage from lingering harm.")
            elif eff["type"] == "hot":
                healed = min(entity.max_hp, entity.hp + round(entity.max_hp * eff["value"])) - entity.hp
                entity.hp += healed
                if healed:
                    log_lines.append(f"{_actor_label(entity)} {_verb(entity, 'recover')} **{healed}** HP over time.")
        for eff in entity.timed_effects:
            eff["remaining"] -= 1
        entity.timed_effects = [e for e in entity.timed_effects if e["remaining"] > 0]


def _roll_on_hit_procs(actor, monster_state, equipped_items: list[dict], dmg: int, log_lines: list[str]) -> None:
    """After a damage-dealing hit lands (never on a dodge or a non-damage action -- callers only
    reach this once `dmg` is known), independently rolls each of `actor`'s equipped items' own
    on_hit effect (if any) against its own `chance`. `type == "lifesteal_fraction"` is
    special-cased -- unlike every other on_hit-eligible type, its handler doesn't apply anything
    itself, it only sets a `mods` flag a caller reads back against a damage number that, for an
    on-hit proc, is exactly this hit's own already-known `dmg` -- so it's applied directly here
    with the same formula the primary hit's own lifesteal already uses, instead of going through
    EFFECT_HANDLERS. Every other on_hit-eligible type is fully self-contained (only ever touches
    `actor`/`monster_state`), so it dispatches through the normal EFFECT_HANDLERS unchanged."""
    for item in equipped_items:
        for effect in item["effects"]:
            if effect.get("trigger") != "on_hit" or random.random() >= effect["chance"]:
                continue
            if effect["type"] == "lifesteal_fraction":
                healed = min(actor.max_hp, actor.hp + round(dmg * effect["value"])) - actor.hp
                actor.hp += healed
                if healed:
                    log_lines.append(f"{item['name']} drains **{healed}** HP from the strike.")
            else:
                EFFECT_HANDLERS[effect["type"]](actor, monster_state, effect, log_lines, _default_mods())


def _resolve_monster_attack(
    attacker: "MonsterInstance", target, moon_effect: str | None, log_lines: list[str],
) -> tuple[int, dict | None, bool]:
    """One monster's turn against `target` (anything with `.def_`/`.spdef`/`.hp`/`.timed_effects`
    -- a DelveSession or PartyMember) -- either its plain attack or one of its own skills
    (dungeon.pick_monster_action, weighted by the monster's own attack_chance vs. each skill's own
    chance). `monster_state` is `target` here (not `attacker`) -- a monster skill's effects have
    full parity with a player's own (dungeon.py's module comment above _validate_monster_skill), so
    an opponent-targeted effect (def_shred, the *_debuff family) correctly lands on the player
    being fought, while self-targeted ones (heal/guard/buffs/the timed dodge/resist/dot/hot ones)
    only ever read `actor`, which is still `attacker` either way. `target` first gets a dodge-chance
    roll (base DEF/SpDef minus its own debuff, plus any active dodge_buff/resist_buff -- see
    _defended_dodge_chance) -- a dodge negates the WHOLE action (no effects applied at all, not
    even the monster's own lifesteal), rolled before _apply_effects runs so a dodged hit truly does
    nothing. Returns (damage, skill-or-None, dodged) -- callers apply the damage to `target.hp`
    themselves (solo and party handle the aftermath -- death vs. knockout -- differently) and use
    skill/dodged to log their own "unleashes X" / "dodges" / "strikes" line."""
    skill = dungeon.pick_monster_action(attacker.monster)
    special = bool(skill.get("special")) if skill else False
    target_def = max(0, (target.spdef - target.spdef_debuff) if special else (target.def_ - target.def_debuff))
    if random.random() < _defended_dodge_chance(target, target_def, special):
        return 0, skill, True
    effects = skill["effects"] if skill else []
    mods = _apply_effects(attacker, target, effects, log_lines)
    monster_moon_mult = _moon_combat_multiplier(moon_effect, "monster")
    attacker_atk = (attacker.spatk - attacker.spatk_debuff) if special else (attacker.atk - attacker.atk_debuff)
    dmg = dungeon.roll_damage(attacker_atk, target_def, mods["multiplier"] * monster_moon_mult)
    for extra_multiplier in mods["extra_attack_multipliers"]:
        dmg += dungeon.roll_damage(attacker_atk, target_def, extra_multiplier * monster_moon_mult)
    if mods["lifesteal_fraction"]:
        healed = min(attacker.max_hp, attacker.hp + round(dmg * mods["lifesteal_fraction"])) - attacker.hp
        attacker.hp += healed
        if healed:
            log_lines.append(f"**{attacker.monster['name']}** drains **{healed}** HP from the strike.")
    return dmg, skill, False


async def _build_combat_view(session: DelveSession) -> "CombatView":
    """Fetches current consumable holdings fresh (unlike unlocked_skills, these change turn to
    turn as items get crafted/used) and builds a CombatView reflecting them."""
    held = await asyncio.to_thread(db.get_inventory, session.guild_id, session.user_id)
    usable_items = [dungeon.CONSUMABLES[item_id] for item_id, qty in held.items() if item_id in dungeon.CONSUMABLES and qty > 0]
    return CombatView(session, usable_items)


async def _solo_death_embed(session: DelveSession, log_lines: list[str]) -> discord.Embed:
    """Ends the delve (via _forfeit) and builds the "You Have Fallen" embed -- shared by every
    place a solo player's hp can drop to 0 (their own action's aftermath, a monster's turn, or a
    DoT ticking as their own turn comes up), so there's one death screen, not one per cause."""
    currency = db.get_currency_name(session.guild_id)
    await _forfeit(session)
    return discord.Embed(
        title="💀 You Have Fallen",
        description="\n".join(log_lines) + f"\n\nYou're carried out of the dungeon empty-handed, losing this "
        f"delve's **{session.loot_total}** {currency} haul.",
        color=discord.Color.dark_red(),
    )


async def _advance_solo_turns(interaction: discord.Interaction, session: DelveSession, log_lines: list[str]) -> None:
    """Resolves consecutive automatic monster turns (per dungeon.preview_next_turns) until either
    the player's own turn comes up or the fight/room ends -- called both when entering/resuming a
    combat room (_build_room_display, log_lines seeded with room/monster flavor -- a fast enough
    monster group can land a hit before the player's very first action, a real CTB ambush) and
    after the player's own action resolves (_resolve_combat_turn's tail, right after their own
    turn_clock advances). Always ends by sending exactly one response via
    interaction.response.edit_message -- the player's own turn view, a death screen, or a
    room-cleared screen. A monster's own DoT-caused death is handled inline here (award the kill,
    `continue` the loop) rather than the old batch-collect-casualties pattern -- there's no longer
    a "round" to batch within, just this one monster's turn."""
    moon_effect = moon.effect_for("dungeon")
    player_moon_mult = _moon_combat_multiplier(moon_effect, "player")
    while True:
        if not session.living_monsters():
            await _present_room_result(interaction, session, log_lines)
            return

        combatants = [{"id": None, "speed": max(0, session.speed - session.speed_debuff), "clock": session.turn_clock}]
        combatants += [
            {"id": m.slot, "speed": max(0, m.speed - m.speed_debuff), "clock": m.turn_clock}
            for m in session.living_monsters()
        ]
        next_id = dungeon.preview_next_turns(combatants, 1)[0]

        if next_id is None:
            # The player's own turn -- tick their own timed effects right as it comes up (a DoT
            # can end the fight here, before they ever get to act) then render and hand control
            # back.
            _tick_timed_effects([session], log_lines)
            if session.hp <= 0:
                embed = await _solo_death_embed(session, log_lines)
                await interaction.response.edit_message(embed=embed, attachments=[], view=None)
                return
            embed, file = _combat_embed(session, "\n".join(log_lines))
            view = await _build_combat_view(session)
            await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
            return

        monster = next(m for m in session.living_monsters() if m.slot == next_id)
        _tick_timed_effects([monster], log_lines)
        monster.turn_clock += dungeon.turn_interval(max(1, monster.speed - monster.speed_debuff))
        if monster.hp <= 0:
            log_lines.append(f"**{monster.monster['name']}** succumbs to its wounds!")
            await _award_kill(
                session.guild_id, monster.monster, session, session.current_room_id, log_lines,
                loot_mult=session.loot_mult * player_moon_mult, chance_mult=1.0,
            )
            continue  # never got to act this turn -- loop re-checks living_monsters() at the top

        monster_dmg, monster_skill, dodged = _resolve_monster_attack(monster, session, moon_effect, log_lines)
        verb = f"unleashes **{monster_skill['name']}**" if monster_skill else "strikes back"
        if dodged:
            log_lines.append(f"You dodge **{monster.monster['name']}**'s attack!")
        else:
            monster_dmg = _consume_guard_charge(session, monster_dmg, log_lines)
            log_lines.append(f"**{monster.monster['name']}** {verb} for **{monster_dmg}**.")
            session.hp -= monster_dmg

        if session.hp <= 0:
            embed = await _solo_death_embed(session, log_lines)
            await interaction.response.edit_message(embed=embed, attachments=[], view=None)
            return


async def _resolve_combat_turn(
    interaction: discord.Interaction, session: DelveSession, effects: list[dict], verb: str, log_lines: list[str],
    special: bool = False,
) -> bool:
    """Resolves the player's own chosen action (plain Attack, a skill, or a consumed item) --
    applies `effects` via _resolve_player_action (each effect independently single-target or AOE
    per its own "aoe" flag -- solo's `ally_pool` is always just [session], nothing else to expand
    an ally-aoe effect to, but an enemy-aoe effect/attack still meaningfully hits every monster in
    a multi-monster solo room), then checks for a kill/room-clear per monster that took damage --
    then hands off to _advance_solo_turns for whatever happens next (automatic monster turns, in
    speed order, until the player's own turn comes back around). `verb` only matters if a damage
    roll happens (e.g. "attack", "unleash **Fireball**", "use **Healing Draught**") -- callers that
    only heal/buff never reach the line that reads it. `special` (always False for a plain Attack)
    picks SpAtk/SpDef instead of ATK/DEF for the damage roll -- set by callers from the triggering
    skill/item's own "special" flag. Always returns True (this always consumes the turn -- any
    "can't do this right now" rejection happens before this is called)."""
    moon_effect = moon.effect_for("dungeon")
    player_moon_mult = _moon_combat_multiplier(moon_effect, "player")
    equipped_items = [dungeon.EQUIPMENT[iid] for iid in session.equipped.values()]
    hit_monsters = _resolve_player_action(
        session, [session], session.living_monsters(), session.current_target(), effects, special, verb,
        "You", "your", "drain", player_moon_mult, equipped_items, False, log_lines,
    )

    session.turn_clock += dungeon.turn_interval(max(1, session.speed - session.speed_debuff))

    for target in hit_monsters:
        if target.hp <= 0:
            log_lines.append(f"**{target.monster['name']} is defeated!**")
            await _award_kill(
                session.guild_id, target.monster, session, session.current_room_id, log_lines,
                loot_mult=session.loot_mult * player_moon_mult, chance_mult=1.0,
            )
    if hit_monsters and not session.living_monsters():
        await _present_room_result(interaction, session, log_lines)
        return True

    await _advance_solo_turns(interaction, session, log_lines)
    return True


async def _handle_action(interaction: discord.Interaction, session: DelveSession, skill: dict | None) -> bool:
    """`skill` is the unlocked skill dict the player chose (an Ability button), or None for a
    plain Attack. Returns whether this actually consumed the player's turn (False only for the
    not-enough-chips rejection, which leaves the calling CombatView live and waiting) -- callers
    use this to decide whether to stop() the view that dispatched them."""
    if skill is not None and skill["chip_cost"] > session.chips:
        await interaction.response.send_message("Not enough Chips to use that skill.", ephemeral=True)
        return False

    effects = skill["effects"] if skill is not None else []
    special = bool(skill.get("special")) if skill is not None else False
    if skill is not None:
        session.chips -= skill["chip_cost"]
    verb = f"unleash **{skill['name']}**" if skill is not None else "attack"
    return await _resolve_combat_turn(interaction, session, effects, verb, [], special)


async def _handle_use_item(interaction: discord.Interaction, session: DelveSession, item: dict) -> bool:
    """Consumes one of `item` and resolves its effects as a full turn -- not gated by Chips
    (items are separately scarce, via what the crafting economy actually produces) but it still
    costs a turn and still draws the monster's counter-attack, so stockpiling items can't
    trivialize a fight for free."""
    consumed = await asyncio.to_thread(db.consume_inventory_item, session.guild_id, session.user_id, item["id"], 1)
    if not consumed:
        await interaction.response.send_message("You don't have that anymore.", ephemeral=True)
        return False
    verb = f"use **{item['name']}**"
    return await _resolve_combat_turn(interaction, session, item["effects"], verb, [], item.get("special", False))


async def _handle_cast_item(interaction: discord.Interaction, session: DelveSession, item: dict) -> bool:
    """Triggers `item`'s on_use effects as a full turn -- mechanically identical to
    _handle_use_item's consumable cast (same _resolve_combat_turn tail), just not consumed and
    gated by a once-per-fight-per-item flag (session.used_item_effects) instead of an inventory
    quantity."""
    if item["id"] in session.used_item_effects:
        await interaction.response.send_message("You've already used that this fight.", ephemeral=True)
        return False
    session.used_item_effects.add(item["id"])
    effects = [e for e in item["effects"] if e["trigger"] == "on_use"]
    verb = f"unleash **{item['name']}**"
    return await _resolve_combat_turn(interaction, session, effects, verb, [], item.get("special", False))


async def _present_room_result(interaction: discord.Interaction, session: DelveSession, log_lines: list[str]):
    currency = db.get_currency_name(session.guild_id)
    room = session.rooms_by_id[session.current_room_id]
    next_room = room.get("next")

    embed = discord.Embed(title="🏆 Room Cleared!", description="\n".join(log_lines), color=discord.Color.gold())
    embed.add_field(name="Loot this delve", value=f"{session.loot_total} {currency}", inline=True)
    embed.add_field(name="HP", value=f"{session.hp}/{session.max_hp}", inline=True)

    if next_room is None:
        balance = await asyncio.to_thread(db.update_balance, session.guild_id, session.user_id, session.loot_total)
        await asyncio.to_thread(db.log_bet, session.guild_id, session.user_id, "dungeon", 0, session.loot_total)
        await asyncio.to_thread(db.set_current_hp, session.guild_id, session.user_id, session.hp)
        _cleanup(session)
        embed.description += f"\n\nYou've cleared the dungeon! Balance: **{balance}** {currency}."
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        return

    embed.description += "\n\nRetreat with your loot, or push deeper for a tougher fight and better rewards?"
    view = RoomResultView(session)
    await interaction.response.edit_message(embed=embed, attachments=[], view=view)


# --- Party combat ---------------------------------------------------------------------------
# Same CTB scheduling as solo (dungeon.preview_next_turns), just run across every living member
# AND every living monster at once instead of one player -- see _advance_party_turns, the party
# sibling of _advance_solo_turns.


def _party_turn_order_cards(session: PartyDelveSession) -> list[dict]:
    living_members = session.living_members()
    living_monsters = session.living_monsters()
    combatants = [
        {"id": m.user_id, "speed": max(0, m.speed - m.speed_debuff), "clock": m.turn_clock} for m in living_members
    ] + [
        {"id": m.slot, "speed": max(0, m.speed - m.speed_debuff), "clock": m.turn_clock} for m in living_monsters
    ]
    member_ids = {m.user_id for m in living_members}
    members_by_id = {m.user_id: m for m in living_members}
    monsters_by_slot = {m.slot: m for m in living_monsters}
    order = dungeon.preview_next_turns(combatants, TURN_ORDER_PREVIEW_COUNT)
    cards = []
    for cid in order:
        if cid in member_ids:
            member = members_by_id[cid]
            cards.append(_player_card(member.player_name, member.main_class, member.subclass))
        else:
            cards.append(_monster_card(monsters_by_slot[cid].monster))
    return cards


def _party_combat_embed(
    session: PartyDelveSession, log_text: str, current_actor: PartyMember,
) -> tuple[discord.Embed, discord.File]:
    living_monsters = session.living_monsters()
    title = f"🗡️ {living_monsters[0].monster['name']}" if len(living_monsters) == 1 else "🗡️ Combat"
    embed = discord.Embed(title=title, description=log_text, color=discord.Color.dark_red())
    for m in session.members:
        if m.knocked_out:
            status = "💀 Knocked out"
        elif m.user_id == current_actor.user_id:
            status = f"❤️ HP {max(m.hp, 0)}/{m.max_hp}\n🪙 Chips {m.chips}/{m.max_chips} ⬅️ acting now"
        else:
            status = f"❤️ HP {max(m.hp, 0)}/{m.max_hp}\n🪙 Chips {m.chips}/{m.max_chips}"
        embed.add_field(name=m.label, value=status, inline=True)
    # Only the currently-acting member's target is shown -- every other member's independent
    # target choice isn't relevant to the embed until it's their own turn.
    target_slot = session.target_for(current_actor).slot if living_monsters else None
    for m in living_monsters:
        marker = " ⬅️ target" if len(living_monsters) > 1 and m.slot == target_slot else ""
        embed.add_field(name=m.monster["name"], value=f"❤️ HP {max(m.hp, 0)}/{m.max_hp}{marker}", inline=True)
    room = session.rooms_by_id[session.current_room_id]
    buf = dungeon_render.render_room(
        session.rooms_visited, [m.monster for m in living_monsters], _room_background_path(session.delve, room),
        turn_order=_party_turn_order_cards(session),
    )
    file = discord.File(buf, filename="room.png")
    embed.set_image(url="attachment://room.png")
    return embed, file


async def _usable_items_for(session: PartyDelveSession, actor: PartyMember) -> list[dict]:
    held = await asyncio.to_thread(db.get_inventory, session.guild_id, actor.user_id)
    return [dungeon.CONSUMABLES[item_id] for item_id, qty in held.items() if item_id in dungeon.CONSUMABLES and qty > 0]


async def _build_party_combat_view(session: PartyDelveSession, actor: PartyMember) -> "PartyCombatView":
    return PartyCombatView(session, actor, await _usable_items_for(session, actor))


async def _send_party_update(
    interaction: discord.Interaction | None, session: PartyDelveSession,
    embed: discord.Embed, file: discord.File | None, view: discord.ui.View | None,
):
    """Party combat can advance from either a live interaction (a member's own action) or a
    timeout (a stalled member's turn getting skipped, with no interaction to respond to) -- this
    is the one place that branches on which, editing the response either way."""
    attachments = [file] if file else []
    if interaction is not None:
        await interaction.response.edit_message(embed=embed, attachments=attachments, view=view)
        return
    if session.message is None:
        return
    try:
        await session.message.edit(embed=embed, attachments=attachments, view=view)
    except discord.HTTPException:
        pass


async def _advance_party_turns(interaction: discord.Interaction | None, session: PartyDelveSession, log_lines: list[str]) -> None:
    """Party sibling of _advance_solo_turns: resolves consecutive automatic monster turns (per
    dungeon.preview_next_turns, scheduled across every living member AND every living monster at
    once -- member ids are real Discord user_ids, monster ids are their small 0-3 slot, so the two
    id spaces never collide) until either some living member's own turn comes up or the fight/room
    ends. Called both when entering/resuming a combat room (_build_party_room_display, log_lines
    seeded with room/monster flavor -- a fast enough monster group can land a hit before anyone's
    very first action, a real CTB ambush) and after a member's own action or a timed-out skip
    resolves (both advance that member's own turn_clock first, then call this). Always ends by
    sending exactly one response via _send_party_update -- the next acting member's own turn view,
    a party-wipe screen, or a room-cleared screen. A monster's own DoT-caused death is handled
    inline here (award the kill to every living member, `continue` the loop) rather than the old
    batch-collect-casualties pattern -- there's no longer a "round" to batch within."""
    moon_effect = moon.effect_for("dungeon")
    player_moon_mult = _moon_combat_multiplier(moon_effect, "player")
    while True:
        if not session.living_monsters():
            await _present_party_room_result(interaction, session, log_lines)
            return

        if not session.living_members():
            for m in session.members:
                await asyncio.to_thread(db.set_current_hp, session.guild_id, m.user_id, m.hp)
            _cleanup(session)
            embed = discord.Embed(
                title="💀 Your Party Has Fallen",
                description="\n".join(log_lines) + "\n\nEveryone's down — the party stumbles out empty-handed, "
                "losing every haul from this delve.",
                color=discord.Color.dark_red(),
            )
            await _send_party_update(interaction, session, embed, None, None)
            return

        combatants = [
            {"id": m.user_id, "speed": max(0, m.speed - m.speed_debuff), "clock": m.turn_clock}
            for m in session.living_members()
        ] + [
            {"id": m.slot, "speed": max(0, m.speed - m.speed_debuff), "clock": m.turn_clock}
            for m in session.living_monsters()
        ]
        next_id = dungeon.preview_next_turns(combatants, 1)[0]
        member_ids = {m.user_id for m in session.living_members()}

        if next_id in member_ids:
            # A living member's own turn -- tick their own timed effects right as it comes up (a
            # DoT can knock them out here, before they ever get to act).
            member = session.members_by_id[next_id]
            _tick_timed_effects([member], log_lines)
            if member.hp <= 0:
                member.knocked_out = True
                log_lines.append(f"💀 **{member.label}** is knocked out!")
                continue
            embed, file = _party_combat_embed(session, "\n".join(log_lines), member)
            view = await _build_party_combat_view(session, member)
            await _send_party_update(interaction, session, embed, file, view)
            return

        monster = next(m for m in session.living_monsters() if m.slot == next_id)
        _tick_timed_effects([monster], log_lines)
        monster.turn_clock += dungeon.turn_interval(max(1, monster.speed - monster.speed_debuff))
        if monster.hp <= 0:
            log_lines.append(f"**{monster.monster['name']}** succumbs to its wounds!")
            for m in session.living_members():
                member_log: list[str] = []
                loot_mult = m.loot_mult * (1.0 if m.is_leader else 0.5) * player_moon_mult
                chance_mult = 1.0 if m.is_leader else 0.5
                await _award_kill(
                    session.guild_id, monster.monster, m, session.current_room_id, member_log,
                    loot_mult=loot_mult, chance_mult=chance_mult,
                )
                log_lines.append(f"**{m.label}**")
                log_lines.extend(f"> {line}" for line in member_log)
            continue  # never got to act this turn -- loop re-checks living_monsters() at the top

        living = session.living_members()
        if not living:
            continue  # loop re-checks at the top and hits the party-wipe branch
        # Weighted by THIS monster's own threat table (dungeon.pick_target_by_threat), not a flat
        # coin flip -- who it's most likely to go after is a function of damage dealt against it
        # specifically and any taunt/lower_threat used, read live so a mid-fight swing changes the
        # very next pick.
        target_id = dungeon.pick_target_by_threat(
            [{"id": m.user_id, "threat": monster.threat.get(m.user_id, 0)} for m in living]
        )
        target = session.members_by_id[target_id]
        monster_dmg, monster_skill, dodged = _resolve_monster_attack(monster, target, moon_effect, log_lines)
        if dodged:
            log_lines.append(f"**{target.label}** dodges **{monster.monster['name']}**'s attack!")
        else:
            monster_dmg = _consume_guard_charge(target, monster_dmg, log_lines)
            verb = f"unleashes **{monster_skill['name']}** on" if monster_skill else "strikes"
            log_lines.append(f"**{monster.monster['name']}** {verb} **{target.label}** for **{monster_dmg}**.")
            target.hp -= monster_dmg
            if target.hp <= 0:
                target.knocked_out = True
                log_lines.append(f"💀 **{target.label}** is knocked out!")


async def _resolve_party_turn(
    interaction: discord.Interaction, session: PartyDelveSession, member: PartyMember,
    effects: list[dict], verb: str, log_lines: list[str], special: bool = False,
) -> bool:
    """Party sibling of _resolve_combat_turn: applies `effects` via _resolve_player_action (each
    effect independently single-target or AOE per its own "aoe" flag -- ally-aoe expands to
    `session.living_members()`, enemy-aoe expands to `session.living_monsters()`), and on each kill
    runs the party's independent-per-member reward loop (leader at full rate, joiners halved -- see
    _award_kill). Advances `member`'s own turn_clock, then hands off to _advance_party_turns for
    whatever happens next (automatic member/monster turns, in speed order, until some living
    member's own turn comes back around). `special` picks SpAtk/SpDef instead of ATK/DEF for the
    damage roll, same as _resolve_combat_turn."""
    target = session.target_for(member)
    moon_effect = moon.effect_for("dungeon")
    player_moon_mult = _moon_combat_multiplier(moon_effect, "player")
    equipped_items = [dungeon.EQUIPMENT[iid] for iid in member.equipped.values()]
    hit_monsters = _resolve_player_action(
        member, session.living_members(), session.living_monsters(), target, effects, special, verb,
        member.label, f"{member.label}'s", "drains", player_moon_mult, equipped_items, True, log_lines,
    )

    member.turn_clock += dungeon.turn_interval(max(1, member.speed - member.speed_debuff))

    for target in hit_monsters:
        if target.hp <= 0:
            log_lines.append(f"**{target.monster['name']} is defeated!**")
            for m in session.living_members():
                member_log: list[str] = []
                loot_mult = m.loot_mult * (1.0 if m.is_leader else 0.5) * player_moon_mult
                chance_mult = 1.0 if m.is_leader else 0.5
                await _award_kill(
                    session.guild_id, target.monster, m, session.current_room_id, member_log,
                    loot_mult=loot_mult, chance_mult=chance_mult,
                )
                log_lines.append(f"**{m.label}**")
                log_lines.extend(f"> {line}" for line in member_log)
    if hit_monsters and not session.living_monsters():
        await _present_party_room_result(interaction, session, log_lines)
        return True

    await _advance_party_turns(interaction, session, log_lines)
    return True


async def _skip_party_turn(session: PartyDelveSession, member: PartyMember):
    """A stalled member's turn timing out -- they simply pass (no damage dealt, no penalty beyond
    losing this turn) rather than forfeiting the whole party over one AFK player. Advances their
    own turn_clock exactly as a real action would (same clock-advance path _resolve_party_turn
    uses), then re-enters the auto-turn loop. PartyCombatView.on_timeout already guards against a
    stale/superseded view firing this twice (session.current_view is not self), so no separate
    "already resolved" check is needed here."""
    log_lines = [f"⌛ {member.label} takes too long and passes their turn."]
    member.turn_clock += dungeon.turn_interval(max(1, member.speed - member.speed_debuff))
    await _advance_party_turns(None, session, log_lines)


async def _handle_party_action(
    interaction: discord.Interaction, session: PartyDelveSession, member: PartyMember, skill: dict | None,
) -> bool:
    if skill is not None and skill["chip_cost"] > member.chips:
        await interaction.response.send_message("Not enough Chips to use that skill.", ephemeral=True)
        return False
    effects = skill["effects"] if skill is not None else []
    special = bool(skill.get("special")) if skill is not None else False
    if skill is not None:
        member.chips -= skill["chip_cost"]
    verb = f"unleash **{skill['name']}**" if skill is not None else "attack"
    return await _resolve_party_turn(interaction, session, member, effects, verb, [], special)


async def _handle_party_use_item(
    interaction: discord.Interaction, session: PartyDelveSession, member: PartyMember, item: dict,
) -> bool:
    consumed = await asyncio.to_thread(db.consume_inventory_item, session.guild_id, member.user_id, item["id"], 1)
    if not consumed:
        await interaction.response.send_message("You don't have that anymore.", ephemeral=True)
        return False
    verb = f"use **{item['name']}**"
    return await _resolve_party_turn(
        interaction, session, member, item["effects"], verb, [], item.get("special", False)
    )


async def _handle_party_cast_item(
    interaction: discord.Interaction, session: PartyDelveSession, member: PartyMember, item: dict,
) -> bool:
    """Party sibling of _handle_cast_item -- same once-per-fight-per-item gate, scoped to this
    member's own used_item_effects."""
    if item["id"] in member.used_item_effects:
        await interaction.response.send_message("You've already used that this fight.", ephemeral=True)
        return False
    member.used_item_effects.add(item["id"])
    effects = [e for e in item["effects"] if e["trigger"] == "on_use"]
    verb = f"unleash **{item['name']}**"
    return await _resolve_party_turn(interaction, session, member, effects, verb, [], item.get("special", False))


class PartyAttackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Attack", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        if await _handle_party_action(interaction, self.view.session, self.view.actor, skill=None):
            self.view.stop()


class PartySkillButton(discord.ui.Button):
    def __init__(self, skill: dict, disabled: bool):
        super().__init__(label=skill["name"], style=discord.ButtonStyle.success, disabled=disabled, row=0)
        self.skill = skill

    async def callback(self, interaction: discord.Interaction):
        if await _handle_party_action(interaction, self.view.session, self.view.actor, skill=self.skill):
            self.view.stop()


class PartyUseItemButton(discord.ui.Button):
    def __init__(self, item: dict):
        super().__init__(label=f"🧪 {item['name']}", style=discord.ButtonStyle.secondary, row=1)
        self.item = item

    async def callback(self, interaction: discord.Interaction):
        if await _handle_party_use_item(interaction, self.view.session, self.view.actor, self.item):
            self.view.stop()


class PartyUseItemSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        options = [
            discord.SelectOption(label=item["name"], value=item["id"], description=item["flavor"][:100])
            for item in items[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(placeholder="🧪 Use an item...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        item = dungeon.CONSUMABLES[self.values[0]]
        if await _handle_party_use_item(interaction, self.view.session, self.view.actor, item):
            self.view.stop()


class PartyCastItemButton(discord.ui.Button):
    """Party sibling of CastItemButton -- disabled once its own id is in this member's own
    used_item_effects for this fight."""

    def __init__(self, item: dict, disabled: bool):
        super().__init__(label=f"✨ {item['name']}", style=discord.ButtonStyle.secondary, disabled=disabled, row=3)
        self.item = item

    async def callback(self, interaction: discord.Interaction):
        if await _handle_party_cast_item(interaction, self.view.session, self.view.actor, self.item):
            self.view.stop()


class PartyTargetSelect(discord.ui.Select):
    """Party sibling of TargetSelect -- scoped to whichever member's turn it currently is; picking
    an option only updates that member's own entry in session.member_target_slots (see
    PartyDelveSession.target_for), so it never advances a turn_clock or costs a turn."""

    def __init__(self, session: PartyDelveSession, actor: PartyMember):
        target = session.target_for(actor)
        options = [
            discord.SelectOption(
                label=f"{m.monster['name']} ({m.hp}/{m.max_hp} HP)", value=str(m.slot),
                default=m.slot == target.slot,
            )
            for m in session.living_monsters()
        ]
        super().__init__(placeholder="🎯 Choose a target...", options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        session: PartyDelveSession = self.view.session
        session.member_target_slots[self.view.actor.user_id] = int(self.values[0])
        log_text = interaction.message.embeds[0].description or ""
        embed, file = _party_combat_embed(session, log_text, self.view.actor)
        view = await _build_party_combat_view(session, self.view.actor)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
        self.view.stop()


class PartyCombatView(discord.ui.View):
    def __init__(self, session: PartyDelveSession, actor: PartyMember, usable_items: list[dict] | None = None):
        super().__init__(timeout=PARTY_ACTION_TIMEOUT)
        self.session = session
        self.actor = actor
        self.add_item(PartyAttackButton())
        for skill in actor.unlocked_skills:
            self.add_item(PartySkillButton(skill, disabled=skill["chip_cost"] > actor.chips))
        usable_items = usable_items or []
        if len(usable_items) == 1:
            self.add_item(PartyUseItemButton(usable_items[0]))
        elif len(usable_items) > 1:
            self.add_item(PartyUseItemSelect(usable_items))
        for item in castable_equipment(actor.equipped):
            self.add_item(PartyCastItemButton(item, disabled=item["id"] in actor.used_item_effects))
        if len(session.living_monsters()) > 1:
            self.add_item(PartyTargetSelect(session, actor))
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor.user_id:
            await interaction.response.send_message(f"It's {self.actor.label}'s turn.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return  # superseded -- combat already moved on through some other path
        await _skip_party_turn(session, self.actor)


async def _apply_party_retreat(session: PartyDelveSession) -> discord.Embed:
    currency = db.get_currency_name(session.guild_id)
    payouts = []
    for m in session.members:
        balance = await asyncio.to_thread(db.update_balance, session.guild_id, m.user_id, m.loot_total)
        await asyncio.to_thread(db.log_bet, session.guild_id, m.user_id, "dungeon", 0, m.loot_total)
        await asyncio.to_thread(db.set_current_hp, session.guild_id, m.user_id, m.hp)
        payouts.append(f"{m.label}: **{m.loot_total}** {currency} (balance **{balance}**)")
    _cleanup(session)
    return discord.Embed(title="🏃 Party Retreated Safely", description="\n".join(payouts), color=discord.Color.green())


# --- Party choice rooms ---------------------------------------------------------------------
# Resolved by the party leader alone (their own stats/inventory/cost/hp) -- same authority
# PartyRoomResultView already gives Push Deeper/Retreat. Letting each member pick independently
# would put party members in different rooms at once, breaking the single-shared-session
# invariant party mode depends on.


def _party_choice_embed(session: PartyDelveSession, room: dict, description: str) -> tuple[discord.Embed, discord.File]:
    currency = db.get_currency_name(session.guild_id)
    embed = discord.Embed(title="🚪 A Choice", description=description, color=discord.Color.blurple())
    for m in session.members:
        status = "💀 Knocked out" if m.knocked_out else f"❤️ HP {max(m.hp, 0)}/{m.max_hp}"
        embed.add_field(name=m.label, value=status, inline=True)
    for action in room["actions"]:
        lines = _action_summary_lines(action, currency)
        embed.add_field(name=action["label"], value="\n".join(lines) if lines else "—", inline=True)
    buf = dungeon_render.render_room(session.rooms_visited, [], _room_background_path(session.delve, room))
    file = discord.File(buf, filename="room.png")
    embed.set_image(url="attachment://room.png")
    return embed, file


async def _build_party_choice_room_view(session: PartyDelveSession, room: dict) -> "PartyChoiceRoomView":
    leader = next(m for m in session.members if m.is_leader)
    character = {"main_class": leader.main_class, "subclass": leader.subclass}
    availability = []
    for action in room["actions"]:
        requires = action.get("requires")
        ok = True
        if requires is not None:
            ok = await quests.trigger_satisfied(session.guild_id, leader.user_id, requires, character=character)
        availability.append(ok)
    return PartyChoiceRoomView(session, leader, room, availability)


async def _build_party_room_display(
    interaction: discord.Interaction | None, session: PartyDelveSession, intro_text: str = "",
) -> None:
    """Party sibling of _build_room_display -- renders (and sends, via _send_party_update)
    whatever room the party is CURRENTLY on. A combat room doesn't just show a static view --
    entering one can resolve several automatic turns (any mix of members and monsters) before any
    human gets to act, so intro_text and the room's own flavor (_combat_intro_text) are seeded as
    the STARTING log lines and handed to _advance_party_turns, which does the actual
    rendering/sending for combat."""
    room = session.rooms_by_id[session.current_room_id]
    if room["type"] == "combat":
        log_lines = [intro_text] if intro_text else []
        log_lines.append(_combat_intro_text(room, [m.monster for m in session.living_monsters()]))
        await _advance_party_turns(interaction, session, log_lines)
    else:
        embed, file = _party_choice_embed(session, room, room["prompt"])
        if intro_text:
            embed.description = f"{intro_text}\n\n{embed.description}"
        view = await _build_party_choice_room_view(session, room)
        await _send_party_update(interaction, session, embed, file, view)


async def _goto_party_room(interaction: discord.Interaction, session: PartyDelveSession, room_id: str, intro_text: str = ""):
    """Party sibling of _goto_room -- Push Deeper (after a combat victory) and a leader's choice
    action outcome both funnel through this."""
    session.rooms_visited += 1
    session._enter_room(room_id)
    await _build_party_room_display(interaction, session, intro_text)


async def _handle_party_choice_action(
    interaction: discord.Interaction, session: PartyDelveSession, leader: PartyMember, room: dict, action: dict,
) -> bool:
    """Party sibling of _handle_choice_action -- requires/cost still always gate against the
    leader's own class/inventory/currency (the leader is the one deciding to spend the party's
    resources), but an action with a skill check now hands off to a member picker
    (PartyCheckActorPickerView) instead of always rolling against the leader's own stat -- see
    _resolve_party_choice_action for the part that actually rolls the check and applies its
    outcome (hp_delta included) to whichever member ends up attempting it. A single-survivor party
    skips the picker (nothing to choose) and resolves against that one member directly."""
    requires = action.get("requires")
    if requires is not None:
        character = {"main_class": leader.main_class, "subclass": leader.subclass}
        satisfied = await quests.trigger_satisfied(session.guild_id, leader.user_id, requires, character=character)
        if not satisfied:
            await interaction.response.send_message("You don't meet the requirements for that.", ephemeral=True)
            return False

    cost = action.get("cost")
    if cost is not None:
        materials = {cost["item_id"]: cost.get("item_qty", 1)} if cost.get("item_id") else {}
        status, _balance = await asyncio.to_thread(
            db.craft_item, session.guild_id, leader.user_id, materials, cost.get("currency", 0)
        )
        if status != "ok":
            reason = "You can't afford that." if status == "broke" else "You don't have what that requires."
            await interaction.response.send_message(reason, ephemeral=True)
            return False

    living = session.living_members()
    if action.get("check") is not None and len(living) > 1:
        embed = discord.Embed(
            title="🎯 Who Attempts This?",
            description=f"Who in the party will attempt **{action['label']}**?",
            color=discord.Color.blurple(),
        )
        view = PartyCheckActorPickerView(session, leader, room, action)
        await interaction.response.edit_message(embed=embed, attachments=[], view=view)
        return True

    actor = living[0] if living else leader
    await _resolve_party_choice_action(interaction, session, leader, actor, room, action)
    return True


async def _resolve_party_choice_action(
    interaction: discord.Interaction, session: PartyDelveSession, leader: PartyMember, actor: PartyMember,
    room: dict, action: dict,
):
    """The rest of a party choice action once requires/cost have already passed and (for a check)
    the party has settled on who's attempting it -- `actor` is that member (same as `leader` for a
    no-check action, or a single-survivor party). Rolls the check (if any) against `actor`'s own
    stat and applies hp_delta/knockout to `actor`, not `leader` -- letting the party's tankiest
    member step up for a rough check should mean *they* take the hit, not whoever happened to be
    clicking the button. A knocked-out actor is treated exactly like a combat knockout (skipped,
    party continues) rather than ending the delve -- unless it leaves nobody standing, the same
    full-wipe path _advance_party_turns already uses."""
    log_lines = []
    check = action.get("check")
    if check is not None:
        stat_value = _stat_value_for_check(actor, check["stat"])
        success, rolled = dungeon.roll_check(stat_value, check["dc"])
        stat_label = _CHECK_STAT_LABELS[check["stat"]]
        log_lines.append(
            f"🎲 {actor.label}'s {stat_label} check: rolled **{rolled}** vs DC **{check['dc']}** — "
            f"{'success!' if success else 'failure!'}"
        )
        outcome = action["on_success"] if success else action["on_fail"]
    else:
        outcome = action["on_success"]

    if outcome.get("message"):
        log_lines.append(outcome["message"])

    hp_delta = outcome.get("hp_delta", 0)
    if hp_delta:
        actor.hp = min(actor.max_hp, actor.hp + hp_delta)
        verb = "recovers" if hp_delta > 0 else "takes"
        log_lines.append(f"{actor.label} {verb} **{abs(hp_delta)}** HP.")

    if actor.hp <= 0:
        actor.knocked_out = True
        log_lines.append(f"💀 **{actor.label}** is knocked out!")
        if not session.living_members():
            for m in session.members:
                await asyncio.to_thread(db.set_current_hp, session.guild_id, m.user_id, m.hp)
            _cleanup(session)
            embed = discord.Embed(
                title="💀 Your Party Has Fallen",
                description="\n".join(log_lines) + "\n\nEveryone's down — the party stumbles out empty-handed, "
                "losing every haul from this delve.",
                color=discord.Color.dark_red(),
            )
            await interaction.response.edit_message(embed=embed, attachments=[], view=None)
            return

    next_room = outcome.get("next")
    if next_room is None:
        currency = db.get_currency_name(session.guild_id)
        payouts = []
        for m in session.members:
            balance = await asyncio.to_thread(db.update_balance, session.guild_id, m.user_id, m.loot_total)
            await asyncio.to_thread(db.log_bet, session.guild_id, m.user_id, "dungeon", 0, m.loot_total)
            await asyncio.to_thread(db.set_current_hp, session.guild_id, m.user_id, m.hp)
            payouts.append(f"{m.label}: **{m.loot_total}** {currency} (balance **{balance}**)")
        _cleanup(session)
        embed = discord.Embed(
            title="🏆 Victory!",
            description="\n".join(log_lines) + "\n\nThe party has cleared the dungeon!\n" + "\n".join(payouts),
            color=discord.Color.gold(),
        )
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        return

    await _present_party_choice_outcome(interaction, session, next_room, log_lines)


class PartyCheckActorSelect(discord.ui.Select):
    def __init__(self, session: PartyDelveSession, leader: PartyMember, room: dict, action: dict):
        options = [
            discord.SelectOption(label=m.label, value=str(m.user_id)) for m in session.living_members()
        ]
        super().__init__(placeholder="Choose who attempts this...", options=options, row=0)
        self.leader = leader
        self.room = room
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        session: PartyDelveSession = self.view.session
        actor = session.members_by_id[int(self.values[0])]
        await _resolve_party_choice_action(interaction, session, self.leader, actor, self.room, self.action)
        self.view.stop()


class PartyCheckActorPickerView(discord.ui.View):
    """Shown when the leader picks an action with a skill check, in place of resolving it against
    the leader's own stat right away -- lets the party send up whoever's actually best suited (or
    just whoever's turn it feels like it is), the same "make delegating feel like a real
    multiplayer decision" reasoning as PartyRoomResultView's own Push Deeper/Retreat call. Gated to
    the leader alone, same as every other party room-boundary decision -- picking who else attempts
    something is still the leader's call, not a free-for-all."""

    def __init__(self, session: PartyDelveSession, leader: PartyMember, room: dict, action: dict):
        super().__init__(timeout=PARTY_ACTION_TIMEOUT)
        self.session = session
        self.add_item(PartyCheckActorSelect(session, leader, room, action))
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        leader = next(m for m in self.session.members if m.is_leader)
        if interaction.user.id != leader.user_id:
            await interaction.response.send_message("Only the party leader can decide this.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return
        if session.message is None:
            for m in session.members:
                await asyncio.to_thread(db.set_current_hp, session.guild_id, m.user_id, m.hp)
            _cleanup(session)
            return
        embed = await _apply_party_retreat(session)  # default to the safe choice if the leader doesn't respond
        try:
            await session.message.edit(embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            pass


class PartyChoiceActionButton(discord.ui.Button):
    def __init__(self, action: dict, row: int, disabled: bool):
        super().__init__(label=action["label"][:80], style=discord.ButtonStyle.primary, row=row, disabled=disabled)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if await _handle_party_choice_action(interaction, view.session, view.leader, view.room, self.action):
            view.stop()


class PartyRetreatFromChoiceButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Retreat with Loot", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        embed = await _apply_party_retreat(self.view.session)
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        self.view.stop()


class PartyChoiceRoomView(discord.ui.View):
    """Every room-entry screen (this one and PartyRoomResultView) offers Retreat -- same reasoning
    as solo's ChoiceRoomView. Gated to the leader alone, like every other party room-boundary
    decision."""

    def __init__(self, session: PartyDelveSession, leader: PartyMember, room: dict, availability: list[bool]):
        super().__init__(timeout=PARTY_ACTION_TIMEOUT)
        self.session = session
        self.leader = leader
        self.room = room
        for i, (action, ok) in enumerate(zip(room["actions"], availability)):
            self.add_item(PartyChoiceActionButton(action, row=min(3, i // 5), disabled=not ok))
        self.add_item(PartyRetreatFromChoiceButton(row=4))
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.leader.user_id:
            await interaction.response.send_message("Only the party leader can decide this.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return
        if session.message is None:
            for m in session.members:
                await asyncio.to_thread(db.set_current_hp, session.guild_id, m.user_id, m.hp)
            _cleanup(session)
            return
        embed = await _apply_party_retreat(session)
        try:
            await session.message.edit(embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            pass


class PartyChoiceOutcomeView(discord.ui.View):
    """Party sibling of ChoiceOutcomeView -- same interstitial-before-transitioning idea, gated to
    the leader alone like every other party room-boundary decision (PartyChoiceRoomView/
    PartyRoomResultView)."""

    def __init__(self, session: PartyDelveSession, next_room_id: str):
        super().__init__(timeout=PARTY_ACTION_TIMEOUT)
        self.session = session
        self.next_room_id = next_room_id
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        leader = next(m for m in self.session.members if m.is_leader)
        if interaction.user.id != leader.user_id:
            await interaction.response.send_message("Only the party leader can decide this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _goto_party_room(interaction, self.session, self.next_room_id)
        self.stop()

    @discord.ui.button(label="Retreat with Loot", style=discord.ButtonStyle.secondary)
    async def retreat_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await _apply_party_retreat(self.session)
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        self.stop()

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return
        if session.message is None:
            for m in session.members:
                await asyncio.to_thread(db.set_current_hp, session.guild_id, m.user_id, m.hp)
            _cleanup(session)
            return
        embed = await _apply_party_retreat(session)
        try:
            await session.message.edit(embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            pass


async def _present_party_choice_outcome(
    interaction: discord.Interaction, session: PartyDelveSession, next_room_id: str, log_lines: list[str],
):
    currency = db.get_currency_name(session.guild_id)
    embed = discord.Embed(title="🚪 Onward", description="\n".join(log_lines), color=discord.Color.blurple())
    for m in session.members:
        status = "💀 Knocked out" if m.knocked_out else f"❤️ HP {max(m.hp, 0)}/{m.max_hp}"
        embed.add_field(name=m.label, value=f"{status} — {m.loot_total} {currency} so far", inline=True)
    view = PartyChoiceOutcomeView(session, next_room_id)
    await interaction.response.edit_message(embed=embed, attachments=[], view=view)


class PartyRoomResultView(discord.ui.View):
    """Push Deeper / Retreat is the party leader's call alone -- simplest default for a decision
    the user didn't specify who should make, avoiding one member unilaterally forcing a choice
    that affects everyone's accumulated stake."""

    def __init__(self, session: PartyDelveSession):
        super().__init__(timeout=PARTY_ACTION_TIMEOUT)
        self.session = session
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        leader = next(m for m in self.session.members if m.is_leader)
        if interaction.user.id != leader.user_id:
            await interaction.response.send_message("Only the party leader can decide this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Retreat with Loot", style=discord.ButtonStyle.success)
    async def retreat_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await _apply_party_retreat(self.session)
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        self.stop()

    @discord.ui.button(label="Push Deeper", style=discord.ButtonStyle.danger)
    async def push_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = self.session
        room = session.rooms_by_id[session.current_room_id]
        # Guaranteed non-None -- this view is only ever shown when _present_party_room_result
        # found a "next" room to push into.
        await _goto_party_room(interaction, session, room["next"], "The party presses deeper into the dungeon...")
        self.stop()

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return
        if session.message is None:
            for m in session.members:
                await asyncio.to_thread(db.set_current_hp, session.guild_id, m.user_id, m.hp)
            _cleanup(session)
            return
        embed = await _apply_party_retreat(session)  # default to the safe choice if the leader doesn't respond
        try:
            await session.message.edit(embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            pass


async def _present_party_room_result(
    interaction: discord.Interaction | None, session: PartyDelveSession, log_lines: list[str],
):
    """`interaction` is optional -- a DoT tick can clear a room mid-_advance_party_turns even when
    that call originated from a timeout-skipped turn (_skip_party_turn calls _advance_party_turns
    with interaction=None) -- routes through _send_party_update, the same
    live-interaction-or-session.message split every other timeout-reachable party response already
    uses."""
    room = session.rooms_by_id[session.current_room_id]
    next_room = room.get("next")
    currency = db.get_currency_name(session.guild_id)

    embed = discord.Embed(title="🏆 Room Cleared!", description="\n".join(log_lines), color=discord.Color.gold())
    for m in session.members:
        status = "💀 Knocked out" if m.knocked_out else f"❤️ HP {max(m.hp, 0)}/{m.max_hp}"
        embed.add_field(name=m.label, value=f"{status} — {m.loot_total} {currency} so far", inline=True)

    if next_room is None:
        payouts = []
        for m in session.members:
            balance = await asyncio.to_thread(db.update_balance, session.guild_id, m.user_id, m.loot_total)
            await asyncio.to_thread(db.log_bet, session.guild_id, m.user_id, "dungeon", 0, m.loot_total)
            await asyncio.to_thread(db.set_current_hp, session.guild_id, m.user_id, m.hp)
            payouts.append(f"{m.label}: **{m.loot_total}** {currency} (balance **{balance}**)")
        _cleanup(session)
        embed.description += "\n\nThe party has cleared the dungeon!\n" + "\n".join(payouts)
        await _send_party_update(interaction, session, embed, None, None)
        return

    embed.description += "\n\nThe party leader decides: retreat with the loot so far, or push deeper?"
    view = PartyRoomResultView(session)
    await _send_party_update(interaction, session, embed, None, view)


def _build_lobby_embed(lobby: PartyLobby) -> discord.Embed:
    delve = lobby.delve
    roster_lines = [
        f"👑 {lobby.member_names[uid]}" if uid == lobby.leader_id else f"• {lobby.member_names[uid]}"
        for uid in lobby.member_ids
    ]
    embed = discord.Embed(
        title=f"👥 Party Forming — {delve['name']}",
        description=(
            f"*{delve['flavor']}*\n\n{len(delve['rooms'])} rooms.\n\n"
            f"Anyone can **Join** for free (no energy cost) — only {lobby.member_names[lobby.leader_id]} "
            f"spends energy, and only once they click Start Delve."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name=f"Party ({len(lobby.member_ids)}/{PARTY_SIZE_CAP})", value="\n".join(roster_lines), inline=False)
    return embed


async def _spend_delve_energy(guild_id: int, user_id: int) -> bool:
    """Spends 1 delve energy and reports whether that succeeded -- unless this guild has delve
    test mode on (db.get_delve_test_mode, toggled by !setdelvetest), in which case starting a delve
    never costs energy at all, the same way test mode already makes every delve playable regardless
    of its own "active" flag (see dungeon.active_delves) -- a test server shouldn't have to `!rest`
    between playtests. The one place both DelveModeChoiceView's Solo button and PartyLobbyView's
    Start Delve button spend the charge, so this rule only needs to live once."""
    if await asyncio.to_thread(db.get_delve_test_mode, guild_id):
        return True
    return await asyncio.to_thread(db.spend_energy, guild_id, user_id, 1)


class PartyLobbyView(discord.ui.View):
    def __init__(self, lobby: PartyLobby):
        super().__init__(timeout=PARTY_LOBBY_TIMEOUT)
        self.lobby = lobby
        lobby.current_view = self

    async def on_timeout(self):
        lobby = self.lobby
        if lobby.current_view is not self:
            return
        _cleanup(lobby)
        if lobby.message is None:
            return
        embed = discord.Embed(
            title="👥 Party Disbanded",
            description="Nobody started the delve in time — no energy was spent.",
            color=discord.Color.dark_grey(),
        )
        try:
            await lobby.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = self.lobby
        user_id = interaction.user.id
        if user_id in lobby.member_ids:
            await interaction.response.send_message("You're already in this party.", ephemeral=True)
            return
        if len(lobby.member_ids) >= PARTY_SIZE_CAP:
            await interaction.response.send_message("This party is full.", ephemeral=True)
            return
        if user_id in active_delves or user_id in busy_players:
            await interaction.response.send_message("Finish up whatever you're already doing first.", ephemeral=True)
            return
        character = await asyncio.to_thread(db.get_character, lobby.guild_id, user_id)
        if character is None:
            await interaction.response.send_message(
                "You don't have a character yet — run `!class` to pick one first.", ephemeral=True
            )
            return
        if character["current_hp"] <= 0:
            await interaction.response.send_message(
                "You're too beat up to delve — run `!rest` to heal first.", ephemeral=True
            )
            return
        lobby.member_ids.append(user_id)
        lobby.member_names[user_id] = interaction.user.display_name
        active_delves[user_id] = lobby
        busy_players.add(user_id)
        await interaction.response.edit_message(embed=_build_lobby_embed(lobby), view=self)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = self.lobby
        user_id = interaction.user.id
        if user_id == lobby.leader_id:
            await interaction.response.send_message("You're leading this party — Cancel it instead.", ephemeral=True)
            return
        if user_id not in lobby.member_ids:
            await interaction.response.send_message("You're not in this party.", ephemeral=True)
            return
        lobby.member_ids.remove(user_id)
        del lobby.member_names[user_id]
        active_delves.pop(user_id, None)
        busy_players.discard(user_id)
        await interaction.response.edit_message(embed=_build_lobby_embed(lobby), view=self)

    @discord.ui.button(label="Start Delve", style=discord.ButtonStyle.primary)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = self.lobby
        if interaction.user.id != lobby.leader_id:
            await interaction.response.send_message("Only the party leader can start the delve.", ephemeral=True)
            return
        has_energy = await _spend_delve_energy(lobby.guild_id, lobby.leader_id)
        if not has_energy:
            await interaction.response.send_message("You're out of energy — run `!rest` to refill it.", ephemeral=True)
            return

        members = []
        for uid in lobby.member_ids:
            is_leader = uid == lobby.leader_id
            character = lobby.leader_character if is_leader else await asyncio.to_thread(db.get_character, lobby.guild_id, uid)
            equipped = await asyncio.to_thread(db.get_equipped_items, lobby.guild_id, uid)
            members.append(PartyMember(lobby.guild_id, uid, lobby.member_names[uid], character, equipped, is_leader))

        session = PartyDelveSession(lobby.guild_id, lobby.delve, members)
        for uid in lobby.member_ids:
            active_delves[uid] = session  # swap PartyLobby -> PartyDelveSession in place, ids stay registered throughout
        await _build_party_room_display(interaction, session)
        session.message = await interaction.original_response()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = self.lobby
        if interaction.user.id != lobby.leader_id:
            await interaction.response.send_message("Only the party leader can cancel.", ephemeral=True)
            return
        _cleanup(lobby)
        embed = discord.Embed(title="👥 Party Cancelled", description="No energy was spent.", color=discord.Color.dark_grey())
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


# --- Dueling (1v1 PvP) ---------------------------------------------------------------------------
# Player-vs-player combat reusing the same combat engine as PvE (_resolve_player_action,
# EFFECT_HANDLERS, dungeon.preview_next_turns/turn_interval) -- each duelist is literally a
# PartyMember (guild_id/user_id/player_name/character/equipped/is_leader -- is_leader/loot_mult/
# loot_total go unused here, harmless), so every combat stat (atk/def_/spatk/spdef/speed + all
# debuffs, timed_effects, guard_charge, hp/max_hp, chips/max_chips, unlocked_skills,
# used_item_effects, .label) already exists with zero new class needed. A duel never touches either
# player's real persisted current_hp -- both start fresh at their own full HP each duel, and
# nothing carries over afterward except an optional currency wager (both sides stake the same
# amount at Accept time, winner takes the pot). No energy cost, no XP/loot, no moon-effect nudge
# (that's a player-vs-monster balance lever with no "monster side" to favor in a PvP fight).
DUEL_CHALLENGE_TIMEOUT = 120  # 2 minutes to accept/decline -- shorter than a party lobby (only one
# specific other person needs to respond, not "gather a group")


class DuelChallenge:
    """Pending state between !duel and the target accepting/declining -- a separate class from
    DuelSession (same reasoning as PartyLobby/PartyDelveSession) so combat-only state never exists
    half-initialized during the accept window."""

    def __init__(
        self, guild_id: int, challenger_id: int, challenger_name: str, target_id: int, target_name: str, wager: int,
    ):
        self.guild_id = guild_id
        self.challenger_id = challenger_id
        self.challenger_name = challenger_name
        self.target_id = target_id
        self.target_name = target_name
        self.wager = wager
        self.message: discord.Message | None = None
        self.current_view: discord.ui.View | None = None

    def all_user_ids(self) -> list[int]:
        return [self.challenger_id, self.target_id]


class DuelSession:
    """Combat state for an active duel, built the moment the target accepts. Always exactly two
    combatants -- no knockout-and-keep-fighting like a party; the duel ends the instant either
    side's hp drops to 0 (see _end_duel)."""

    def __init__(self, guild_id: int, challenger: PartyMember, opponent: PartyMember, wager: int):
        self.guild_id = guild_id
        self.challenger = challenger
        self.opponent = opponent
        self.wager = wager
        self.message: discord.Message | None = None
        self.current_view: discord.ui.View | None = None

    def all_user_ids(self) -> list[int]:
        return [self.challenger.user_id, self.opponent.user_id]

    def other(self, duelist: PartyMember) -> PartyMember:
        return self.opponent if duelist is self.challenger else self.challenger


def build_duel_challenge_embed(challenge: DuelChallenge) -> discord.Embed:
    wager_line = f"\n💰 Wager: **{challenge.wager}** each" if challenge.wager else ""
    return discord.Embed(
        title="⚔️ Duel Challenge",
        description=f"**{challenge.challenger_name}** has challenged **{challenge.target_name}** to a duel!{wager_line}",
        color=discord.Color.gold(),
    )


class DuelChallengeView(discord.ui.View):
    def __init__(self, challenge: DuelChallenge):
        super().__init__(timeout=DUEL_CHALLENGE_TIMEOUT)
        self.challenge = challenge
        challenge.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.challenge.target_id:
            await interaction.response.send_message("This challenge isn't addressed to you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        challenge = self.challenge
        if challenge.current_view is not self:
            return  # superseded -- already accepted/declined through some other path
        _cleanup(challenge)
        if challenge.message is None:
            return
        embed = discord.Embed(
            title="⚔️ Duel Challenge Expired",
            description=f"**{challenge.target_name}** didn't respond in time.",
            color=discord.Color.dark_grey(),
        )
        try:
            await challenge.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = self.challenge
        guild_id = challenge.guild_id

        target_character = await asyncio.to_thread(db.get_character, guild_id, challenge.target_id)
        if target_character is None:
            await interaction.response.send_message(
                "You don't have a character yet — run `!class` to pick one first.", ephemeral=True
            )
            return
        challenger_character = await asyncio.to_thread(db.get_character, guild_id, challenge.challenger_id)
        if challenger_character is None:
            # Extremely unlikely (the challenger somehow lost their character between challenging
            # and now) -- cancel cleanly rather than build a broken duelist.
            _cleanup(challenge)
            embed = discord.Embed(
                title="⚔️ Duel Cancelled", description="The challenger no longer has a character.",
                color=discord.Color.dark_grey(),
            )
            await interaction.response.edit_message(embed=embed, view=None)
            self.stop()
            return

        wager = challenge.wager
        if wager:
            currency = db.get_currency_name(guild_id)
            status, _ = await asyncio.to_thread(db.spend_currency, guild_id, challenge.challenger_id, wager)
            if status != "ok":
                _cleanup(challenge)
                embed = discord.Embed(
                    title="⚔️ Duel Cancelled",
                    description=f"**{challenge.challenger_name}** can no longer afford the **{wager}** {currency} wager.",
                    color=discord.Color.dark_grey(),
                )
                await interaction.response.edit_message(embed=embed, view=None)
                self.stop()
                return
            status, _ = await asyncio.to_thread(db.spend_currency, guild_id, challenge.target_id, wager)
            if status != "ok":
                await asyncio.to_thread(db.update_balance, guild_id, challenge.challenger_id, wager)  # refund
                _cleanup(challenge)
                embed = discord.Embed(
                    title="⚔️ Duel Cancelled",
                    description=f"**{challenge.target_name}** can't afford the **{wager}** {currency} wager.",
                    color=discord.Color.dark_grey(),
                )
                await interaction.response.edit_message(embed=embed, view=None)
                self.stop()
                return

        challenger_equipped = await asyncio.to_thread(db.get_equipped_items, guild_id, challenge.challenger_id)
        target_equipped = await asyncio.to_thread(db.get_equipped_items, guild_id, challenge.target_id)
        challenger = PartyMember(
            guild_id, challenge.challenger_id, challenge.challenger_name, challenger_character, challenger_equipped, True,
        )
        opponent = PartyMember(
            guild_id, challenge.target_id, interaction.user.display_name, target_character, target_equipped, True,
        )

        session = DuelSession(guild_id, challenger, opponent, wager)
        for uid in session.all_user_ids():
            active_delves[uid] = session  # swap DuelChallenge -> DuelSession in place, ids stay reserved throughout
        log_lines = [f"**{challenger.label}** and **{opponent.label}** step into the ring!"]
        await _advance_duel_turns(interaction, session, log_lines)
        session.message = await interaction.original_response()
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = self.challenge
        _cleanup(challenge)
        embed = discord.Embed(
            title="⚔️ Duel Declined", description=f"**{challenge.target_name}** declined the duel.",
            color=discord.Color.dark_grey(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


async def _send_duel_update(
    interaction: discord.Interaction | None, session: DuelSession, embed: discord.Embed,
    file: discord.File | None, view: discord.ui.View | None,
):
    """Duel sibling of _send_party_update -- a duel can advance from either a live interaction (a
    duelist's own action) or a timeout (a stalled duelist's turn getting skipped)."""
    attachments = [file] if file else []
    if interaction is not None:
        await interaction.response.edit_message(embed=embed, attachments=attachments, view=view)
        return
    if session.message is None:
        return
    try:
        await session.message.edit(embed=embed, attachments=attachments, view=view)
    except discord.HTTPException:
        pass


# The duel arena's background -- reuses the "Slug Dome" room's own art (rooms.json's slug_dome
# entry), an arena that already exists for exactly this look. Fixed for now rather than admin-
# configurable (unlike a real room's background_path) -- easy to promote to a setting later if a
# second arena backdrop is ever wanted.
DUEL_ARENA_BACKGROUND = "assets/rooms/slug_dome.jpg"


def _duel_combat_embed(
    session: DuelSession, log_text: str, current_actor: PartyMember,
) -> tuple[discord.Embed, discord.File]:
    embed = discord.Embed(title="⚔️ Duel", description=log_text, color=discord.Color.dark_red())
    if session.wager:
        embed.add_field(name="💰 Wager", value=f"{session.wager} each ({session.wager * 2} to the winner)", inline=False)
    for d in (session.challenger, session.opponent):
        marker = " ⬅️ acting now" if d.user_id == current_actor.user_id else ""
        embed.add_field(
            name=d.label, value=f"❤️ HP {max(d.hp, 0)}/{d.max_hp}\n🪙 Chips {d.chips}/{d.max_chips}{marker}", inline=True,
        )
    buf = dungeon_render.render_room(1, [], DUEL_ARENA_BACKGROUND, label="Duel")
    file = discord.File(buf, filename="duel.png")
    embed.set_image(url="attachment://duel.png")
    return embed, file


async def _end_duel(
    interaction: discord.Interaction | None, session: DuelSession, winner: PartyMember | None, log_lines: list[str],
) -> None:
    """Ends the duel and pays out the wager (if any). `winner=None` is a defensive fallback for a
    simultaneous double-KO -- not actually reachable with today's effect vocabulary (every
    self-damaging effect is a timed dot, and a dot only ever ticks on its own caster's turn, never
    both duelists' at once), kept rather than assumed impossible."""
    currency = db.get_currency_name(session.guild_id)
    if winner is None:
        if session.wager:
            await asyncio.to_thread(db.update_balance, session.guild_id, session.challenger.user_id, session.wager)
            await asyncio.to_thread(db.update_balance, session.guild_id, session.opponent.user_id, session.wager)
        title = "⚔️ Draw!"
        refund_note = " Wagers refunded." if session.wager else ""
        description = "\n".join(log_lines) + f"\n\nBoth duelists go down together.{refund_note}"
    else:
        loser = session.other(winner)
        payout_line = ""
        if session.wager:
            pot = session.wager * 2
            await asyncio.to_thread(db.update_balance, session.guild_id, winner.user_id, pot)
            payout_line = f"\n\n💰 **{winner.label}** wins **{pot}** {currency}!"
        title = f"⚔️ {winner.label} wins!"
        description = "\n".join(log_lines) + f"\n\n💀 **{loser.label}** is defeated.{payout_line}"
    embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
    _cleanup(session)
    await _send_duel_update(interaction, session, embed, None, None)


async def _advance_duel_turns(interaction: discord.Interaction | None, session: DuelSession, log_lines: list[str]) -> None:
    """Duel sibling of _advance_party_turns -- far simpler, since both combatants are real players:
    there's no monster branch to auto-resolve, every turn just shows whoever's turn it is next.
    Always ends by sending exactly one response via _send_duel_update -- the next duelist's own
    turn view, or the duel's end screen."""
    duelists = [session.challenger, session.opponent]
    combatants = [{"id": d.user_id, "speed": max(0, d.speed - d.speed_debuff), "clock": d.turn_clock} for d in duelists]
    next_id = dungeon.preview_next_turns(combatants, 1)[0]
    actor = session.challenger if next_id == session.challenger.user_id else session.opponent
    opponent = session.other(actor)

    # Tick the incoming actor's own timed effects right as their turn comes up (a self-inflicted
    # dot could finish them here, before they ever get to act) -- same point _advance_party_turns
    # ticks a member's/monster's own timed_effects.
    _tick_timed_effects([actor], log_lines)
    if actor.hp <= 0 and opponent.hp <= 0:
        await _end_duel(interaction, session, None, log_lines)
        return
    if actor.hp <= 0:
        await _end_duel(interaction, session, opponent, log_lines)
        return

    embed, file = _duel_combat_embed(session, "\n".join(log_lines), actor)
    view = await _build_duel_combat_view(session, actor)
    await _send_duel_update(interaction, session, embed, file, view)


async def _resolve_duel_turn(
    interaction: discord.Interaction, session: DuelSession, actor: PartyMember, effects: list[dict],
    verb: str, log_lines: list[str], special: bool = False,
) -> bool:
    """Duel sibling of _resolve_party_turn -- always exactly one possible opponent (the other
    duelist), no threat gain (PvP has no monster threat table), no moon multiplier."""
    opponent = session.other(actor)
    equipped_items = [dungeon.EQUIPMENT[iid] for iid in actor.equipped.values()]
    _resolve_player_action(
        actor, [actor], [opponent], opponent, effects, special, verb,
        actor.label, f"{actor.label}'s", "drains", 1.0, equipped_items, False, log_lines,
    )

    actor.turn_clock += dungeon.turn_interval(max(1, actor.speed - actor.speed_debuff))

    if opponent.hp <= 0:
        await _end_duel(interaction, session, actor, log_lines)
        return True

    await _advance_duel_turns(interaction, session, log_lines)
    return True


async def _skip_duel_turn(session: DuelSession, actor: PartyMember):
    """Mirrors _skip_party_turn -- a stalled duelist's turn just passes, no forfeit."""
    log_lines = [f"⌛ {actor.label} takes too long and passes their turn."]
    actor.turn_clock += dungeon.turn_interval(max(1, actor.speed - actor.speed_debuff))
    await _advance_duel_turns(None, session, log_lines)


async def _handle_duel_action(
    interaction: discord.Interaction, session: DuelSession, actor: PartyMember, skill: dict | None,
) -> bool:
    if skill is not None and skill["chip_cost"] > actor.chips:
        await interaction.response.send_message("Not enough Chips to use that skill.", ephemeral=True)
        return False
    effects = skill["effects"] if skill is not None else []
    special = bool(skill.get("special")) if skill is not None else False
    if skill is not None:
        actor.chips -= skill["chip_cost"]
    verb = f"unleash **{skill['name']}**" if skill is not None else "attack"
    return await _resolve_duel_turn(interaction, session, actor, effects, verb, [], special)


async def _handle_duel_use_item(
    interaction: discord.Interaction, session: DuelSession, actor: PartyMember, item: dict,
) -> bool:
    consumed = await asyncio.to_thread(db.consume_inventory_item, session.guild_id, actor.user_id, item["id"], 1)
    if not consumed:
        await interaction.response.send_message("You don't have that anymore.", ephemeral=True)
        return False
    verb = f"use **{item['name']}**"
    return await _resolve_duel_turn(interaction, session, actor, item["effects"], verb, [], item.get("special", False))


async def _handle_duel_cast_item(
    interaction: discord.Interaction, session: DuelSession, actor: PartyMember, item: dict,
) -> bool:
    if item["id"] in actor.used_item_effects:
        await interaction.response.send_message("You've already used that this fight.", ephemeral=True)
        return False
    actor.used_item_effects.add(item["id"])
    effects = [e for e in item["effects"] if e["trigger"] == "on_use"]
    verb = f"unleash **{item['name']}**"
    return await _resolve_duel_turn(interaction, session, actor, effects, verb, [], item.get("special", False))


class DuelAttackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Attack", style=discord.ButtonStyle.primary, row=0)

    async def callback(self, interaction: discord.Interaction):
        if await _handle_duel_action(interaction, self.view.session, self.view.actor, skill=None):
            self.view.stop()


class DuelSkillButton(discord.ui.Button):
    def __init__(self, skill: dict, disabled: bool):
        super().__init__(label=skill["name"], style=discord.ButtonStyle.success, disabled=disabled, row=0)
        self.skill = skill

    async def callback(self, interaction: discord.Interaction):
        if await _handle_duel_action(interaction, self.view.session, self.view.actor, skill=self.skill):
            self.view.stop()


class DuelUseItemButton(discord.ui.Button):
    def __init__(self, item: dict):
        super().__init__(label=f"🧪 {item['name']}", style=discord.ButtonStyle.secondary, row=1)
        self.item = item

    async def callback(self, interaction: discord.Interaction):
        if await _handle_duel_use_item(interaction, self.view.session, self.view.actor, self.item):
            self.view.stop()


class DuelUseItemSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        options = [
            discord.SelectOption(label=item["name"], value=item["id"], description=item["flavor"][:100])
            for item in items[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(placeholder="🧪 Use an item...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        item = dungeon.CONSUMABLES[self.values[0]]
        if await _handle_duel_use_item(interaction, self.view.session, self.view.actor, item):
            self.view.stop()


class DuelCastItemButton(discord.ui.Button):
    def __init__(self, item: dict, disabled: bool):
        super().__init__(label=f"✨ {item['name']}", style=discord.ButtonStyle.secondary, disabled=disabled, row=3)
        self.item = item

    async def callback(self, interaction: discord.Interaction):
        if await _handle_duel_cast_item(interaction, self.view.session, self.view.actor, self.item):
            self.view.stop()


async def _build_duel_combat_view(session: DuelSession, actor: PartyMember) -> "DuelCombatView":
    return DuelCombatView(session, actor, await _usable_items_for(session, actor))


class DuelCombatView(discord.ui.View):
    def __init__(self, session: DuelSession, actor: PartyMember, usable_items: list[dict] | None = None):
        super().__init__(timeout=PARTY_ACTION_TIMEOUT)
        self.session = session
        self.actor = actor
        self.add_item(DuelAttackButton())
        for skill in actor.unlocked_skills:
            self.add_item(DuelSkillButton(skill, disabled=skill["chip_cost"] > actor.chips))
        usable_items = usable_items or []
        if len(usable_items) == 1:
            self.add_item(DuelUseItemButton(usable_items[0]))
        elif len(usable_items) > 1:
            self.add_item(DuelUseItemSelect(usable_items))
        for item in castable_equipment(actor.equipped):
            self.add_item(DuelCastItemButton(item, disabled=item["id"] in actor.used_item_effects))
        # No target-select -- there's only ever one possible opponent in a 1v1 duel.
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor.user_id:
            await interaction.response.send_message(f"It's {self.actor.label}'s turn.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return
        await _skip_duel_turn(session, self.actor)


async def start_duel(guild_id: int, challenger_id: int, challenger_name: str, target_id: int, target_name: str, wager: int) -> DuelChallenge:
    """Registers both players (active_delves/busy_players, mirroring how opening a party lobby
    already reserves everyone in it) and returns the pending DuelChallenge -- bot.py's !duel command
    builds the challenge embed + DuelChallengeView around this and sends it."""
    challenge = DuelChallenge(guild_id, challenger_id, challenger_name, target_id, target_name, wager)
    for uid in challenge.all_user_ids():
        active_delves[uid] = challenge
        busy_players.add(uid)
    return challenge


class DuelWagerModal(discord.ui.Modal):
    """Collects the (optional) wager once a target's already picked (DuelTargetSelect) -- a
    second step, since unlike ranch_view.HorsePickerSelect's one-argument case, !duel needs two
    collected values (who + how much) and a Select's own callback can only hand back the one
    value it collected itself. Blank means no wager, matching !duel's own default of 0 when typed
    out in full."""

    def __init__(self, on_pick, target: discord.Member):
        super().__init__(title=f"Duel {target.display_name}"[:45])  # Discord's modal title cap
        self.on_pick = on_pick
        self.target = target
        self.wager_input = discord.ui.TextInput(
            label="Wager (optional)", placeholder="e.g. 100 — leave blank for none", required=False,
        )
        self.add_item(self.wager_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.wager_input.value.strip()
        if not raw:
            wager = 0
        else:
            try:
                wager = int(raw)
            except ValueError:
                await interaction.response.send_message("Enter a whole number.", ephemeral=True)
                return
        await interaction.response.defer()
        await self.on_pick(hub_ui.InteractionContext(interaction), self.target, wager)


class DuelTargetSelect(discord.ui.UserSelect):
    """Presented by !duel's own response when called with no target (see bot.py's duel_cmd) --
    NOT constructed by room_view.py, same reasoning as ranch_view.HorsePickerSelect: an Arena
    room's Duel button stays a plain zero-arg command wrapper like every other room button, and
    !duel's own response is what supplies the richer picker UI, not the room. Uses Discord's
    native user-picker component (discord.ui.UserSelect) rather than an enumerated dropdown --
    unlike a horse roster, "any other player in the server" has no fixed short list to enumerate.
    Picking someone opens DuelWagerModal for the second (wager) argument."""

    def __init__(self, on_pick):
        super().__init__(placeholder="Choose who to duel...")
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DuelWagerModal(self.on_pick, self.values[0]))


def build_duel_target_picker(on_pick) -> discord.ui.View:
    """A one-off View wrapping DuelTargetSelect -- what !duel sends back when called with no
    target (bare !duel, or an Arena room's Duel button, which can only ever invoke a command with
    zero collected args). Lives here, not bot.py, same "view modules build UI, bot.py only
    invokes commands" boundary ranch_view.build_horse_picker already established."""
    view = discord.ui.View(timeout=120)
    view.add_item(DuelTargetSelect(on_pick))
    return view


class DelveModeChoiceView(discord.ui.View):
    """Shown every time !delve resolves which delve to run (typed, pinned via a room button, or
    picked from DelvePickerView) -- the player chooses to delve alone (unchanged existing flow) or
    open a party others can join for free. Energy is only ever spent once a delve actually starts
    (Solo here, or Start Delve in the lobby), never just for opening this choice."""

    def __init__(self, guild_id: int, user_id: int, character: dict, delve: dict):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.user_id = user_id
        self.character = character
        self.delve = delve

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your delve to start.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⚔️ Solo Delve", style=discord.ButtonStyle.primary)
    async def solo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.user_id in active_delves:
            await interaction.response.send_message("You're already tied up in a delve — finish or leave it first.", ephemeral=True)
            return
        has_energy = await _spend_delve_energy(self.guild_id, self.user_id)
        if not has_energy:
            await interaction.response.send_message("You're out of energy — run `!rest` to refill it.", ephemeral=True)
            return
        session = await _new_delve_session(self.guild_id, self.user_id, self.character, self.delve)
        await _build_room_display(interaction, session)
        session.message = await interaction.original_response()
        self.stop()

    @discord.ui.button(label="👥 Start Party", style=discord.ButtonStyle.secondary)
    async def party_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.user_id in active_delves:
            await interaction.response.send_message("You're already tied up in a delve — finish or leave it first.", ephemeral=True)
            return
        lobby = PartyLobby(self.guild_id, self.user_id, interaction.user.display_name, self.character, self.delve)
        active_delves[self.user_id] = lobby
        busy_players.add(self.user_id)
        view = PartyLobbyView(lobby)
        await interaction.response.edit_message(embed=_build_lobby_embed(lobby), view=view)
        lobby.message = await interaction.original_response()
        self.stop()


async def build_mode_choice_display(
    guild_id: int, user_id: int, character: dict, delve: dict,
) -> tuple[discord.Embed, DelveModeChoiceView]:
    embed = discord.Embed(
        title=f"🗡️ {delve['name']}",
        description=f"*{delve['flavor']}*\n\n{len(delve['rooms'])} rooms.\n\n"
        f"Delve alone, or start a party others can join for free (no energy cost to join).",
        color=discord.Color.blurple(),
    )
    return embed, DelveModeChoiceView(guild_id, user_id, character, delve)


async def _new_delve_session(guild_id: int, user_id: int, character: dict, delve: dict) -> DelveSession:
    """Session construction + active_delves/busy_players registration for a solo delve -- shared
    by DelveModeChoiceView's Solo Delve button regardless of whether it's editing the mode-choice
    message in place or (via DelvePickerView -> DelveConfirmButton -> this same choice) a message
    that started life as the multi-delve picker."""
    equipped = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
    session = DelveSession(guild_id, user_id, character, equipped, delve)
    active_delves[session.user_id] = session
    busy_players.add(session.user_id)
    return session


class DelveSelect(discord.ui.Select):
    def __init__(self, picker: "DelvePickerView"):
        options = [
            discord.SelectOption(label=d["name"], value=d_id, description=f"{len(d['rooms'])} rooms")
            for d_id, d in list(dungeon.active_delves(include_inactive=picker.test_mode).items())[:25]
        ]
        super().__init__(placeholder="Choose a delve...", options=options, row=0)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction):
        self.picker.delve_id = self.values[0]
        for option in self.options:
            option.default = option.value == self.values[0]
        await interaction.response.edit_message(embed=self.picker.build_embed(), view=self.picker)


class DelvePickerView(discord.ui.View):
    """Shown by !delve only when there's more than one active delve (dungeon.active_delves()) to
    choose between -- a single-active-delve server never sees this. Either way, once a delve is
    settled on, the player lands on the same DelveModeChoiceView (Solo/Party) as every other
    !delve entry path."""

    def __init__(self, guild_id: int, user_id: int, character: dict, test_mode: bool = False):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.user_id = user_id
        self.character = character
        self.test_mode = test_mode
        self.delve_id: str | None = None
        self.add_item(DelveSelect(self))
        self.add_item(DelveConfirmButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your delve to start.", ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🗡️ Choose a Delve", color=discord.Color.blurple())
        if self.delve_id:
            delve = dungeon.DELVES[self.delve_id]
            embed.description = f"*{delve['flavor']}*\n\n{len(delve['rooms'])} rooms."
        else:
            embed.description = "Pick which dungeon to run, then confirm."
        return embed


class DelveConfirmButton(discord.ui.Button):
    def __init__(self, picker: DelvePickerView):
        super().__init__(label="Delve", style=discord.ButtonStyle.success, row=1)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction):
        picker = self.picker
        if not picker.delve_id:
            await interaction.response.send_message("Pick a delve first.", ephemeral=True)
            return

        # No energy spent here -- picking which delve to run isn't committing to it yet. The
        # Solo/Party choice (same one !delve shows for every other entry path) is what actually
        # spends energy, once the player picks Solo or a party's leader clicks Start Delve.
        delve = dungeon.DELVES[picker.delve_id]
        embed, view = await build_mode_choice_display(picker.guild_id, picker.user_id, picker.character, delve)
        await interaction.response.edit_message(embed=embed, view=view)
        picker.stop()


async def build_delve_picker_display(
    guild_id: int, user_id: int, character: dict, test_mode: bool = False
) -> tuple[discord.Embed, DelvePickerView]:
    view = DelvePickerView(guild_id, user_id, character, test_mode)
    return view.build_embed(), view


class ClassSelect(discord.ui.Select):
    def __init__(self, picker: "ClassPickerView"):
        options = [
            discord.SelectOption(label=label, value=value, description=desc)
            for value, label, desc in CLASS_OPTIONS
        ]
        super().__init__(placeholder="Choose your class...", options=options, row=0)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction):
        self.picker.main_class = self.values[0]
        for option in self.options:
            option.default = option.value == self.values[0]
        await interaction.response.edit_message(embed=self.picker.build_embed(), view=self.picker)


class SubclassSelect(discord.ui.Select):
    def __init__(self, picker: "ClassPickerView"):
        options = [
            discord.SelectOption(label=label, value=value, description=desc)
            for value, label, desc in SUBCLASS_OPTIONS
        ]
        super().__init__(placeholder="Choose your subclass...", options=options, row=1)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction):
        self.picker.subclass = self.values[0]
        for option in self.options:
            option.default = option.value == self.values[0]
        await interaction.response.edit_message(embed=self.picker.build_embed(), view=self.picker)


class ClassPickerView(discord.ui.View):
    """One-time character creation -- class and subclass are picked independently (in either
    order) from two selects on the same message, then confirmed. Permanent once created."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.user_id = user_id
        self.main_class: str | None = None
        self.subclass: str | None = None
        self.add_item(ClassSelect(self))
        self.add_item(SubclassSelect(self))
        self.add_item(ConfirmButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your character to create.", ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🗡️ Choose Your Character", color=discord.Color.blurple())
        embed.description = "This choice is **permanent** — pick a class and a subclass, then confirm."
        if self.main_class and self.subclass:
            name = dungeon.display_name(self.main_class, self.subclass)
            stats = dungeon.compute_stats(self.main_class, self.subclass)
            skill = dungeon.unlocked_skills(self.main_class, self.subclass, 1)[0]
            dodge_pct = round(dungeon.dodge_chance(stats["def"]) * 100)
            resist_pct = round(dungeon.dodge_chance(stats["spdef"]) * 100)
            embed.add_field(
                name=f"Preview: {name}",
                value=f"HP {stats['hp']} / ATK {stats['atk']} / DEF {stats['def']} / "
                      f"SpAtk {stats['spatk']} / SpDef {stats['spdef']} / 🏃 Speed {stats['speed']} / 🪙 Chips {stats['chips']}\n"
                      f"Dodge {dodge_pct}% / Resist {resist_pct}%\n"
                      f"Skill: **{skill['name']}** — {skill['flavor']}",
                inline=False,
            )
        return embed


class ConfirmButton(discord.ui.Button):
    def __init__(self, picker: ClassPickerView):
        super().__init__(label="Confirm", style=discord.ButtonStyle.success, row=2)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction):
        picker = self.picker
        if not picker.main_class or not picker.subclass:
            await interaction.response.send_message("Pick both a class and a subclass first.", ephemeral=True)
            return

        stats = dungeon.compute_stats(picker.main_class, picker.subclass)
        created = await asyncio.to_thread(
            db.create_character, picker.guild_id, picker.user_id, picker.main_class, picker.subclass,
            stats["hp"], stats["atk"], stats["def"], stats["spatk"], stats["spdef"], stats["speed"],
        )
        if not created:
            await interaction.response.send_message("You already have a character.", ephemeral=True)
            return

        name = dungeon.display_name(picker.main_class, picker.subclass)
        dodge_pct = round(dungeon.dodge_chance(stats["def"]) * 100)
        resist_pct = round(dungeon.dodge_chance(stats["spdef"]) * 100)
        embed = discord.Embed(
            title=f"✅ You are now a {name}!",
            description=f"HP {stats['hp']} / ATK {stats['atk']} / DEF {stats['def']} / "
                        f"SpAtk {stats['spatk']} / SpDef {stats['spdef']} / 🏃 Speed {stats['speed']} / 🪙 Chips {stats['chips']}\n"
                        f"Dodge {dodge_pct}% / Resist {resist_pct}%\n\n"
                        f"Use `!delve` to enter the dungeon.",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        picker.stop()


async def extra_embed_fields(guild_id: int, user_id: int) -> list[tuple[str, str, bool]]:
    """room_view.py's dungeon specialization hook -- the energy field, a live per-player DB read a
    generic room embed has no way to know about on its own."""
    energy = await asyncio.to_thread(db.get_energy, guild_id, user_id)
    return [("⚡ Energy", f"{energy}/{db.ENERGY_MAX} — 1 per delve, refills with `!rest`", False)]


