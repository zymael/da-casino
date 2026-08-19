import asyncio

import discord

import achievements
import db
import horserace
import ranch_render

RANCH_BANNER_PATH = "assets/ranch_banner.png"
MAX_RANCH_HORSES = 10  # Discord embed field values cap at 1024 chars -- same cap style as !stats
MAX_SELECT_OPTIONS = 25  # Discord's hard limit on a single Select's options


async def build_ranch_display(
    guild_id: int, user_id: int, display_name: str, selected_horse_index: int | None
) -> tuple[discord.Embed, "RanchView"]:
    """Builds the (embed, view) pair for a player's ranch dashboard -- reused by the initial
    !ranch command and every button/select refresh afterward, so the dashboard always reflects
    fresh DB state rather than the state at the time the message was first sent."""
    currency = db.get_currency_name(guild_id)
    owned = await asyncio.to_thread(db.get_ranch_horses, guild_id, user_id)
    tier = await asyncio.to_thread(db.get_facility_tier, guild_id, user_id)

    if selected_horse_index is not None and not any(h["horse_index"] == selected_horse_index for h in owned):
        selected_horse_index = None  # no longer valid (horse renumbered/lost some other way)

    embed = discord.Embed(title=f"🐴 {display_name}'s Ranch", color=discord.Color.dark_gold())
    embed.set_image(url="attachment://ranch_banner.png")

    if tier > 0:
        facility = horserace.FACILITY_TIERS[tier - 1]
        facility_text = f"**{facility['name']}** (Tier {tier}) — +{int(facility['bonus'] * 100)}% training gains"
    else:
        facility_text = "None yet"
    embed.add_field(name="🏗️ Facility", value=facility_text, inline=False)

    if owned:
        lines = []
        for horse in owned[:MAX_RANCH_HORSES]:
            sex_symbol = horserace.SEX_SYMBOLS.get(horse["sex"], "")
            kind = "🐣" if horse["is_foal"] else "🏆"
            record = (
                f"{horse['wins']}W-{horse['places']}P-{horse['shows']}S ({horse['races']} starts)"
                if horse["races"] else "unraced"
            )
            boost_text = f" — 🧪 {horse['pending_boost_stat']} boost queued" if horse["pending_boost_stat"] else ""
            marker = "👉 " if horse["horse_index"] == selected_horse_index else ""
            lines.append(
                f"{marker}{kind} **{horse['horse_index'] + 1}. {horse['name']}** {sex_symbol} {horse['coat']} — "
                f"Age {horse['age']}\nSPD {horse['speed']:.0f} / END {horse['endurance']:.0f} / "
                f"SPI {horse['spirit']:.0f} — {record}{boost_text}"
            )
        if len(owned) > MAX_RANCH_HORSES:
            lines.append(f"...and {len(owned) - MAX_RANCH_HORSES} more — see `!horses`.")
        embed.add_field(name=f"Your Horses ({len(owned)})", value="\n\n".join(lines), inline=False)
        footer = f"Pick a horse below, then Train or Boost it. Boost items cost {horserace.ITEM_COST} {currency} each."
    else:
        embed.add_field(name="Your Horses", value="None yet — try `!buyhorse` or `!buyfoal`.", inline=False)
        footer = "Buy a horse to unlock Train/Boost — Upgrade Facility is still available."
    embed.set_footer(text=footer)

    view = RanchView(guild_id, user_id, owned, selected_horse_index)
    return embed, view


class RanchHorseSelect(discord.ui.Select):
    def __init__(self, ranch_view: "RanchView"):
        options = [
            discord.SelectOption(
                label=f"{h['horse_index'] + 1}. {h['name']}"[:100],
                value=str(h["horse_index"]),
                default=(h["horse_index"] == ranch_view.selected_horse_index),
            )
            for h in ranch_view.owned[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(placeholder="Choose a horse for Train/Boost...", options=options, row=0)
        self.ranch_view = ranch_view

    async def callback(self, interaction: discord.Interaction):
        embed, view = await build_ranch_display(
            self.ranch_view.guild_id, self.ranch_view.user_id, interaction.user.display_name, int(self.values[0])
        )
        await interaction.response.edit_message(embed=embed, view=view)


class RanchView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, owned: list[dict], selected_horse_index: int | None):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        self.owned = owned
        self.selected_horse_index = selected_horse_index
        if owned:
            self.add_item(RanchHorseSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your ranch.", ephemeral=True)
            return False
        return True

    def _selected_horse(self) -> dict | None:
        if self.selected_horse_index is None:
            return None
        return next((h for h in self.owned if h["horse_index"] == self.selected_horse_index), None)

    async def _refresh(self, interaction: discord.Interaction):
        """Rebuilds and re-edits the dashboard message in place after an action succeeds --
        separate from the interaction's own (already-sent, ephemeral) response, matching how
        this codebase's other views update a persistent message outside the initial response
        (e.g. blackjack's on_timeout editing self.message directly)."""
        embed, view = await build_ranch_display(
            self.guild_id, self.user_id, interaction.user.display_name, self.selected_horse_index
        )
        await interaction.message.edit(embed=embed, view=view)

    @discord.ui.button(label="🏋️ Train", style=discord.ButtonStyle.primary, row=1)
    async def train_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        horse = self._selected_horse()
        if horse is None:
            await interaction.response.send_message("Select a horse first.", ephemeral=True)
            return

        tier = await asyncio.to_thread(db.get_facility_tier, self.guild_id, self.user_id)
        facility_bonus = horserace.facility_bonus_for_tier(tier)
        speed_gain, endurance_gain, spirit_gain = horserace.compute_training_gains(
            facility_bonus, horse["pending_boost_stat"]
        )
        status, payload = await asyncio.to_thread(
            db.train_horse, self.guild_id, horse["horse_index"], self.user_id,
            speed_gain, endurance_gain, spirit_gain, horserace.STAT_CAP,
        )
        if status == "cooldown":
            await interaction.response.send_message("That horse has already been trained today.", ephemeral=True)
            return
        if status == "not_owner":
            await interaction.response.send_message("You don't own that horse anymore.", ephemeral=True)
            return

        new_speed, new_endurance, new_spirit, new_age = payload
        await interaction.response.send_message(
            f"🏋️ **{horse['name']}** trained! SPD {new_speed:.0f} / END {new_endurance:.0f} / "
            f"SPI {new_spirit:.0f} — Age {new_age}",
            ephemeral=True,
        )
        await self._refresh(interaction)

    async def _boost(self, interaction: discord.Interaction, stat: str):
        horse = self._selected_horse()
        if horse is None:
            await interaction.response.send_message("Select a horse first.", ephemeral=True)
            return
        status, balance = await asyncio.to_thread(
            db.buy_horse_item, self.guild_id, self.user_id, horse["horse_index"], stat, horserace.ITEM_COST
        )
        currency = db.get_currency_name(self.guild_id)
        if status == "pending":
            await interaction.response.send_message(
                "That horse already has a boost queued — train it first to use it up.", ephemeral=True
            )
            return
        if status == "broke":
            await interaction.response.send_message(
                f"A training-boost item costs **{horserace.ITEM_COST}** {currency} — you only have **{balance}**.",
                ephemeral=True,
            )
            return
        if status == "not_owner":
            await interaction.response.send_message("You don't own that horse anymore.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"🧪 Queued a **{stat}** boost on **{horse['name']}**! Balance: **{balance}** {currency}.", ephemeral=True
        )
        await self._refresh(interaction)

    @discord.ui.button(label="⚡ Boost SPD", style=discord.ButtonStyle.secondary, row=2)
    async def boost_speed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._boost(interaction, "speed")

    @discord.ui.button(label="🛡️ Boost END", style=discord.ButtonStyle.secondary, row=2)
    async def boost_endurance_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._boost(interaction, "endurance")

    @discord.ui.button(label="✨ Boost SPI", style=discord.ButtonStyle.secondary, row=2)
    async def boost_spirit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._boost(interaction, "spirit")

    @discord.ui.button(label="🏗️ Upgrade Facility", style=discord.ButtonStyle.success, row=3)
    async def upgrade_facility_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        tier = await asyncio.to_thread(db.get_facility_tier, self.guild_id, self.user_id)
        if tier >= len(horserace.FACILITY_TIERS):
            await interaction.response.send_message("You're already at the highest facility tier!", ephemeral=True)
            return

        next_facility = horserace.FACILITY_TIERS[tier]
        status, balance = await asyncio.to_thread(
            db.upgrade_facility, self.guild_id, self.user_id, next_facility["tier"], next_facility["cost"],
            len(horserace.FACILITY_TIERS),
        )
        currency = db.get_currency_name(self.guild_id)
        if status == "broke":
            await interaction.response.send_message(
                f"**{next_facility['name']}** costs **{next_facility['cost']}** {currency} — "
                f"you only have **{balance}**.",
                ephemeral=True,
            )
            return
        if status == "wrong_tier":
            await interaction.response.send_message("Your facility tier changed — try again.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"🏗️ Built **{next_facility['name']}**! All your horses now train "
            f"+{int(next_facility['bonus'] * 100)}% faster. Balance: **{balance}** {currency}.",
            ephemeral=True,
        )
        await self._refresh(interaction)

    @discord.ui.button(label="👋 Introduce yourself to Kel", style=discord.ButtonStyle.secondary, row=4)
    async def kel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Deliberately not a text command -- the only way to meet Kel is to click this. Grants
        the (idempotent, one-time) love_in_bloom achievement and swaps the dashboard's banner for
        a speech-bubble version, every time it's clicked (the greeting always replays, even
        though the achievement itself only fires once)."""
        await achievements.try_award_many(
            interaction.channel.send, self.guild_id, self.user_id, interaction.user.display_name, ["love_in_bloom"]
        )

        embed, view = await build_ranch_display(
            self.guild_id, self.user_id, interaction.user.display_name, self.selected_horse_index
        )
        buf = await asyncio.to_thread(ranch_render.render_kel_introduction)
        file = discord.File(buf, filename="kel_intro.png")
        embed.set_image(url="attachment://kel_intro.png")
        await interaction.response.edit_message(embed=embed, attachments=[file], view=view)
