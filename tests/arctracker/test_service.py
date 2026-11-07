import json
import logging
import os
import shutil
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from arc_helper.arctracker.mcp_client import ArcTrackerAuthError
from arc_helper.arctracker.mcp_client import ArcTrackerError
from arc_helper.arctracker.service import AdvisorService
from arc_helper.arctracker.snapshot import PlayerSnapshot
from arc_helper.arctracker.snapshot import save_snapshot
from arc_helper.arctracker.verdict import VerdictKind
from arc_helper.config import Settings

FIX = Path(__file__).parent / "fixtures"
KEY = "FAKEKEY-not-a-real-one"
T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC).timestamp()


def _fix(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _raw():
    return {
        "inventory": [_fix("inventory_p1.json"), _fix("inventory_p2.json")],
        "needs": [_fix("needs_p1.json"), _fix("needs_p2.json")],
        "tracking": _fix("tracking.json"),
        "progress_blueprints": _fix("progress_blueprints.json"),
        "progress_workshop": _fix("progress_workshop.json"),
    }


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeClient:
    """Returns fixture data; `script` is a list of exceptions/None consumed per
    call (None = success); when exhausted, `default` applies."""

    def __init__(self, script=(), default=None):
        self.script = list(script)
        self.default = default
        self.calls = 0

    def fetch_raw(self):
        self.calls += 1
        outcome = self.script.pop(0) if self.script else self.default
        if outcome is not None:
            raise outcome
        return _raw()


def _settings(tmp_path, key=KEY):
    return Settings(
        _env_file=None,
        arctracker_api_key=key,
        rules_path=tmp_path / "rules.yaml",
        arctracker_cache_dir=tmp_path / "cache",
    )


def _loader(value):
    return lambda *_a, **_k: value


def _write_catalogue_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    shutil.copy(FIX / "catalogue_subset.json", cache / "catalogue.json")


def _service(tmp_path, client, clock=None, key=KEY, cat=None):
    _write_catalogue_cache(tmp_path)
    return AdvisorService(
        _settings(tmp_path, key),
        client=client,
        clock=clock or Clock(),
        catalogue_loader=_loader(cat),
    )


def _run(svc, clock, seconds):
    """Advance the fake clock one loop tick (1 s) at a time."""
    for _ in range(seconds):
        svc.step()
        clock.t += 1


def test_lookup_uses_cached_snapshot_offline(tmp_path):
    synced = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
    snap = PlayerSnapshot(
        synced_at=synced,
        fetched_at=synced,
        owned={"canister": 10},
        needs=(),
        keep_amounts={},
        learned_blueprints=frozenset(),
        blueprints_complete=True,
        bench_levels={},
    )
    save_snapshot(snap, tmp_path / "cache" / "snapshot.json")
    clock = Clock()
    svc = _service(tmp_path, FakeClient(default=ArcTrackerError("offline")), clock)
    # works before the service thread has done anything
    got = svc.lookup("CANISTER")
    assert got is not None
    verdict, age, stale = got
    assert verdict.title
    assert age == "⟳ 2h"
    assert stale is False
    svc.step()  # refresh fails; cached snapshot stays
    assert svc.lookup("CANISTER")[1] == "⟳ 2h"


def test_keeps_last_snapshot_and_backs_off_on_error(tmp_path):  # Review Focus 3
    clock = Clock()
    err = ArcTrackerError("boom")
    client = FakeClient(script=[None, err, err, err], default=err)
    svc = _service(tmp_path, client, clock)
    _run(svc, clock, 600)
    assert client.calls <= 6
    assert svc.lookup("CANISTER") is not None
    assert (tmp_path / "cache" / "snapshot.json").exists()


def test_backoff_schedule(tmp_path):
    clock = Clock()
    err = ArcTrackerError("boom")
    client = FakeClient(script=[None], default=err)
    svc = _service(tmp_path, client, clock)
    times = []
    for _ in range(1200):
        before = client.calls
        svc.step()
        if client.calls != before:
            times.append(clock.t - T0)
        clock.t += 1
    # ok at 0; fail at 300; then 60, 120, 300, 300 s apart
    assert times[:6] == [0, 300, 360, 480, 780, 1080]


def test_auth_error_stops_retrying(tmp_path, caplog):
    clock = Clock()
    client = FakeClient(default=ArcTrackerAuthError("unauthorized"))
    svc = _service(tmp_path, client, clock)
    with caplog.at_level(logging.WARNING):
        _run(svc, clock, 2000)
        svc.request_refresh()
        _run(svc, clock, 100)
    assert client.calls == 1
    assert sum("API key" in r.getMessage() for r in caplog.records) == 1
    assert "key" in svc.status().lower()


def test_request_refresh_throttled(tmp_path):
    clock = Clock()
    client = FakeClient()
    svc = _service(tmp_path, client, clock)
    _run(svc, clock, 100)
    assert client.calls == 1
    for _ in range(5):  # 5 requests within 180 s of the start-up fetch
        svc.request_refresh()
        _run(svc, clock, 10)
    assert client.calls == 1
    _run(svc, clock, 30)  # t=180
    svc.request_refresh()
    _run(svc, clock, 1)
    assert client.calls == 2


def test_request_refresh_soon_after_fetch_is_ignored(tmp_path):
    clock = Clock()
    client = FakeClient()
    svc = _service(tmp_path, client, clock)
    _run(svc, clock, 10)
    svc.request_refresh()  # 10 s after the start-up fetch
    _run(svc, clock, 100)
    assert client.calls == 1
    svc.request_refresh()  # 110 s after it: still inside the 180 s throttle
    _run(svc, clock, 1)
    assert client.calls == 1
    _run(svc, clock, 70)
    svc.request_refresh()  # 181 s after it
    _run(svc, clock, 1)
    assert client.calls == 2


def test_rules_edit_rebuilds_plan(tmp_path):
    clock = Clock()
    rules = tmp_path / "rules.yaml"
    rules.write_text("anvil_i: {action: sell}\n", encoding="utf-8")
    svc = _service(tmp_path, FakeClient(), clock)
    _run(svc, clock, 2)
    assert svc.lookup("Anvil I")[0].kind is VerdictKind.SELL
    rules.write_text("anvil_i: {action: keep}\n", encoding="utf-8")
    st = rules.stat()
    os.utime(rules, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    _run(svc, clock, 1)
    assert svc.lookup("Anvil I")[0].kind is VerdictKind.KEEP


def test_lookup_is_fast(tmp_path):
    clock = Clock()
    svc = _service(tmp_path, FakeClient(), clock)
    svc.step()
    names = ["CANISTER", "Light Gun Parts", "RENEGADE IV", "Oil", "Anvil I"]
    start = time.perf_counter()
    for i in range(1000):
        assert svc.lookup(names[i % len(names)]) is not None
    assert time.perf_counter() - start < 5


def test_unknown_name_with_personal_data_is_unknown_verdict(tmp_path):
    svc = _service(tmp_path, FakeClient())
    svc.step()
    verdict, _age, _stale = svc.lookup("NOT AN ITEM AT ALL")
    assert (verdict.kind, verdict.title, verdict.reason) == (
        VerdictKind.UNKNOWN,
        "Unknown",
        "Not in ARC Tracker data",
    )


def test_unknown_name_without_personal_data_is_none(tmp_path):
    svc = _service(tmp_path, FakeClient(default=ArcTrackerError("offline")))
    svc.step()
    assert svc.lookup("NOT AN ITEM AT ALL") is None


def test_no_snapshot_means_no_lookup(tmp_path):
    svc = _service(tmp_path, FakeClient(default=ArcTrackerError("x")))
    svc.step()
    assert svc.lookup("CANISTER") is None


def test_no_key_means_no_lookup(tmp_path, caplog):
    client = FakeClient()
    with caplog.at_level(logging.INFO):
        svc = _service(tmp_path, client, key=None)
        svc.step()
        svc.start()
        svc.start()
    assert svc.lookup("CANISTER") is None
    assert client.calls == 0
    msg = "ARC Tracker: no API key; using generic item database"
    assert [r.getMessage() for r in caplog.records].count(msg) == 1


def test_downloaded_catalogue_used_without_cache(tmp_path, cat):
    svc = AdvisorService(
        _settings(tmp_path),
        client=FakeClient(),
        clock=Clock(),
        catalogue_loader=_loader(cat),
    )
    assert svc.lookup("CANISTER") is None
    svc.step()
    assert svc.lookup("CANISTER") is not None


def test_key_never_leaks(tmp_path, caplog):
    clock = Clock()
    boom = RuntimeError(f"connect failed: Authorization: Bearer {KEY}")
    client = FakeClient(default=boom)
    settings = _settings(tmp_path)
    _write_catalogue_cache(tmp_path)
    svc = AdvisorService(
        settings, client=client, clock=clock, catalogue_loader=_loader(None)
    )
    with caplog.at_level(logging.DEBUG):
        _run(svc, clock, 400)
    assert client.calls >= 2
    assert KEY not in caplog.text
    for rec in caplog.records:
        assert KEY not in rec.getMessage()
        assert not rec.exc_info or KEY not in str(rec.exc_info[1])
    for f in (tmp_path / "cache").rglob("*"):
        if f.is_file():
            assert KEY not in f.read_text(encoding="utf-8", errors="replace")
    assert KEY not in repr(svc)
    assert KEY not in repr(settings)
    assert KEY not in svc.status()


def test_real_client_redacts_key_and_has_safe_repr():
    from arc_helper.arctracker.mcp_client import ArcTrackerClient

    c = ArcTrackerClient(KEY, "http://127.0.0.1:9/mcp")  # nothing listens on 9
    assert KEY not in repr(c)
    with pytest.raises(ArcTrackerError) as info:
        c.fetch_raw()
    assert KEY not in str(info.value)
    assert info.value.__cause__ is None


def test_start_stop(tmp_path):
    client = FakeClient()
    svc = _service(tmp_path, client)
    svc.start()
    deadline = time.monotonic() + 2
    while client.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    svc.stop()
    assert client.calls == 1
    assert svc.lookup("CANISTER") is not None


def _fetch_times(svc, client, clock, seconds, every=None, start=0):
    times = []
    for i in range(seconds):
        if every and i >= start and (i - start) % every == 0:
            svc.request_refresh()
        before = client.calls
        svc.step()
        if client.calls != before:
            times.append(clock.t - T0)
        clock.t += 1
    return times


def test_request_refresh_does_not_shorten_backoff(tmp_path):  # Review Focus 3
    clock = Clock()
    client = FakeClient(script=[None], default=ArcTrackerError("down"))
    svc = _service(tmp_path, client, clock)
    # outage starts at t=300; open the inventory every 61 s from then on
    times = _fetch_times(svc, client, clock, 1200, every=61, start=301)
    assert times[:6] == [0, 300, 360, 480, 780, 1080]


def test_request_refresh_works_again_after_recovery(tmp_path):
    clock = Clock()
    client = FakeClient(script=[None, ArcTrackerError("blip")])
    svc = _service(tmp_path, client, clock)
    _run(svc, clock, 361)  # ok at 0, fail at 300, ok at 360
    assert client.calls == 3
    _run(svc, clock, 180)
    svc.request_refresh()
    _run(svc, clock, 1)
    assert client.calls == 4


def test_raising_catalogue_loader_is_throttled_and_fetch_still_runs(tmp_path, caplog):
    loads = []

    def loader(cache_dir, **_kw):
        loads.append(cache_dir)
        msg = "unexpected payload"
        raise TypeError(msg)

    _write_catalogue_cache(tmp_path)
    clock = Clock()
    client = FakeClient()
    svc = AdvisorService(
        _settings(tmp_path), client=client, clock=clock, catalogue_loader=loader
    )
    with caplog.at_level(logging.WARNING):
        _run(svc, clock, 600)
    assert len(loads) == 2  # t=0 and t=300
    assert client.calls == 2  # t=0 and t=300 personal refreshes still ran
    assert svc.lookup("CANISTER") is not None  # cached catalogue kept
    failures = [r for r in caplog.records if "catalogue load failed" in r.getMessage()]
    assert len(failures) == 2
    assert all(r.exc_info is None for r in failures)


def test_no_key_never_loads_catalogue_or_contacts_server(tmp_path):
    loads = []

    def loader(*a, **k):
        loads.append((a, k))

    _write_catalogue_cache(tmp_path)
    client = FakeClient()
    clock = Clock()
    svc = AdvisorService(
        _settings(tmp_path, key=None),
        client=client,
        clock=clock,
        catalogue_loader=loader,
    )
    svc.start()
    assert svc._thread is None  # noqa: SLF001 - no service thread without a key
    _run(svc, clock, 3 * 86400 // 100)
    svc.request_refresh()
    svc.step()
    svc.stop()
    assert loads == []
    assert client.calls == 0
    assert svc._catalogue is None  # noqa: SLF001 - cache not even parsed


class CountingClient(FakeClient):
    """Reports `calls_made` tool calls per fetch, like ArcTrackerClient."""

    def __init__(self, per_fetch=20, **kw):
        super().__init__(**kw)
        self.per_fetch = per_fetch
        self.calls_made = 0

    def fetch_raw(self):
        self.calls_made = self.per_fetch
        return super().fetch_raw()


def _max_window(times, per_fetch, window=600):
    return max(
        (sum(per_fetch for u in times if t - window < u <= t) for t in times),
        default=0,
    )


def test_request_refresh_never_exceeds_call_window(tmp_path):
    clock = Clock()
    client = CountingClient(per_fetch=20)
    svc = _service(tmp_path, client, clock)
    times = _fetch_times(svc, client, clock, 7200, every=1)  # ask every second
    assert len(times) > 20
    assert _max_window(times, 20) <= 100


def test_scheduled_refresh_waits_for_window_room(tmp_path, monkeypatch):
    from arc_helper.arctracker import service

    monkeypatch.setattr(service, "REFRESH_S", 30)  # would be 400 calls / 10 min
    clock = Clock()
    client = CountingClient(per_fetch=20)
    svc = _service(tmp_path, client, clock)
    times = _fetch_times(svc, client, clock, 3600)
    assert _max_window(times, 20) <= 100
    assert times[:5] == [0, 30, 60, 90, 120]
    assert times[5] == 600  # window full until the t=0 calls age out


def test_raising_verdict_logs_once_per_plan(tmp_path, caplog, monkeypatch):
    from arc_helper.arctracker import service

    def boom(_plan, _item_id):
        msg = "bad verdict"
        raise RuntimeError(msg)

    monkeypatch.setattr(service, "verdict_for", boom)
    svc = _service(tmp_path, FakeClient())
    svc.step()
    with caplog.at_level(logging.ERROR):
        for _ in range(50):  # ~5 s of frame-mode scans
            assert svc.lookup("CANISTER") is None
    failures = [r for r in caplog.records if "verdict failed" in r.getMessage()]
    assert len(failures) == 1


def test_catalogue_json_parsed_once_at_startup(tmp_path, monkeypatch):
    from arc_helper.arctracker.catalogue import Catalogue

    _write_catalogue_cache(tmp_path)  # fresh cache: no download due
    parses = []
    orig = Catalogue.from_api_json
    monkeypatch.setattr(
        Catalogue,
        "from_api_json",
        classmethod(lambda _cls, d: parses.append(1) or orig(d)),
    )
    clock = Clock()
    svc = AdvisorService(_settings(tmp_path), client=FakeClient(), clock=clock)
    _run(svc, clock, 3)
    assert parses == [1]
    assert svc.lookup("CANISTER") is not None
