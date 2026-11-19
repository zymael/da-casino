"""The sell popup, opened via npc_view.SellButton for any NPC with npcs.json's "buys_items" checked.

Ephemeral and redraws itself in place. Holdings are split into one tab per item kind (only kinds
the player actually owns get a tab) and paged PAGE_SIZE at a time, so the embed list and the
picker always show the same items and nothing is ever cut off by Discord's 25-option Select cap
or 4096-char embed limit. Flow: pick one or more items -> for any picked stack, a modal asks how
many of each (dropdowns, default 1) -> a confirm screen shows exactly what goes and the total ->
Sell. Every copy is still re-checked at sale time by sell.sell (via sell.sell_many).
"""

import asyncio

import discord

import db
import dungeon
import inventory_view
import npcs
import sell

# (kind, tab emoji, tab label) in tab order -- one entry per sell.SELLABLE_REGISTRIES kind.
_TABS = (
    ("equipment", "⚔️", "Gear"),
    ("material", "⛏️", "Materials"),
    ("consumable", "🧪", "Potions"),
    ("horse_clothes", "👒", "Horse Clothes"),
    ("housing_item", "🛋️", "Furniture"),
)
_KIND_EMOJI = {kind: emoji for kind, emoji, _ in _TABS}
_KIND_LABEL = {kind: label for kind, _, label in _TABS}
PAGE_SIZE = 25  # Discord's Select option cap
GEAR_PAGE_SIZE = 10  # gear lines carry stats + proc lines, so fewer per page keeps the embed readable
MAX_CONFIRM_LINES = 30
MAX_STACKS_PER_PICK = 5  # Discord's per-modal component cap -- one quantity dropdown per stack


def _item_blurb(item: dict) -> str:
    """Same field-name reconciliation as shop_view._item_blurb -- most content uses "flavor",
    housing items use "description"."""
    return item.get("flavor") or item.get("description") or ""


def _entries(holdings: list[tuple[str, str, int]]) -> dict[str, list[dict]]:
    """{kind: [entry, ...]} sorted most valuable stack first; kinds with nothing held are absent."""
    by_kind: dict[str, list[dict]] = {}
    for kind, item_id, qty in holdings:
        item = sell.SELLABLE_REGISTRIES[kind]()[item_id]
        price = sell.sell_price(item)
        by_kind.setdefault(kind, []).append(
            {"kind": kind, "item_id": item_id, "qty": qty, "item": item, "price": price}
        )
    for entries in by_kind.values():
        entries.sort(key=lambda e: (-e["price"] * e["qty"], e["item"]["name"]))
    return by_kind


def _emoji(entry: dict) -> str:
    if entry["kind"] == "equipment":
        return dungeon.RARITY_EMOJI[entry["item"]["rarity"]]
    return _KIND_EMOJI[entry["kind"]]


def _page_size(kind: str) -> int:
    return GEAR_PAGE_SIZE if kind == "equipment" else PAGE_SIZE


def _gear_summary(item: dict) -> str:
    """'Weapon · iLvl 40 · ATK +5 / HP +10 · rare' -- same stat text !inventory uses."""
    return f"{item['slot'].capitalize()} · {inventory_view._equipment_summary(item)}"


def _gear_lines(item: dict) -> str:
    """Stats on the item's own line, then one indented line per on-use/on-hit effect."""
    procs = inventory_view.dynamic_effect_lines(item)
    return "".join(f"\n　{p}" for p in procs)


def _line(entry: dict, currency: str) -> str:
    item, qty, price = entry["item"], entry["qty"], entry["price"]
    text = f"{_emoji(entry)} **{item['name']}**"
    if entry["kind"] == "equipment":
        text += f" ({_gear_summary(item)})"
    if qty > 1:
        text += f" x{qty} — {price} ea → **{price * qty}** {currency}"
    else:
        text += f" — **{price}** {currency}"
    if entry["kind"] == "equipment":
        text += _gear_lines(item)
    return text[:350]


def _equipped_field(equipped: dict[str, str]) -> str:
    """What's worn right now, so gear for sale can be compared against it."""
    lines = []
    for slot in dungeon.EQUIPMENT_SLOTS:
        item = dungeon.EQUIPMENT.get(equipped.get(slot)) if equipped.get(slot) else None
        if item is None:
            lines.append(f"**{slot.capitalize()}**: *empty*")
        else:
            stats = inventory_view.stat_bonus_text(item)
            lines.append(
                f"**{slot.capitalize()}**: {dungeon.RARITY_EMOJI[item['rarity']]} {item['name']}"
                + (f" — {stats}" if stats else "")
            )
    return "\n".join(lines)


async def build_sell_display(
    guild_id: int, user_id: int, npc_id: str, kind: str | None = None, page: int = 0,
    note: tuple[str, str] | None = None,
) -> tuple[discord.Embed, "SellView"]:
    """`kind`/`page` pick the tab and page (falling back to the first tab / last page if that tab
    emptied out); `note` is an optional (field name, value) shown under the list, e.g. what just sold."""
    npc = npcs.NPCS[npc_id]
    held = await asyncio.to_thread(db.get_inventory, guild_id, user_id)
    stored_equipment = await asyncio.to_thread(db.get_equipment_inventory, guild_id, user_id)
    clothes_in_use = await asyncio.to_thread(sell.horse_clothes_in_use, guild_id, user_id)
    by_kind = _entries(sell.for_sale_holdings(held, stored_equipment, clothes_in_use))
    balance = await asyncio.to_thread(db.get_balance, guild_id, user_id)
    currency = db.get_currency_name(guild_id)

    embed = discord.Embed(title=f"💰 Sell to {npc['name']}", color=discord.Color.dark_gold())
    if kind not in by_kind:
        kind = next((k for k, _, _ in _TABS if k in by_kind), None)
    page_entries: list[dict] = []
    pages = 1
    if kind is None:
        embed.description = "You don't have anything to sell right now."
        embed.set_footer(text=f"You have {balance} {currency}.")
    else:
        entries = by_kind[kind]
        size = _page_size(kind)
        pages = (len(entries) + size - 1) // size
        page = max(0, min(page, pages - 1))
        page_entries = entries[page * size:(page + 1) * size]
        heading = f"**{_KIND_EMOJI[kind]} {_KIND_LABEL[kind]}**"
        if pages > 1:
            heading += f" — page {page + 1}/{pages}"
        embed.description = heading + "\n" + "\n".join(_line(e, currency) for e in page_entries)
        tab_total = sum(e["price"] * e["qty"] for e in entries)
        all_total = sum(e["price"] * e["qty"] for es in by_kind.values() for e in es)
        embed.set_footer(
            text=f"This tab: {tab_total} {currency} · Everything: {all_total} {currency} · You have {balance} {currency}"
        )
        if kind == "equipment":
            equipped = await asyncio.to_thread(db.get_equipped_items, guild_id, user_id)
            embed.add_field(name="Currently equipped", value=_equipped_field(equipped), inline=False)
    if note:
        embed.add_field(name=note[0], value=note[1][:1024], inline=False)

    view = SellView(guild_id, user_id, npc_id, by_kind, kind, page, pages, page_entries, currency)
    return embed, view


class SellView(discord.ui.View):
    def __init__(
        self, guild_id: int, user_id: int, npc_id: str, by_kind: dict[str, list[dict]], kind: str | None,
        page: int, pages: int, page_entries: list[dict], currency: str,
    ):
        super().__init__(timeout=300)
        self.guild_id, self.user_id, self.npc_id = guild_id, user_id, npc_id
        self.kind, self.page, self.currency = kind, page, currency
        if kind is None:
            return
        if len(by_kind) > 1:
            for k, emoji, label in _TABS:
                if k in by_kind:
                    self.add_item(_TabButton(k, f"{label} ({len(by_kind[k])})", emoji, active=k == kind))
        self.add_item(SellSelect(page_entries, currency))
        if pages > 1:
            self.add_item(_PageButton("◀ Prev", page - 1, disabled=page == 0))
            self.add_item(_PageButton("Next ▶", page + 1, disabled=page >= pages - 1))
        self.tab_entries = by_kind[kind]
        self.add_item(_SellTabButton(f"Sell All {_KIND_LABEL[kind]}"))

    async def redraw(self, interaction: discord.Interaction, kind=None, page=None, note=None):
        embed, view = await build_sell_display(
            self.guild_id, self.user_id, self.npc_id,
            self.kind if kind is None else kind, self.page if page is None else page, note,
        )
        if interaction.response.is_done():  # already deferred (see ConfirmSellView.confirm_button)
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)


class _TabButton(discord.ui.Button):
    def __init__(self, kind: str, label: str, emoji: str, active: bool):
        super().__init__(
            label=label, emoji=emoji, row=0, disabled=active,
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
        )
        self.kind = kind

    async def callback(self, interaction: discord.Interaction):
        await self.view.redraw(interaction, kind=self.kind, page=0)


class _PageButton(discord.ui.Button):
    def __init__(self, label: str, target: int, disabled: bool):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=2, disabled=disabled)
        self.target = target

    async def callback(self, interaction: discord.Interaction):
        await self.view.redraw(interaction, page=self.target)


class _SellTabButton(discord.ui.Button):
    """Every copy of everything in the current tab (all pages), via the same confirm screen."""

    def __init__(self, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.danger, row=2)

    async def callback(self, interaction: discord.Interaction):
        await _show_confirm(interaction, self.view, [(e, e["qty"]) for e in self.view.tab_entries])


class SellSelect(discord.ui.Select):
    def __init__(self, entries: list[dict], currency: str):
        self.entries = entries
        options = []
        for i, e in enumerate(entries):
            item = e["item"]
            label = f"{item['name']}" + (f" x{e['qty']}" if e["qty"] > 1 else "") + f" — {e['price']} {currency}"
            detail = _gear_summary(item) if e["kind"] == "equipment" else _item_blurb(item)
            options.append(discord.SelectOption(
                label=label[:100], value=str(i), description=detail[:100] or None, emoji=_emoji(e),
            ))
        super().__init__(
            placeholder="Pick items to sell...", options=options, min_values=1, max_values=len(options), row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        picked = [self.entries[int(v)] for v in self.values]
        stacks = [e for e in picked if e["qty"] > 1]
        if len(stacks) > MAX_STACKS_PER_PICK:
            await interaction.response.send_message(
                f"Pick at most {MAX_STACKS_PER_PICK} items you have more than one of at a time.", ephemeral=True,
            )
            return
        if stacks:
            await interaction.response.send_modal(QuantityModal(self.view, picked))
            return
        await _show_confirm(interaction, self.view, [(e, 1) for e in picked])


def _quantity_options(qty: int) -> list[discord.SelectOption]:
    counts = list(range(1, qty + 1)) if qty <= PAGE_SIZE else list(range(1, PAGE_SIZE)) + [qty]
    return [
        discord.SelectOption(label=f"All {n}" if n == qty else str(n), value=str(n), default=n == 1)
        for n in counts
    ]


class QuantityModal(discord.ui.Modal):
    """One "how many?" dropdown per picked stack (single copies need no question), default 1."""

    def __init__(self, parent: SellView, picked: list[dict]):
        super().__init__(title="How many?", timeout=300)
        self.parent = parent
        self.picked = picked
        self.selects: dict[int, discord.ui.Select] = {}
        for i, e in enumerate(picked):
            if e["qty"] > 1:
                select = discord.ui.Select(options=_quantity_options(e["qty"]), min_values=1, max_values=1)
                self.selects[i] = select
                self.add_item(discord.ui.Label(
                    text=e["item"]["name"][:45], description=f"You have {e['qty']}", component=select,
                ))

    async def on_submit(self, interaction: discord.Interaction):
        picks = [(e, int(self.selects[i].values[0]) if i in self.selects else 1) for i, e in enumerate(self.picked)]
        await _show_confirm(interaction, self.parent, picks)


async def _show_confirm(interaction: discord.Interaction, parent: SellView, picks: list[tuple[dict, int]]):
    currency = parent.currency
    total = sum(e["price"] * n for e, n in picks)
    lines = []
    for e, n in picks[:MAX_CONFIRM_LINES]:
        line = f"{_emoji(e)} **{e['item']['name']}**"
        line += f" x{n} → {e['price'] * n} {currency}" if n > 1 else f" → {e['price']} {currency}"
        lines.append(line[:120])
    if len(picks) > MAX_CONFIRM_LINES:
        lines.append(f"*...and {len(picks) - MAX_CONFIRM_LINES} more*")
    count = sum(n for _, n in picks)
    embed = discord.Embed(
        title="💰 Sell these?",
        description="\n".join(lines) + f"\n\n**{count} item{'s' if count != 1 else ''} · Total: {total} {currency}**",
        color=discord.Color.dark_gold(),
    )
    await interaction.response.edit_message(embed=embed, view=ConfirmSellView(parent, picks))


class ConfirmSellView(discord.ui.View):
    def __init__(self, parent: SellView, picks: list[tuple[dict, int]]):
        super().__init__(timeout=300)
        self.parent = parent
        self.picks = picks
        self.done = False

    @discord.ui.button(label="Sell", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return  # double-click guard -- the second click must not sell a second batch
        self.done = True
        self.stop()
        # Acknowledge first: Discord drops an interaction not answered within 3s, and a big batch
        # can take longer than that -- before this, the sale went through but the reply was lost.
        await interaction.response.defer()
        p = self.parent
        result = await sell.sell_many(
            p.guild_id, p.user_id, [(e["kind"], e["item_id"], n) for e, n in self.picks],
        )
        lines = [
            f"💰 **{item['name']}**" + (f" x{count}" if count > 1 else "") + f" for {earned} {p.currency}"
            for item, count, earned in result["sold"]
        ]
        lines += [
            f"⚠️ **{item['name']}**: you only had {got} of {wanted} left" if got else
            f"⚠️ **{item['name']}**: you don't have that anymore"
            for item, wanted, got in result["short"]
        ]
        title = f"Sold for {result['total']} {p.currency}" if result["sold"] else "Nothing sold"
        await p.redraw(interaction, note=(title, "\n".join(lines)))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await self.parent.redraw(interaction)
