"""Live check against the real ARC Tracker MCP server (needs ARCTRACKER_API_KEY)."""

import os

import pytest

from arc_helper.arctracker.mcp_client import ArcTrackerClient
from arc_helper.arctracker.snapshot import parse_inventory

pytestmark = pytest.mark.skipif(
    not os.environ.get("ARCTRACKER_API_KEY"), reason="ARCTRACKER_API_KEY not set"
)


def test_fetch_raw_live():
    url = os.environ.get("ARCTRACKER_MCP_URL", "https://mcp.arctracker.io/mcp")
    raw = ArcTrackerClient(os.environ["ARCTRACKER_API_KEY"], url).fetch_raw()
    assert "readFromGame" in raw["inventory"][0]
    owned, _ = parse_inventory(raw["inventory"])
    assert owned
