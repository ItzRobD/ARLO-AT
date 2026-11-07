"""Synchronous ARC Tracker MCP client: fetches the player's raw personal data.

The API key is only ever placed in the Authorization header. It never appears
in repr(), exception text, or anything this module logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

AUTH_STATUSES = frozenset({401, 403})
MAX_PAGES = 10  # per paged tool; guards against a server that never stops paging
TIMEOUT_S = 30.0
TOTAL_TIMEOUT_S = 120.0


class ArcTrackerError(Exception):
    """Fetching personal data failed (network, server or protocol error)."""


class ArcTrackerAuthError(ArcTrackerError):
    """The server rejected the API key."""


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


def _describe(exc: BaseException) -> str:
    """Leaf messages of a (possibly nested) exception group."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def _find_own(exc: BaseException) -> ArcTrackerError | None:
    """Our own error, possibly wrapped in task-group exception groups."""
    if isinstance(exc, ArcTrackerError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for e in exc.exceptions:
            if (found := _find_own(e)) is not None:
                return found
    return None


class ArcTrackerClient:
    def __init__(self, api_key: str, url: str):
        self._key = api_key
        self.url = url

    def __repr__(self) -> str:
        return f"ArcTrackerClient(url={self.url!r})"

    def fetch_raw(self) -> dict[str, Any]:
        """One MCP session; returns the raw tool results (see service.py)."""
        statuses: list[int] = []
        try:
            return asyncio.run(self._fetch(statuses))
        except Exception as exc:  # noqa: BLE001 - every failure becomes ArcTrackerError
            auth = next((s for s in statuses if s in AUTH_STATUSES), None)
            if auth is not None:
                msg = f"ARC Tracker rejected the API key (HTTP {auth})"
                raise ArcTrackerAuthError(msg) from None
            own = _find_own(exc)
            if own is not None:
                raise type(own)(redact(str(own), self._key)) from None
            msg = redact(f"ARC Tracker request failed: {_describe(exc)}", self._key)
            raise ArcTrackerError(msg) from None

    async def _fetch(self, statuses: list[int]) -> dict[str, Any]:
        async def on_response(response: httpx2.Response) -> None:  # noqa: RUF029 - httpx2 async hook
            statuses.append(response.status_code)

        http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=httpx2.Timeout(TIMEOUT_S),
            event_hooks={"response": [on_response]},
        )
        with anyio.fail_after(TOTAL_TIMEOUT_S):
            async with (
                http,
                streamable_http_client(self.url, http_client=http) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                return await _collect(session)


async def _call(session: ClientSession, tool: str, args: dict[str, Any]) -> dict:
    result = await session.call_tool(tool, args)
    content = getattr(result, "content", None) or []
    text = getattr(content[0], "text", None) if content else None
    if getattr(result, "is_error", False):
        msg = f"{tool} returned an error: {(text or '')[:200]}"
        raise ArcTrackerError(msg)
    if not isinstance(text, str):
        msg = f"{tool} returned no text content"
        raise ArcTrackerError(msg)
    try:
        data = json.loads(text)
    except ValueError:
        msg = f"{tool} returned non-JSON content: {text[:200]}"
        raise ArcTrackerError(msg) from None
    if not isinstance(data, dict):
        msg = f"{tool} returned {type(data).__name__}, expected an object"
        raise ArcTrackerError(msg)
    return data


def _more_offset(page: dict) -> int | None:
    more = page.get("more")
    off = more.get("nextOffset") if isinstance(more, dict) else None
    return off if isinstance(off, int) else None


async def _collect(session: ClientSession) -> dict[str, Any]:
    inventory: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        page = await _call(
            session, "get_my_inventory", {"limit": 100, "offset": offset}
        )
        inventory.append(page)
        nxt = page.get("nextOffset")
        if not page.get("truncated") or not isinstance(nxt, int) or nxt <= offset:
            break
        offset = nxt

    needs: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        page = await _call(session, "get_my_needs", {"limit": 30, "offset": offset})
        needs.append(page)
        nxt = _more_offset(page)
        if nxt is None or nxt <= offset:
            break
        offset = nxt

    tracking = await _call(session, "get_my_tracking", {"limit": 50})
    entries = list(tracking.get("entries") or [])
    offset = 0
    for _ in range(MAX_PAGES - 1):
        nxt = _more_offset(tracking)
        if nxt is None or nxt <= offset:
            break
        offset = nxt
        tracking = await _call(
            session, "get_my_tracking", {"limit": 50, "offset": offset}
        )
        entries.extend(tracking.get("entries") or [])

    blueprints = await _call(session, "get_my_progress", {"section": "blueprints"})
    workshop = await _call(session, "get_my_progress", {"section": "workshop"})
    return {
        "inventory": inventory,
        "needs": needs,
        "tracking": {"entries": entries},
        "progress_blueprints": blueprints,
        "progress_workshop": workshop,
    }
