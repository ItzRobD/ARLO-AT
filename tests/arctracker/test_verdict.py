# ruff: noqa: RUF001 - expected popup copy uses the multiplication sign
import dataclasses
from datetime import UTC
from datetime import datetime
from itertools import starmap

import pytest

from arc_helper.arctracker.catalogue import Catalogue
from arc_helper.arctracker.planner import build_plan
from arc_helper.arctracker.rules import parse_rules
from arc_helper.arctracker.snapshot import Need
from arc_helper.arctracker.snapshot import PlayerSnapshot
from arc_helper.arctracker.verdict import CraftRow
from arc_helper.arctracker.verdict import VerdictKind
from arc_helper.arctracker.verdict import verdict_for

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
K = VerdictKind


@pytest.fixture
def plan(cat):
    """build_plan around a snapshot built from keyword arguments.

    needs: (item_id, needed, kind, label) tuples. snapshot=False plans without
    any personal data.
    """

    def _plan(
        *,
        owned=None,
        needs=(),
        rules="",
        keep=None,
        learned=(),
        complete=True,
        benches=None,
        snapshot=True,
        catalogue=None,
    ):
        catalogue = catalogue or cat
        r = parse_rules(rules, catalogue)
        assert r.errors == ()
        snap = (
            PlayerSnapshot(
                synced_at=NOW,
                fetched_at=NOW,
                owned=dict(owned or {}),
                needs=tuple(starmap(Need, needs)),
                keep_amounts=dict(keep or {}),
                learned_blueprints=frozenset(learned),
                blueprints_complete=complete,
                bench_levels=dict(benches or {}),
            )
            if snapshot
            else None
        )
        return build_plan(catalogue, snap, r)

    return _plan


LIGHT = "light_gun_parts: {min: 15, ideal: 50}"
GUN_PARTS = LIGHT + "\nheavy_gun_parts: {min: 15, ideal: 50}"


# --- Unknown ---------------------------------------------------------------


def test_unknown_id(plan):
    v = verdict_for(plan(owned={"brand_new_item": 3}), "brand_new_item")
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.UNKNOWN,
        "Unknown",
        "Not in ARC Tracker data",
        "",
    )


def test_unknown_owned_item_does_not_break_others(plan):  # Review Focus 1
    p = plan(owned={"brand_new_item": 3, "oil": 5})
    assert verdict_for(p, "brand_new_item").kind is K.UNKNOWN
    assert verdict_for(p, "oil").kind in {K.SELL, K.RECYCLE}


# --- Protected needs -------------------------------------------------------


def test_quest_protected_even_when_rule_says_recycle(plan):  # anti-goal
    v = verdict_for(
        plan(
            owned={"bombardier_cell": 2},
            needs=[("bombardier_cell", 3, "quest", "Taking Stock")],
            rules="bombardier_cell: {action: recycle}",
        ),
        "bombardier_cell",
    )
    assert v.kind is K.KEEP
    assert v.title == "Keep"
    assert v.reason == "Quest · Taking Stock ×3"
    assert v.counts == "Own 2 · need 3"


def test_need_owned_equal_to_total_is_kept(plan):
    v = verdict_for(
        plan(owned={"oil": 3}, needs=[("oil", 3, "workshop", "Refiner 2")]), "oil"
    )
    assert v.kind is K.KEEP
    assert v.reason == "Workshop · Refiner 2 ×3"


def test_need_kind_labels(plan):
    cases = {
        "quest": "Quest",
        "workshop": "Workshop",
        "outpost": "Outpost",
        "project": "Project",
        "goal": "Craft goal",
        "stock": "Keep amount",
        "research": "Research",
        "upgrade": "Upgrade",
        "build": "Build",
    }
    for kind, word in cases.items():
        v = verdict_for(plan(needs=[("oil", 2, kind, "X")]), "oil")
        assert v.reason == f"{word} · X ×2", kind


def test_need_reason_highest_priority_kind_first(plan):
    p = plan(
        owned={"oil": 1},
        needs=[
            ("oil", 2, "stock", "Stock"),
            ("oil", 4, "project", "Expedition"),
            ("oil", 1, "quest", "Greasy"),
            ("oil", 5, "quest", "Later quest"),
        ],
    )
    v = verdict_for(p, "oil")
    assert v.reason == "Quest · Greasy ×1"  # quest beats project; snapshot order
    assert v.counts == "Own 1 · need 12"


def test_need_surplus_above_total_is_sellable(plan):
    v = verdict_for(plan(owned={"oil": 5}, needs=[("oil", 3, "quest", "Q")]), "oil")
    assert v.kind is K.SELL
    assert v.title == "Sell 2"
    assert v.reason == "Surplus"


# --- Loadout ---------------------------------------------------------------


def test_loadout_met(plan):
    v = verdict_for(
        plan(
            owned={"medium_shield": 1},
            rules="loadout: {shields: {items: [medium_shield], copies: 1}}",
        ),
        "medium_shield",
    )
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.KEEP,
        "Keep",
        "Loadout · 1 copy",
        "Own 1 · keep 1",
    )


def test_loadout_low(plan):
    v = verdict_for(
        plan(rules="loadout: {weapons: {items: [renegade_iv], copies: 2}}"),
        "renegade_iv",
    )
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.KEEP_LOW,
        "Keep · low",
        "Loadout · 2 copies",
        "Own 0 · keep 2",
    )


def test_loadout_building_with_lower_tier_copy(plan):
    p = plan(
        owned={"renegade_iv": 1, "renegade_iii": 1},
        rules="loadout: {weapons: {items: [renegade_iv], copies: 2}}",
    )
    v = verdict_for(p, "renegade_iv")
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.KEEP_BUILDING,
        "Keep · building",
        "Loadout · 2 copies",
        "Own 1 · keep 2",
    )
    lower = verdict_for(p, "renegade_iii")
    assert (lower.kind, lower.title, lower.reason, lower.counts) == (
        K.KEEP_BUILDING,
        "Keep · building",
        "Loadout · upgrade to Renegade IV",
        "Own 1",
    )


def test_loadout_excess_copies_are_sellable(plan):
    v = verdict_for(
        plan(
            owned={"medium_shield": 3},
            rules="loadout: {shields: {items: [medium_shield], copies: 1}}",
        ),
        "medium_shield",
    )
    assert v.kind is K.SELL
    assert v.title == "Sell 2"
    assert v.reason == "Above reserve"
    assert v.counts == "Own 3 · ideal 1 · 2,000₡ ea"


# --- Reserve targets -------------------------------------------------------


def test_target_low_uses_note(plan):
    v = verdict_for(
        plan(
            owned={"light_gun_parts": 10},
            rules="light_gun_parts: {min: 15, ideal: 50, note: Craft buffer}",
        ),
        "light_gun_parts",
    )
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.KEEP_LOW,
        "Keep · low",
        "Craft buffer",
        "Own 10 · min 15 · ideal 50",
    )


def test_zero_owned_is_meaningful(plan):  # Review Focus 5
    v = verdict_for(plan(owned={}, rules=LIGHT), "light_gun_parts")
    assert v.kind is K.KEEP_LOW
    assert v.counts == "Own 0 · min 15 · ideal 50"


def test_target_building(plan):
    v = verdict_for(plan(owned={"light_gun_parts": 21}, rules=LIGHT), "light_gun_parts")
    assert (v.kind, v.title, v.reason) == (
        K.KEEP_BUILDING,
        "Keep · building",
        "Reserve target",
    )


def test_target_counts_when_min_equals_ideal(plan):
    v = verdict_for(plan(owned={"oil": 2}, rules="oil: {target: 8}"), "oil")
    assert v.kind is K.KEEP_LOW
    assert v.counts == "Own 2 · target 8"


def test_keep_amount_alone_is_a_target(plan):
    v = verdict_for(plan(owned={"oil": 2}, keep={"oil": 6}), "oil")
    assert v.kind is K.KEEP_LOW
    assert v.counts == "Own 2 · target 6"


def test_above_ideal_sells_only_the_excess(plan):
    v = verdict_for(plan(owned={"light_gun_parts": 60}, rules=LIGHT), "light_gun_parts")
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.SELL,
        "Sell 10",
        "Above reserve",
        "Own 60 · ideal 50 · 700₡ ea",
    )


# --- Ingredients -----------------------------------------------------------


def test_ingredient_crafts_rows(plan):  # mockup v4 case
    p = plan(
        owned={"simple_gun_parts": 40, "light_gun_parts": 21, "heavy_gun_parts": 8},
        rules=GUN_PARTS,
    )
    v = verdict_for(p, "simple_gun_parts")
    # fixture recipe 4 simple -> 1 light/heavy: heavy 50-8=42 short (168),
    # light 50-21=29 short (116); demand 284.
    assert v.title == "Keep all"
    assert v.kind is K.KEEP_LOW
    assert v.reason == "Crafting ingredient"
    assert v.counts == "Own 40 · crafts need 284"
    assert v.crafts == (
        CraftRow("Heavy Gun Parts ×42", "168", "low"),
        CraftRow("Light Gun Parts ×29", "116", "building"),
    )


def test_ingredient_partly_claimed_building(plan):
    p = plan(owned={"simple_gun_parts": 200, "light_gun_parts": 21}, rules=LIGHT)
    v = verdict_for(p, "simple_gun_parts")
    assert v.kind is K.KEEP_BUILDING
    assert v.title == "Keep 116"
    assert v.counts == "Own 200 · crafts need 116"


def test_crafts_rows_sorted_low_first_then_demand(plan):
    p = plan(
        owned={"simple_gun_parts": 4, "light_gun_parts": 40, "heavy_gun_parts": 10},
        rules=GUN_PARTS + "\nmedium_gun_parts: {min: 15, ideal: 60}",
    )
    v = verdict_for(p, "simple_gun_parts")
    # medium low (0<15, short 60 -> 240), heavy low (short 40 -> 160),
    # light building (short 10 -> 40)
    assert [r.label for r in v.crafts] == [
        "Medium Gun Parts ×60",
        "Heavy Gun Parts ×40",
        "Light Gun Parts ×10",
    ]
    assert [r.state for r in v.crafts] == ["low", "low", "building"]


def test_crafts_skip_met_targets(plan):
    p = plan(owned={"simple_gun_parts": 3, "light_gun_parts": 50}, rules=LIGHT)
    v = verdict_for(p, "simple_gun_parts")
    assert v.crafts == ()


# --- Policy rules ----------------------------------------------------------


def test_action_keep(plan):
    v = verdict_for(plan(owned={"oil": 9}, rules="oil: {action: keep}"), "oil")
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.KEEP,
        "Keep",
        "Rule · keep",
        "Own 9",
    )
    v = verdict_for(
        plan(owned={"oil": 9}, rules="oil: {action: keep, note: Retain}"), "oil"
    )
    assert v.reason == "Retain"


def test_key_keep_rarity(plan):
    v = verdict_for(
        plan(
            owned={"blue_gate_confiscation_room_key": 1},
            rules="keys: {keep_rarities: [Epic, Legendary]}",
        ),
        "blue_gate_confiscation_room_key",
    )
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.KEEP,
        "Keep",
        "Key · Epic",
        "Own 1",
    )


def test_key_other_rarity_use_or_sell(plan):
    v = verdict_for(
        plan(
            owned={"ancient_fort_security_code": 2},
            rules="keys: {keep_rarities: [Epic]}",
        ),
        "ancient_fort_security_code",
    )
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.SELL,
        "Sell 2",
        "Key · use or sell",
        "Own 2",
    )


# --- No rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    "item_id", ["advanced_camera_blueprint", "medium_shield", "anvil_i"]
)
def test_no_rule_classes(plan, cat, item_id):
    v = verdict_for(plan(owned={item_id: 2}), item_id)
    assert (v.kind, v.title, v.reason) == (
        K.NO_RULE,
        "No rule",
        "No rule for this item",
    )
    assert v.counts == f"Own 2 · {cat.items[item_id].value:,}₡ ea"


def test_rule_overrides_no_rule(plan):
    v = verdict_for(
        plan(owned={"anvil_i": 2}, rules="anvil_i: {action: sell}"), "anvil_i"
    )
    assert v.kind is K.SELL
    assert v.reason == "Rule · sell"


def test_gun_parts_are_not_no_rule(plan):  # weapons category, but not a Firearm
    v = verdict_for(plan(owned={"light_gun_parts": 2}), "light_gun_parts")
    assert v.kind is K.SELL


# --- Surplus ---------------------------------------------------------------


def test_sell_vs_recycle_by_value(plan, cat):
    # oil sells for 300; recycles into 3 chemicals at 50 = 150 -> sell
    v = verdict_for(plan(owned={"oil": 5}), "oil")
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.SELL,
        "Sell 5",
        "Surplus",
        "Own 5 · 300₡ ea",
    )
    cheap = dataclasses.replace(cat.items["oil"], value=100)
    cat2 = Catalogue(items={**cat.items, "oil": cheap})
    v = verdict_for(plan(owned={"oil": 5}, catalogue=cat2), "oil")
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.RECYCLE,
        "Recycle 5",
        "Surplus",
        "→ 3× Chemicals each",
    )


def test_rule_forces_recycle(plan):
    v = verdict_for(plan(owned={"oil": 5}, rules="oil: {action: recycle}"), "oil")
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.RECYCLE,
        "Recycle 5",
        "Rule · recycle",
        "→ 3× Chemicals each",
    )


def test_owned_zero_nothing_applies(plan):
    v = verdict_for(plan(), "oil")
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.SELL,
        "Sell",
        "Surplus",
        "Own 0 · 300₡ ea",
    )


def test_everything_claimed_uses_top_claimant_reason(plan):
    p = plan(
        owned={"oil": 8},
        needs=[("oil", 3, "quest", "Greasy")],
        rules="oil: {target: 5}",
    )
    v = verdict_for(p, "oil")
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.KEEP,
        "Keep",
        "Quest · Greasy ×3",
        "Own 8",
    )


def test_category_from_catalogue_and_rules_override(plan):
    assert verdict_for(plan(), "oil").category == "crafting"
    v = verdict_for(plan(rules="oil: {category: base}"), "oil")
    assert v.category == "base"


# --- Made from and hints ---------------------------------------------------

LEARNED = {"learned": {"light_gun_parts"}, "benches": {"refiner": 3}}


def test_made_from_hidden_when_met(plan):
    p = plan(
        owned={"light_gun_parts": 50, "simple_gun_parts": 24}, rules=LIGHT, **LEARNED
    )
    v = verdict_for(p, "light_gun_parts")
    assert v.made_from == ()
    assert v.hint is None


def test_made_from_and_hint(plan):
    p = plan(
        owned={"light_gun_parts": 21, "simple_gun_parts": 24}, rules=LIGHT, **LEARNED
    )
    v = verdict_for(p, "light_gun_parts")
    assert v.made_from == (
        CraftRow("Simple Gun Parts ×4", "own 24 · 24 spare", "partial"),
    )
    assert v.hint == "Craft 6 now → 27 / 50"
    assert v.hint_state == "partial"


def test_made_from_spare_excludes_other_claims(plan):
    # a quest holds 8 simple parts: they are owned but not spare for light
    p = plan(
        owned={"light_gun_parts": 21, "simple_gun_parts": 24},
        needs=[("simple_gun_parts", 8, "quest", "Q")],
        rules=LIGHT,
        **LEARNED,
    )
    v = verdict_for(p, "light_gun_parts")
    assert v.made_from[0].right == "own 24 · 16 spare"
    assert v.hint == "Craft 4 now → 25 / 50"


def test_hint_covers_when_shortfall_reachable(plan):
    p = plan(
        owned={"light_gun_parts": 48, "simple_gun_parts": 24}, rules=LIGHT, **LEARNED
    )
    v = verdict_for(p, "light_gun_parts")
    assert v.made_from[0].state == "covers"
    assert v.hint == "Craft 2 now → 50 / 50"
    assert v.hint_state == "covers"


def test_hint_farm_when_nothing_craftable(plan):
    p = plan(
        owned={"light_gun_parts": 21, "simple_gun_parts": 3}, rules=LIGHT, **LEARNED
    )
    v = verdict_for(p, "light_gun_parts")
    assert v.made_from[0].state == "none"
    assert v.hint == "Farm Simple Gun Parts"
    assert v.hint_state == "none"


def test_hint_farm_names_scarcest_ingredient(plan):
    p = plan(
        owned={"mechanical_components": 0, "metal_parts": 70, "rubber_parts": 1},
        rules="mechanical_components: {target: 5}",
        benches={"refiner": 3},
    )
    v = verdict_for(p, "mechanical_components")
    assert [r.label for r in v.made_from] == ["Metal Parts ×7", "rubber_parts ×3"]
    assert v.hint == "Farm rubber_parts"


def test_hint_bench_too_low(plan):
    p = plan(
        owned={"light_gun_parts": 21, "simple_gun_parts": 24},
        rules=LIGHT,
        learned={"light_gun_parts"},
        benches={"refiner": 1},
    )
    v = verdict_for(p, "light_gun_parts")
    assert v.hint == "Needs Refiner 2"
    assert v.made_from != ()


def test_hint_blueprint_not_learned(plan):
    p = plan(
        owned={"light_gun_parts": 21, "simple_gun_parts": 24},
        rules=LIGHT,
        benches={"refiner": 3},
    )
    assert verdict_for(p, "light_gun_parts").hint == "Blueprint not learned"


def test_hint_hidden_when_blueprint_unknown(plan):
    p = plan(
        owned={"light_gun_parts": 21, "simple_gun_parts": 24},
        rules=LIGHT,
        benches={"refiner": 3},
        complete=False,
    )
    v = verdict_for(p, "light_gun_parts")
    assert v.hint is None
    assert v.made_from != ()


def test_hint_hidden_without_progress_data(plan):
    p = plan(owned={"light_gun_parts": 21, "simple_gun_parts": 24}, rules=LIGHT)
    v = verdict_for(p, "light_gun_parts")
    assert v.hint is None
    assert v.made_from != ()


def test_no_snapshot_at_all(plan):
    p = plan(snapshot=False, rules=LIGHT)
    v = verdict_for(p, "light_gun_parts")
    assert v.kind is K.KEEP_LOW
    assert v.counts == "Own 0 · min 15 · ideal 50"
    assert v.hint is None
    assert verdict_for(p, "oil").kind is K.SELL


def test_weapon_made_from_uses_upgrade_cost(plan):
    p = plan(
        owned={
            "renegade_iii": 1,
            "advanced_mechanical_components": 5,
            "medium_gun_parts": 5,
        },
        rules="loadout: {weapons: {items: [renegade_iv], copies: 1}}",
        benches={"weapon_bench": 3},
    )
    v = verdict_for(p, "renegade_iv")
    assert [r.label for r in v.made_from] == [
        "Advanced Mechanical Components ×2",
        "Medium Gun Parts ×2",
    ]
    assert v.hint == "Craft 1 now → 1 / 1"
    assert v.hint_state == "covers"


@pytest.mark.parametrize("scale", [0, 1, 3, 20])
def test_every_catalogue_item_gets_a_safe_verdict(plan, cat, scale):
    p = plan(
        owned={i: (n % 7) * scale for n, i in enumerate(cat.items)},
        needs=[
            ("oil", 200, "quest", "Q"),
            ("arc_alloy", 200, "project", "P"),
            ("bombardier_cell", 1, "quest", "Q2"),
        ],
        rules=GUN_PARTS
        + "\nmedium_shield: {action: keep}"
        + "\nmechanical_components: {min: 4, ideal: 9}"
        + "\nloadout: {weapons: {items: [renegade_iv, anvil_i], copies: 2},"
        " shields: {items: [medium_shield], copies: 0}}"
        + "\nkeys: {keep_rarities: [Epic]}",
        keep={"smoke_grenade": 3, "not_in_catalogue": 2},
        learned={"light_gun_parts"},
        benches={"refiner": 2},
    )
    for item_id in [*cat.items, "not_in_catalogue"]:
        v = verdict_for(p, item_id)
        assert v.title, item_id
        assert isinstance(v.kind, VerdictKind), item_id
        if v.kind not in {K.SELL, K.RECYCLE}:
            continue
        # never sell or recycle a unit someone else holds
        n = v.title.split(" ", 1)[1] if " " in v.title else "0"
        assert int(n) <= p.unclaimed.get(item_id, 0), item_id
        unmet = [w for w in p.demand.get(item_id, {}) if p.targets[w].state != "met"]
        assert not unmet, item_id
        t = p.targets.get(item_id)
        assert t is None or t.state == "met", item_id
        if p.need_claims.get(item_id, 0) > 0:  # only the surplus above needs
            assert p.owned[item_id] > p.need_totals[item_id], item_id


def test_sweep_needs_owned_within_total_are_never_sold(plan, cat):
    owned = dict.fromkeys(cat.items, 3)
    p = plan(owned=owned, needs=[(i, 5, "quest", "Q") for i in cat.items])
    for item_id in cat.items:
        assert verdict_for(p, item_id).kind is K.KEEP, item_id


# --- Fix round 1 -----------------------------------------------------------


def test_zero_owned_ingredient_of_short_target(plan):
    v = verdict_for(plan(owned={}, rules=LIGHT), "simple_gun_parts")
    # light 0/15/50: short 50 -> 200 simple parts
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.KEEP_LOW,
        "Keep",
        "Crafting ingredient",
        "Own 0 · crafts need 200",
    )
    assert v.crafts == (CraftRow("Light Gun Parts ×50", "200", "low"),)


def test_zero_owned_blueprint_is_no_rule(plan):
    v = verdict_for(plan(), "advanced_camera_blueprint")
    assert (v.kind, v.title, v.counts) == (K.NO_RULE, "No rule", "Own 0 · 5,000₡ ea")


def test_zero_owned_lower_tier_weapon(plan):
    p = plan(rules="loadout: {weapons: {items: [renegade_iv], copies: 2}}")
    v = verdict_for(p, "renegade_iii")
    assert (v.kind, v.title, v.reason, v.counts) == (
        K.KEEP_BUILDING,
        "Keep · building",
        "Loadout · upgrade to Renegade IV",
        "Own 0",
    )


def test_extra_lower_tier_copy_is_no_rule(plan):
    p = plan(
        owned={"renegade_iii": 2},
        rules="loadout: {weapons: {items: [renegade_iv], copies: 1}}",
    )
    assert verdict_for(p, "renegade_iii").kind is K.NO_RULE


def test_copies_zero_loadout_is_not_a_keep(plan):
    rules = "loadout: {shields: {items: [medium_shield], copies: 0}}"
    v = verdict_for(plan(rules=rules), "medium_shield")
    assert (v.kind, v.reason) == (K.NO_RULE, "No rule for this item")
    v = verdict_for(plan(owned={"medium_shield": 2}, rules=rules), "medium_shield")
    assert v.kind is K.NO_RULE
    rules = "loadout: {heals: {items: [herbal_bandage], copies: 0}}"
    v = verdict_for(plan(rules=rules), "herbal_bandage")
    assert (v.kind, v.title, v.reason) == (K.SELL, "Sell", "Surplus")


def test_made_from_pip_per_ingredient(plan):
    # mechanical components: 7 metal + 3 rubber; target 5 -> shortfall 5
    p = plan(
        owned={"metal_parts": 70, "rubber_parts": 6},
        rules="mechanical_components: {target: 5}",
        benches={"refiner": 3},
    )
    v = verdict_for(p, "mechanical_components")
    assert [(r.label, r.state) for r in v.made_from] == [
        ("Metal Parts ×7", "covers"),  # 70 // 7 = 10 >= 5
        ("rubber_parts ×3", "partial"),  # 6 // 3 = 2
    ]
    assert (v.hint, v.hint_state) == ("Craft 2 now → 2 / 5", "partial")


def test_weapon_craft_now_capped_by_lower_copies(plan):
    p = plan(
        owned={
            "renegade_iii": 1,
            "advanced_mechanical_components": 20,
            "medium_gun_parts": 20,
        },
        rules="loadout: {weapons: {items: [renegade_iv], copies: 2}}",
        benches={"weapon_bench": 3},
    )
    v = verdict_for(p, "renegade_iv")  # shortfall 2, one copy to upgrade
    assert (v.hint, v.hint_state) == ("Craft 1 now → 1 / 2", "partial")


AMMO = "light_ammo: {target: 60}"


def test_craft_yield_hint_and_pips(plan):
    # shortfall 50 at 25 per craft -> 2 crafts; metal covers only 1
    p = plan(
        owned={"light_ammo": 10, "chemicals": 10, "metal_parts": 3},
        rules=AMMO,
        benches={"workbench": 1},
    )
    v = verdict_for(p, "light_ammo")
    assert [(r.label, r.state) for r in v.made_from] == [
        ("Chemicals ×2", "covers"),  # 10 // 2 = 5 >= 2 crafts
        ("Metal Parts ×3", "partial"),  # 3 // 3 = 1 < 2 crafts
    ]
    assert (v.hint, v.hint_state) == ("Craft 1 now → 35 / 60", "partial")
    p = plan(
        owned={"light_ammo": 10, "chemicals": 10, "metal_parts": 6},
        rules=AMMO,
        benches={"workbench": 1},
    )
    v = verdict_for(p, "light_ammo")
    assert all(r.state == "covers" for r in v.made_from)
    assert (v.hint, v.hint_state) == ("Craft 2 now → 60 / 60", "covers")
