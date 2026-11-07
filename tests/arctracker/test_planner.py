from datetime import UTC
from datetime import datetime

import pytest

from arc_helper.arctracker.catalogue import Catalogue
from arc_helper.arctracker.planner import NEED
from arc_helper.arctracker.planner import SELF
from arc_helper.arctracker.planner import TargetState
from arc_helper.arctracker.planner import build_plan
from arc_helper.arctracker.planner import effective_target
from arc_helper.arctracker.rules import Target
from arc_helper.arctracker.rules import parse_rules
from arc_helper.arctracker.snapshot import Need
from arc_helper.arctracker.snapshot import PlayerSnapshot

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def snap(owned=None, needs=(), keep=None):
    return PlayerSnapshot(
        synced_at=NOW,
        fetched_at=NOW,
        owned=dict(owned or {}),
        needs=tuple(Need(i, n, k, f"{k} {i}") for i, n, k in needs),
        keep_amounts=dict(keep or {}),
        learned_blueprints=frozenset(),
        blueprints_complete=True,
        bench_levels={},
    )


@pytest.fixture
def rules(cat):
    def _rules(text):
        r = parse_rules(text, cat)
        assert r.errors == ()
        return r

    return _rules


@pytest.fixture
def plan(cat):
    """build_plan that also asserts the claim invariants on every plan."""

    def _plan(snapshot, rules_, catalogue=None):
        p = build_plan(catalogue or cat, snapshot, rules_)
        check_invariants(p)
        return p

    return _plan


def check_invariants(p):
    for item, by in p.claims.items():
        assert all(v > 0 for v in by.values()), (item, by)
        assert sum(by.values()) <= p.owned.get(item, 0), (item, by)
    for item, have in p.owned.items():
        claimed = sum(p.claims.get(item, {}).values())
        assert p.unclaimed[item] >= 0
        assert claimed + p.unclaimed[item] == max(have, 0), item
    for item, by in p.demand.items():
        assert all(v > 0 for v in by.values()), (item, by)
    for t in p.targets.values():
        assert t.shortfall >= 0
        assert t.held >= 0
        if t.exact is None:  # non-weapon: held comes from its own __self__ units
            assert t.held <= p.claims.get(t.item_id, {}).get(SELF, 0)
        else:
            assert t.exact <= t.held == t.owned


def renegade_full_cost(cat):
    total: dict[str, int] = dict(cat.items["renegade_i"].recipe)
    for tier in ("renegade_ii", "renegade_iii", "renegade_iv"):
        for k, v in cat.items[tier].upgrade_cost.items():
            total[k] = total.get(k, 0) + v
    return total


# --- brief tests ------------------------------------------------------------


def test_need_claims_first(plan, rules):
    p = plan(
        snap(
            owned={"canister": 10},
            needs=[("canister", 10, "workshop"), ("canister", 5, "project")],
        ),
        rules(""),
    )
    assert p.need_claims["canister"] == 10
    assert p.need_totals["canister"] == 15
    assert p.unclaimed["canister"] == 0
    assert p.claims["canister"] == {NEED: 10}
    assert p.need_reasons["canister"].kind == "workshop"


def test_ingredient_not_double_counted(plan, rules):
    p = plan(
        snap(
            owned={"simple_gun_parts": 40, "light_gun_parts": 21, "heavy_gun_parts": 8}
        ),
        rules(
            "light_gun_parts: {min: 15, ideal: 50}\n"
            "heavy_gun_parts: {min: 15, ideal: 50}"
        ),
    )
    assert sum(p.claims["simple_gun_parts"].values()) == 40
    assert p.claims["simple_gun_parts"]["heavy_gun_parts"] == 28  # min pass: (15-8)*4
    assert p.claims["simple_gun_parts"]["light_gun_parts"] == 12  # ideal pass leftover
    assert p.demand["simple_gun_parts"] == {
        "light_gun_parts": 116,  # (50-21)*4
        "heavy_gun_parts": 168,  # (50-8)*4
    }
    assert p.claims["light_gun_parts"] == {SELF: 21}
    assert p.claims["heavy_gun_parts"] == {SELF: 8}
    assert p.unclaimed["simple_gun_parts"] == 0


def test_higher_tier_wins(plan, rules, cat):
    p = plan(
        snap(owned={"medium_gun_parts": 3}),
        rules(
            "medium_gun_parts: {min: 10, ideal: 50}\n"
            "loadout:\n  weapons: {items: [renegade_iv], copies: 1}"
        ),
    )
    full = renegade_full_cost(cat)
    assert p.targets["renegade_iv"].craft_inputs == full
    assert p.claims["medium_gun_parts"] == {"renegade_iv": 3}
    assert p.demand["medium_gun_parts"]["renegade_iv"] == full["medium_gun_parts"]
    assert p.targets["medium_gun_parts"].state == "low"
    assert p.unclaimed["medium_gun_parts"] == 0


def test_lower_weapon_tier_counts_as_copy(plan, rules, cat):
    p = plan(
        snap(
            owned={
                "renegade_iii": 1,
                "advanced_mechanical_components": 5,
                "medium_gun_parts": 5,
            }
        ),
        rules("loadout:\n  weapons: {items: [renegade_iv], copies: 1}"),
    )
    t = p.targets["renegade_iv"]
    assert (t.owned, t.held, t.exact, t.tier) == (1, 1, 0, 2)
    # Brief said renegade_iii.upgrade_cost, but in the catalogue a tier's
    # upgrade_cost is the cost to *reach* it, so III -> IV is renegade_iv's.
    up4 = cat.items["renegade_iv"].upgrade_cost
    assert up4 == {"advanced_mechanical_components": 2, "medium_gun_parts": 2}
    assert t.craft_inputs == up4
    assert p.claims["renegade_iii"] == {SELF: 1}
    assert p.unclaimed["renegade_iii"] == 0
    # held but not met: the III -> IV upgrade is demanded and claimed
    for ing, q in up4.items():
        assert p.demand[ing] == {"renegade_iv": q}
        assert p.claims[ing] == {"renegade_iv": q}
    assert t.state == "building"
    assert t.shortfall == 1


def test_lower_copy_plus_missing_copy(plan, rules, cat):
    p = plan(
        snap(owned={"renegade_iii": 1}),
        rules("loadout:\n  weapons: {items: [renegade_iv], copies: 2}"),
    )
    t = p.targets["renegade_iv"]
    up4, full = cat.items["renegade_iv"].upgrade_cost, renegade_full_cost(cat)
    expected = {k: up4.get(k, 0) + full.get(k, 0) for k in up4 | full}
    assert expected == {
        "advanced_mechanical_components": 8,
        "medium_gun_parts": 11,
        "oil": 5,
    }
    assert {ing: by["renegade_iv"] for ing, by in p.demand.items()} == expected
    assert (t.held, t.exact, t.state, t.shortfall) == (1, 0, "low", 2)
    assert t.craft_inputs == up4  # next copy: upgrade the held III


def test_overlapping_chains_do_not_double_count(plan, rules):
    p = plan(
        snap(owned={"renegade_ii": 1}),
        rules("loadout:\n  weapons: {items: [renegade_ii, renegade_iv], copies: 1}"),
    )
    assert p.claims["renegade_ii"] == {SELF: 1}
    ii, iv = p.targets["renegade_ii"], p.targets["renegade_iv"]
    assert (ii.owned, ii.state) == (1, "met")
    assert (iv.owned, iv.held, iv.state) == (0, 0, "low")


def test_keep_amount_raises_target(plan, rules):
    r = rules("oil: {min: 5, ideal: 10}")
    s = snap(owned={"oil": 25}, keep={"oil": 20})
    assert effective_target(r, s, "oil") == Target(20, 20, 5)
    p = plan(s, r)
    assert p.targets["oil"].min == 20
    assert p.targets["oil"].ideal == 20
    assert p.claims["oil"] == {SELF: 20}
    assert p.unclaimed["oil"] == 5


def test_no_snapshot_yields_empty_plan(plan, rules):
    p = plan(None, rules("oil: {target: 5}"))
    assert p.owned == {}
    assert p.targets["oil"].owned == 0
    assert p.unclaimed == {}
    assert p.need_claims == {}


# --- effective_target -------------------------------------------------------


def test_effective_target_variants(rules):
    r = rules("oil: {min: 5, ideal: 10, tier: 4}\ncanister: {target: 3}")
    s = snap(keep={"oil": 8, "canister": 1, "wires": 7})
    assert effective_target(r, s, "oil") == Target(8, 10, 4)  # only min raised
    assert effective_target(r, s, "canister") == Target(3, 3, 5)  # keep lower
    assert effective_target(r, s, "wires") == Target(7, 7, 5)  # keep alone
    assert effective_target(r, s, "chemicals") is None
    assert effective_target(r, None, "oil") == Target(5, 10, 4)
    assert effective_target(r, None, "wires") is None


def test_keep_only_target_is_tier_5(plan, rules):
    p = plan(snap(owned={"wires": 3}, keep={"wires": 7}), rules(""))
    t = p.targets["wires"]
    assert (t.min, t.ideal, t.tier, t.owned, t.held) == (7, 7, 5, 3, 3)
    assert t.state == "low"
    assert t.shortfall == 4
    assert p.claims["wires"] == {SELF: 3}


# --- TargetState ------------------------------------------------------------


@pytest.mark.parametrize(
    ("held", "state", "short"),
    [(0, "low", 10), (4, "low", 6), (5, "building", 5), (10, "met", 0), (12, "met", 0)],
)
def test_target_state_uses_held_not_owned(held, state, short):
    t = TargetState("x", 99, 5, 10, 5, {}, held)
    assert t.state == state
    assert t.shortfall == short


@pytest.mark.parametrize(
    ("held", "exact", "state", "short"),
    [
        (0, 0, "low", 2),
        (1, 1, "low", 1),
        (2, 0, "building", 2),
        (2, 1, "building", 1),
        (2, 2, "met", 0),
    ],
)
def test_weapon_target_state(held, exact, state, short):
    t = TargetState("w", held, 2, 2, 2, {}, held, exact)
    assert t.state == state
    assert t.shortfall == short


# --- needs ------------------------------------------------------------------


def test_needs_beat_targets_and_ingredient_claims(plan, rules):
    p = plan(
        snap(
            owned={"simple_gun_parts": 10, "light_gun_parts": 2},
            needs=[("simple_gun_parts", 6, "quest")],
        ),
        rules("light_gun_parts: {target: 5}"),
    )
    assert p.claims["simple_gun_parts"] == {NEED: 6, "light_gun_parts": 4}
    assert p.demand["simple_gun_parts"] == {"light_gun_parts": 12}
    assert p.unclaimed["simple_gun_parts"] == 0


def test_need_units_are_not_counted_for_target(plan, rules):
    # oil needed 4 by a project and targeted 5: the target only gets what is left.
    p = plan(
        snap(owned={"oil": 6}, needs=[("oil", 4, "project")]), rules("oil: {target: 5}")
    )
    assert p.claims["oil"] == {NEED: 4, SELF: 2}
    assert p.unclaimed["oil"] == 0
    t = p.targets["oil"]
    assert (t.owned, t.held, t.state, t.shortfall) == (6, 2, "low", 3)
    p = plan(
        snap(owned={"oil": 6}, needs=[("oil", 4, "project")]),
        rules("oil: {min: 2, ideal: 5}"),
    )
    t = p.targets["oil"]
    assert (t.held, t.state, t.shortfall) == (2, "building", 3)


def test_need_for_unowned_item(plan, rules):
    p = plan(snap(needs=[("arc_alloy", 3, "workshop")]), rules(""))
    assert p.need_claims["arc_alloy"] == 0
    assert p.need_totals["arc_alloy"] == 3
    assert "arc_alloy" not in p.claims
    assert p.need_reasons["arc_alloy"].needed == 3


# --- loadout ----------------------------------------------------------------


def test_zero_copies_creates_target_but_protects_nothing(plan, rules):
    p = plan(
        snap(owned={"renegade_iv": 1, "medium_gun_parts": 4}),
        rules("loadout:\n  weapons: {items: [renegade_iv], copies: 0}"),
    )
    t = p.targets["renegade_iv"]
    assert (t.min, t.ideal, t.owned, t.held, t.state) == (0, 0, 0, 0, "met")
    assert p.claims == {}
    assert p.unclaimed == {"renegade_iv": 1, "medium_gun_parts": 4}


def test_loadout_beats_rules_target_for_same_item(plan, rules):
    p = plan(
        snap(owned={"medium_shield": 3}),
        rules(
            "medium_shield: {target: 1, tier: 6}\n"
            "loadout:\n  shields: {items: [medium_shield], copies: 2}"
        ),
    )
    t = p.targets["medium_shield"]
    assert (t.min, t.ideal, t.tier) == (2, 2, 3)
    assert len(p.targets) == 1
    assert p.claims["medium_shield"] == {SELF: 2}


def test_non_weapon_loadout_uses_recipe(plan, rules, cat):
    p = plan(
        snap(owned={"battery": 3}),
        rules("loadout:\n  shields: {items: [medium_shield], copies: 2}"),
    )
    t = p.targets["medium_shield"]
    assert t.craft_inputs == cat.items["medium_shield"].recipe
    assert p.demand["battery"] == {"medium_shield": 8}
    assert p.claims["battery"] == {"medium_shield": 3}


def test_weapon_copies_prefer_highest_tier_and_count_all_tiers(plan, rules, cat):
    p = plan(
        snap(owned={"renegade_i": 2, "renegade_ii": 1, "renegade_iv": 1}),
        rules("loadout:\n  weapons: {items: [renegade_iv], copies: 3}"),
    )
    t = p.targets["renegade_iv"]
    assert (t.owned, t.held, t.exact, t.state, t.shortfall) == (3, 3, 1, "building", 2)
    assert p.claims["renegade_iv"] == {SELF: 1}
    assert p.claims["renegade_ii"] == {SELF: 1}
    assert p.claims["renegade_i"] == {SELF: 1}
    assert p.unclaimed["renegade_i"] == 1
    up2, up3, up4 = (
        cat.items[f"renegade_{n}"].upgrade_cost for n in ("ii", "iii", "iv")
    )
    # next copy: best held lower copy is II, so upgrades to III and IV
    assert t.craft_inputs == {k: up3.get(k, 0) + up4.get(k, 0) for k in up3 | up4}
    # demand: II->IV for the held II plus I->IV for the held I
    want: dict[str, int] = {}
    for cost in (up3, up4, up2, up3, up4):
        for k, v in cost.items():
            want[k] = want.get(k, 0) + v
    assert {ing: by["renegade_iv"] for ing, by in p.demand.items()} == want


def test_weapon_owned_at_target_tier_needs_full_craft_for_more_copies(plan, rules, cat):
    p = plan(
        snap(owned={"renegade_iv": 1, "oil": 20}),
        rules("loadout:\n  weapons: {items: [renegade_iv], copies: 2}"),
    )
    t = p.targets["renegade_iv"]
    assert t.craft_inputs == renegade_full_cost(cat)
    assert p.claims["oil"] == {"renegade_iv": 5}
    assert p.unclaimed["oil"] == 15
    assert (t.held, t.exact, t.state, t.shortfall) == (1, 1, "low", 1)


def test_weapon_shortfall_demands_per_copy(plan, rules, cat):
    p = plan(
        snap(),
        rules("loadout:\n  weapons: {items: [renegade_iv], copies: 2}"),
    )
    full = renegade_full_cost(cat)
    assert p.demand["oil"] == {"renegade_iv": 2 * full["oil"]}


def test_unresolvable_chain_has_no_craft_inputs(plan):
    broken = Catalogue.from_api_json(
        {
            "items": [
                {"id": "gun_i", "recipe": {"oil": 2}, "upgradesTo": "gun_ii"},
                {"id": "gun_ii", "upgradesTo": "gun_iii"},  # no upgradeCost
                {"id": "gun_iii", "upgradeCost": {"oil": 1}},
                {"id": "oil", "stackSize": 15},
                {"id": "lone_gun"},  # tier I, no recipe
            ]
        }
    )
    r = parse_rules(
        "loadout:\n  weapons: {items: [gun_iii, lone_gun], copies: 1}", broken
    )
    p = plan(snap(owned={"oil": 10}), r, broken)
    assert p.targets["gun_iii"].craft_inputs == {}
    assert p.targets["lone_gun"].craft_inputs == {}
    assert p.claims == {}
    # owning gun_ii means only II -> III is left, which is resolvable
    p = plan(snap(owned={"oil": 10, "gun_ii": 1}), r, broken)
    assert p.targets["gun_iii"].craft_inputs == {"oil": 1}
    assert p.claims["oil"] == {"gun_iii": 1}


def test_tiers_and_min_before_ideal_ordering(plan, rules):
    # Same tier, shared ingredient: both mins are served before either ideal,
    # so the second target gets 8 instead of the first taking all 12 at ideal.
    p = plan(
        snap(owned={"simple_gun_parts": 12}),
        rules(
            "light_gun_parts: {min: 1, ideal: 5}\nheavy_gun_parts: {min: 2, ideal: 5}"
        ),
    )
    assert p.claims["simple_gun_parts"] == {"light_gun_parts": 4, "heavy_gun_parts": 8}
    assert p.demand["simple_gun_parts"] == {
        "light_gun_parts": 20,
        "heavy_gun_parts": 20,
    }
    # leftovers after both mins go to the ideal pass in file order
    p = plan(
        snap(owned={"simple_gun_parts": 20}),
        rules(
            "light_gun_parts: {min: 1, ideal: 5}\nheavy_gun_parts: {min: 2, ideal: 5}"
        ),
    )
    assert p.claims["simple_gun_parts"] == {"light_gun_parts": 12, "heavy_gun_parts": 8}
    p = plan(
        snap(owned={"simple_gun_parts": 8}),
        rules(
            "light_gun_parts: {min: 1, ideal: 5, tier: 6}\n"
            "heavy_gun_parts: {min: 1, ideal: 5, tier: 4}"
        ),
    )
    # tier 4 heavy takes min then ideal (all 8) before tier-6 light sees any
    assert p.claims["simple_gun_parts"] == {"heavy_gun_parts": 8}
    assert p.demand["simple_gun_parts"] == {
        "heavy_gun_parts": 20,
        "light_gun_parts": 20,
    }


def test_negative_owned_counts_are_treated_as_zero(plan, rules):
    p = plan(snap(owned={"oil": -3}), rules("oil: {target: 5}"))
    assert p.unclaimed["oil"] == 0
    assert p.targets["oil"].owned == 0
