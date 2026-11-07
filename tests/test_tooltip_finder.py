"""Tests for whole-frame tooltip detection and popup placement."""

import numpy as np

from arc_helper.overlay import place_beside
from arc_helper.tooltip_finder import find_tooltip
from arc_helper.tooltip_finder import find_tooltips

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


def test_full_width_grey_box_does_not_split_tooltip():
    # Regression: Crude Explosives / Skulker Targeter tooltips have a grey
    # description box and stat rows spanning nearly the full width, which
    # split the cream run so only the lower half (no name) was found.
    frame = _tooltip(_frame(), 1072, 424, 1576, 1116)
    frame[600:680, 1072 + 24 : 1576 - 24] = (215, 210, 200)  # grey box
    frame[700:760, 1072 + 24 : 1576 - 24] = (60, 60, 60)  # stats rows
    left, top, right, bottom = find_tooltip(frame)
    assert abs(top - 424) <= 4
    assert abs(bottom - 1116) <= 4


def test_cream_outside_tooltip_columns_does_not_move_top():
    # Regression: pale "100/100" durability text beside the tooltip, just
    # above its top, used to lift those rows over ROW_FRAC. ARLO's own popup
    # covering/uncovering that text flipped the top 476<->492 every frame.
    clean = _tooltip(_frame(), 1212, 492, 1716, 1000)
    noisy = clean.copy()
    noisy[468:484, 1740:1880] = CREAM
    noisy[468:484, 1960:2100] = CREAM
    assert find_tooltip(noisy) == find_tooltip(clean)
    assert abs(find_tooltip(noisy)[1] - 492) <= 4


def test_finds_two_panels_left_to_right():
    # Crafting screen: hovered blueprint tooltip plus the selected panel
    frame = _tooltip(_frame(), 2316, 220, 2936, 1000)
    frame = _tooltip(frame, 1288, 296, 1792, 776)
    found = find_tooltips(frame)
    assert len(found) == 2
    for got, want in zip(found, [(1288, 296, 1792), (2316, 220, 2936)], strict=True):
        assert all(abs(g - w) <= 4 for g, w in zip(got[:3], want, strict=True))


def test_find_tooltips_single_and_none():
    assert find_tooltips(_frame()) == []
    frame = _tooltip(_frame(), 1212, 636, 1716, 1116)
    assert find_tooltips(frame) == [find_tooltip(frame)]
