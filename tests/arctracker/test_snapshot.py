import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from arc_helper.arctracker.snapshot import PlayerSnapshot
from arc_helper.arctracker.snapshot import format_age
from arc_helper.arctracker.snapshot import load_snapshot
from arc_helper.arctracker.snapshot import parse_inventory
from arc_helper.arctracker.snapshot import parse_needs
from arc_helper.arctracker.snapshot import parse_progress
from arc_helper.arctracker.snapshot import parse_tracking
from arc_helper.arctracker.snapshot import save_snapshot

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def inv_pages():
    return [_load("inventory_p1"), _load("inventory_p2")]


@pytest.fixture
def needs_pages():
    return [_load("needs_p1"), _load("needs_p2")]


def test_inventory_sums_stash_and_loadout_across_pages(inv_pages):
    owned, at = parse_inventory(inv_pages)
    assert owned["light_gun_parts"] == 21
    assert owned["metal_parts"] == 40
    assert owned["renegade_iv"] == 1
    assert owned["light_ammo"] == 158
    assert at == datetime(2026, 11, 7, 5, 23, 0, 117000, tzinfo=UTC)


def test_inventory_counts_loadout_once_and_handles_missing_fields():
    owned, at = parse_inventory([{"items": [{"id": "a", "count": 2}]}])
    assert owned == {"a": 2}
    assert at is None


def test_needs_flattened_in_order(needs_pages):
    needs = parse_needs(needs_pages)
    assert [(n.item_id, n.needed, n.kind) for n in needs] == [
        ("skulker_targeter", 1, "quest"),
        ("canister", 10, "workshop"),
        ("canister", 5, "project"),
    ]
    assert needs[0].label == "Tracks of the Beast"


def test_needs_skips_zero_needed():
    page = {
        "groups": [{"kind": "quest", "name": "Q", "items": [{"id": "x", "needed": 0}]}]
    }
    assert parse_needs([page]) == ()


def test_tracking_real_shape_keeps_only_keep_entries():
    # Real get_my_tracking entries: {"id", "shape", "keepAmount", ...}.
    assert parse_tracking(_load("tracking")) == {"oil": 20}


def test_tracking_is_defensive():
    data = {
        "entries": [
            {"shape": "keep", "item": {"id": "a"}, "amount": 3},
            {"shape": "keep", "itemId": "b", "target": 4},
            {"shape": "keep", "id": "c"},
            {"shape": "craft", "id": "d", "keepAmount": 9},
            "junk",
            {"shape": "keep", "id": "e", "keepAmount": "many"},
        ]
    }
    assert parse_tracking(data) == {"a": 3, "b": 4}
    assert parse_tracking({}) == {}
    assert parse_tracking({"entries": None}) == {}


def test_progress_marks_truncated_blueprints():
    learned, complete, benches = parse_progress(
        _load("progress_blueprints"), _load("progress_workshop")
    )
    assert "anvil_i" in learned
    assert complete is False
    assert benches["refiner"] == 3
    assert benches["weapon_bench"] == 3


def _snap():
    return PlayerSnapshot(
        synced_at=datetime(2026, 11, 7, 5, 23, 0, 117000, tzinfo=UTC),
        fetched_at=NOW,
        owned={"a": 1},
        needs=tuple(parse_needs([_load("needs_p1")])),
        keep_amounts={"oil": 20},
        learned_blueprints=frozenset({"anvil_i"}),
        blueprints_complete=False,
        bench_levels={"refiner": 3},
    )


def test_snapshot_roundtrip_and_corrupt_cache(tmp_path):
    path = tmp_path / "snap.json"
    s = _snap()
    save_snapshot(s, path)
    assert load_snapshot(path) == s
    path.write_text("{garbage", encoding="utf-8")
    assert load_snapshot(path) is None
    assert load_snapshot(tmp_path / "missing.json") is None
    path.write_text('{"owned": 3}', encoding="utf-8")
    assert load_snapshot(path) is None


def test_snapshot_roundtrip_none_synced_at(tmp_path):
    s = PlayerSnapshot(
        synced_at=None,
        fetched_at=NOW,
        owned={},
        needs=(),
        keep_amounts={},
        learned_blueprints=frozenset(),
        blueprints_complete=True,
        bench_levels={},
    )
    save_snapshot(s, tmp_path / "s.json")
    assert load_snapshot(tmp_path / "s.json") == s


@pytest.mark.parametrize(
    ("delta", "label", "stale"),
    [
        (timedelta(minutes=12), "⟳ 12m", False),
        (timedelta(hours=10), "⟳ 10h", True),
        (timedelta(days=3), "⟳ 3d", True),
        (timedelta(seconds=20), "⟳ 0m", False),
        (timedelta(hours=3), "⟳ 3h", False),
    ],
)
def test_format_age(delta, label, stale):
    assert format_age(NOW - delta, NOW) == (label, stale)


def test_format_age_none():
    assert format_age(None, NOW) == ("⟳ —", False)
