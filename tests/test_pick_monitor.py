"""Tests for choosing the X11 monitor that matches the portal stream."""

from arc_helper.screen.base import MonitorGeometry
from arc_helper.screen.base import compute_scale
from arc_helper.screen.base import pick_monitor

# Three-monitor KDE layout, XWayland in physical pixels.
DP3 = MonitorGeometry(name="DP-3", x=0, y=0, width=1440, height=2560)
DP2 = MonitorGeometry(name="DP-2", x=1440, y=298, width=3440, height=1440, primary=True)
DP1 = MonitorGeometry(name="DP-1", x=4880, y=298, width=2560, height=1440)
LAYOUT = [DP3, DP2, DP1]


def test_matches_portal_connector_name():
    assert pick_monitor(LAYOUT, "DP-1", (2560, 1440)) == DP1


def test_falls_back_to_unique_size_match():
    assert pick_monitor(LAYOUT, None, (3440, 1440)) == DP2


def test_falls_back_to_primary_when_size_ambiguous():
    assert pick_monitor(LAYOUT, "HDMI-9", (5000, 2000)) == DP2


def test_single_monitor_without_metadata():
    only = MonitorGeometry(name="X", x=0, y=0, width=1920, height=1080)
    assert pick_monitor([only], None, (3840, 2160)) == only


def test_no_monitors():
    assert pick_monitor([], "DP-2", (3440, 1440)) is None


def test_scale_is_per_monitor_not_whole_desktop():
    # Regression: dividing by the 7440px desktop width gave 0.4624.
    assert compute_scale(3440, pick_monitor(LAYOUT, "DP-2", (3440, 1440)).width) == 1.0
