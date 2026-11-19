"""Player-facing text for skill/item effects -- what !skills, !combathelp and item tooltips show.

admin_server.format_effect stays the terse authoring vocabulary for the admin list view; this is
the version a player reads, so it says who each effect lands on, how long it lasts, how likely it
is, and uses the guild's own stat labels (dungeon.stat_label) rather than ATK/DEF/SPD.
"""

import dungeon

# Effects that hurt whoever they land on -- flagged with ⚠️ when a skill aims one at its own side,
# so a built-in drawback (a self-stun, shredding your own DEF) can't be misread as a perk.
_HARMFUL_TYPES = {
    "damage_multiplier", "extra_attack", "execute_multiplier", "def_shred", "atk_debuff",
    "spatk_debuff", "speed_debuff", "dot", "sap", "stun", "hex", "charm", "hook",
}
# Effects that scale the damage roll they ride with -- listed alongside the damage, not as a
# separate status (mirrors dungeon.MODS_ONLY_EFFECT_TYPES + dungeon.TARGET_DAMAGE_SCALER_TYPES).
_DAMAGE_RIDERS = dungeon.MODS_ONLY_EFFECT_TYPES | dungeon.TARGET_DAMAGE_SCALER_TYPES
_STAT_BUFFS = {"atk_buff": "atk", "def_buff": "def", "spatk_buff": "spatk", "speed_buff": "speed"}
_STAT_DEBUFFS = {"atk_debuff": "atk", "spatk_debuff": "spatk", "speed_debuff": "speed", "def_shred": "def"}


def _pct(fraction: float) -> str:
    return f"{round(fraction * 100)}%"


def _rounds(n: int) -> str:
    return f"{n} round" if n == 1 else f"{n} rounds"


def _turns(n: int) -> str:
    return f"{n} turn" if n == 1 else f"{n} turns"


def recipient(effect: dict) -> str:
    """Who `effect` lands on, as a group label -- mirrors dungeon_view._resolve_player_action's own
    target resolution (dungeon.default_effect_target when "target" is unset). "Ally" is whichever
    teammate the caster has selected, which outside a party is the caster themselves."""
    target = effect.get("target") or dungeon.default_effect_target(effect["type"])
    if target == "self":
        return "You"
    if target == "ally":
        return "Party" if effect.get("aoe") else "Ally"
    return "All enemies" if effect.get("aoe") else "Enemy"


def effect_phrase(effect: dict, scales_with_caster: bool = True) -> str:
    """One effect, no recipient and no chance prefix -- e.g. 'stunned 1 turn', 'Cover −4',
    'next hit taken −40%'. `scales_with_caster` is False for an item, whose heal is exactly its
    authored value (see dungeon_view._effect_heal_fraction)."""
    t, v = effect["type"], effect.get("value")
    if t == "damage_multiplier":
        return f"{v:g}× damage"
    if t == "extra_attack":
        return f"bonus hit at {effect.get('multiplier', 1.0):g}×"
    if t == "execute_multiplier":
        return f"×{effect['base']:g} to ×{effect['base'] + effect['scale']:g} more the lower its HP"
    if t == "lifesteal_fraction":
        return f"drain {_pct(v)} of the damage as HP"
    if t == "heal_fraction":
        text = f"heal {_pct(v)}+ HP (grows with {dungeon.stat_label('spatk')})" if scales_with_caster else f"heal {_pct(v)} HP"
        return text + _overheal_note(effect)
    if t == "guard":
        # The next hit is multiplied by `reduction`, so 0.6 means 40% less, not 60%.
        return f"next hit taken −{_pct(1 - effect['reduction'])}"
    if t in _STAT_BUFFS:
        return f"{dungeon.stat_label(_STAT_BUFFS[t])} +{v:g}"
    if t in _STAT_DEBUFFS:
        return f"{dungeon.stat_label(_STAT_DEBUFFS[t])} −{v:g}"
    if t == "hp_buff":
        return f"max {dungeon.stat_label('hp')} +{v:g}"
    if t == "chip_gain":
        return f"{dungeon.stat_label('chips')} +{v:g}"
    if t == "taunt":
        return f"draws aggro (+{v:g} threat, its next attack is on you)"
    if t == "lower_threat":
        return f"sheds aggro (−{v:g} threat)"
    if t == "dodge_buff":
        return f"{dungeon.stat_label('dodge')} +{_pct(v)} for {_rounds(effect['duration'])}"
    if t == "resist_buff":
        return f"{dungeon.stat_label('resist')} +{_pct(v)} for {_rounds(effect['duration'])}"
    if t == "dot":
        return f"{round(v)} dmg/round for {_rounds(effect['duration'])}"
    if t == "hot":
        return f"regen {_pct(v)} HP/round for {_rounds(effect['duration'])}" + _overheal_note(effect)
    if t == "stun":
        return f"stunned {_turns(effect['duration'])}"
    if t == "sap":
        return f"sapped {_turns(effect['duration'])} (breaks on damage)"
    if t == "cleanse_dot":
        return "cures damage over time"
    if t == "cleanse_cc":
        return "cures stun/sap"
    return _resource_phrase(effect) or t


def _overheal_note(effect: dict) -> str:
    share = effect.get("overheal_barrier")
    return f" ({_pct(share)} of any overheal becomes {dungeon.stat_label('barrier')})" if share else ""


def _resource_phrase(effect: dict) -> str | None:
    """The class-resource effect types (see dungeon_view's "Class resources" sections)."""
    t, v, L = effect["type"], effect.get("value"), dungeon.stat_label
    if t == "charge":
        text = f"{L('charge')}: your next damaging {L('spatk')} skill does {v:g}× on every hit"
        stacks = effect.get("max_stacks", 1)
        if stacks > 1:
            top = v + effect.get("step", 0) * (stacks - 1)
            text += f", recast to stack up to {top:g}×"
            safe = effect.get("safe_stacks", stacks)
            if effect.get("short_chance") and safe < stacks:
                text += (f" (past {safe} stacks: {_pct(effect['short_chance'])} to short out, losing it "
                         f"and {_pct(effect.get('short_damage', 0))} of your {L('hp')})")
        return text
    if t == "overload":
        return (f"roll {effect['min']:g}×–{effect['max']:g}× for your next damaging {L('spatk')} skill; recasting rerolls, "
                f"+{_pct(effect.get('bust_chance', 0))} bust risk per roll (bust: lose it, stunned {_turns(effect.get('bust_stun', 2))})")
    if t == "hex":
        cap = f", max {_pct(effect['cap'])} of its {L('hp')}" if effect.get("cap") else ""
        return f"{L('hex')} for your next {_turns(effect['duration'])}: {_pct(v)} of all damage it takes meanwhile comes due at the end (less in bigger parties){cap}"
    if t in ("hype_spend", "grudge_spend"):
        meter = "hype" if t == "hype_spend" else "grudge"
        source = "anyone in your party hitting or dodging" if meter == "hype" else "each hit you take"
        return f"+{effect['per_point']:g}× per {L(meter)} (built by {source}, max {effect.get('max_points', 10)})"
    if t == "counter":
        return f"hit back for {_pct(v)} of every hit taken for {_rounds(effect['duration'])}"
    if t == "workmans_comp":
        hits = effect.get("max_hits", 3)
        return f"each hit you take for {_rounds(effect['duration'])} refunds {v:g} {L('chips')} (up to {hits} hits)"
    if t == "blood_in_water":
        return f"+1 bite at {effect['multiplier']:g}× each if it's slowed, under half {L('hp')}, or slowed as far as it goes"
    if t == "intercept":
        return f"take their next hit for them; {_pct(v)} of it becomes a party {L('barrier')}"
    if t == "collect_tab":
        return f"cash in its {L('tab')} (what it's hit you for) as {v:g}× bonus damage; an unpaid {L('tab')} boosts its loot"
    if t == "corner":
        return f"gets back up once at {_pct(v)} {L('hp')} if knocked out"
    if t == "pass_the_buck":
        return "takes on your whole party's stuns, DoTs and stat drops"
    if t == "plate_spend":
        return f"pass the {L('plate')} (filled by your overhealing): heals the party and smites every enemy for {_pct(v)} of it"
    if t == "hook":
        grow = f" (+{_pct(effect['growth'])} each of your turns)" if effect.get("growth") else ""
        return f"{L('hook')} for your next {_turns(effect['duration'])}: party hits on it drain {_pct(v)}{grow} as {L('hp')}"
    if t == "tells_spend":
        text = f"+{effect['per_point']:g}× per {L('tells')[:-1] if L('tells').endswith('s') else L('tells')} on it (your hits leave them, max {effect.get('max_points', 5)})"
        if effect.get("stunned_bonus"):
            text += ", extra on stunned targets"
        if effect.get("stun_at"):
            text += f"; at {effect['stun_at']}+ also stunned 2 turns"
        return text
    if t == "contract":
        return f"{L('contract')}: +{_pct(v)} per hit you land (max +{_pct(effect.get('max_bonus', 0.5))}), kill it for an extra turn"
    if t == "charm":
        return f"{L('charm').lower()} {_turns(effect['duration'])} (attacks its own ally)"
    if t == "pot_add":
        return f"{L('pot')} +{v:g} (max {effect.get('max', 3):g})"
    if t == "pot_cash":
        return f"× (1 + {L('pot')}), then it's emptied"
    return None


def _fragment(effect: dict, label: str, scales_with_caster: bool, text: str | None = None) -> str:
    text = text or effect_phrase(effect, scales_with_caster)
    if effect.get("chance", 1.0) < 1.0:
        text = f"{_pct(effect['chance'])}: {text}"
    if effect["type"] in _HARMFUL_TYPES and label in ("You", "Ally", "Party"):
        text = f"⚠️ {text}"
    return text


def describe_effects(effects: list[dict], scales_with_caster: bool = True, always_label: bool = False) -> str:
    """One cast's effects, grouped by who they land on: '**You:** ⚠️ stunned 1 turn · Moxie +7 —
    **Enemy:** Moxie −5'. A skill that's nothing but damage on one enemy drops the label ('1.3×
    damage'), unless `always_label`. Every damage-shaped mod (extra hits, a second multiplier, lifesteal) sits with the
    primary damage effect, since that's the roll they all feed (dungeon_view._resolve_player_action)."""
    mods = [e for e in effects if e["type"] in _DAMAGE_RIDERS]
    primary = next((e for e in mods if e["type"] == "damage_multiplier"), None) or next(
        (e for e in mods if e["type"] == "extra_attack"), None
    )
    damage_label = recipient(primary) if primary else "Enemy"

    groups: dict[str, list[str]] = {}
    if primary is not None:
        damage_parts = []
        if primary["type"] == "extra_attack":
            damage_parts.append("1× damage")  # the base roll still happens, the extra hits stack on it
        seen_multiplier = False
        for e in mods:
            text = None
            if e["type"] == "damage_multiplier":
                if seen_multiplier:
                    text = f"damage ×{e['value']:g}"
                seen_multiplier = True
            damage_parts.append(_fragment(e, damage_label, scales_with_caster, text))
        groups[damage_label] = damage_parts
    for e in effects:
        if e in mods:
            continue
        label = recipient(e)
        groups.setdefault(label, []).append(_fragment(e, label, scales_with_caster))

    _merge_dodge_resist(groups)
    if not always_label and list(groups) == ["Enemy"] and all(e in mods for e in effects):
        return " · ".join(groups["Enemy"])
    return " — ".join(f"**{label}:** {' · '.join(parts)}" for label, parts in groups.items())


def _merge_dodge_resist(groups: dict[str, list[str]]) -> None:
    """'Dodge +50% for 3 rounds · Resist +50% for 3 rounds' -> 'Dodge & Resist +50% for 3 rounds',
    in place -- the pairing shows up on most of the defensive skills."""
    dodge, resist = dungeon.stat_label("dodge"), dungeon.stat_label("resist")
    for parts in groups.values():
        for i, part in enumerate(parts):
            if not part.startswith(f"{dodge} +"):
                continue
            twin = part.replace(f"{dodge} +", f"{resist} +", 1)
            if twin in parts:
                parts[i] = part.replace(f"{dodge} +", f"{dodge} & {resist} +", 1)
                parts.remove(twin)
                return


def _all_effects(skill: dict) -> list[dict]:
    if skill.get("effects"):
        return skill["effects"]
    return [e for g in skill.get("effect_groups", []) for e in g["effects"]]


def describe_rolls(skill: dict) -> str:
    """Which stat the skill's damage scales with, and how an enemy avoids each part of it -- Dodge
    against the damage (their DEF), Resist against everything else aimed at them (their Magic), the
    same split dungeon_view._resolve_player_action rolls. Each piece appears only when it applies;
    empty for a pure self/party skill."""
    effects = _all_effects(skill)
    damage_types = ("damage_multiplier", "extra_attack")
    hits_enemy = any(e["type"] in damage_types and recipient(e) in ("Enemy", "All enemies") for e in effects)
    has_status = any(
        recipient(e) in ("Enemy", "All enemies") and e["type"] not in _DAMAGE_RIDERS
        for e in effects
    )
    L = dungeon.stat_label
    lines = []
    if any(e["type"] in damage_types for e in effects):
        lines.append(f"Hits with your {L('spatk' if skill.get('special') else 'atk')}.")
    dodge = f"{L('dodge')} the damage with their {L('def')}"
    resist = f"{L('resist')} the effects with their {L('spatk')}"
    if hits_enemy and has_status:
        lines.append(f"Enemies can {dodge} and {resist}.")
    elif hits_enemy:
        lines.append(f"Enemies can {L('dodge')} it with their {L('def')}.")
    elif has_status:
        lines.append(f"Enemies can {L('resist')} it with their {L('spatk')}.")
    return " ".join(lines)


def describe_skill(skill: dict) -> str:
    """A whole skill: its effects, or for an effect_groups skill each alternative behind 'One at
    random:' (with odds when the groups' relative "chance" weights aren't all equal)."""
    if skill.get("effects"):
        return describe_effects(skill["effects"])
    groups = skill.get("effect_groups", [])
    weights = [g.get("chance", dungeon.DEFAULT_EFFECT_GROUP_CHANCE) for g in groups]
    total = sum(weights) or 1
    even = len(set(weights)) <= 1
    texts = [describe_effects(g["effects"]) for g in groups]
    if any(t.startswith("**") for t in texts):
        # Labels are all-or-nothing across the alternatives, or a bare one reads as a different target.
        texts = [describe_effects(g["effects"], always_label=True) for g in groups]
    options = [t if even else f"({_pct(w / total)}) {t}" for t, w in zip(texts, weights)]
    return "One at random: " + " / ".join(options)
