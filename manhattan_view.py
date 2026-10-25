"""Embeds for the Mane-hay-tan Project (manhattan.py) -- !manhattan's status board and the
detonation announcement. bot.py never builds discord.ui/embeds for this itself."""

import discord

BAR_LENGTH = 20


def _progress_bar(pot: int, goal: int) -> str:
    filled = min(BAR_LENGTH, pot * BAR_LENGTH // goal) if goal else BAR_LENGTH
    return "🟩" * filled + "⬛" * (BAR_LENGTH - filled)


def build_status_embed(status: dict, currency: str, name_for) -> discord.Embed:
    """`name_for(user_id)` resolves a donor's display name (bot.py owns member lookup)."""
    bomb, pot = status["bomb"], status["pot"]
    goal = bomb["goal"]
    embed = discord.Embed(
        title="☢️ The Mane-hay-tan Project",
        description=f"Dubious Research into **{bomb['name']}**",
        color=discord.Color.red() if status["armed"] else discord.Color.dark_green(),
    )
    percent = min(100, pot * 100 // goal)
    embed.add_field(
        name="Progress",
        value=f"{_progress_bar(pot, goal)}\n**{pot:,}** / **{goal:,}** {currency} ({percent}%)",
        inline=False,
    )
    if status["armed"]:
        embed.add_field(name="🔴 ARMED", value="The bomb is built. Anyone can `!launch` it.", inline=False)
    if status["fallout_until"]:
        embed.add_field(
            name="☢️ Fallout",
            value=f"Irradiated foals and boosted training. The Front is a crater. Clears <t:{status['fallout_until']}:R>.",
            inline=False,
        )
    if status["top_donors"]:
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < len(medals) else f'`#{i + 1}`'} {name_for(user_id)}: **{amount:,}**"
            for i, (user_id, amount) in enumerate(status["top_donors"])
        ]
        embed.add_field(name="Top Researchers", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Bombs detonated: {status['detonations']} · !donate <amount> to fund the research")
    return embed


def build_detonation_embed(bomb: dict, launcher_name: str, fallout_until: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"☢️ {bomb['name']} HAS BEEN LAUNCHED",
        description=bomb["detonation_text"],
        color=discord.Color.orange(),
    )
    embed.add_field(name="Button pressed by", value=launcher_name, inline=False)
    embed.add_field(
        name="☢️ Fallout until",
        value=f"<t:{fallout_until}:f> (<t:{fallout_until}:R>)\n"
              "Foals bought now are born Irradiated, training gains are boosted, and The Front is closed.",
        inline=False,
    )
    return embed
