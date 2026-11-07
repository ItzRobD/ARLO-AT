"""Player snapshot built from ARC Tracker MCP responses, with a JSON cache."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 3 * 3600


@dataclass(frozen=True)
class Need:
    item_id: str
    needed: int
    kind: str
    label: str


@dataclass(frozen=True)
class PlayerSnapshot:
    synced_at: datetime | None
    fetched_at: datetime
    owned: dict[str, int]
    needs: tuple[Need, ...]
    keep_amounts: dict[str, int]
    learned_blueprints: frozenset[str]
    blueprints_complete: bool
    bench_levels: dict[str, int]


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _add(owned: dict[str, int], entries: Any) -> None:
    if not isinstance(entries, list):
        return
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("id"), str):
            count = e.get("count")
            if isinstance(count, int):
                owned[e["id"]] = owned.get(e["id"], 0) + count


def parse_inventory(pages: list[dict]) -> tuple[dict[str, int], datetime | None]:
    """Sum stash items across pages plus the loadout (taken once, from the first
    page that has one, since every page repeats it)."""
    owned: dict[str, int] = {}
    synced_at: datetime | None = None
    loadout_done = False
    for page in pages:
        _add(owned, page.get("items"))
        if synced_at is None:
            synced_at = _parse_dt((page.get("readFromGame") or {}).get("at"))
        loadout = page.get("loadout")
        if not loadout_done and isinstance(loadout, dict):
            groups = loadout.get("items")
            if isinstance(groups, dict):
                for lst in groups.values():
                    _add(owned, lst)
                loadout_done = True
    return owned, synced_at


def parse_needs(pages: list[dict]) -> tuple[Need, ...]:
    out: list[Need] = []
    for page in pages:
        for group in page.get("groups") or []:
            kind = str(group.get("kind", ""))
            label = str(group.get("name", ""))
            for item in group.get("items") or []:
                needed = item.get("needed")
                if (
                    isinstance(needed, int)
                    and needed > 0
                    and isinstance(item.get("id"), str)
                ):
                    out.append(Need(item["id"], needed, kind, label))
    return tuple(out)


def parse_tracking(data: dict) -> dict[str, int]:
    """Keep amounts. Real entry: {"id", "shape": "keep", "keepAmount"}. Also
    accepts item id at item.id / itemId and amount at amount / target."""
    keeps: dict[str, int] = {}
    entries = data.get("entries") if isinstance(data, dict) else None
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or e.get("shape") != "keep":
            continue
        item = e.get("item")
        item_id = item.get("id") if isinstance(item, dict) else None
        item_id = item_id or e.get("itemId") or e.get("id")
        amount = next(
            (
                e[k]
                for k in ("keepAmount", "amount", "target")
                if isinstance(e.get(k), int)
            ),
            None,
        )
        if isinstance(item_id, str) and amount is not None:
            keeps[item_id] = amount
    return keeps


def parse_progress(
    blueprints: dict, workshop: dict
) -> tuple[frozenset[str], bool, dict[str, int]]:
    bp = blueprints.get("blueprints") or {}
    learned = frozenset(
        i["id"] for i in bp.get("items") or [] if isinstance(i, dict) and "id" in i
    )
    complete = not bp.get("namesTruncated", False)
    benches = {
        m["id"]: m["level"]
        for m in (workshop.get("workshop") or {}).get("modules") or []
        if isinstance(m, dict) and "id" in m and isinstance(m.get("level"), int)
    }
    return learned, complete, benches


def snapshot_to_json(s: PlayerSnapshot) -> dict:
    return {
        "synced_at": s.synced_at.isoformat() if s.synced_at else None,
        "fetched_at": s.fetched_at.isoformat(),
        "owned": s.owned,
        "needs": [[n.item_id, n.needed, n.kind, n.label] for n in s.needs],
        "keep_amounts": s.keep_amounts,
        "learned_blueprints": sorted(s.learned_blueprints),
        "blueprints_complete": s.blueprints_complete,
        "bench_levels": s.bench_levels,
    }


def snapshot_from_json(d: dict) -> PlayerSnapshot:
    fetched = _parse_dt(d["fetched_at"])
    if fetched is None:
        msg = "bad fetched_at"
        raise ValueError(msg)
    return PlayerSnapshot(
        synced_at=_parse_dt(d.get("synced_at")),
        fetched_at=fetched,
        owned={str(k): int(v) for k, v in d["owned"].items()},
        needs=tuple(
            Need(str(i), int(n), str(k), str(lb)) for i, n, k, lb in d["needs"]
        ),
        keep_amounts={str(k): int(v) for k, v in d["keep_amounts"].items()},
        learned_blueprints=frozenset(str(x) for x in d["learned_blueprints"]),
        blueprints_complete=bool(d["blueprints_complete"]),
        bench_levels={str(k): int(v) for k, v in d["bench_levels"].items()},
    )


def save_snapshot(s: PlayerSnapshot, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot_to_json(s), f)
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


def load_snapshot(path: Path) -> PlayerSnapshot | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return snapshot_from_json(data)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        logger.warning("Ignoring unreadable ARC Tracker snapshot cache %s", path)
        return None


def format_age(synced_at: datetime | None, now: datetime) -> tuple[str, bool]:
    if synced_at is None:
        return "⟳ —", False
    secs = max(0.0, (now - synced_at).total_seconds())
    if secs < 3600:
        label = f"{int(secs // 60)}m"
    elif secs < 48 * 3600:
        label = f"{int(secs // 3600)}h"
    else:
        label = f"{int(secs // 86400)}d"
    return f"⟳ {label}", secs > STALE_AFTER_SECONDS
