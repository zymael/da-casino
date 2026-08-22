import asyncio
import random

import discord

import db
import dungeon
import dungeon_render
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

        # monster/monster_hp/monster_def_debuff/ability_used are combat-room-only state -- None/0
        # if the start room happens to be a non-combat "choice" room instead (see dungeon.py's
        # room shape). _goto_room (re)populates these on every transition into a combat room.
        start_room = self.rooms_by_id[self.current_room_id]
        if start_room["type"] == "combat":
            self.monster = dungeon.monster_for_room(start_room)
            self.monster_hp = self.monster["hp"]
        else:
            self.monster = None
            self.monster_hp = 0
        self.monster_def_debuff = 0  # from def_shred-type skills; resets each new monster
        self.ability_used = False

        self.message: discord.Message | None = None
        # Which view is currently the "live" one attached to `message` -- lets a stale view's
        # on_timeout (from a room/fight the player has already moved past) recognize it's been
        # superseded and no-op, since there's no single coroutine blocking on each view in turn
        # here (unlike blackjack's play_round) to guarantee ordering.
        self.current_view: discord.ui.View | None = None

    def all_user_ids(self) -> list[int]:
        return [self.user_id]


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
    party's monster_hp/monster_def_debuff is shared across members rather than living on each one
    individually (see PartyDelveSession, which owns that shared state instead)."""

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
        # Same "start wherever the last delve left off" rule as DelveSession -- see its own hp
        # comment for why.
        self.hp = min(character["current_hp"], self.max_hp)
        self.loot_mult = dungeon.SUBCLASSES[self.subclass]["loot_mult"]
        self.build_name = dungeon.display_name(self.main_class, self.subclass)
        self.unlocked_skills = dungeon.unlocked_skills(self.main_class, self.subclass, self.level)
        self.is_leader = is_leader
        self.ability_used = False
        self.knocked_out = False
        self.loot_total = 0

    @property
    def label(self) -> str:
        return f"{self.player_name} ({self.build_name})"


class PartyLobby:
    """Pre-combat party-forming state, shown after the leader picks "Start Party" from
    DelveModeChoiceView. Deliberately a separate class from PartyDelveSession (rather than a
    started flag on one combined class) so combat-only fields (monster, turn_queue, ...) never
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
        self.monster_def_debuff = 0
        self.monster: dict | None = None
        self.monster_hp = 0
        self.monster_max_hp = 0
        self.turn_queue: list[int] = []
        self.rooms_visited = 1
        self._enter_room(delve["start_room"])

        self.message: discord.Message | None = None
        self.current_view: discord.ui.View | None = None

    def living_members(self) -> list[PartyMember]:
        return [m for m in self.members if not m.knocked_out]

    def all_user_ids(self) -> list[int]:
        return [m.user_id for m in self.members]

    def _enter_room(self, room_id: str):
        """Moves into room_id -- rolls a fresh monster and rebuilds the round's turn queue if it's
        a combat room (called on session init and on Push Deeper), or clears combat state if not.
        Monster HP is scaled by however many members are alive RIGHT NOW (party_hp_multiplier),
        not the party's original size, so a roster thinned by earlier knockouts faces a fight
        scaled to its current strength. Doesn't touch rooms_visited -- callers bump that
        themselves, since __init__'s first call shouldn't double-count the starting room."""
        self.current_room_id = room_id
        room = self.rooms_by_id[room_id]
        if room["type"] == "combat":
            self.monster = dungeon.monster_for_room(room)
            mult = dungeon.party_hp_multiplier(len(self.living_members()))
            self.monster_max_hp = round(self.monster["hp"] * mult)
            self.monster_hp = self.monster_max_hp
            self.monster_def_debuff = 0
            for m in self.members:
                m.ability_used = False
            self.turn_queue = [m.user_id for m in self.living_members()]
        else:
            self.monster = None
            self.monster_hp = 0
            self.monster_max_hp = 0
            self.turn_queue = []


def _room_background_path(delve: dict, room: dict) -> str | None:
    """A room's own background_path if it set one, else the delve's top-level default -- lets
    different rooms in the same delve look different without requiring every room to set one."""
    return room.get("background_path") or delve.get("background_path")


def _combat_intro_text(room: dict, monster: dict) -> str:
    """The description shown the moment a combat room is entered -- the room's own optional
    "prompt" (introducing the room itself, dungeon.py's module docstring) followed by the
    monster's own flavor text. Only ever used at room-entry (_build_room_display/
    _build_party_room_display); a later combat-turn embed reuses _combat_embed/_party_combat_embed
    with the fight's actual log lines instead, so a room's intro is shown exactly once per visit,
    not repeated on every attack."""
    parts = [f"*{room['prompt']}*"] if room.get("prompt") else []
    parts.append(f"*{monster['flavor']}*")
    return "\n\n".join(parts)


def _combat_embed(session: DelveSession, log_text: str) -> tuple[discord.Embed, discord.File]:
    embed = discord.Embed(title=f"🗡️ {session.monster['name']}", description=log_text, color=discord.Color.dark_red())
    embed.add_field(name=f"{session.display_name} (You)", value=f"HP {max(session.hp, 0)}/{session.max_hp}", inline=True)
    embed.add_field(
        name=session.monster["name"], value=f"HP {max(session.monster_hp, 0)}/{session.monster['hp']}", inline=True
    )
    room = session.rooms_by_id[session.current_room_id]
    buf = dungeon_render.render_room(session.rooms_visited, session.monster, _room_background_path(session.delve, room))
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
    embed.add_field(name=f"{session.display_name} (You)", value=f"HP {max(session.hp, 0)}/{session.max_hp}", inline=False)
    for action in room["actions"]:
        lines = _action_summary_lines(action, currency)
        embed.add_field(name=action["label"], value="\n".join(lines) if lines else "—", inline=True)
    buf = dungeon_render.render_room(session.rooms_visited, None, _room_background_path(session.delve, room))
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


async def _build_room_display(session: DelveSession) -> tuple[discord.Embed, discord.File, discord.ui.View]:
    """Builds the (embed, file, view) triple for whatever room a session is CURRENTLY sitting on
    -- used both for a fresh delve's very first room and, via _goto_room, every later transition,
    so there's one place that branches on room type rather than duplicating it at each call site."""
    room = session.rooms_by_id[session.current_room_id]
    if room["type"] == "combat":
        embed, file = _combat_embed(session, _combat_intro_text(room, session.monster))
        view = await _build_combat_view(session)
    else:
        embed, file = _choice_embed(session, room, room["prompt"])
        view = await _build_choice_room_view(session, room)
    return embed, file, view


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
        session.monster = dungeon.monster_for_room(room)
        session.monster_hp = session.monster["hp"]
        session.monster_def_debuff = 0
        session.ability_used = False
    else:
        session.monster = None
        session.monster_hp = 0
    embed, file, view = await _build_room_display(session)
    if intro_text:
        embed.description = f"{intro_text}\n\n{embed.description}"
    await interaction.response.edit_message(embed=embed, attachments=[file], view=view)


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
    embed.add_field(name=f"{session.display_name} (You)", value=f"HP {max(session.hp, 0)}/{session.max_hp}", inline=True)
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
    skill gets one, a higher-level build gets more, all sharing the same once-per-fight charge
    (session.ability_used), so unlocking more skills is a choice of *which* effect to use in a
    given fight, not extra actions."""

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


class CombatView(discord.ui.View):
    def __init__(self, session: DelveSession, usable_items: list[dict] | None = None):
        super().__init__(timeout=DELVE_ACTION_TIMEOUT)
        self.session = session
        self.add_item(AttackButton())
        for skill in session.unlocked_skills:
            self.add_item(SkillButton(skill, disabled=session.ability_used))
        usable_items = usable_items or []
        if len(usable_items) == 1:
            self.add_item(UseItemButton(usable_items[0]))
        elif len(usable_items) > 1:
            self.add_item(UseItemSelect(usable_items))
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

    xp_gain = dungeon.xp_for_monster(monster["tier"])
    level_result = await asyncio.to_thread(
        db.add_xp, guild_id, actor.user_id, xp_gain,
        dungeon.LEVEL_HP_GAIN, dungeon.LEVEL_ATK_GAIN, dungeon.LEVEL_DEF_GAIN,
    )
    log_lines.append(f"+{xp_gain} XP")
    if level_result["levels_gained"] > 0:
        actor.level = level_result["new_level"]
        hp_delta = dungeon.LEVEL_HP_GAIN * level_result["levels_gained"]
        atk_delta = dungeon.LEVEL_ATK_GAIN * level_result["levels_gained"]
        def_delta = dungeon.LEVEL_DEF_GAIN * level_result["levels_gained"]
        actor.max_hp += hp_delta
        actor.hp += hp_delta
        actor.atk += atk_delta
        actor.def_ += def_delta
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
            old_bonuses = current_item["stat_bonuses"] if current_item else {}
            new_bonuses = dropped["stat_bonuses"]
            actor.max_hp += new_bonuses.get("hp", 0) - old_bonuses.get("hp", 0)
            actor.hp += new_bonuses.get("hp", 0) - old_bonuses.get("hp", 0)
            actor.atk += new_bonuses.get("atk", 0) - old_bonuses.get("atk", 0)
            actor.def_ += new_bonuses.get("def", 0) - old_bonuses.get("def", 0)
            actor.equipped[slot] = dropped["id"]
            if current_item:
                log_lines.append(f"⚔️ Found **{dropped['name']}**! Replaced {current_item['name']} — equipped (stored in `!equipment`).")
            else:
                log_lines.append(f"⚔️ Found **{dropped['name']}**! Equipped.")
        else:
            await asyncio.to_thread(db.store_equipment_item, guild_id, actor.user_id, dropped["id"])
            log_lines.append(f"⚔️ Found **{dropped['name']}**, but your current {slot} is better — stored in `!equipment`.")

    await quests.record_progress(guild_id, actor.user_id, "kill_monster", monster_id=monster["id"], tier=monster["tier"])

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
# `actor` (hp/max_hp/atk/def_) and `monster_state` (monster/monster_hp/monster_def_debuff) are
# split into two params rather than one "session" because a party delve's monster state is shared
# across every member, while hp/atk/def_ belong to whichever one PartyMember is acting -- a solo
# DelveSession satisfies both shapes itself, so solo callers just pass it twice with zero behavior
# change; a party call passes (member, party_session).

# Effect types that make this action roll monster damage (as opposed to a pure utility action
# like Heal/Guard, which resolve entirely inside _apply_effects and skip the damage roll below).
DAMAGE_EFFECT_TYPES = {"damage_multiplier", "extra_attack"}


def _default_mods() -> dict:
    return {"multiplier": 1.0, "guard_reduction": None, "lifesteal_fraction": None, "extra_attack_multipliers": []}


def _effect_damage_multiplier(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    mods["multiplier"] *= effect["value"]


def _effect_heal_fraction(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    healed = min(actor.max_hp, actor.hp + round(actor.max_hp * effect["value"])) - actor.hp
    actor.hp += healed
    log_lines.append(f"You recover **{healed}** HP.")


def _effect_guard(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    mods["guard_reduction"] = effect["reduction"]
    log_lines.append("You raise your guard, ready to blunt the next blow.")


def _effect_lifesteal_fraction(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    mods["lifesteal_fraction"] = effect["value"]  # applied against total damage dealt, once known


def _effect_def_shred(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    monster_state.monster_def_debuff += effect["value"]
    log_lines.append(f"**{monster_state.monster['name']}**'s defenses crumble by **{effect['value']}**.")


def _effect_extra_attack(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    mods["extra_attack_multipliers"].append(effect.get("multiplier", 1.0))


def _effect_atk_buff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    actor.atk += effect["value"]
    log_lines.append(f"Your ATK rises by **{effect['value']}** for the rest of the fight.")


def _effect_def_buff(actor, monster_state, effect: dict, log_lines: list[str], mods: dict):
    actor.def_ += effect["value"]
    log_lines.append(f"Your DEF rises by **{effect['value']}** for the rest of the fight.")


EFFECT_HANDLERS = {
    "damage_multiplier": _effect_damage_multiplier,
    "heal_fraction": _effect_heal_fraction,
    "guard": _effect_guard,
    "lifesteal_fraction": _effect_lifesteal_fraction,
    "def_shred": _effect_def_shred,
    "extra_attack": _effect_extra_attack,
    "atk_buff": _effect_atk_buff,
    "def_buff": _effect_def_buff,
}


def _apply_effects(actor, monster_state, effects: list[dict], log_lines: list[str]) -> dict:
    """Runs every effect in order, mutating actor (HP/ATK/DEF), monster_state (monster DEF
    debuff), and appending log lines as it goes. Returns this-action modifiers the caller still
    needs for the damage roll: multiplier, guard_reduction (None if not guarding),
    lifesteal_fraction (None if no lifesteal), and extra_attack_multipliers (one roll_damage call
    per entry, on top of the primary hit)."""
    mods = _default_mods()
    for effect in effects:
        EFFECT_HANDLERS[effect["type"]](actor, monster_state, effect, log_lines, mods)
    return mods


async def _build_combat_view(session: DelveSession) -> "CombatView":
    """Fetches current consumable holdings fresh (unlike unlocked_skills, these change turn to
    turn as items get crafted/used) and builds a CombatView reflecting them."""
    held = await asyncio.to_thread(db.get_inventory, session.guild_id, session.user_id)
    usable_items = [dungeon.CONSUMABLES[item_id] for item_id, qty in held.items() if item_id in dungeon.CONSUMABLES and qty > 0]
    return CombatView(session, usable_items)


async def _resolve_combat_turn(
    interaction: discord.Interaction, session: DelveSession, effects: list[dict], verb: str, log_lines: list[str],
) -> bool:
    """Shared tail for every kind of combat action (plain Attack, a skill, or a consumed item):
    applies `effects`, rolls damage if any effect is damage-shaped, resolves victory or the
    monster's counter-attack, and renders the next view. `verb` only matters if a damage roll
    happens (e.g. "attack", "unleash **Fireball**", "use **Healing Draught**") -- callers that
    only heal/buff never reach the line that reads it. Always returns True (this always consumes
    the turn -- any "can't do this right now" rejection happens before this is called)."""
    currency = db.get_currency_name(session.guild_id)
    # A plain Attack (no effects) always rolls damage; anything else rolls damage only if at
    # least one of its effects is damage-shaped -- everything else (Heal, Guard, ...) is pure
    # utility and skips the roll_damage call below entirely, same branch shape as the old
    # class-name ladder this replaced.
    is_damage_action = not effects or any(e["type"] in DAMAGE_EFFECT_TYPES for e in effects)
    mods = _apply_effects(session, session, effects, log_lines)
    moon_effect = moon.effect_for("dungeon")
    player_moon_mult = _moon_combat_multiplier(moon_effect, "player")

    if is_damage_action:
        effective_monster_def = max(0, session.monster["def"] - session.monster_def_debuff)
        dmg = dungeon.roll_damage(session.atk, effective_monster_def, mods["multiplier"] * player_moon_mult)
        for extra_multiplier in mods["extra_attack_multipliers"]:
            dmg += dungeon.roll_damage(session.atk, effective_monster_def, extra_multiplier * player_moon_mult)
        if mods["lifesteal_fraction"]:
            healed = min(session.max_hp, session.hp + round(dmg * mods["lifesteal_fraction"])) - session.hp
            session.hp += healed
            if healed:
                log_lines.append(f"You drain **{healed}** HP from the strike.")
        session.monster_hp -= dmg
        log_lines.append(f"You {verb} for **{dmg}** damage.")

    if session.monster_hp <= 0:
        log_lines.append(f"**{session.monster['name']} is defeated!**")
        await _award_kill(
            session.guild_id, session.monster, session, session.current_room_id, log_lines,
            loot_mult=session.loot_mult * player_moon_mult, chance_mult=1.0,
        )
        await _present_room_result(interaction, session, log_lines)
        return True

    monster_dmg = dungeon.roll_damage(
        session.monster["atk"], session.def_, _moon_combat_multiplier(moon_effect, "monster")
    )
    if mods["guard_reduction"] is not None:
        monster_dmg = max(1, round(monster_dmg * mods["guard_reduction"]))
        log_lines.append(f"Your guard softens the blow — **{session.monster['name']}** hits for **{monster_dmg}**.")
    else:
        log_lines.append(f"**{session.monster['name']}** strikes back for **{monster_dmg}**.")
    session.hp -= monster_dmg

    if session.hp <= 0:
        await _forfeit(session)
        embed = discord.Embed(
            title="💀 You Have Fallen",
            description="\n".join(log_lines) + f"\n\nYou're carried out of the dungeon empty-handed, losing this "
            f"delve's **{session.loot_total}** {currency} haul.",
            color=discord.Color.dark_red(),
        )
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        return True

    embed, file = _combat_embed(session, "\n".join(log_lines))
    view = await _build_combat_view(session)
    await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
    return True


async def _handle_action(interaction: discord.Interaction, session: DelveSession, skill: dict | None) -> bool:
    """`skill` is the unlocked skill dict the player chose (an Ability button), or None for a
    plain Attack. Returns whether this actually consumed the player's turn (False only for the
    already-used-ability rejection, which leaves the calling CombatView live and waiting) --
    callers use this to decide whether to stop() the view that dispatched them."""
    if skill is not None and session.ability_used:
        await interaction.response.send_message("You've already used your ability this fight.", ephemeral=True)
        return False

    effects = skill["effects"] if skill is not None else []
    if skill is not None:
        session.ability_used = True
    verb = f"unleash **{skill['name']}**" if skill is not None else "attack"
    return await _resolve_combat_turn(interaction, session, effects, verb, [])


async def _handle_use_item(interaction: discord.Interaction, session: DelveSession, item: dict) -> bool:
    """Consumes one of `item` and resolves its effects as a full turn -- not gated by
    ability_used (items are separately scarce, via what the crafting economy actually produces)
    but it still costs a turn and still draws the monster's counter-attack, so stockpiling items
    can't trivialize a fight for free."""
    consumed = await asyncio.to_thread(db.consume_inventory_item, session.guild_id, session.user_id, item["id"], 1)
    if not consumed:
        await interaction.response.send_message("You don't have that anymore.", ephemeral=True)
        return False
    verb = f"use **{item['name']}**"
    return await _resolve_combat_turn(interaction, session, item["effects"], verb, [])


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
# Same monster/room model as solo, but the monster only counter-attacks once a full round (every
# living member acting once, in join order) has passed, instead of after every single action --
# see _advance_party_round, the one real behavioral fork from solo's _resolve_combat_turn.


def _party_combat_embed(session: PartyDelveSession, log_text: str) -> tuple[discord.Embed, discord.File]:
    embed = discord.Embed(title=f"🗡️ {session.monster['name']}", description=log_text, color=discord.Color.dark_red())
    current_actor_id = session.turn_queue[0] if session.turn_queue else None
    for m in session.members:
        if m.knocked_out:
            status = "💀 Knocked out"
        elif m.user_id == current_actor_id:
            status = f"HP {max(m.hp, 0)}/{m.max_hp} ⬅️ acting now"
        else:
            status = f"HP {max(m.hp, 0)}/{m.max_hp}"
        embed.add_field(name=m.label, value=status, inline=True)
    embed.add_field(
        name=session.monster["name"], value=f"HP {max(session.monster_hp, 0)}/{session.monster_max_hp}", inline=True
    )
    room = session.rooms_by_id[session.current_room_id]
    buf = dungeon_render.render_room(session.rooms_visited, session.monster, _room_background_path(session.delve, room))
    file = discord.File(buf, filename="room.png")
    embed.set_image(url="attachment://room.png")
    return embed, file


async def _usable_items_for(session: PartyDelveSession, actor: PartyMember) -> list[dict]:
    held = await asyncio.to_thread(db.get_inventory, session.guild_id, actor.user_id)
    return [dungeon.CONSUMABLES[item_id] for item_id, qty in held.items() if item_id in dungeon.CONSUMABLES and qty > 0]


async def _build_party_combat_view(session: PartyDelveSession) -> "PartyCombatView":
    actor = session.members_by_id[session.turn_queue[0]]
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


async def _advance_party_round(interaction: discord.Interaction | None, session: PartyDelveSession, log_lines: list[str]):
    """Shared tail once a member's turn (whether an actual action or a timeout-skip) is resolved
    and the monster is still alive: if another member is still queued this round, render their
    turn -- no monster counter yet. Otherwise the round is over: the monster gets its one
    counter-attack against a random living member (not specified by the user -- simplest fair
    default), then either the party wipes (nobody left standing, no payout for anyone, same shape
    as solo's death forfeit) or a fresh round begins."""
    if session.turn_queue:
        next_actor = session.members_by_id[session.turn_queue[0]]
        embed, file = _party_combat_embed(session, "\n".join(log_lines))
        view = PartyCombatView(session, next_actor, await _usable_items_for(session, next_actor))
        await _send_party_update(interaction, session, embed, file, view)
        return

    moon_effect = moon.effect_for("dungeon")
    target = random.choice(session.living_members())
    monster_dmg = dungeon.roll_damage(
        session.monster["atk"], target.def_, _moon_combat_multiplier(moon_effect, "monster")
    )
    target.hp -= monster_dmg
    log_lines.append(f"**{session.monster['name']}** strikes **{target.label}** for **{monster_dmg}**.")
    if target.hp <= 0:
        target.knocked_out = True
        log_lines.append(f"💀 **{target.label}** is knocked out!")

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

    session.turn_queue = [m.user_id for m in session.living_members()]
    next_actor = session.members_by_id[session.turn_queue[0]]
    embed, file = _party_combat_embed(session, "\n".join(log_lines))
    view = PartyCombatView(session, next_actor, await _usable_items_for(session, next_actor))
    await _send_party_update(interaction, session, embed, file, view)


async def _resolve_party_turn(
    interaction: discord.Interaction, session: PartyDelveSession, member: PartyMember,
    effects: list[dict], verb: str, log_lines: list[str],
) -> bool:
    """Party sibling of _resolve_combat_turn: applies `effects`, rolls `member`'s damage against
    the SHARED session.monster_hp, and on a kill runs the party's independent-per-member reward
    loop (leader at full rate, joiners halved -- see _award_kill). Unlike solo, this never rolls
    the monster's counter-attack itself -- that only happens once the whole round is done, via
    _advance_party_round."""
    is_damage_action = not effects or any(e["type"] in DAMAGE_EFFECT_TYPES for e in effects)
    mods = _apply_effects(member, session, effects, log_lines)
    moon_effect = moon.effect_for("dungeon")
    player_moon_mult = _moon_combat_multiplier(moon_effect, "player")

    if is_damage_action:
        effective_monster_def = max(0, session.monster["def"] - session.monster_def_debuff)
        dmg = dungeon.roll_damage(member.atk, effective_monster_def, mods["multiplier"] * player_moon_mult)
        for extra_multiplier in mods["extra_attack_multipliers"]:
            dmg += dungeon.roll_damage(member.atk, effective_monster_def, extra_multiplier * player_moon_mult)
        if mods["lifesteal_fraction"]:
            healed = min(member.max_hp, member.hp + round(dmg * mods["lifesteal_fraction"])) - member.hp
            member.hp += healed
            if healed:
                log_lines.append(f"{member.label} drains **{healed}** HP from the strike.")
        session.monster_hp -= dmg
        log_lines.append(f"{member.label} {verb} for **{dmg}** damage.")

    if session.monster_hp <= 0:
        log_lines.append(f"**{session.monster['name']} is defeated!**")
        for m in session.living_members():
            member_log: list[str] = []
            loot_mult = m.loot_mult * (1.0 if m.is_leader else 0.5) * player_moon_mult
            chance_mult = 1.0 if m.is_leader else 0.5
            await _award_kill(
                session.guild_id, session.monster, m, session.current_room_id, member_log,
                loot_mult=loot_mult, chance_mult=chance_mult,
            )
            log_lines.append(f"**{m.label}**")
            log_lines.extend(f"> {line}" for line in member_log)
        await _present_party_room_result(interaction, session, log_lines)
        return True

    session.turn_queue.remove(member.user_id)
    await _advance_party_round(interaction, session, log_lines)
    return True


async def _skip_party_turn(session: PartyDelveSession, member: PartyMember):
    """A stalled member's turn timing out -- they simply pass (no damage dealt, no penalty beyond
    losing this turn) rather than forfeiting the whole party over one AFK player."""
    if member.user_id not in session.turn_queue:
        return  # already resolved by some other path
    log_lines = [f"⌛ {member.label} takes too long and passes their turn."]
    session.turn_queue.remove(member.user_id)
    await _advance_party_round(None, session, log_lines)


async def _handle_party_action(
    interaction: discord.Interaction, session: PartyDelveSession, member: PartyMember, skill: dict | None,
) -> bool:
    if skill is not None and member.ability_used:
        await interaction.response.send_message("You've already used your ability this fight.", ephemeral=True)
        return False
    effects = skill["effects"] if skill is not None else []
    if skill is not None:
        member.ability_used = True
    verb = f"unleash **{skill['name']}**" if skill is not None else "attack"
    return await _resolve_party_turn(interaction, session, member, effects, verb, [])


async def _handle_party_use_item(
    interaction: discord.Interaction, session: PartyDelveSession, member: PartyMember, item: dict,
) -> bool:
    consumed = await asyncio.to_thread(db.consume_inventory_item, session.guild_id, member.user_id, item["id"], 1)
    if not consumed:
        await interaction.response.send_message("You don't have that anymore.", ephemeral=True)
        return False
    verb = f"use **{item['name']}**"
    return await _resolve_party_turn(interaction, session, member, item["effects"], verb, [])


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


class PartyCombatView(discord.ui.View):
    def __init__(self, session: PartyDelveSession, actor: PartyMember, usable_items: list[dict] | None = None):
        super().__init__(timeout=PARTY_ACTION_TIMEOUT)
        self.session = session
        self.actor = actor
        self.add_item(PartyAttackButton())
        for skill in actor.unlocked_skills:
            self.add_item(PartySkillButton(skill, disabled=actor.ability_used))
        usable_items = usable_items or []
        if len(usable_items) == 1:
            self.add_item(PartyUseItemButton(usable_items[0]))
        elif len(usable_items) > 1:
            self.add_item(PartyUseItemSelect(usable_items))
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
        status = "💀 Knocked out" if m.knocked_out else f"HP {max(m.hp, 0)}/{m.max_hp}"
        embed.add_field(name=m.label, value=status, inline=True)
    for action in room["actions"]:
        lines = _action_summary_lines(action, currency)
        embed.add_field(name=action["label"], value="\n".join(lines) if lines else "—", inline=True)
    buf = dungeon_render.render_room(session.rooms_visited, None, _room_background_path(session.delve, room))
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


async def _build_party_room_display(session: PartyDelveSession) -> tuple[discord.Embed, discord.File, discord.ui.View]:
    """Party sibling of _build_room_display -- builds the (embed, file, view) triple for whatever
    room the party is CURRENTLY on."""
    room = session.rooms_by_id[session.current_room_id]
    if room["type"] == "combat":
        embed, file = _party_combat_embed(session, _combat_intro_text(room, session.monster))
        view = await _build_party_combat_view(session)
    else:
        embed, file = _party_choice_embed(session, room, room["prompt"])
        view = await _build_party_choice_room_view(session, room)
    return embed, file, view


async def _goto_party_room(interaction: discord.Interaction, session: PartyDelveSession, room_id: str, intro_text: str = ""):
    """Party sibling of _goto_room -- Push Deeper (after a combat victory) and a leader's choice
    action outcome both funnel through this."""
    session.rooms_visited += 1
    session._enter_room(room_id)
    embed, file, view = await _build_party_room_display(session)
    if intro_text:
        embed.description = f"{intro_text}\n\n{embed.description}"
    await interaction.response.edit_message(embed=embed, attachments=[file], view=view)


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
    full-wipe path _advance_party_round already uses."""
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
        status = "💀 Knocked out" if m.knocked_out else f"HP {max(m.hp, 0)}/{m.max_hp}"
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


async def _present_party_room_result(interaction: discord.Interaction, session: PartyDelveSession, log_lines: list[str]):
    room = session.rooms_by_id[session.current_room_id]
    next_room = room.get("next")
    currency = db.get_currency_name(session.guild_id)

    embed = discord.Embed(title="🏆 Room Cleared!", description="\n".join(log_lines), color=discord.Color.gold())
    for m in session.members:
        status = "💀 Knocked out" if m.knocked_out else f"HP {max(m.hp, 0)}/{m.max_hp}"
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
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        return

    embed.description += "\n\nThe party leader decides: retreat with the loot so far, or push deeper?"
    view = PartyRoomResultView(session)
    await interaction.response.edit_message(embed=embed, attachments=[], view=view)


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
        has_energy = await asyncio.to_thread(db.spend_energy, lobby.guild_id, lobby.leader_id, 1)
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
        embed, file, view = await _build_party_room_display(session)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
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
        has_energy = await asyncio.to_thread(db.spend_energy, self.guild_id, self.user_id, 1)
        if not has_energy:
            await interaction.response.send_message("You're out of energy — run `!rest` to refill it.", ephemeral=True)
            return
        session = await _new_delve_session(self.guild_id, self.user_id, self.character, self.delve)
        embed, file, view = await _build_room_display(session)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
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
            for d_id, d in list(dungeon.active_delves().items())[:25]
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

    def __init__(self, guild_id: int, user_id: int, character: dict):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.user_id = user_id
        self.character = character
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


async def build_delve_picker_display(guild_id: int, user_id: int, character: dict) -> tuple[discord.Embed, DelvePickerView]:
    view = DelvePickerView(guild_id, user_id, character)
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
            embed.add_field(
                name=f"Preview: {name}",
                value=f"HP {stats['hp']} / ATK {stats['atk']} / DEF {stats['def']}\nSkill: **{skill['name']}** — {skill['flavor']}",
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
            stats["hp"], stats["atk"], stats["def"],
        )
        if not created:
            await interaction.response.send_message("You already have a character.", ephemeral=True)
            return

        name = dungeon.display_name(picker.main_class, picker.subclass)
        embed = discord.Embed(
            title=f"✅ You are now a {name}!",
            description=f"HP {stats['hp']} / ATK {stats['atk']} / DEF {stats['def']}\n\nUse `!delve` to enter the dungeon.",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        picker.stop()


async def extra_embed_fields(guild_id: int, user_id: int) -> list[tuple[str, str, bool]]:
    """room_view.py's dungeon specialization hook -- the energy field, a live per-player DB read a
    generic room embed has no way to know about on its own."""
    energy = await asyncio.to_thread(db.get_energy, guild_id, user_id)
    return [("⚡ Energy", f"{energy}/{db.ENERGY_MAX} — 1 per delve, refills with `!rest`", False)]


