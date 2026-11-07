"""Tiered allocation of owned items across needs, loadout and reserve targets.

Priority (lower tier first): 1 ARC Tracker needs, 2 loadout weapons, 3 other
loadout groups, 4..6 rules.yaml / ARC keep targets. Within a tier, targets keep
their file order and are served in two passes: every ``min`` first, then every
``ideal``. A target that is short claims the ingredients to craft the shortfall.
Units are claimed at most once, so a later Keep/Sell verdict can sell only what
is left in ``Plan.unclaimed``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

from arc_helper.arctracker.rules import DEFAULT_TIER
from arc_helper.arctracker.rules import Target

if TYPE_CHECKING:
    from arc_helper.arctracker.catalogue import Catalogue
    from arc_helper.arctracker.rules import Rules
    from arc_helper.arctracker.snapshot import Need
    from arc_helper.arctracker.snapshot import PlayerSnapshot

NEED = "__need__"
SELF = "__self__"
WEAPON_TIER = 2


@dataclass(frozen=True)
class TargetState:
    item_id: str
    owned: int
    min: int
    ideal: int
    tier: int
    craft_inputs: dict[str, int]

    @property
    def state(self) -> Literal["low", "building", "met"]:
        if self.owned < self.min:
            return "low"
        if self.owned < self.ideal:
            return "building"
        return "met"

    @property
    def shortfall(self) -> int:
        return max(0, self.ideal - self.owned)


@dataclass(frozen=True)
class Plan:
    catalogue: Catalogue
    snapshot: PlayerSnapshot | None
    rules: Rules
    owned: dict[str, int]
    need_claims: dict[str, int]  # units claimed by needs (<= owned)
    need_reasons: dict[str, Need]  # first need per item, snapshot order
    targets: dict[str, TargetState]
    claims: dict[str, dict[str, int]]  # item -> {target id | NEED | SELF: units}
    demand: dict[str, dict[str, int]]  # ingredient -> {target id: units to craft}
    unclaimed: dict[str, int]
    need_totals: dict[str, int]  # summed Need.needed per item


@dataclass(frozen=True)
class _Spec:
    item_id: str
    target: Target
    units: tuple[str, ...]  # items whose units count as this target, best first
    craft_inputs: dict[str, int]


def _raise_to_keep(target: Target, keep: int | None) -> Target:
    if keep is None or keep <= 0:
        return target
    return Target(max(target.min, keep), max(target.ideal, keep), target.tier)


def _keep(snapshot: PlayerSnapshot | None, item_id: str) -> int | None:
    return snapshot.keep_amounts.get(item_id) if snapshot is not None else None


def effective_target(
    rules: Rules, snapshot: PlayerSnapshot | None, item_id: str
) -> Target | None:
    """The rules target raised to the ARC keep amount when that is higher; a
    keep amount alone gives ``Target(keep, keep, DEFAULT_TIER)``."""
    keep = _keep(snapshot, item_id)
    target = rules.targets.get(item_id)
    if target is None:
        if keep is None or keep <= 0:
            return None
        return Target(keep, keep, DEFAULT_TIER)
    return _raise_to_keep(target, keep)


def _add(total: dict[str, int], more: dict[str, int]) -> None:
    for k, v in more.items():
        total[k] = total.get(k, 0) + v


def _weapon_inputs(
    catalogue: Catalogue, chain: list[str], owned: dict[str, int]
) -> dict[str, int]:
    """Per-copy cost to reach chain[-1]: upgrades from the best owned lower tier,
    else the tier I recipe plus every upgrade. {} when any step is unknown.

    A tier's ``upgrade_cost`` is the cost to reach it from the tier below
    (tier I's carries its first upgrade, so it is never used here).
    """
    if any(x not in catalogue.items for x in chain):
        return {}
    items = [catalogue.items[x] for x in chain]
    start = 0
    for idx in range(len(chain) - 1):
        if owned.get(chain[idx], 0) > 0:
            start = idx + 1
    total: dict[str, int] = {}
    if start == 0:
        if not items[0].recipe:
            return {}
        _add(total, items[0].recipe)
        start = 1
    for item in items[start:]:
        if not item.upgrade_cost:
            return {}
        _add(total, item.upgrade_cost)
    return total


def _specs(
    catalogue: Catalogue,
    snapshot: PlayerSnapshot | None,
    rules: Rules,
    owned: dict[str, int],
) -> dict[str, _Spec]:
    specs: dict[str, _Spec] = {}
    for group in rules.loadout:
        for item_id in group.items:
            prev = specs.get(item_id)
            if prev is not None and prev.target.tier <= group.tier:
                continue
            specs.pop(item_id, None)
            target = _raise_to_keep(
                Target(group.copies, group.copies, group.tier),
                _keep(snapshot, item_id),
            )
            if group.tier == WEAPON_TIER:
                chain = (
                    catalogue.weapon_chain(item_id)
                    if item_id in catalogue.items
                    else [item_id]
                )
                specs[item_id] = _Spec(
                    item_id,
                    target,
                    tuple(reversed(chain)),
                    _weapon_inputs(catalogue, chain, owned),
                )
            else:
                specs[item_id] = _Spec(
                    item_id, target, (item_id,), _recipe(catalogue, item_id)
                )
    for item_id, base in rules.targets.items():
        if item_id not in specs:
            target = _raise_to_keep(base, _keep(snapshot, item_id))
            specs[item_id] = _Spec(
                item_id, target, (item_id,), _recipe(catalogue, item_id)
            )
    if snapshot is not None:
        for item_id, keep in snapshot.keep_amounts.items():
            if item_id not in specs and keep > 0:
                specs[item_id] = _Spec(
                    item_id,
                    Target(keep, keep, DEFAULT_TIER),
                    (item_id,),
                    _recipe(catalogue, item_id),
                )
    return specs


def _recipe(catalogue: Catalogue, item_id: str) -> dict[str, int]:
    item = catalogue.items.get(item_id)
    return dict(item.recipe) if item is not None else {}


def build_plan(
    catalogue: Catalogue, snapshot: PlayerSnapshot | None, rules: Rules
) -> Plan:
    owned = (
        {k: max(0, int(v)) for k, v in snapshot.owned.items()}
        if snapshot is not None
        else {}
    )
    remaining = dict(owned)
    claims: dict[str, dict[str, int]] = {}
    demand: dict[str, dict[str, int]] = {}

    def claim(item: str, who: str, units: int) -> int:
        units = min(units, remaining.get(item, 0))
        if units <= 0:
            return 0
        remaining[item] -= units
        by = claims.setdefault(item, {})
        by[who] = by.get(who, 0) + units
        return units

    # Tier 1: ARC Tracker needs.
    need_totals: dict[str, int] = {}
    need_reasons: dict[str, Need] = {}
    for need in snapshot.needs if snapshot is not None else ():
        need_totals[need.item_id] = need_totals.get(need.item_id, 0) + need.needed
        need_reasons.setdefault(need.item_id, need)
    need_claims = {
        item: claim(item, NEED, total) for item, total in need_totals.items()
    }

    # Tiers 2..6: loadout and reserve targets.
    specs = _specs(catalogue, snapshot, rules, owned)
    held = dict.fromkeys(specs, 0)  # units claimed for each target itself
    for tier in sorted({s.target.tier for s in specs.values()}):
        group = [s for s in specs.values() if s.target.tier == tier]
        for level in ("min", "ideal"):
            for s in group:
                goal = s.target.min if level == "min" else s.target.ideal
                for unit in s.units:
                    if held[s.item_id] >= goal:
                        break
                    held[s.item_id] += claim(unit, SELF, goal - held[s.item_id])
                short = goal - held[s.item_id]
                if short <= 0:
                    continue
                for ing, qty in s.craft_inputs.items():
                    want = short * qty
                    if want <= 0:
                        continue
                    by = demand.setdefault(ing, {})
                    by[s.item_id] = max(by.get(s.item_id, 0), want)
                    claim(ing, s.item_id, want - claims.get(ing, {}).get(s.item_id, 0))

    targets = {
        s.item_id: TargetState(
            s.item_id,
            sum(owned.get(u, 0) for u in s.units),
            s.target.min,
            s.target.ideal,
            s.target.tier,
            s.craft_inputs,
        )
        for s in specs.values()
    }
    return Plan(
        catalogue=catalogue,
        snapshot=snapshot,
        rules=rules,
        owned=owned,
        need_claims=need_claims,
        need_reasons=need_reasons,
        targets=targets,
        claims=claims,
        demand=demand,
        unclaimed=remaining,
        need_totals=need_totals,
    )
