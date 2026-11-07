"""Background advisor: keeps the player's ARC Tracker data and plan fresh.

The scanner thread calls `lookup`, which never does I/O: it reads one
attribute (`self._live`, an immutable bundle of plan + memo caches) that the
service thread replaces whole whenever the catalogue, snapshot or rules change.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from arc_helper.arctracker.catalogue import Catalogue
from arc_helper.arctracker.catalogue import fetch_catalogue
from arc_helper.arctracker.mcp_client import ArcTrackerAuthError
from arc_helper.arctracker.mcp_client import ArcTrackerClient
from arc_helper.arctracker.mcp_client import redact
from arc_helper.arctracker.planner import Plan
from arc_helper.arctracker.planner import build_plan
from arc_helper.arctracker.rules import Rules
from arc_helper.arctracker.rules import RulesWatcher
from arc_helper.arctracker.rules import parse_rules
from arc_helper.arctracker.snapshot import PlayerSnapshot
from arc_helper.arctracker.snapshot import format_age
from arc_helper.arctracker.snapshot import load_snapshot
from arc_helper.arctracker.snapshot import parse_inventory
from arc_helper.arctracker.snapshot import parse_needs
from arc_helper.arctracker.snapshot import parse_progress
from arc_helper.arctracker.snapshot import parse_tracking
from arc_helper.arctracker.snapshot import save_snapshot
from arc_helper.arctracker.verdict import Verdict
from arc_helper.arctracker.verdict import verdict_for

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

REFRESH_S = 300
REQUEST_THROTTLE_S = 60
BACKOFF_S = (60, 120, 300)
CATALOGUE_REFRESH_S = 86400
CATALOGUE_RETRY_S = 3600
TICK_S = 1.0
RESOLVE_MEMO_MAX = 4096
NO_KEY_MSG = "ARC Tracker: no API key; using generic item database"


@dataclass(frozen=True)
class _Live:
    """What `lookup` reads. Swapped as a whole; the memo dicts belong to this
    plan only, so they need no clearing or locking."""

    plan: Plan
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    resolved: dict[str, str | None] = field(default_factory=dict)


def _read_cached_catalogue(path: Path) -> Catalogue | None:
    if not path.exists():
        return None
    try:
        return Catalogue.from_api_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Unreadable catalogue cache %s", path)
        return None


class AdvisorService:
    def __init__(
        self,
        settings: Any,
        *,
        client: Any = None,
        clock: Callable[[], float] = time.time,
        catalogue_loader: Callable[..., Catalogue | None] = fetch_catalogue,
    ):
        secret = settings.arctracker_api_key
        key = secret.get_secret_value() if secret is not None else ""
        self._has_key = bool(key)
        self._redact_key = key  # only used to scrub text before logging
        self._cache_dir = Path(settings.arctracker_cache_dir)
        self._snapshot_path = self._cache_dir / "snapshot.json"
        self._clock = clock
        self._catalogue_loader = catalogue_loader
        if client is None and self._has_key:
            client = ArcTrackerClient(key, settings.arctracker_mcp_url)
        self._client = client if self._has_key else None
        self._rules_watcher = RulesWatcher(settings.rules_path)

        self._lock = threading.Lock()  # guards the schedule fields below
        self._next_fetch = 0.0
        self._last_fetch: float | None = None
        self._failures = 0
        self._auth_failed = False
        self._last_error: str | None = None
        self._next_catalogue = 0.0

        self._catalogue: Catalogue | None = None
        self._snapshot: PlayerSnapshot | None = None
        self._empty_rules: tuple[Catalogue, Rules] | None = None
        self._built_from: tuple[object, object, object] | None = None
        self._live: _Live | None = None

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        if not self._has_key:
            logger.info(NO_KEY_MSG)
        # Startup: cached data first, so lookup works offline immediately.
        self._catalogue = _read_cached_catalogue(self._cache_dir / "catalogue.json")
        if self._has_key and self._snapshot_path.exists():
            self._snapshot = load_snapshot(self._snapshot_path)
        self._rebuild()

    def __repr__(self) -> str:
        return f"AdvisorService(cache_dir={str(self._cache_dir)!r})"

    # -- scanner-thread API ------------------------------------------------

    def lookup(self, ocr_name: str) -> tuple[Verdict, str, bool] | None:
        live = self._live  # the single read; never changes under us
        if live is None:
            return None
        try:
            if ocr_name in live.resolved:
                item_id = live.resolved[ocr_name]
            else:
                item_id = live.plan.catalogue.resolve(ocr_name)
                if len(live.resolved) >= RESOLVE_MEMO_MAX:
                    live.resolved.clear()
                live.resolved[ocr_name] = item_id
            if item_id is None:
                return None
            verdict = live.verdicts.get(item_id)
            if verdict is None:
                verdict = verdict_for(live.plan, item_id)
                live.verdicts[item_id] = verdict
        except Exception:
            logger.exception("ARC Tracker verdict failed for %r", ocr_name)
            return None
        snap = live.plan.snapshot
        now = datetime.fromtimestamp(self._clock(), UTC)
        label, stale = format_age(snap.synced_at if snap else None, now)
        return verdict, label, stale

    def request_refresh(self) -> None:
        """Ask for fresh personal data. Ignored within 60 s of the last fetch."""
        with self._lock:
            if not self._has_key or self._auth_failed:
                return
            now = self._clock()
            last = self._last_fetch
            if last is not None and now - last < REQUEST_THROTTLE_S:
                return  # data is under a minute old (or a retry is pending)
            self._next_fetch = min(self._next_fetch, now)
        self._wake.set()

    def status(self) -> str:
        if not self._has_key:
            return "ARC Tracker: no API key"
        if self._auth_failed:
            return "ARC Tracker: API key rejected"
        snap = self._snapshot
        if snap is None:
            return (
                "ARC Tracker: connecting"
                if self._last_error is None
                else (f"ARC Tracker: offline ({self._last_error})")
            )
        now = datetime.fromtimestamp(self._clock(), UTC)
        label, stale = format_age(snap.synced_at, now)
        state = "offline, cached" if self._last_error else "synced"
        return f"ARC Tracker: {state} {label}" + (" (stale)" if stale else "")

    # -- service thread ----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="arctracker-advisor", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                logger.exception("ARC Tracker advisor tick failed")
            self._wake.wait(TICK_S)
            self._wake.clear()

    def step(self) -> None:
        """One loop tick: catalogue (daily), personal data (when due), rules."""
        now = self._clock()
        if now >= self._next_catalogue:
            self._refresh_catalogue(now)
        if self._client is not None:
            with self._lock:
                due = not self._auth_failed and now >= self._next_fetch
            if due:
                self._refresh_personal(now)
        self._rebuild()

    def _refresh_catalogue(self, now: float) -> None:
        cat = self._catalogue_loader(self._cache_dir)
        if cat is None:
            self._next_catalogue = now + CATALOGUE_RETRY_S
            return
        self._next_catalogue = now + CATALOGUE_REFRESH_S
        old = self._catalogue
        if old is None or cat.version != old.version or not cat.version:
            self._catalogue = cat

    def _refresh_personal(self, now: float) -> None:
        assert self._client is not None
        try:
            raw = self._client.fetch_raw()
            snapshot = _snapshot_from_raw(raw, now)
        except ArcTrackerAuthError as exc:
            with self._lock:
                self._auth_failed = True
                self._last_fetch = now
            logger.warning(
                "ARC Tracker: API key rejected (%s); personal data paused until "
                "ARCTRACKER_API_KEY changes and ARLO restarts",
                redact(str(exc), self._redact_key),
            )
            return
        except Exception as exc:  # noqa: BLE001 - keep the last snapshot on any failure
            with self._lock:
                delay = BACKOFF_S[min(self._failures, len(BACKOFF_S) - 1)]
                self._failures += 1
                self._last_fetch = now
                self._next_fetch = now + delay
            text = redact(f"{type(exc).__name__}: {exc}", self._redact_key)
            self._last_error = "fetch failed"
            logger.warning("ARC Tracker refresh failed, retry in %ss: %s", delay, text)
            return
        with self._lock:
            self._failures = 0
            self._last_fetch = now
            self._next_fetch = now + REFRESH_S
        self._last_error = None
        self._snapshot = snapshot
        try:
            save_snapshot(snapshot, self._snapshot_path)
        except OSError as exc:
            logger.warning("Cannot write ARC Tracker snapshot cache: %s", exc)

    def _rules(self, catalogue: Catalogue) -> Rules:
        rules = self._rules_watcher.current(catalogue)
        if rules is not None:
            return rules
        if self._empty_rules is None or self._empty_rules[0] is not catalogue:
            self._empty_rules = (catalogue, parse_rules("", catalogue))
        return self._empty_rules[1]

    def _rebuild(self) -> None:
        cat, snap = self._catalogue, self._snapshot
        if cat is None or snap is None or not self._has_key:
            return
        rules = self._rules(cat)
        built = self._built_from
        if built is not None and all(
            a is b for a, b in zip(built, (cat, snap, rules), strict=True)
        ):
            return
        self._built_from = (cat, snap, rules)  # also on failure: log once per input
        try:
            plan = build_plan(cat, snap, rules)
        except Exception:
            logger.exception("ARC Tracker plan build failed; keeping previous plan")
            return
        self._live = _Live(plan)


def _snapshot_from_raw(raw: dict[str, Any], now: float) -> PlayerSnapshot:
    owned, synced_at = parse_inventory(raw["inventory"])
    learned, complete, benches = parse_progress(
        raw["progress_blueprints"], raw["progress_workshop"]
    )
    return PlayerSnapshot(
        synced_at=synced_at,
        fetched_at=datetime.fromtimestamp(now, UTC),
        owned=owned,
        needs=parse_needs(raw["needs"]),
        keep_amounts=parse_tracking(raw["tracking"]),
        learned_blueprints=learned,
        blueprints_complete=complete,
        bench_levels=benches,
    )
