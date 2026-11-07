"""Tiered allocation of owned items across needs, loadout and reserve targets.

Priority (lower tier first): 1 ARC Tracker needs, 2 loadout weapons, 3 other
loadout groups, 4..6 rules.yaml / ARC keep targets. Within a tier, targets keep
their file order and are served in two passes: every ``min`` first, then every
``ideal``. A target that is short claims the ingredients to craft the shortfall.
A loadout weapon W with C copies holds exact W copies first, then lower-tier
copies (highest tier first); a held lower copy still demands its upgrade cost to
W, and each missing copy demands the full tier I recipe plus every upgrade.
Units are claimed at most once, so a later Keep/Sell verdict can sell only what
is left in ``Plan.unclaimed``.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
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
    owned: int  # raw owned count; for weapons the chain units held for it
    min: int
    ideal: int
    tier: int
    craft_inputs: dict[str, int]  # per-unit cost of the next unit / copy
    held: int = 0  # units claimed __self__ for this target
    exact: int | None = None  # weapons only: held copies at the target tier

    @property
    def state(self) -> Literal["low", "building", "met"]:
        if self.exact is not None:  # weapon: lower-tier copies are not "met"
            if self.exact >= self.ideal:
                return "met"
            return "low" if self.held < self.min else "building"
        if self.held < self.min:
            return "low"
        if self.held < self.ideal:
            return "building"
        return "met"

    @property
    def shortfall(self) -> int:
        have = self.exact if self.exact is not None else self.held
        return max(0, self.ideal - have)


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
    recipe: dict[str, int]  # per-unit craft cost (non-weapons)
    chain: tuple[str, ...] | None = None  # weapons: tier I .. item_id


@dataclass
class _Held:
    total: int = 0
    lower: dict[int, int] = field(default_factory=dict)  # chain index -> copies


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


def _add(total: dict[str, int], more: dict[str, int], times: int = 1) -> None:
    for k, v in more.items():
        total[k] = total.get(k, 0) + v * times


def _chain_cost(
    catalogue: Catalogue, chain: tuple[str, ...], start: int
) -> dict[str, int]:
    """Cost to bring one copy at chain[start] up to chain[-1]; start -1 means
    crafting from scratch (tier I recipe + every upgrade). {} when unknown.

    A tier's ``upgrade_cost`` is the cost to reach it from the tier below
    (tier I's carries its first upgrade, so it is never used here).
    """
    if any(x not in catalogue.items for x in chain):
        return {}
    items = [catalogue.items[x] for x in chain]
    total: dict[str, int] = {}
    if start < 0:
        if not items[0].recipe:
            return {}
        _add(total, items[0].recipe)
        start = 0
    for item in items[start + 1 :]:
        if not item.upgrade_cost:
            return {}
        _add(total, item.upgrade_cost)
    return total


def _specs(
    catalogue: Catalogue, snapshot: PlayerSnapshot | None, rules: Rules
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
            chain = None
            if group.tier == WEAPON_TIER:
                chain = tuple(
                    catalogue.weapon_chain(item_id)
                    if item_id in catalogue.items
                    else [item_id]
                )
            specs[item_id] = _Spec(item_id, target, _recipe(catalogue, item_id), chain)
    for item_id, base in rules.targets.items():
        if item_id not in specs:
            target = _raise_to_keep(base, _keep(snapshot, item_id))
            specs[item_id] = _Spec(item_id, target, _recipe(catalogue, item_id))
    if snapshot is not None:
        for item_id, keep in snapshot.keep_amounts.items():
            if item_id not in specs and keep > 0:
                specs[item_id] = _Spec(
                    item_id,
                    Target(keep, keep, DEFAULT_TIER),
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
    specs = _specs(catalogue, snapshot, rules)
    held = {item_id: _Held() for item_id in specs}
    full_cost = {
        s.item_id: _chain_cost(catalogue, s.chain, -1)
        for s in specs.values()
        if s.chain is not None
    }

    def weapon_want(s: _Spec, goal: int) -> dict[str, int]:
        assert s.chain is not None
        h = held[s.item_id]
        top = len(s.chain) - 1
        h.total += claim(s.chain[top], SELF, goal - h.total)
        for idx in range(top - 1, -1, -1):
            got = claim(s.chain[idx], SELF, goal - h.total)
            if got:
                h.lower[idx] = h.lower.get(idx, 0) + got
                h.total += got
        want: dict[str, int] = {}
        for idx, copies in h.lower.items():
            _add(want, _chain_cost(catalogue, s.chain, idx), copies)
        _add(want, full_cost[s.item_id], max(0, goal - h.total))
        return want

    def item_want(s: _Spec, goal: int) -> dict[str, int]:
        h = held[s.item_id]
        h.total += claim(s.item_id, SELF, goal - h.total)
        want: dict[str, int] = {}
        _add(want, s.recipe, max(0, goal - h.total))
        return want

    for tier in sorted({s.target.tier for s in specs.values()}):
        group = [s for s in specs.values() if s.target.tier == tier]
        for level in ("min", "ideal"):
            for s in group:
                goal = s.target.min if level == "min" else s.target.ideal
                want = (
                    weapon_want(s, goal) if s.chain is not None else item_want(s, goal)
                )
                for ing, units in want.items():
                    if units <= 0:
                        continue
                    by = demand.setdefault(ing, {})
                    by[s.item_id] = max(by.get(s.item_id, 0), units)
                    claim(ing, s.item_id, units - claims.get(ing, {}).get(s.item_id, 0))

    targets = {
        s.item_id: _state(s, held[s.item_id], owned, full_cost, catalogue)
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


def _state(
    s: _Spec,
    h: _Held,
    owned: dict[str, int],
    full_cost: dict[str, dict[str, int]],
    catalogue: Catalogue,
) -> TargetState:
    t = s.target
    if s.chain is None:
        return TargetState(
            s.item_id,
            owned.get(s.item_id, 0),
            t.min,
            t.ideal,
            t.tier,
            s.recipe,
            h.total,
        )
    exact = h.total - sum(h.lower.values())
    # next copy: upgrade the best held lower copy, else craft from scratch
    inputs = (
        _chain_cost(catalogue, s.chain, max(h.lower))
        if h.lower
        else full_cost[s.item_id]
    )
    return TargetState(
        s.item_id, h.total, t.min, t.ideal, t.tier, inputs, h.total, exact
    )
