"""Tests for whole-frame tooltip detection and popup placement."""

import numpy as np

from arc_helper.overlay import place_beside
from arc_helper.tooltip_finder import find_tooltip

CREAM = (249, 238, 223)


def _frame(w=3440, h=1440):
    return np.full((h, w, 3), (20, 22, 30), dtype=np.uint8)


def _tooltip(frame, left, top, right, bottom):
    frame[top:bottom, left:right] = CREAM
    # Dark text lines and a coloured tag row, like the real tooltip
    frame[top + 20 : top + 56, left + 30 : left + 400] = (80, 200, 60)
    for y in range(top + 80, bottom - 20, 40):
        frame[y : y + 14, left + 30 : right - 40] = (40, 40, 40)
    return frame


def test_finds_tooltip_body():
    frame = _tooltip(_frame(), 1212, 636, 1716, 1116)
    left, top, right, bottom = find_tooltip(frame)
    assert abs(left - 1212) <= 4
    assert abs(top - 636) <= 4
    assert abs(right - 1716) <= 4
    assert abs(bottom - 1116) <= 4


def test_no_tooltip_returns_none():
    assert find_tooltip(_frame()) is None


def test_small_cream_fragment_ignored():
    frame = _frame()
    frame[100:140, 100:200] = CREAM
    assert find_tooltip(frame) is None


def test_works_at_1080p():
    frame = _tooltip(_frame(1920, 1080), 700, 400, 1080, 760)
    assert find_tooltip(frame) is not None


def test_popup_goes_right_of_tooltip():
    assert place_beside((1212, 636, 1716, 1116), (400, 200), (3440, 1440)) == (1732, 636)


def test_popup_falls_back_to_left_at_screen_edge():
    x, _ = place_beside((3000, 600, 3400, 1000), (400, 200), (3440, 1440))
    assert x == 3000 - 16 - 400


def test_popup_clamped_vertically():
    _, y = place_beside((1212, 1300, 1716, 1430), (400, 300), (3440, 1440))
    assert y == 1440 - 300
