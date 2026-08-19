import asyncio

import discord

import db
import dungeon
import dungeon_render
from holdem_view import busy_players

# user_id -> DelveSession, so a player can only have one active delve at a time (across any
# channel, matching how busy_players itself is unscoped by channel/guild).
active_delves: dict[int, "DelveSession"] = {}

CLASS_OPTIONS = [
    ("fighter", "Fighter (Ace)", "Tank — high HP/DEF. Signature: Guard."),
    ("healer", "Healer (King)", "Support — balanced spread. Signature: Heal."),
    ("mage", "Mage (Queen)", "High ATK, fragile. Signature: Fireball."),
    ("rogue", "Rogue (Jack)", "Balanced/quick. Signature: Sneak Attack."),
]
SUBCLASS_OPTIONS = [
    ("clubs", "♣ Brawler", "More HP/ATK."),
    ("spades", "♠ Lethal", "More ATK, less DEF."),
    ("hearts", "♥ Loyal", "More HP/DEF."),
    ("diamonds", "♦ Greedy", "Better loot rolls."),
]


class DelveSession:
    def __init__(self, guild_id: int, user_id: int, character: dict):
        self.guild_id = guild_id
        self.user_id = user_id
        self.main_class = character["main_class"]
        self.subclass = character["subclass"]
        self.max_hp = character["hp"]
        self.atk = character["atk"]
        self.def_ = character["def"]
        self.loot_mult = dungeon.SUBCLASSES[self.subclass]["loot_mult"]
        self.display_name = dungeon.display_name(self.main_class, self.subclass)
        self.ability_name = dungeon.CLASSES[self.main_class]["ability"]

        self.hp = self.max_hp
        self.room_index = 0
        self.monster = dungeon.monster_for_room(0)
        self.monster_hp = self.monster["hp"]
        self.ability_used = False
        self.loot_total = 0

        self.message: discord.Message | None = None
        # Which view is currently the "live" one attached to `message` -- lets a stale view's
        # on_timeout (from a room/fight the player has already moved past) recognize it's been
        # superseded and no-op, since there's no single coroutine blocking on each view in turn
        # here (unlike blackjack's play_round) to guarantee ordering.
        self.current_view: discord.ui.View | None = None


def _combat_embed(session: DelveSession, log_text: str) -> tuple[discord.Embed, discord.File]:
    embed = discord.Embed(title=f"🗡️ {session.monster['name']}", description=log_text, color=discord.Color.dark_red())
    embed.add_field(name=f"{session.display_name} (You)", value=f"HP {max(session.hp, 0)}/{session.max_hp}", inline=True)
    embed.add_field(
        name=session.monster["name"], value=f"HP {max(session.monster_hp, 0)}/{session.monster['hp']}", inline=True
    )
    buf = dungeon_render.render_room(session.room_index, dungeon.ROOM_COUNT, session.monster)
    file = discord.File(buf, filename="room.png")
    embed.set_image(url="attachment://room.png")
    return embed, file


async def _apply_retreat(session: DelveSession) -> discord.Embed:
    currency = db.get_currency_name(session.guild_id)
    balance = await asyncio.to_thread(db.update_balance, session.guild_id, session.user_id, session.loot_total)
    await asyncio.to_thread(db.log_bet, session.guild_id, session.user_id, "dungeon", 0, session.loot_total)
    active_delves.pop(session.user_id, None)
    busy_players.discard(session.user_id)
    return discord.Embed(
        title="🏃 Retreated Safely",
        description=f"You make it back with **{session.loot_total}** {currency}. Balance: **{balance}** {currency}.",
        color=discord.Color.green(),
    )


def _forfeit(session: DelveSession):
    """Ends the delve with no payout -- shared cleanup for death, abandonment (timeout), and
    anything else short of a deliberate retreat."""
    active_delves.pop(session.user_id, None)
    busy_players.discard(session.user_id)


DELVE_ACTION_TIMEOUT = 1200  # 20 minutes -- plenty of time to notice it's your turn and act


class CombatView(discord.ui.View):
    def __init__(self, session: DelveSession):
        super().__init__(timeout=DELVE_ACTION_TIMEOUT)
        self.session = session
        self.ability_button.label = session.ability_name
        self.ability_button.disabled = session.ability_used
        session.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.user_id:
            await interaction.response.send_message("This isn't your delve.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Attack", style=discord.ButtonStyle.primary)
    async def attack_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # stop() cancels this view's own pending timeout once its turn is actually resolved --
        # without it, this view's on_timeout could still fire ~20 minutes later even after
        # combat has long since moved on to a new view (or ended), incorrectly overwriting an
        # already-concluded delve with a false "abandoned" message despite the real payout
        # having already landed.
        if await _handle_action(interaction, self.session, ability=False):
            self.stop()

    @discord.ui.button(label="Ability", style=discord.ButtonStyle.success)
    async def ability_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _handle_action(interaction, self.session, ability=True):
            self.stop()

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return  # superseded -- the player already moved on through some other path
        _forfeit(session)
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
        session.room_index += 1
        session.monster = dungeon.monster_for_room(session.room_index)
        session.monster_hp = session.monster["hp"]
        session.ability_used = False
        embed, file = _combat_embed(session, f"You press deeper into the dungeon...\n\n*{session.monster['flavor']}*")
        view = CombatView(session)
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
        self.stop()

    async def on_timeout(self):
        session = self.session
        if session.current_view is not self:
            return
        if session.message is None:
            _forfeit(session)
            return
        embed = await _apply_retreat(session)  # default to the safe choice if they don't respond
        try:
            await session.message.edit(embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            pass


async def _handle_action(interaction: discord.Interaction, session: DelveSession, ability: bool) -> bool:
    """Returns whether this actually consumed the player's turn (False only for the
    already-used-ability rejection, which leaves the calling CombatView live and waiting) --
    callers use this to decide whether to stop() the view that dispatched them."""
    if ability and session.ability_used:
        await interaction.response.send_message("You've already used your ability this fight.", ephemeral=True)
        return False

    currency = db.get_currency_name(session.guild_id)
    log_lines = []
    guard_active = False

    if ability and session.main_class == "healer":
        healed = min(session.max_hp, session.hp + round(session.max_hp * dungeon.HEAL_FRACTION)) - session.hp
        session.hp += healed
        log_lines.append(f"You use **{session.ability_name}** and recover **{healed}** HP.")
        session.ability_used = True
    elif ability and session.main_class == "fighter":
        guard_active = True
        log_lines.append("You raise your guard, ready to blunt the next blow.")
        session.ability_used = True
    else:
        multiplier = 1.0
        if ability:
            multiplier = dungeon.FIREBALL_MULTIPLIER if session.main_class == "mage" else dungeon.SNEAK_ATTACK_MULTIPLIER
            session.ability_used = True
        dmg = dungeon.roll_damage(session.atk, session.monster["def"], multiplier)
        session.monster_hp -= dmg
        verb = f"unleash **{session.ability_name}**" if ability else "attack"
        log_lines.append(f"You {verb} for **{dmg}** damage.")

    if session.monster_hp <= 0:
        loot = dungeon.roll_loot(session.monster, session.loot_mult)
        session.loot_total += loot
        log_lines.append(f"**{session.monster['name']} is defeated!** You find **{loot}** {currency}.")
        await _present_room_result(interaction, session, log_lines)
        return True

    monster_dmg = dungeon.roll_damage(session.monster["atk"], session.def_)
    if guard_active:
        monster_dmg = max(1, round(monster_dmg * dungeon.GUARD_DAMAGE_REDUCTION))
        log_lines.append(f"Your guard softens the blow — **{session.monster['name']}** hits for **{monster_dmg}**.")
    else:
        log_lines.append(f"**{session.monster['name']}** strikes back for **{monster_dmg}**.")
    session.hp -= monster_dmg

    if session.hp <= 0:
        _forfeit(session)
        embed = discord.Embed(
            title="💀 You Have Fallen",
            description="\n".join(log_lines) + f"\n\nYou're carried out of the dungeon empty-handed, losing this "
            f"delve's **{session.loot_total}** {currency} haul.",
            color=discord.Color.dark_red(),
        )
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        return True

    embed, file = _combat_embed(session, "\n".join(log_lines))
    view = CombatView(session)
    await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
    return True


async def _present_room_result(interaction: discord.Interaction, session: DelveSession, log_lines: list[str]):
    currency = db.get_currency_name(session.guild_id)
    is_last_room = session.room_index >= dungeon.ROOM_COUNT - 1

    embed = discord.Embed(title=f"🏆 Room {session.room_index + 1} Cleared!", description="\n".join(log_lines), color=discord.Color.gold())
    embed.add_field(name="Loot this delve", value=f"{session.loot_total} {currency}", inline=True)
    embed.add_field(name="HP", value=f"{session.hp}/{session.max_hp}", inline=True)

    if is_last_room:
        balance = await asyncio.to_thread(db.update_balance, session.guild_id, session.user_id, session.loot_total)
        await asyncio.to_thread(db.log_bet, session.guild_id, session.user_id, "dungeon", 0, session.loot_total)
        active_delves.pop(session.user_id, None)
        busy_players.discard(session.user_id)
        embed.description += f"\n\nYou've cleared the dungeon! Balance: **{balance}** {currency}."
        await interaction.response.edit_message(embed=embed, attachments=[], view=None)
        return

    embed.description += "\n\nRetreat with your loot, or push deeper for a tougher fight and better rewards?"
    view = RoomResultView(session)
    await interaction.response.edit_message(embed=embed, attachments=[], view=view)


async def start_delve(ctx, character: dict):
    """Starts a fresh delve for a player who already has a character and just cleared today's
    cooldown (both checked by the caller). Sends the one persistent message this whole delve
    session will reuse via edits."""
    session = DelveSession(ctx.guild.id, ctx.author.id, character)
    active_delves[session.user_id] = session
    busy_players.add(session.user_id)

    embed, file = _combat_embed(session, f"*{session.monster['flavor']}*")
    view = CombatView(session)
    message = await ctx.send(embed=embed, file=file, view=view)
    session.message = message


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
            embed.add_field(
                name=f"Preview: {name}",
                value=f"HP {stats['hp']} / ATK {stats['atk']} / DEF {stats['def']}",
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
