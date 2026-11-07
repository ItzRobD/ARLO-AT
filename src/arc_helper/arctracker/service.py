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
from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from arc_helper.arctracker.catalogue import Catalogue
from arc_helper.arctracker.catalogue import fetch_catalogue
from arc_helper.arctracker.mcp_client import MAX_CALLS
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
from arc_helper.arctracker.verdict import craft_verdict_for
from arc_helper.arctracker.verdict import unknown_verdict
from arc_helper.arctracker.verdict import verdict_for

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

REFRESH_S = 300
REQUEST_THROTTLE_S = 180
# Our own cap on MCP tool calls, under the server's 150 per 10 min: no refresh
# starts while the last 10 min already hold CALL_WINDOW_MAX calls.
CALL_WINDOW_S = 600
CALL_WINDOW_MAX = 100
BACKOFF_S = (60, 120, 300)
CATALOGUE_REFRESH_S = 86400
CATALOGUE_RETRY_S = 3600
TICK_S = 1.0
RESOLVE_MEMO_MAX = 4096
CATALOGUE_ERROR_RETRY_S = 300
_MISSING: Any = object()
NO_KEY_MSG = "ARC Tracker: no API key; using generic item database"


@dataclass(frozen=True)
class _Live:
    """What `lookup` reads. Swapped as a whole; the memo dicts belong to this
    plan only, so they need no clearing or locking."""

    plan: Plan
    # item id -> verdict; None marks a verdict_for that raised (logged once)
    verdicts: dict[str, Verdict | None] = field(default_factory=dict)
    craft_verdicts: dict[str, Verdict | None] = field(default_factory=dict)
    resolved: dict[str, str | None] = field(default_factory=dict)


_UNKNOWN = unknown_verdict()


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _read_cached_catalogue(path: Path) -> tuple[Catalogue, int] | None:
    """The cached catalogue and the mtime_ns it was read at."""
    mtime = _mtime_ns(path)
    if mtime is None:
        return None
    try:
        cat = Catalogue.from_api_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Unreadable catalogue cache %s", path)
        return None
    return cat, mtime


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
        self._catalogue_path = self._cache_dir / "catalogue.json"
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
        self._calls: deque[tuple[float, int]] = deque()  # (start, tool calls)
        self._no_key_logged = False

        self._catalogue: Catalogue | None = None
        self._catalogue_mtime_ns: int | None = None  # cache file it came from
        self._snapshot: PlayerSnapshot | None = None
        self._empty_rules: tuple[Catalogue, Rules] | None = None
        self._built_from: tuple[object, object, object] | None = None
        self._live: _Live | None = None

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        if not self._has_key:
            return  # no key: never load the catalogue or contact arctracker.io
        # Startup: cached data first, so lookup works offline immediately.
        cached = _read_cached_catalogue(self._catalogue_path)
        if cached is not None:
            self._catalogue, self._catalogue_mtime_ns = cached
        if self._snapshot_path.exists():
            self._snapshot = load_snapshot(self._snapshot_path)
        self._rebuild()

    def __repr__(self) -> str:
        return f"AdvisorService(cache_dir={str(self._cache_dir)!r})"

    # -- scanner-thread API ------------------------------------------------

    def lookup(
        self, ocr_name: str, *, crafting: bool = False
    ) -> tuple[Verdict, str, bool] | None:
        """Verdict, age label and staleness; None without personal data.

        ``crafting`` gives the crafting-screen verdict (crafts to the ideal
        target) instead of the stash Keep/Sell/Recycle verdict.

        With personal data, a name the catalogue cannot resolve gets the
        Unknown verdict (never the generic database's fuzzy match).
        """
        live = self._live  # the single read; never changes under us
        if live is None:
            return None
        try:
            item_id = live.resolved.get(ocr_name, _MISSING)
            if item_id is _MISSING:
                item_id = live.plan.catalogue.resolve(ocr_name)
                if len(live.resolved) >= RESOLVE_MEMO_MAX:
                    live.resolved.clear()
                live.resolved[ocr_name] = item_id
        except Exception:
            logger.exception("ARC Tracker name resolution failed for %r", ocr_name)
            return None
        if item_id is None:
            verdict = _UNKNOWN
        else:
            memo = live.craft_verdicts if crafting else live.verdicts
            decide = craft_verdict_for if crafting else verdict_for
            verdict = memo.get(item_id, _MISSING)
            if verdict is _MISSING:
                try:
                    verdict = decide(live.plan, item_id)
                except Exception:
                    logger.exception(
                        "ARC Tracker verdict failed for %r; not retried until "
                        "the data changes",
                        ocr_name,
                    )
                    verdict = None
                memo[item_id] = verdict
            if verdict is None:
                return None
        snap = live.plan.snapshot
        now = datetime.fromtimestamp(self._clock(), UTC)
        label, stale = format_age(snap.synced_at if snap else None, now)
        return verdict, label, stale

    def request_refresh(self) -> None:
        """Ask for fresh personal data. Ignored within 180 s of the last fetch,
        while backing off after a failure, and while the call window is full."""
        with self._lock:
            if not self._has_key or self._auth_failed:
                return
            now = self._clock()
            last = self._last_fetch
            if last is not None and now - last < REQUEST_THROTTLE_S:
                return  # data is under three minutes old
            if self._failures:
                return  # in backoff: a request never brings the retry forward
            if self._window_calls(now) >= CALL_WINDOW_MAX:
                return
            self._next_fetch = min(self._next_fetch, now)
        self._wake.set()

    def _window_calls(self, now: float) -> int:
        """Tool calls started in the last CALL_WINDOW_S. Caller holds _lock."""
        calls = self._calls
        while calls and calls[0][0] <= now - CALL_WINDOW_S:
            calls.popleft()
        return sum(n for _, n in calls)

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
        if not self._has_key:
            if not self._no_key_logged:
                self._no_key_logged = True
                logger.info(NO_KEY_MSG)
            return  # nothing to do: no thread, no network
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
        if not self._has_key:
            return
        now = self._clock()
        if now >= self._next_catalogue:
            self._refresh_catalogue(now)
        if self._client is not None:
            with self._lock:
                due = (
                    not self._auth_failed
                    and now >= self._next_fetch
                    and self._window_calls(now) < CALL_WINDOW_MAX
                )
            if due:
                self._refresh_personal(now)
        self._rebuild()

    def _refresh_catalogue(self, now: float) -> None:
        # Scheduled first, so a loader that raises is retried at most every 300 s.
        self._next_catalogue = now + CATALOGUE_ERROR_RETRY_S
        current = self._catalogue
        mtime = self._catalogue_mtime_ns
        reuse = (current, mtime) if current is not None and mtime is not None else None
        try:
            cat = self._catalogue_loader(self._cache_dir, current=reuse)
        except Exception as exc:  # noqa: BLE001 - never let the catalogue stall the loop
            logger.warning(
                "ARC Tracker catalogue load failed, retry in %ss: %s: %s",
                CATALOGUE_ERROR_RETRY_S,
                type(exc).__name__,
                exc,
            )
            return
        if cat is None:
            self._next_catalogue = now + CATALOGUE_RETRY_S
            return
        self._next_catalogue = now + CATALOGUE_REFRESH_S
        old = self._catalogue
        if cat is old:
            return
        self._catalogue_mtime_ns = _mtime_ns(self._catalogue_path)
        if old is None or cat.version != old.version or not cat.version:
            self._catalogue = cat

    def _refresh_personal(self, now: float) -> None:
        assert self._client is not None
        try:
            try:
                raw = self._client.fetch_raw()
            finally:
                self._record_calls(now)
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

    def _record_calls(self, now: float) -> None:
        used = getattr(self._client, "calls_made", MAX_CALLS)
        if not isinstance(used, int) or isinstance(used, bool) or used < 0:
            used = MAX_CALLS  # unknown: assume the whole budget
        with self._lock:
            self._calls.append((now, used))

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
