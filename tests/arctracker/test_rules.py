import os

from arc_helper.arctracker.rules import LoadoutGroup
from arc_helper.arctracker.rules import RulesWatcher
from arc_helper.arctracker.rules import Target
from arc_helper.arctracker.rules import parse_rules


def test_units_conversion(cat):
    r = parse_rules(
        "light_gun_parts: {min: 15, ideal: 50}\nsmoke_grenade: {rows: 1}\noil: {target: 3}",
        cat,
    )
    assert r.targets["light_gun_parts"] == Target(15, 50, 5)
    assert r.targets["oil"] == Target(3, 3, 5)
    assert r.targets["smoke_grenade"].ideal == 4 * cat.items["smoke_grenade"].stack_size


def test_min_or_ideal_alone_sets_both(cat):
    r = parse_rules("oil: {min: 4}\ncanister: {ideal: 9, tier: 4}", cat)
    assert r.targets["oil"] == Target(4, 4, 5)
    assert r.targets["canister"] == Target(9, 9, 4)


def test_range_stacks(cat):
    r = parse_rules("canister: {stacks: [1, 2]}", cat)
    s = cat.items["canister"].stack_size
    assert r.targets["canister"] == Target(s, 2 * s, 5)


def test_rows_range(cat):
    r = parse_rules("canister: {rows: [1, 2]}", cat)
    s = cat.items["canister"].stack_size
    assert r.targets["canister"] == Target(4 * s, 8 * s, 5)


def test_unknown_id_and_bad_field_collected_not_fatal(cat):
    r = parse_rules(
        "not_an_item: {target: 5}\ncanister: {rows: banana}\noil: {target: 20}", cat
    )
    assert "oil" in r.targets
    assert "canister" not in r.targets
    assert len(r.errors) == 2


def test_order_preserved(cat):
    r = parse_rules(
        "oil: {target: 1}\ncanister: {target: 2}\nsmoke_grenade: {target: 3}", cat
    )
    assert list(r.targets) == ["oil", "canister", "smoke_grenade"]


def test_invalid_tier_skips_entry(cat):
    r = parse_rules("oil: {target: 1, tier: 9}\ncanister: {target: 2}", cat)
    assert "oil" not in r.targets
    assert "canister" in r.targets
    assert len(r.errors) == 1


def test_unknown_action_and_category_skip_entry(cat):
    r = parse_rules("oil: {action: burn}\ncanister: {category: junk}", cat)
    assert not r.actions
    assert not r.categories
    assert len(r.errors) == 2


def test_action_only_entry_has_no_target(cat):
    r = parse_rules("oil: {action: sell, note: too many, category: base}", cat)
    assert "oil" not in r.targets
    assert r.actions["oil"] == "sell"
    assert r.notes["oil"] == "too many"
    assert r.categories["oil"] == "base"
    assert r.errors == ()


def test_stack_size_unknown_is_error(cat):
    import dataclasses

    from arc_helper.arctracker.catalogue import Catalogue

    items = dict(cat.items)
    items["oil"] = dataclasses.replace(items["oil"], stack_size=None)
    c2 = Catalogue(items=items)
    r = parse_rules("oil: {rows: 1}\ncanister: {target: 1}", c2)
    assert "oil" not in r.targets
    assert any("stack size unknown" in e for e in r.errors)


def test_actions_loadout_keys(cat):
    r = parse_rules(
        "bombardier_cell: {action: recycle}\n"
        "loadout:\n  weapons: {items: [renegade_iv], copies: 1}\n"
        "  shields: {items: [medium_shield], copies: 0}\n"
        "keys: {keep_rarities: [Rare, Epic]}",
        cat,
    )
    assert r.actions["bombardier_cell"] == "recycle"
    assert r.loadout[0] == LoadoutGroup("weapons", ("renegade_iv",), 1, 2)
    assert r.loadout[1].tier == 3
    assert r.keep_key_rarities == {"Rare", "Epic"}


def test_loadout_unknown_item_dropped_with_error(cat):
    r = parse_rules("loadout:\n  weapons: {items: [nope, renegade_iv], copies: 2}", cat)
    assert r.loadout[0].items == ("renegade_iv",)
    assert len(r.errors) == 1


def test_empty_and_non_mapping(cat):
    assert parse_rules("", cat).targets == {}
    assert len(parse_rules("- a\n- b", cat).errors) == 1


def _bump(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def test_watcher_keeps_previous_on_invalid_yaml(tmp_path, cat):
    p = tmp_path / "rules.yaml"
    p.write_text("oil: {target: 3}\n")
    w = RulesWatcher(p)
    assert w.current(cat).targets["oil"].ideal == 3
    p.write_text("a: [")
    _bump(p)
    assert w.current(cat).targets["oil"].ideal == 3


def test_watcher_reloads_on_change(tmp_path, cat):
    p = tmp_path / "rules.yaml"
    p.write_text("oil: {target: 3}\n")
    w = RulesWatcher(p)
    assert w.current(cat).targets["oil"].ideal == 3
    p.write_text("oil: {target: 7}\n")
    _bump(p)
    assert w.current(cat).targets["oil"].ideal == 7


def test_watcher_missing_file_returns_none(tmp_path, cat):
    assert RulesWatcher(tmp_path / "nope.yaml").current(cat) is None
