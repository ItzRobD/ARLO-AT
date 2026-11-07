import json
import os
import time
from pathlib import Path

import pytest
import requests

from arc_helper.arctracker.catalogue import Catalogue
from arc_helper.arctracker.catalogue import fetch_catalogue

FIXTURE = Path(__file__).parent / "fixtures" / "catalogue_subset.json"


@pytest.fixture(scope="module")
def raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cat(raw):
    return Catalogue.from_api_json(raw)


def test_parses_recipe_and_stack(cat):
    lgp = cat.items["light_gun_parts"]
    assert lgp.recipe == {"simple_gun_parts": 4}
    assert lgp.stack_size == 5
    assert lgp.value == 700
    assert lgp.benches == ("refiner",)
    assert lgp.station_level == 2
    assert lgp.blueprint_locked is True
    assert lgp.recycles_into == {"simple_gun_parts": 2}


def test_parses_upgrades(cat):
    assert cat.items["renegade_i"].upgrades_to == "renegade_ii"
    assert cat.items["renegade_ii"].upgrade_cost == {
        "advanced_mechanical_components": 1,
        "medium_gun_parts": 2,
    }


def test_bench_list_normalised(cat):  # craftBench may be str or list
    assert all(isinstance(i.benches, tuple) for i in cat.items.values())
    assert cat.items["herbal_bandage"].benches == ("in_raid", "med_station")


def test_resolve_exact_ocr_forms(cat):
    assert cat.resolve("CANISTER") == "canister"
    assert cat.resolve("LIGHTGUNPARTS") == "light_gun_parts"
    assert cat.resolve("RENEGADEIV") == "renegade_iv"


def test_resolve_never_crosses_tier_or_blueprint(cat):
    assert cat.resolve("RENEGADEIII") == "renegade_iii"
    assert cat.resolve("RENEGADEVI") is None  # no tier VI
    assert cat.resolve("ANVILI") == "anvil_i"


def test_resolve_fuzzy_typo(cat):
    assert cat.resolve("CANISTFR") == "canister"
    assert cat.resolve("XQZWV") is None


def test_weapon_chain(cat):
    assert cat.weapon_chain("renegade_iv") == [
        "renegade_i",
        "renegade_ii",
        "renegade_iii",
        "renegade_iv",
    ]


def test_produced_by(cat):
    assert set(cat.produced_by("simple_gun_parts")) >= {
        "light_gun_parts",
        "medium_gun_parts",
        "heavy_gun_parts",
    }


def test_categories(cat):
    assert cat.items["light_gun_parts"].category == "weapons"
    assert cat.items["renegade_iv"].category == "weapons"
    assert cat.items["light_ammo"].category == "weapons"
    assert cat.items["herbal_bandage"].category == "medical"
    assert cat.items["smoke_grenade"].category == "consumables"
    assert cat.items["metal_parts"].category == "base"
    assert cat.items["canister"].category == "crafting"
    assert cat.items["bombardier_cell"].category == "arc_rare"
    assert cat.items["arc_alloy"].category == "arc_rare"


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _Session:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.calls = data, error, 0

    def get(self, _url, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return _Resp(self.data)


def test_fetch_uses_fresh_cache_and_falls_back(tmp_path, raw):
    # nothing cached + HTTP error -> None
    bad = _Session(error=requests.ConnectionError("down"))
    assert fetch_catalogue(tmp_path, session=bad) is None

    # download writes the cache
    ok = _Session(data=raw)
    c = fetch_catalogue(tmp_path, session=ok)
    assert c is not None
    assert "canister" in c.items
    assert ok.calls == 1
    assert (tmp_path / "catalogue.json").exists()

    # fresh cache: no HTTP call made
    never = _Session(error=AssertionError("must not fetch"))
    assert fetch_catalogue(tmp_path, session=never).version == c.version
    assert never.calls == 0

    # stale cache + HTTP error -> stale returned
    old = time.time() - 10 * 86400
    os.utime(tmp_path / "catalogue.json", (old, old))
    stale = fetch_catalogue(tmp_path, session=bad)
    assert stale is not None
    assert "canister" in stale.items


def test_is_weapon_from_firearm_classification(cat):
    assert cat.items["renegade_iv"].is_weapon is True
    assert cat.items["anvil_i"].is_weapon is True
    assert cat.items["light_gun_parts"].is_weapon is False  # weapons category only
    assert cat.items["light_ammo"].is_weapon is False


def test_craft_quantity_parsed(cat):
    assert cat.items["light_ammo"].craft_quantity == 25
    assert cat.items["light_gun_parts"].craft_quantity == 1  # no craftQuantity


def test_empty_download_keeps_old_cache(tmp_path, raw):
    assert fetch_catalogue(tmp_path, session=_Session(data=raw)) is not None
    path = tmp_path / "catalogue.json"
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))
    before = path.read_bytes()
    empty = _Session(data={"items": [], "version": "x"})
    got = fetch_catalogue(tmp_path, session=empty)
    assert empty.calls == 1
    assert got is not None
    assert "canister" in got.items  # the stale cache, not the empty download
    assert path.read_bytes() == before
    assert path.stat().st_mtime == pytest.approx(old)


def test_empty_download_without_cache_is_none(tmp_path):
    assert fetch_catalogue(tmp_path, session=_Session(data={"items": []})) is None
    assert not (tmp_path / "catalogue.json").exists()


def test_fresh_cache_reuses_current_catalogue(tmp_path, raw, monkeypatch):
    first = fetch_catalogue(tmp_path, session=_Session(data=raw))
    mtime = (tmp_path / "catalogue.json").stat().st_mtime_ns
    parses = []
    orig = Catalogue.from_api_json
    monkeypatch.setattr(
        Catalogue,
        "from_api_json",
        classmethod(lambda _cls, d: parses.append(1) or orig(d)),
    )
    never = _Session(error=AssertionError("must not fetch"))
    got = fetch_catalogue(tmp_path, session=never, current=(first, mtime))
    assert got is first
    assert parses == []
    # a cache rewritten since `current` was read is parsed again
    got = fetch_catalogue(tmp_path, session=never, current=(first, mtime - 1))
    assert got is not first
    assert parses == [1]
