"""Frame-mode tooltip stability (bug B: a bbox that oscillates by a few
pixels with what is behind the tooltip must not hide the popup)."""

import logging
from types import SimpleNamespace

import numpy as np
import pytest

from arc_helper import ocr as ocr_mod
from arc_helper.ocr import OCREngine

A = (1072, 388, 1576, 968)
B = (1072, 396, 1576, 976)  # 8 px below A: past STABLE_TOLERANCE_PX
FAR = (1072, 500, 1576, 1080)


@pytest.fixture
def engine(monkeypatch):
    """OCREngine fed a scripted sequence of find_tooltip results."""
    frame = np.zeros((1440, 3440, 3), dtype=np.uint8)
    monkeypatch.setattr(ocr_mod, "get_screen_resolution", lambda: (3440, 1440))
    monkeypatch.setattr(ocr_mod, "grab_frame_array", lambda *_: frame)
    e = OCREngine()
    e.debug_mode = False
    monkeypatch.setattr(e, "preprocess_tooltip", lambda image, *_: image)
    monkeypatch.setattr(e, "_read_item_name", lambda *_: "TEMPEST I")
    script: list = []
    monkeypatch.setattr(ocr_mod, "find_tooltip", lambda *_: script.pop(0))
    e.script = script
    return e


def _scan(e, bbox):
    e.script.append(bbox)
    return e.extract_item_name_from_frame()


def test_settles_after_two_close_scans(engine):
    assert _scan(engine, A) is None
    assert engine.last_tooltip_bbox is None
    assert _scan(engine, A) == "TEMPEST I"
    assert engine.last_tooltip_bbox == A


def test_alternating_bbox_keeps_the_name(engine):
    _scan(engine, A)
    _scan(engine, A)
    for bbox in (B, A, B, A, B, A, B):
        assert _scan(engine, bbox) == "TEMPEST I"
        assert engine.last_tooltip_bbox == bbox


def test_real_move_still_resettles(engine):
    _scan(engine, A)
    _scan(engine, A)
    assert _scan(engine, FAR) is None
    assert engine.last_tooltip_bbox is None
    assert _scan(engine, FAR) == "TEMPEST I"


def test_tooltip_gone_forgets_the_accepted_bbox(engine):
    _scan(engine, A)
    _scan(engine, A)
    assert _scan(engine, None) is None
    assert _scan(engine, B) is None  # a new tooltip must settle again
    assert engine.last_tooltip_bbox is None


def test_debug_logs_bbox_jitter_once_a_second(engine, monkeypatch, caplog):
    engine.debug_mode = True
    monkeypatch.setattr(engine, "_maybe_save_full_frame", lambda *_: None)
    monkeypatch.setattr(ocr_mod.Image.Image, "save", lambda *_, **__: None)
    clock = iter([100.0, 100.3, 100.6, 101.2, 101.4])
    monkeypatch.setattr(ocr_mod, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    caplog.set_level(logging.DEBUG, logger=ocr_mod.logger.name)
    _scan(engine, A)
    _scan(engine, A)  # unchanged: no jitter
    for bbox in (B, A, B, A, B):
        _scan(engine, bbox)
    jitter = [r for r in caplog.records if "jitter" in r.getMessage()]
    assert len(jitter) == 2
