"""Popup verdict for one hovered item, read from the allocation ``Plan``.

Decision order (first match wins): unknown id, protected ARC need, loadout,
own reserve target, crafting ingredient, ``action: keep``, keys, item classes
with no rule, surplus (sell / recycle / consume), nothing owned, everything
claimed.
Only ``Plan.unclaimed`` units are ever offered for sale or recycling, so an
item a need, the loadout or a reserve holds is never sold.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from operator import itemgetter
from typing import TYPE_CHECKING

from arc_helper.arctracker.planner import NEED
from arc_helper.arctracker.planner import SELF

if TYPE_CHECKING:
    from arc_helper.arctracker.catalogue import CatalogueItem
    from arc_helper.arctracker.planner import Plan
    from arc_helper.arctracker.planner import TargetState
    from arc_helper.arctracker.snapshot import Need

LOADOUT_MAX_TIER = 3  # planner tiers 2 (weapons) and 3 (other loadout groups)
NO_RULE_TYPES = frozenset({"Blueprint", "Modification", "Augment", "Shield"})
MAX_CRAFT_ROWS = 3
TIMES = "\N{MULTIPLICATION SIGN}"  # the popup copy uses U+00D7
IN_RAID = "in_raid"  # a craftBench entry that is no workshop module
ON_DEMAND = "Loadout · craft on demand"  # owned item of a copies: 0 group
CONSUME_REASON = "Research · hand in"

NEED_KINDS = {
    "quest": "Quest",
    "workshop": "Workshop",
    "outpost": "Outpost",
    "project": "Project",
    "goal": "Craft goal",
    "stock": "Keep amount",
    "keep_amount": "Keep amount",
    "research": "Research",
    "upgrade": "Upgrade",
    "build": "Build",
}
# Highest priority first; unlisted kinds rank after these.
NEED_PRIORITY = (
    "quest",
    "project",
    "research",
    "workshop",
    "outpost",
    "upgrade",
    "build",
    "goal",
    "stock",
)

BENCH_NAMES = {
    "refiner": "Refiner",
    "weapon_bench": "Gunsmith",
    "equipment_bench": "Gear Bench",
    "utility_bench": "Utility Station",
    "explosives_bench": "Explosives Station",
    "med_station": "Medical Lab",
    "workbench": "Workbench",
}


class VerdictKind(Enum):
    KEEP = "keep"
    KEEP_BUILDING = "keep_building"
    KEEP_LOW = "keep_low"
    SELL = "sell"
    RECYCLE = "recycle"
    CONSUME = "consume"  # research item: hand in at the Research Station
    NO_RULE = "no_rule"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CraftRow:
    label: str
    right: str
    state: str  # low | building | met | covers | partial | none


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    title: str
    reason: str
    counts: str
    category: str
    crafts: tuple[CraftRow, ...] = ()
    made_from: tuple[CraftRow, ...] = ()
    hint: str | None = None
    hint_state: str | None = None


_STATE_KIND = {
    "met": (VerdictKind.KEEP, "Keep"),
    "building": (VerdictKind.KEEP_BUILDING, "Keep · building"),
    "low": (VerdictKind.KEEP_LOW, "Keep · low"),
}


def _name(plan: Plan, item_id: str) -> str:
    item = plan.catalogue.items.get(item_id)
    return item.name if item is not None else item_id


def _top_need(plan: Plan, item_id: str) -> Need | None:
    needs = [
        n
        for n in (plan.snapshot.needs if plan.snapshot else ())
        if n.item_id == item_id
    ]
    if not needs:
        return plan.need_reasons.get(item_id)

    def rank(n: Need) -> int:
        k = n.kind.lower()
        return NEED_PRIORITY.index(k) if k in NEED_PRIORITY else len(NEED_PRIORITY)

    return min(needs, key=rank)  # min keeps the first of equal rank


def _need_reason(need: Need) -> str:
    kind = NEED_KINDS.get(need.kind.lower(), need.kind.replace("_", " ").capitalize())
    return f"{kind} · {need.label} {TIMES}{need.needed}"


def _target_counts(owned: int, t: TargetState) -> str:
    if t.min == t.ideal:
        return f"Own {owned} · target {t.ideal}"
    return f"Own {owned} · min {t.min} · ideal {t.ideal}"


def _is_loadout(t: TargetState) -> bool:
    return t.tier <= LOADOUT_MAX_TIER


def _copies(n: int) -> str:
    return f"{n} cop{'y' if n == 1 else 'ies'}"


def _weapon_for_lower(plan: Plan, item_id: str) -> str | None:
    """The loadout weapon target whose chain holds this lower-tier copy."""
    for tid, t in plan.targets.items():
        if t.exact is None or tid == item_id or tid not in plan.catalogue.items:
            continue
        if item_id in plan.catalogue.weapon_chain(tid):
            return tid
    return None


def _unmet_demand(plan: Plan, item_id: str) -> dict[str, int]:
    """Demand for this ingredient from targets that are not met."""
    return {
        tid: units
        for tid, units in plan.demand.get(item_id, {}).items()
        if tid in plan.targets and plan.targets[tid].state != "met"
    }


def _crafts(plan: Plan, item_id: str) -> tuple[CraftRow, ...]:
    rows: list[tuple[int, int, CraftRow]] = []
    for tid, units in _unmet_demand(plan, item_id).items():
        t = plan.targets[tid]
        row = CraftRow(f"{_name(plan, tid)} {TIMES}{t.shortfall}", str(units), t.state)
        rows.append((0 if t.state == "low" else 1, -units, row))
    rows.sort(key=itemgetter(0, 1))  # stable: demand order on ties
    return tuple(r[2] for r in rows[:MAX_CRAFT_ROWS])


def _spare_for(plan: Plan, ingredient: str, target_id: str) -> int:
    return plan.claims.get(ingredient, {}).get(target_id, 0) + plan.unclaimed.get(
        ingredient, 0
    )


def _hint(
    plan: Plan,
    item: CatalogueItem,
    t: TargetState,
    craft_now: int,
    scarcest: str,
) -> tuple[str | None, str | None]:
    snap = plan.snapshot
    if snap is None or not (snap.bench_levels or snap.learned_blueprints):
        return None, None  # no progress data
    if item.station_level is not None:
        benches = [b for b in item.benches if b != IN_RAID]
        known = [b for b in benches if b in snap.bench_levels]
        if benches and not known:
            return None, None  # craft bench level unknown: no data, no hint
        if known and all(snap.bench_levels[b] < item.station_level for b in known):
            bench = BENCH_NAMES.get(known[0], known[0])
            return f"Needs {bench} {item.station_level}", None
    if item.blueprint_locked and item.id not in snap.learned_blueprints:
        if snap.blueprints_complete:
            return "Blueprint not learned", None
        return None, None
    if craft_now > 0:
        have = t.exact if t.exact is not None else t.held
        state = "covers" if craft_now >= t.crafts_needed else "partial"
        made = have + craft_now * t.craft_quantity
        return f"Craft {craft_now} now → {made} / {t.ideal}", state
    return f"Farm {_name(plan, scarcest)}", "none"


def _cover_state(crafts: int, shortfall: int) -> str:
    if crafts >= shortfall:
        return "covers"
    return "partial" if crafts > 0 else "none"


def _upgradable_copies(plan: Plan, item_id: str, t: TargetState) -> int:
    """For a weapon whose next copy is an upgrade, the held copies at the best
    lower tier (craft_inputs is that tier's upgrade cost); else no cap."""
    if t.exact is None or t.held <= t.exact or item_id not in plan.catalogue.items:
        return t.shortfall
    chain = plan.catalogue.weapon_chain(item_id)[:-1]
    for lower in reversed(chain):
        copies = plan.claims.get(lower, {}).get(SELF, 0)
        if copies > 0:
            return copies
    return t.shortfall


def _made_from(
    plan: Plan, item_id: str
) -> tuple[tuple[CraftRow, ...], str | None, str | None]:
    t = plan.targets.get(item_id)
    item = plan.catalogue.items.get(item_id)
    if t is None or item is None or t.state == "met" or not t.craft_inputs:
        return (), None, None
    inputs = {k: q for k, q in t.craft_inputs.items() if q > 0}
    if not inputs:
        return (), None, None
    spare = {k: _spare_for(plan, k, item_id) for k in inputs}
    cap = _upgradable_copies(plan, item_id, t)
    crafts = t.crafts_needed
    craft_now = min(crafts, cap, *(spare[k] // q for k, q in inputs.items()))
    rows = tuple(
        CraftRow(
            f"{_name(plan, k)} {TIMES}{q}",
            f"own {plan.owned.get(k, 0)} · {spare[k]} spare",
            _cover_state(spare[k] // q, crafts),
        )
        for k, q in inputs.items()
    )
    scarcest = min(inputs, key=lambda k: (spare[k] // inputs[k], spare[k]))
    hint, hint_state = _hint(plan, item, t, craft_now, scarcest)
    return rows, hint, hint_state


def _recycle_value(plan: Plan, item: CatalogueItem) -> int:
    items = plan.catalogue.items
    return sum(q * items[k].value for k, q in item.recycles_into.items() if k in items)


def unknown_verdict(category: str = "") -> Verdict:
    """Grey verdict for an item ARC Tracker data does not know."""
    return Verdict(
        VerdictKind.UNKNOWN, "Unknown", "Not in ARC Tracker data", "", category
    )


def verdict_for(plan: Plan, item_id: str) -> Verdict:
    item = plan.catalogue.items.get(item_id)
    if item is None:
        return unknown_verdict(plan.rules.categories.get(item_id, ""))
    kind, title, reason, counts = _decide(plan, item)
    made_from, hint, hint_state = _made_from(plan, item_id)
    return Verdict(
        kind,
        title,
        reason,
        counts,
        plan.rules.categories.get(item_id, item.category),
        crafts=_crafts(plan, item_id),
        made_from=made_from,
        hint=hint,
        hint_state=hint_state,
    )


def _decide(plan: Plan, item: CatalogueItem) -> tuple[VerdictKind, str, str, str]:
    item_id = item.id
    rules = plan.rules
    owned = plan.owned.get(item_id, 0)
    spare = plan.unclaimed.get(item_id, 0)
    claims = plan.claims.get(item_id, {})
    t = plan.targets.get(item_id)
    action = rules.actions.get(item_id)
    note = rules.notes.get(item_id)
    value = f"{item.value:,}₡ ea"

    # 1. Protected ARC Tracker need.
    need_total = plan.need_totals.get(item_id, 0)
    need = _top_need(plan, item_id) if need_total else None
    if need is not None and owned <= need_total:
        return (
            VerdictKind.KEEP,
            "Keep",
            _need_reason(need),
            f"Own {owned} · need {need_total}",
        )

    # 2. Loadout item (copies > 0), and lower-tier copies of a loadout weapon.
    ruled = t is not None and t.ideal > 0  # a copies: 0 loadout is no target
    # owned, and in a copies: 0 loadout group (crafted only when needed)
    on_demand = owned > 0 and t is not None and t.ideal == 0 and _is_loadout(t)
    if ruled and t is not None and _is_loadout(t) and owned <= t.ideal:
        k, title = _STATE_KIND[t.state]
        return (
            k,
            title,
            f"Loadout · {_copies(t.ideal)}",
            f"Own {owned} · keep {t.ideal}",
        )
    weapon = _weapon_for_lower(plan, item_id) if t is None else None
    if weapon is not None and spare == 0:
        held = claims.get(SELF, 0) > 0
        if held or (owned == 0 and plan.targets[weapon].state != "met"):
            return (
                VerdictKind.KEEP_BUILDING,
                "Keep · building",
                f"Loadout · upgrade to {_name(plan, weapon)}",
                f"Own {owned}",
            )

    # 3. Own reserve target below ideal.
    if t is not None and t.state != "met":
        k, title = _STATE_KIND[t.state]
        return k, title, note or "Reserve target", _target_counts(owned, t)

    # 4. Ingredient of unmet targets (also when none is owned).
    unmet = _unmet_demand(plan, item_id)
    if unmet:
        low = any(plan.targets[w].state == "low" for w in unmet)
        claimed = sum(claims.values())
        if owned == 0:
            title = "Keep"
        elif claimed >= owned:
            title = "Keep all"
        else:
            title = f"Keep {claimed}"
        return (
            VerdictKind.KEEP_LOW if low else VerdictKind.KEEP_BUILDING,
            title,
            "Crafting ingredient",
            f"Own {owned} · crafts need {sum(unmet.values())}",
        )

    # 5. Policy rules and keys.
    if action == "keep":
        return VerdictKind.KEEP, "Keep", note or "Rule · keep", f"Own {owned}"
    if item.type == "Key":
        if item.rarity in rules.keep_key_rarities:
            return VerdictKind.KEEP, "Keep", f"Key · {item.rarity}", f"Own {owned}"
        if spare > 0 or owned == 0:
            title = f"Sell {spare}" if spare > 0 else "Sell"
            return VerdictKind.SELL, title, "Key · use or sell", f"Own {owned}"

    # 6. Classes ARC Tracker never computes surplus for: never Sell by default.
    no_rule_class = item.type in NO_RULE_TYPES or item.is_weapon
    if no_rule_class and action is None and not ruled and (spare > 0 or owned == 0):
        return (
            VerdictKind.NO_RULE,
            "No rule",
            ON_DEMAND if on_demand else "No rule for this item",
            f"Own {owned} · {value}",
        )

    # 7. Surplus. `action: consume` (research items) replaces Sell / Recycle.
    if action == "consume" and (spare > 0 or owned == 0):
        title = f"Consume {spare}" if spare > 0 else "Consume"
        return VerdictKind.CONSUME, title, note or CONSUME_REASON, f"Own {owned}"
    if spare > 0:
        if action == "recycle":
            recycle = True
        elif action == "sell":
            recycle = False
        else:
            recycle = (
                bool(item.recycles_into) and _recycle_value(plan, item) > item.value
            )
        if ruled:
            why = "Above reserve"
        elif on_demand:
            why = ON_DEMAND
        elif action is not None:
            why = f"Rule · {action.replace('_', ' ')}"
        else:
            why = "Surplus"
        if ruled and t is not None:
            counts = f"Own {owned} · ideal {t.ideal} · {value}"
        elif recycle and item.recycles_into:
            outputs = ", ".join(
                f"{q}{TIMES} {_name(plan, k)}" for k, q in item.recycles_into.items()
            )
            counts = f"→ {outputs} each"
        else:
            counts = f"Own {owned} · {value}"
        if recycle:
            return VerdictKind.RECYCLE, f"Recycle {spare}", why, counts
        return VerdictKind.SELL, f"Sell {spare}", why, counts

    if owned == 0:  # nothing protects it and nothing is owned
        return VerdictKind.SELL, "Sell", "Surplus", f"Own 0 · {value}"

    # 8. Everything claimed: the top claimant's reason.
    if claims.get(NEED, 0) > 0 and need is not None:
        why = _need_reason(need)
    elif ruled and t is not None and _is_loadout(t):
        why = f"Loadout · {_copies(t.ideal)}"
    elif on_demand:
        why = ON_DEMAND
    elif t is not None:
        why = note or "Reserve target"
    else:
        why = (
            f"Loadout · upgrade to {_name(plan, weapon)}"
            if weapon is not None
            else note or "Reserve target"
        )
    return VerdictKind.KEEP, "Keep", why, f"Own {owned}"
