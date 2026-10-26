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
    "spatk_debuff", "speed_debuff", "dot", "sap", "stun",
}
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
        return f"heal {_pct(v)}+ HP (grows with {dungeon.stat_label('spatk')})" if scales_with_caster else f"heal {_pct(v)} HP"
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
        return f"draws aggro (+{v:g} threat)"
    if t == "lower_threat":
        return f"sheds aggro (−{v:g} threat)"
    if t == "dodge_buff":
        return f"{dungeon.stat_label('dodge')} +{_pct(v)} for {_rounds(effect['duration'])}"
    if t == "resist_buff":
        return f"{dungeon.stat_label('resist')} +{_pct(v)} for {_rounds(effect['duration'])}"
    if t == "dot":
        return f"{round(v)} dmg/round for {_rounds(effect['duration'])}"
    if t == "hot":
        return f"regen {_pct(v)} HP/round for {_rounds(effect['duration'])}"
    if t == "stun":
        return f"stunned {_turns(effect['duration'])}"
    if t == "sap":
        return f"sapped {_turns(effect['duration'])} (breaks on damage)"
    if t == "cleanse_dot":
        return "cures damage over time"
    if t == "cleanse_cc":
        return "cures stun/sap"
    return t


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
    mods = [e for e in effects if e["type"] in dungeon.MODS_ONLY_EFFECT_TYPES or e["type"] == "execute_multiplier"]
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
    """Which stat the skill's damage scales with, and how an enemy avoids it -- each only when it
    applies: no damage stat for a skill that deals none, and no avoid line for one that never touches
    an enemy (dungeon_view._resolve_player_action only rolls avoidance for enemy-resolved effects).
    Empty for a pure self/party skill."""
    effects = _all_effects(skill)
    parts = []
    if any(e["type"] in ("damage_multiplier", "extra_attack") for e in effects):
        parts.append(f"Hits with your {dungeon.stat_label('spatk' if skill.get('special') else 'atk')}")
    if any(recipient(e) in ("Enemy", "All enemies") for e in effects if e["type"] != "lifesteal_fraction"):
        avoid = dungeon.resolved_avoid_type(skill)
        defense = dungeon.stat_label("def" if avoid == "dodge" else "spatk")
        parts.append(f"enemies can {dungeon.stat_label(avoid)} it with their {defense}")
    if not parts:
        return ""
    text = ", ".join(parts) + "."
    return text[0].upper() + text[1:]


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
