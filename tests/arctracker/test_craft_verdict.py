"""Crafting-screen verdict: crafts still needed to reach the ideal target."""

import pytest

from arc_helper.arctracker.verdict import VerdictKind
from arc_helper.arctracker.verdict import craft_verdict_for

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
