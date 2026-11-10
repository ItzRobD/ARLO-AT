# ruff: noqa: RUF001 - expected popup copy uses the multiplication sign
"""Crafting-screen verdict: crafts still needed to reach the ideal target."""

import pytest

from arc_helper.arctracker.verdict import CraftRow
from arc_helper.arctracker.verdict import VerdictKind
from arc_helper.arctracker.verdict import buy_verdict_for
from arc_helper.arctracker.verdict import craft_verdict_for
from arc_helper.arctracker.verdict import verdict_for

from .test_verdict import plan  # noqa: F401 - pytest fixture

K = VerdictKind


def _v(p, item_id):
    v = craft_verdict_for(p, item_id)
    return v.kind, v.title, v.reason, v.counts


def test_crafts_to_ideal(plan):  # noqa: F811
    p = plan(
        owned={"heavy_gun_parts": 20}, rules="heavy_gun_parts: {min: 15, ideal: 50}"
    )
    assert _v(p, "heavy_gun_parts") == (
        K.KEEP_BUILDING,
        "Craft 30",
        "Reserve target",
        "Own 20 · ideal 50",
    )


def test_below_min_is_low(plan):  # noqa: F811
    p = plan(
        owned={"heavy_gun_parts": 3}, rules="heavy_gun_parts: {min: 15, ideal: 50}"
    )
    kind, title, _, _ = _v(p, "heavy_gun_parts")
    assert (kind, title) == (K.KEEP_LOW, "Craft 47")


def test_target_met(plan):  # noqa: F811
    p = plan(
        owned={"heavy_gun_parts": 60}, rules="heavy_gun_parts: {min: 15, ideal: 50}"
    )
    assert _v(p, "heavy_gun_parts")[:2] == (K.KEEP, "Target met")


def test_no_target(plan):  # noqa: F811
    p = plan(owned={"herbal_bandage": 4})
    assert _v(p, "herbal_bandage") == (
        K.NO_RULE,
        "No target",
        "No target for this item",
        "Own 4",
    )


def test_counts_crafts_not_units(plan):  # noqa: F811
    # light_ammo yields 25 per craft: 60 short -> 3 crafts
    p = plan(owned={"light_ammo": 40}, rules="light_ammo: {target: 100}")
    assert _v(p, "light_ammo") == (
        K.KEEP_LOW,
        "Craft 3",
        "Reserve target",
        "Own 40 · ideal 100 · ×25 per craft",
    )


def test_need_adds_to_target_and_gives_reason(plan):  # noqa: F811
    p = plan(
        owned={"heavy_gun_parts": 10},
        needs=[("heavy_gun_parts", 5, "quest", "Gunsmith")],
        rules="heavy_gun_parts: {min: 15, ideal: 20}",
    )
    kind, title, reason, counts = _v(p, "heavy_gun_parts")
    assert title == "Craft 15"  # 5 for the quest + 20 ideal - 10 owned
    assert reason.startswith("Quest")
    assert counts == "Own 10 · ideal 25"


def test_need_only(plan):  # noqa: F811
    p = plan(needs=[("herbal_bandage", 3, "project", "Clinic")])
    assert _v(p, "herbal_bandage")[1] == "Craft 3"


def test_loadout_weapon_counts_copies(plan):  # noqa: F811
    p = plan(
        owned={"renegade_iv": 1},
        rules="loadout: {weapons: {items: [renegade_iv], copies: 3}}",
    )
    assert _v(p, "renegade_iv") == (
        K.KEEP_LOW,  # a loadout's min is its copies
        "Craft 2",
        "Loadout · 3 copies",
        "Own 1 · ideal 3",
    )


def test_unknown_item(plan):  # noqa: F811
    assert craft_verdict_for(plan(), "not_an_item").kind is K.UNKNOWN


@pytest.mark.parametrize("item_id", ["heavy_gun_parts", "light_ammo", "renegade_iv"])
def test_without_snapshot(plan, item_id):  # noqa: F811
    v = craft_verdict_for(plan(snapshot=False), item_id)
    assert v.title == "No target"


# --- Ingredients other targets still need, same as the stash ----------------

LIGHT_SHORT = "light_gun_parts: {target: 50, tier: 4}"


def test_counts_what_other_targets_still_need(plan):  # noqa: F811
    # 2 Light Gun Parts short -> 8 Simple Gun Parts, on top of the reserve of 20
    p = plan(
        owned={"light_gun_parts": 48, "simple_gun_parts": 21},
        rules=LIGHT_SHORT + "\nsimple_gun_parts: {target: 20}",
    )
    v = craft_verdict_for(p, "simple_gun_parts")
    assert v.title == "Craft 7"
    assert v.counts == "Own 21 · ideal 28"
    assert v.crafts == (CraftRow("Light Gun Parts ×2", "8", "low"),)


def test_low_matches_the_stash_when_reserve_is_claimed(plan):  # noqa: F811
    p = plan(
        owned={"light_gun_parts": 48, "simple_gun_parts": 21},
        rules=LIGHT_SHORT + "\nsimple_gun_parts: {target: 20}",
    )
    assert craft_verdict_for(p, "simple_gun_parts").kind is K.KEEP_LOW
    assert verdict_for(p, "simple_gun_parts").kind is K.KEEP_LOW


def test_ingredient_without_its_own_target(plan):  # noqa: F811
    p = plan(owned={"light_gun_parts": 48, "simple_gun_parts": 3}, rules=LIGHT_SHORT)
    v = craft_verdict_for(p, "simple_gun_parts")
    assert (v.title, v.reason) == ("Craft 5", "Crafting ingredient")


def test_ingredient_of_a_building_target_is_not_low(plan):  # noqa: F811
    p = plan(
        owned={"light_gun_parts": 20, "simple_gun_parts": 3},
        rules="light_gun_parts: {min: 15, ideal: 21}",
    )
    assert craft_verdict_for(p, "simple_gun_parts").kind is K.KEEP_BUILDING


def test_ingredient_already_covered(plan):  # noqa: F811
    p = plan(owned={"light_gun_parts": 48, "simple_gun_parts": 8}, rules=LIGHT_SHORT)
    assert craft_verdict_for(p, "simple_gun_parts").title == "Target met"


def test_met_target_reserves_nothing(plan):  # noqa: F811
    p = plan(
        owned={"light_gun_parts": 50, "simple_gun_parts": 21},
        rules=LIGHT_SHORT + "\nsimple_gun_parts: {target: 20}",
    )
    v = craft_verdict_for(p, "simple_gun_parts")
    assert (v.title, v.crafts) == ("Target met", ())


def test_item_claimed_by_nothing_stays_no_target(plan):  # noqa: F811
    p = plan(owned={"light_gun_parts": 50, "simple_gun_parts": 21}, rules=LIGHT_SHORT)
    assert craft_verdict_for(p, "simple_gun_parts").title == "No target"


# --- Blueprint status -------------------------------------------------------

SHORT = {"owned": {"light_gun_parts": 21}, "rules": LIGHT_SHORT}


def test_blueprint_status_unknown_when_list_is_cut_off(plan):  # noqa: F811
    p = plan(benches={"refiner": 3}, complete=False, **SHORT)
    v = craft_verdict_for(p, "light_gun_parts")
    assert (v.title, v.hint) == ("Craft 29", "BP status unknown")


def test_blueprint_learned_has_no_marker(plan):  # noqa: F811
    p = plan(learned={"light_gun_parts"}, complete=False, **SHORT)
    assert craft_verdict_for(p, "light_gun_parts").hint is None


def test_blueprint_not_learned_when_list_is_complete(plan):  # noqa: F811
    p = plan(benches={"refiner": 3}, **SHORT)
    assert craft_verdict_for(p, "light_gun_parts").hint == "Blueprint not learned"


def test_no_blueprint_marker_when_nothing_to_craft(plan):  # noqa: F811
    p = plan(
        owned={"light_gun_parts": 50},
        rules=LIGHT_SHORT,
        benches={"refiner": 3},
        complete=False,
    )
    assert craft_verdict_for(p, "light_gun_parts").hint is None


# --- Trader screen ----------------------------------------------------------


def test_buy_counts_units_not_crafts(plan):  # noqa: F811
    p = plan(owned={"light_ammo": 40}, rules="light_ammo: {target: 100}")
    v = buy_verdict_for(p, "light_ammo")
    assert (v.kind, v.title, v.counts) == (K.KEEP_LOW, "Buy 60", "Own 40 · ideal 100")


def test_buy_has_no_blueprint_hint(plan):  # noqa: F811
    p = plan(benches={"refiner": 3}, complete=False, **SHORT)
    v = buy_verdict_for(p, "light_gun_parts")
    assert (v.title, v.hint) == ("Buy 29", None)


def test_buy_same_target_as_crafting(plan):  # noqa: F811
    p = plan(
        owned={"light_gun_parts": 48, "simple_gun_parts": 21},
        rules=LIGHT_SHORT + "\nsimple_gun_parts: {target: 20}",
    )
    buy = buy_verdict_for(p, "simple_gun_parts")
    craft = craft_verdict_for(p, "simple_gun_parts")
    assert buy.title == "Buy 7"
    assert (buy.reason, buy.counts, buy.crafts) == (
        craft.reason,
        craft.counts,
        craft.crafts,
    )


@pytest.mark.parametrize(
    ("owned", "title"), [({"light_gun_parts": 50}, "Target met"), ({}, "No target")]
)
def test_buy_met_and_no_target(plan, owned, title):  # noqa: F811
    rules = LIGHT_SHORT if owned else ""
    assert (
        buy_verdict_for(plan(owned=owned, rules=rules), "light_gun_parts").title
        == title
    )
