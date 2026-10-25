"""!war/!raid's UI -- target/army pickers and the round-by-round battle embed. Same boundary as
ranch_view/dungeon_view: bot.py never builds discord.ui components itself, it asks this module
for them. A War Room's buttons stay plain zero-arg command wrappers; !war/!raid's own responses
are what present these pickers when called without a target (see bot.py's war_cmd/raid_cmd)."""

import asyncio

import discord

import hub_ui
import war

MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options
ROUND_DELAY_SECONDS = 2.0
HP_BAR_SEGMENTS = 10
MAX_ARMY_LINES = 12  # keeps a big stable's field under Discord's 1024-char embed field cap
MAX_INJURED_NAMES = 15
MAX_EVENTS_PER_KIND = 3  # rally/KO lines per round in the battle report, same field-cap reason
SIDE_LABELS = {war.ATTACKER: "attacker", war.DEFENDER: "defender"}


class _OwnerOnlyView(discord.ui.View):
    """A picker only the player who asked for it can use -- anyone else's click would otherwise
    run the command as themselves with someone else's half-finished choices."""

    def __init__(self, owner_id: int, timeout: float):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your war to wage.", ephemeral=True)
            return False
        return True


class WarTargetSelect(discord.ui.UserSelect):
    """Discord's native user picker, same reasoning as dungeon_view.DuelTargetSelect -- "any other
    player in the server" has no fixed short list to enumerate. `on_pick` is an async (ctx, member)
    callback; this class only collects the argument."""

    def __init__(self, on_pick, placeholder: str):
        super().__init__(placeholder=placeholder)
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.on_pick(hub_ui.InteractionContext(interaction), self.values[0])


def build_target_picker(owner_id: int, on_pick, placeholder: str) -> discord.ui.View:
    view = _OwnerOnlyView(owner_id, timeout=120)
    view.add_item(WarTargetSelect(on_pick, placeholder))
    return view


class ArmySelect(discord.ui.Select):
    """Multi-select of the attacker's healthy horses. `on_pick` is an async (ctx, [horse_index])
    callback -- bot.py re-checks ownership/health before anything is spent."""

    def __init__(self, healthy: list[dict], on_pick):
        options = [
            discord.SelectOption(
                label=f"{h['horse_index'] + 1}. {h['name']}"[:100],
                description=f"SPD {h['speed']:.0f} / END {h['endurance']:.0f} / SPI {h['spirit']:.0f}",
                value=str(h["horse_index"]),
            )
            for h in healthy[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(
            placeholder=f"Choose up to {war.MAX_ARMY_SIZE} horses to ride to war...", min_values=1,
            max_values=min(war.MAX_ARMY_SIZE, len(options)), options=options,
        )
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.stop()
        await self.on_pick(hub_ui.InteractionContext(interaction), [int(v) for v in self.values])


def build_army_picker(owner_id: int, healthy: list[dict], on_pick) -> discord.ui.View:
    view = _OwnerOnlyView(owner_id, timeout=180)
    view.add_item(ArmySelect(healthy, on_pick))
    return view


def _stat_line(horse: dict) -> str:
    return f"SPD {horse['speed']:.0f} / END {horse['endurance']:.0f} / SPI {horse['spirit']:.0f}"


class RaiderSelect(discord.ui.Select):
    """Single-select of one horse: the operative sent on a !raid op, a stolen horse to rescue, or
    a sentry to station. `extra_options` go first (e.g. !sentry's "relieve" choice, value -1)."""

    def __init__(self, horses: list[dict], on_pick, placeholder: str, extra_options: list[discord.SelectOption] = ()):
        options = list(extra_options) + [
            discord.SelectOption(
                label=f"{h['horse_index'] + 1}. {h['name']}"[:100],
                description=h.get("pick_note") or _stat_line(h),
                value=str(h["horse_index"]),
            )
            for h in horses[:MAX_SELECT_OPTIONS - len(extra_options)]
        ]
        super().__init__(placeholder=placeholder, options=options)
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.stop()
        await self.on_pick(hub_ui.InteractionContext(interaction), int(self.values[0]))


def build_raider_picker(owner_id: int, healthy: list[dict], on_pick, placeholder: str = "Choose your operative...") -> discord.ui.View:
    view = _OwnerOnlyView(owner_id, timeout=180)
    view.add_item(RaiderSelect(healthy, on_pick, placeholder))
    return view


def build_sentry_picker(owner_id: int, healthy: list[dict], has_sentry: bool, on_pick) -> discord.ui.View:
    """on_pick gets a horse_index, or -1 for "relieve the current sentry"."""
    extra = [discord.SelectOption(label="Relieve the sentry", emoji="🛌", value="-1")] if has_sentry else []
    view = _OwnerOnlyView(owner_id, timeout=180)
    view.add_item(RaiderSelect(healthy, on_pick, "Choose who stands watch...", extra))
    return view


class OperationSelect(discord.ui.Select):
    """!raid's first question: which op. `on_pick` is an async (ctx, op_id) callback."""

    def __init__(self, ops: list[war.Operation], on_pick):
        options = [
            discord.SelectOption(label=op.name, emoji=op.emoji, description=op.blurb[:100], value=op.id)
            for op in ops
        ]
        super().__init__(placeholder="Choose an operation...", options=options)
        self.on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.stop()
        await self.on_pick(hub_ui.InteractionContext(interaction), self.values[0])


def build_operation_picker(owner_id: int, ops: list[war.Operation], on_pick) -> discord.ui.View:
    view = _OwnerOnlyView(owner_id, timeout=120)
    view.add_item(OperationSelect(ops, on_pick))
    return view


def build_spy_report(
    target_name: str, horses: list[dict], injured: set[int], sentry_index: int | None,
    clothes: dict[int, dict[str, str]], luck: int, balance: int, currency: str, heat: int,
    facility_tier: int, fortress_off: bool, ceasefire_until: int, war_ready_at: int, character: dict | None,
) -> discord.Embed:
    """What a successful spy brings home. Timestamps are unix seconds (0 = not in effect)."""
    embed = discord.Embed(
        title=f"🕵️ Dossier: {target_name}",
        description=f"Intel is good for {war.INTEL_SECONDS // 3600}h: march on them before it goes stale.",
        color=discord.Color.dark_grey(),
    )
    lines = []
    for h in horses[:MAX_ARMY_LINES]:
        barding = war.barding_bonuses(clothes.get(h["horse_index"], {}))
        icon = "🛡️" if h["horse_index"] == sentry_index else ("🤕" if h["horse_index"] in injured else "🐴")
        line = f"{icon} **{h['name']}** SPD {h['speed']:.0f} / END {h['endurance']:.0f} / SPI {h['spirit']:.0f}"
        if any(barding.values()):
            line += f" (barding +{barding['speed']}/+{barding['endurance']}/+{barding['spirit']})"
        lines.append(line)
    if len(horses) > MAX_ARMY_LINES:
        lines.append(f"...and {len(horses) - MAX_ARMY_LINES} more")
    embed.add_field(
        name=f"🐎 Stable ({len(horses)} horses, {len(injured)} injured)",
        value=("\n".join(lines) or "Empty stalls.")[:1024], inline=False,
    )
    lines = [f"🍀 Luck: **{luck}**", f"💰 {currency}: **{balance}**"]
    if character is not None:
        lines.append(f"🎖️ General: level {character['level']} {character['main_class']}")
    else:
        lines.append("🎖️ General: none")
    if facility_tier:
        lines.append(f"🏰 Fortress: tier {facility_tier}" + (" (burned out)" if fortress_off else ""))
    lines.append("🛡️ Sentry: on duty" if sentry_index is not None else "🛡️ Sentry: nobody on watch")
    lines.append(f"🌡️ Heat: {heat}")
    if ceasefire_until:
        lines.append(f"🏳️ Ceasefire until <t:{ceasefire_until}:R>")
    lines.append(f"⚔️ Can declare war <t:{war_ready_at}:R>" if war_ready_at else "⚔️ Ready to declare war")
    embed.add_field(name="📋 Assessment", value="\n".join(lines), inline=False)
    return embed


# --- Battle report ------------------------------------------------------------------------------

def _hp_bar(fraction: float) -> str:
    filled = max(0, min(HP_BAR_SEGMENTS, round(fraction * HP_BAR_SEGMENTS)))
    return "▰" * filled + "▱" * (HP_BAR_SEGMENTS - filled)


def _army_lines(army: war.Army) -> str:
    lines = []
    for unit in army.units[:MAX_ARMY_LINES]:
        icon = "🚶" if unit.horse_index is None else ("🐴" if unit.alive else "💥")
        hp = max(0, round(unit.hp))
        lines.append(f"{icon} **{unit.name}** {_hp_bar(unit.hp / unit.max_hp)} {hp}/{round(unit.max_hp)}")
    hidden = army.units[MAX_ARMY_LINES:]
    if hidden:
        lines.append(f"...and {len(hidden)} more ({sum(u.alive for u in hidden)} still standing)")
    perks = []
    if army.general_charge_bonus or army.general_hp_bonus:
        perks.append(
            f"🎖️ General: +{army.general_charge_bonus:.0%} charge, +{army.general_hp_bonus:.0%} line"
        )
    if army.fortress_bonus:
        perks.append(f"🏰 Fortress: +{army.fortress_bonus:.0%} HP")
    if army.fortune_low > war.FORTUNE_LOW:
        perks.append("🕵️ Marching on intel: the dice of war can't roll as low")
    if army.spirit_penalty:
        perks.append(f"🪧 Shaken by propaganda: -{army.spirit_penalty:.0f} Spirit")
    if army.on_foot:
        perks.append("No horses to ride, so they fight on foot!")
    return "\n".join(perks + lines)[:1024]


def _round_lines(index: int, log: war.RoundLog, names: dict[str, str]) -> str:
    lines = [f"**Round {index}:** {names[log.first_striker]} gets the jump."]
    lines += [f"✨ {unit} is behind and goes all in!" for _side, unit in log.rallies[:MAX_EVENTS_PER_KIND]]
    if len(log.rallies) > MAX_EVENTS_PER_KIND:
        lines.append(f"✨ ...and {len(log.rallies) - MAX_EVENTS_PER_KIND} more go all in!")
    lines += [f"💥 {unit} ({names[side]}) is knocked out!" for side, unit in log.knockouts[:MAX_EVENTS_PER_KIND]]
    if len(log.knockouts) > MAX_EVENTS_PER_KIND:
        lines.append(f"💥 ...and {len(log.knockouts) - MAX_EVENTS_PER_KIND} more are knocked out!")
    return "\n".join(lines)


def build_battle_embed(
    names: dict[str, str], result: war.BattleResult, rounds_shown: int, armies_now: dict[str, war.Army],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚔️ WAR: {names[war.ATTACKER]} vs {names[war.DEFENDER]}",
        description=(
            f"{names[war.ATTACKER]} marches on {names[war.DEFENDER]}'s Luck reserves.\n"
            f"🎲 The dice of war: {names[war.ATTACKER]} rolled x{result.attacker.fortune:.2f}, "
            f"{names[war.DEFENDER]} rolled x{result.defender.fortune:.2f}"
        ),
        color=discord.Color.dark_red(),
    )
    embed.add_field(name=f"⚔️ {names[war.ATTACKER]}", value=_army_lines(armies_now[war.ATTACKER]), inline=True)
    embed.add_field(name=f"🛡️ {names[war.DEFENDER]}", value=_army_lines(armies_now[war.DEFENDER]), inline=True)
    if rounds_shown:
        sections = [_round_lines(i + 1, log, names) for i, log in enumerate(result.rounds[:rounds_shown])]
        # A huge battle can outgrow the field -- drop the oldest rounds rather than cut mid-line.
        while len(sections) > 1 and len("\n\n".join(sections)) > 980:
            sections.pop(0)
        if len(sections) < rounds_shown:
            sections.insert(0, "*(earlier rounds omitted)*")
        embed.add_field(name="📜 Battle Report", value="\n\n".join(sections)[:1024], inline=False)
    return embed


def add_outcome(
    embed: discord.Embed, names: dict[str, str], result: war.BattleResult, spoils: int,
    winner_luck: int, loser_luck: int, injured_names: list[str], shielded_name: str | None,
):
    winner, loser = names[result.winner], names[war.DEFENDER if result.winner == war.ATTACKER else war.ATTACKER]
    lines = [
        f"🏆 **{winner}** wins the war and seizes **{spoils} 🍀 Luck**!",
        f"{winner}'s Luck: **{winner_luck}** 🍀 / {loser}'s Luck reserves fall to **{loser_luck}** 🍀",
    ]
    if injured_names:
        shown = ", ".join(injured_names[:MAX_INJURED_NAMES])
        if len(injured_names) > MAX_INJURED_NAMES:
            shown += f" and {len(injured_names) - MAX_INJURED_NAMES} more"
        lines.append(f"🤕 Injured for {war.WAR_INJURY_HOURS}h: {shown}")
    if shielded_name:
        lines.append(f"🏳️ {shielded_name} is under a {war.LOSER_SHIELD_SECONDS // 3600}h ceasefire.")
    embed.add_field(name="Outcome", value="\n".join(lines), inline=False)


async def present_battle(
    send, names: dict[str, str], result: war.BattleResult, spoils: int, winner_luck: int, loser_luck: int, injured_names: list[str], shielded_name: str | None,
    content: str | None = None,
):
    """Plays the battle out by editing one message round by round, replaying result.snapshots
    (snapshots[i] is both armies' state after i rounds, [0] = before the first charge)."""
    snapshots = result.snapshots
    message = await send(content=content, embed=build_battle_embed(names, result, 0, snapshots[0]))
    for i in range(1, len(snapshots)):
        await asyncio.sleep(ROUND_DELAY_SECONDS)
        embed = build_battle_embed(names, result, i, snapshots[i])
        if i == len(snapshots) - 1:
            add_outcome(embed, names, result, spoils, winner_luck, loser_luck, injured_names, shielded_name)
        await message.edit(embed=embed)
