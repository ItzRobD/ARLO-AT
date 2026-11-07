"""Validate rules.example.yaml (and a local rules.yaml, when present) against the live catalogue."""

from pathlib import Path

import pytest

from arc_helper.arctracker.catalogue import fetch_catalogue
from arc_helper.arctracker.rules import parse_rules

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def live_catalogue():
    catalogue = fetch_catalogue(ROOT / "cache", max_age_s=10**9)
    if catalogue is None:
        pytest.skip("no cached catalogue and offline")
    return catalogue


def test_example_rules_parse_cleanly(live_catalogue):
    text = (ROOT / "rules.example.yaml").read_text(encoding="utf-8")
    assert parse_rules(text, live_catalogue).errors == ()


def test_owner_rules_parse_cleanly(live_catalogue):
    path = ROOT / "rules.yaml"
    if not path.exists():
        pytest.skip("no local rules.yaml")
    rules = parse_rules(path.read_text(encoding="utf-8"), live_catalogue)
    assert rules.errors == ()
