# ruff: noqa: RUF001, FBT003, SLF001
# RUF001: popup copy uses the multiplication sign. FBT003: show_verdict's
# positional ``stale`` mirrors the scanner message. SLF001: the wiring tests
# drive private scanner/app paths directly.
"""Personal-verdict popup rendering and its wiring into the scanner."""

import queue
import tkinter as tk
from types import SimpleNamespace

import pytest

from arc_helper import main as main_mod
from arc_helper import overlay as overlay_mod
from arc_helper.arctracker.verdict import CraftRow
from arc_helper.arctracker.verdict import Verdict
from arc_helper.arctracker.verdict import VerdictKind
from arc_helper.database import Item
from arc_helper.overlay import CATEGORY_COLORS
from arc_helper.overlay import PIP_COLORS
from arc_helper.overlay import VERDICT_COLORS
from arc_helper.overlay import OverlayWindow

try:
    _probe = tk.Tk()
    _probe.destroy()
except tk.TclError:
    pytest.skip("no display for Tk", allow_module_level=True)


@pytest.fixture
def root(monkeypatch):
    # Position in plain pixels; never start a screen-capture backend.
    monkeypatch.setattr(overlay_mod, "phys_to_tk_pos", lambda x, y: (x, y))
    r = tk.Tk()
    r.withdraw()
    yield r
    r.destroy()


def _low_ingredient() -> Verdict:
    return Verdict(
        VerdictKind.KEEP_LOW,
        "Keep all",
        "Crafting ingredient",
        "Own 40 · crafts need 284",
        "weapons",
        crafts=(CraftRow("Heavy Gun Parts ×42", "168", "low"),),
    )


def test_renders_verdict_sections(root):
    ov = OverlayWindow(root)
    ov.show_verdict("SIMPLEGUNPARTS", _low_ingredient(), "⟳ 12m", False, None)
    assert ov.title_label.cget("text") == "Keep all"
    assert ov.title_label.cget("fg").lower() == "#f49b50"
    assert ov.verdict_bar.cget("bg").lower() == "#f49b50"
    assert ov.reason_label.cget("text") == "Crafting ingredient"
    assert ov.reason_label.cget("fg").lower() == "#7dd3fc"
    assert ov.counts_label.cget("text") == "Own 40 · crafts need 284"
    assert ov.age_label.cget("text") == "⟳ 12m"
    assert ov.age_label.cget("fg").lower() == "#7d7d99"
    assert ov.crafts_frame.winfo_manager() == "pack"
    assert not ov.made_from_frame.winfo_manager()
    assert ov.verdict_frame.winfo_manager() == "pack"
    assert not ov.generic_frame.winfo_manager()
    assert ov.is_visible()


def test_craft_row_contents(root):
    ov = OverlayWindow(root)
    ov.show_verdict("SIMPLEGUNPARTS", _low_ingredient(), "⟳ 12m", False, None)
    (row,) = ov.craft_rows
    assert row.pip.cget("bg").lower() == PIP_COLORS["low"]
    assert row.label.cget("text") == "Heavy Gun Parts ×42"
    assert row.right.cget("text") == "168"
    assert row.right.cget("fg").lower() == "#9999bb"


def test_made_from_section_and_hint(root):
    ov = OverlayWindow(root)
    v = Verdict(
        VerdictKind.KEEP_BUILDING,
        "Keep · building",
        "Reserve target",
        "Own 21 · min 15 · ideal 50",
        "weapons",
        made_from=(CraftRow("Simple Gun Parts ×4", "own 40 · 24 spare", "partial"),),
        hint="Craft 6 now → 27 / 50",
        hint_state="partial",
    )
    ov.show_verdict("LIGHTGUNPARTS", v, "⟳ 12m", False, None)
    assert not ov.crafts_frame.winfo_manager()
    assert ov.made_from_frame.winfo_manager() == "pack"
    assert ov.made_from_label.cget("text") == "MADE FROM"
    (row,) = ov.made_from_rows
    assert row.right.cget("text") == "own 40 · 24 spare"
    assert row.pip.cget("bg").lower() == "#f5d75e"
    assert ov.hint_label.winfo_manager() == "pack"
    assert ov.hint_label.cget("text") == "Craft 6 now → 27 / 50"
    assert ov.hint_label.cget("fg").lower() == "#f5d75e"


def test_stale_age_orange(root):
    ov = OverlayWindow(root)
    ov.show_verdict("SIMPLEGUNPARTS", _low_ingredient(), "⟳ 3d", True, None)
    assert ov.age_label.cget("text") == "⟳ 3d"
    assert ov.age_label.cget("fg").lower() == "#f49b50"


def test_fallback_show_has_age_dash(root):
    ov = OverlayWindow(root)
    ov.show("CANISTER", Item(name="Canister", action="Sell", sell_price=300))
    assert ov.fallback_age_label.cget("text") == "⟳ —"
    assert ov.generic_frame.winfo_manager() == "pack"
    assert not ov.verdict_frame.winfo_manager()
    assert ov.name_label.cget("text") == "Canister"


def test_swaps_content_in_one_window(root):
    ov = OverlayWindow(root)
    ov.show("CANISTER", Item(name="Canister", action="Sell"))
    win = ov.window
    ov.show_verdict("SIMPLEGUNPARTS", _low_ingredient(), "⟳ 12m", False, None)
    assert ov.window is win
    assert not ov.generic_frame.winfo_manager()
    assert ov.verdict_frame.winfo_manager() == "pack"
    ov.show("CANISTER", Item(name="Canister", action="Sell"))
    assert ov.generic_frame.winfo_manager() == "pack"
    assert not ov.verdict_frame.winfo_manager()


def test_unchanged_verdict_is_not_rebuilt(root):
    ov = OverlayWindow(root)
    builds = []
    real_build = ov._build_rows
    ov._build_rows = lambda *a: builds.append(1) or real_build(*a)
    v = _low_ingredient()
    ov.show_verdict("SIMPLEGUNPARTS", v, "⟳ 12m", False, None)
    root.update()  # map the window so the "already showing" path applies
    assert ov.is_visible()
    first_row = ov.craft_rows[0].frame
    assert len(builds) == 2  # crafts + made-from

    ov.show_verdict("SIMPLEGUNPARTS", v, "⟳ 12m", False, None)
    assert len(builds) == 2  # identical key: nothing rebuilt
    assert ov.craft_rows[0].frame is first_row

    ov.show_verdict("SIMPLEGUNPARTS", v, "⟳ 13m", False, None)
    assert ov.age_label.cget("text") == "⟳ 13m"
    assert len(builds) == 2  # age-only change keeps the rows
    assert ov.craft_rows[0].frame is first_row

    other = Verdict(VerdictKind.SELL, "Sell 18", "Surplus", "Own 118", "base")
    ov.show_verdict("SIMPLEGUNPARTS", other, "⟳ 13m", False, None)
    assert len(builds) == 4  # a different verdict rebuilds


@pytest.fixture
def unit_scale(monkeypatch):
    """Scale 1 regardless of the user's OVERLAY_SCALE / DPI."""
    monkeypatch.setattr(overlay_mod, "get_dpi_scale", lambda: 1.0)
    settings = overlay_mod.get_settings()
    monkeypatch.setattr(settings.overlay, "scale", 1.0)


@pytest.mark.usefixtures("unit_scale")
@pytest.mark.parametrize("title", ["Keep · building", "Recycle 120", "Keep all"])
def test_popup_has_fixed_width(root, title):
    ov = OverlayWindow(root)
    assert ov._scale(260) == 260
    v = Verdict(
        VerdictKind.KEEP_BUILDING,
        title,
        "Reserve target",
        "Own 21 · min 15 · ideal 50",
        "weapons",
        crafts=(CraftRow("Heavy Gun Parts ×35", "140", "low"),),
    )
    ov.show_verdict("X", v, "⟳ 12m", False, None)
    ov.window.update_idletasks()
    assert ov.window.winfo_reqwidth() == 260


@pytest.mark.usefixtures("unit_scale")
def test_long_text_never_widens_popup(root):
    ov = OverlayWindow(root)
    long = "Keep 30 · craft 12 · and a great deal more text than fits"
    v = Verdict(
        VerdictKind.KEEP_BUILDING,
        long,
        long,
        long,
        "crafting",
        crafts=(CraftRow(long, "own 40 · 24 spare", "partial"),),
        made_from=(CraftRow(long, long, "none"),),
        hint=long,
        hint_state="none",
    )
    ov.show_verdict("X", v, "⟳ 10h", True, None)
    ov.window.update_idletasks()
    assert ov.window.winfo_reqwidth() == 260


def test_hide_then_reshow(root):
    ov = OverlayWindow(root)
    v = _low_ingredient()
    ov.show_verdict("SIMPLEGUNPARTS", v, "⟳ 12m", False, None)
    ov.hide()
    assert not ov.is_visible()
    ov.show_verdict("SIMPLEGUNPARTS", v, "⟳ 12m", False, None)
    assert ov.is_visible()


def test_colour_tables():
    assert VERDICT_COLORS[VerdictKind.KEEP] == "#52cb89"
    assert VERDICT_COLORS[VerdictKind.KEEP_BUILDING] == "#f5d75e"
    assert VERDICT_COLORS[VerdictKind.KEEP_LOW] == "#f49b50"
    assert VERDICT_COLORS[VerdictKind.SELL] == "#FFD700"
    assert VERDICT_COLORS[VerdictKind.RECYCLE] == "#00CED1"
    assert VERDICT_COLORS[VerdictKind.NO_RULE] == "#888888"
    assert VERDICT_COLORS[VerdictKind.UNKNOWN] == "#888888"
    assert VERDICT_COLORS[VerdictKind.CONSUME] == "#c4b5fd"
    assert set(VERDICT_COLORS) == set(VerdictKind)
    assert CATEGORY_COLORS == {
        "weapons": "#7dd3fc",
        "consumables": "#60a5fa",
        "medical": "#f9a8d4",
        "crafting": "#e7c66b",
        "base": "#53c8ae",
        "arc_rare": "#bf9af7",
    }
    assert PIP_COLORS == {
        "low": "#f49b50",
        "building": "#f5d75e",
        "met": "#52cb89",
        "covers": "#52cb89",
        "partial": "#f5d75e",
        "none": "#f49b50",
    }


# -- scanner wiring (no Tk needed beyond the module guard) ------------------


class FakeAdvisor:
    def __init__(self, result=None):
        self.result = result
        self.lookups: list[str] = []
        self.refreshes = 0

    def lookup(self, name):
        self.lookups.append(name)
        return self.result

    def request_refresh(self):
        self.refreshes += 1


class FakeDb:
    def __init__(self, item=None):
        self.item = item
        self.lookups = 0
        self.missing: list[str] = []

    def lookup(self, _name):
        self.lookups += 1
        return self.item

    def log_missing_item(self, name):
        self.missing.append(name)


def _scanner(monkeypatch, advisor, db, detection="frame"):
    ocr = SimpleNamespace(detection=detection, last_tooltip_bbox=(1, 2, 3, 4))
    monkeypatch.setattr(main_mod, "get_ocr_engine", lambda: ocr)
    return main_mod.Scanner(
        root=None,
        overlay=None,
        status=None,
        db=db,
        hotkey=SimpleNamespace(),
        advisor=advisor,
        ui_queue=queue.Queue(),
    )


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_scanner_queues_verdict(monkeypatch):
    v = _low_ingredient()
    adv = FakeAdvisor((v, "⟳ 12m", False))
    db = FakeDb()
    sc = _scanner(monkeypatch, adv, db)
    sc._handle_detected_item("SIMPLEGUNPARTS")
    assert _drain(sc.ui_queue) == [
        ("verdict", "SIMPLEGUNPARTS", v, "⟳ 12m", False, (1, 2, 3, 4))
    ]
    assert db.lookups == 0
    assert db.missing == []


def test_scanner_falls_back_to_db(monkeypatch):
    item = Item(name="Canister", action="Sell")
    sc = _scanner(monkeypatch, FakeAdvisor(None), FakeDb(item))
    sc._handle_detected_item("CANISTER")
    assert _drain(sc.ui_queue) == [("overlay", "CANISTER", item, (1, 2, 3, 4))]


def test_scanner_unknown_logs_missing(monkeypatch):
    db = FakeDb(None)
    sc = _scanner(monkeypatch, FakeAdvisor(None), db)
    sc._handle_detected_item("MYSTERY")
    assert db.missing == ["MYSTERY"]
    assert _drain(sc.ui_queue) == [("overlay", "MYSTERY", None, (1, 2, 3, 4))]


def test_scanner_without_advisor_uses_db(monkeypatch):
    item = Item(name="Canister", action="Sell")
    sc = _scanner(monkeypatch, None, FakeDb(item))
    sc._handle_detected_item("CANISTER")
    assert _drain(sc.ui_queue) == [("overlay", "CANISTER", item, (1, 2, 3, 4))]


def test_frame_mode_reshow_reuses_verdict(monkeypatch):
    v = _low_ingredient()
    adv = FakeAdvisor((v, "⟳ 12m", False))
    db = FakeDb()
    sc = _scanner(monkeypatch, adv, db)
    sc._handle_detected_item("SIMPLEGUNPARTS")
    sc._handle_detected_item("SIMPLEGUNPARTS")
    msgs = _drain(sc.ui_queue)
    assert [m[0] for m in msgs] == ["verdict", "verdict"]
    assert msgs[0] == msgs[1]
    assert sc.stats.items_detected == 1  # second scan is a re-show
    assert db.lookups == 0


def test_inventory_trigger_requests_refresh_once(monkeypatch):
    adv = FakeAdvisor(None)
    sc = _scanner(monkeypatch, adv, FakeDb())
    reads = []

    def read_name():
        reads.append(1)
        if len(reads) >= 5:
            sc._running = False

    ocr = main_mod.get_ocr_engine()
    ocr.check_trigger_any = lambda _regions: True  # inventory stays open
    ocr.read_hovered_item_name = read_name
    ocr.last_tooltip_bbox = None
    sc.hotkey = SimpleNamespace(is_held=lambda: False)
    monkeypatch.setattr(main_mod.time, "sleep", lambda _s: None)
    sc._running = True
    sc._scan_loop()
    assert sc.state == main_mod.ScannerState.ACTIVE
    assert len(reads) == 5  # several ACTIVE scans...
    assert adv.refreshes == 1  # ...but one refresh, on the transition


def test_verdict_counts_as_found(monkeypatch):
    adv = FakeAdvisor((_low_ingredient(), "⟳ 12m", False))
    sc = _scanner(monkeypatch, adv, FakeDb())
    sc._handle_detected_item("SIMPLEGUNPARTS")
    sc._handle_detected_item("SIMPLEGUNPARTS")  # frame-mode re-show
    assert sc.stats.items_detected == 1
    assert sc.stats.items_found_in_db == 1


def test_frame_mode_reshow_fallback_no_relog(monkeypatch):
    db = FakeDb(None)
    sc = _scanner(monkeypatch, FakeAdvisor(None), db)
    sc._handle_detected_item("MYSTERY")
    sc._handle_detected_item("MYSTERY")
    assert db.missing == ["MYSTERY"]
    assert db.lookups == 1
    assert [m[0] for m in _drain(sc.ui_queue)] == ["overlay", "overlay"]


class FakeOverlay:
    def __init__(self):
        self.calls: list[tuple] = []

    def show(self, *a, **k):
        self.calls.append(("show", a, k))

    def show_verdict(self, *a, **k):
        self.calls.append(("show_verdict", a, k))

    def hide(self):
        self.calls.append(("hide",))


def test_pump_dispatches_verdict():
    app = main_mod.Application.__new__(main_mod.Application)
    app.overlay = FakeOverlay()
    app.ui_queue = queue.Queue()
    app.root = SimpleNamespace(after=lambda *_: None)
    v = _low_ingredient()
    app.ui_queue.put(("verdict", "X", v, "⟳ 1m", True, (1, 2, 3, 4)))
    app._pump_ui_queue()
    assert app.overlay.calls == [
        ("show_verdict", ("X", v, "⟳ 1m", True), {"anchor": (1, 2, 3, 4)})
    ]


def test_quit_stops_advisor(monkeypatch):
    stopped = []
    app = main_mod.Application.__new__(main_mod.Application)
    app.scanner = SimpleNamespace(stop=lambda: None)
    app.advisor = SimpleNamespace(stop=lambda **_: stopped.append(True))
    app.root = SimpleNamespace(quit=lambda: None, destroy=lambda: None)
    monkeypatch.setattr("arc_helper.screen.reset_backend", lambda: None)
    app.quit()
    assert stopped == [True]


def test_quit_stops_advisor_when_scanner_stop_interrupted(monkeypatch):
    stopped = []
    app = main_mod.Application.__new__(main_mod.Application)

    def interrupted():
        raise KeyboardInterrupt

    app.scanner = SimpleNamespace(stop=interrupted)
    app.advisor = SimpleNamespace(stop=lambda **_: stopped.append(True))
    app.root = SimpleNamespace(quit=lambda: None, destroy=lambda: None)
    monkeypatch.setattr("arc_helper.screen.reset_backend", lambda: None)
    with pytest.raises(KeyboardInterrupt):
        app.quit()
    assert stopped == [True]


def _unknown_verdict() -> Verdict:
    return Verdict(VerdictKind.UNKNOWN, "Unknown", "Not in ARC Tracker data", "", "")


def test_scanner_unresolved_with_personal_data_shows_unknown(monkeypatch):
    v = _unknown_verdict()
    db = FakeDb(Item(name="Mystery", action="Sell"))
    sc = _scanner(monkeypatch, FakeAdvisor((v, "⟳ 12m", False)), db)
    sc._handle_detected_item("MYSTERY")
    sc._handle_detected_item("MYSTERY")  # frame-mode re-show: no relog
    msgs = _drain(sc.ui_queue)
    assert [m[:3] for m in msgs] == [("verdict", "MYSTERY", v)] * 2
    assert db.lookups == 0  # no generic LIKE / fuzzy fallback
    assert db.missing == ["MYSTERY"]
    assert sc.stats.items_found_in_db == 0
