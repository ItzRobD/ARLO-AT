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

MAX_PAGES = 6  # per paged tool; guards against a server that never stops paging
MAX_CALLS = 10  # tool calls per refresh, paging included (5 are fixed)
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


def _auth_status(responses: list[tuple[int, str]]) -> int | None:
    """401 always means a bad key; 403 only when the server says the token is
    invalid (any other 403 is treated as transient)."""
    for status, www_auth in responses:
        if status == 401 or (status == 403 and "invalid_token" in www_auth):
            return status
    return None


class ArcTrackerClient:
    def __init__(self, api_key: str, url: str, *, transport: Any = None):
        self._key = api_key
        self.url = url
        self._transport = transport  # tests: an httpx2.MockTransport

    def __repr__(self) -> str:
        return f"ArcTrackerClient(url={self.url!r})"

    def fetch_raw(self) -> dict[str, Any]:
        """One MCP session; returns the raw tool results (see service.py)."""
        responses: list[tuple[int, str]] = []
        try:
            return asyncio.run(self._fetch(responses))
        except Exception as exc:  # noqa: BLE001 - every failure becomes ArcTrackerError
            error_cls, msg = self._classify(exc, responses)
        # Raised outside the except block so the original exception (which may
        # quote the key) is neither the __cause__ nor the __context__.
        raise error_cls(msg)

    def _classify(
        self, exc: Exception, responses: list[tuple[int, str]]
    ) -> tuple[type[ArcTrackerError], str]:
        auth = _auth_status(responses)
        if auth is not None:
            return (
                ArcTrackerAuthError,
                f"ARC Tracker rejected the API key (HTTP {auth})",
            )
        own = _find_own(exc)
        if own is not None:
            return type(own), redact(str(own), self._key)
        failed = [s for s, _ in responses if s >= 400]
        http = f" (HTTP {failed[0]})" if failed else ""
        text = f"ARC Tracker request failed{http}: {_describe(exc)}"
        return ArcTrackerError, redact(text, self._key)

    async def _fetch(self, responses: list[tuple[int, str]]) -> dict[str, Any]:
        async def on_response(response: httpx2.Response) -> None:  # noqa: RUF029 - httpx2 async hook
            www_auth = response.headers.get("www-authenticate", "")
            responses.append((response.status_code, www_auth))

        http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=httpx2.Timeout(TIMEOUT_S),
            event_hooks={"response": [on_response]},
            transport=self._transport,
        )
        with anyio.fail_after(TOTAL_TIMEOUT_S):
            async with (
                http,
                streamable_http_client(self.url, http_client=http) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                return await _collect(session)


class _Budget:
    def __init__(self, calls: int):
        self.left = calls

    def spend(self, tool: str) -> None:
        if self.left <= 0:
            msg = (
                f"more than {MAX_CALLS} tool calls needed (at {tool}); refresh aborted"
            )
            raise ArcTrackerError(msg)
        self.left -= 1


async def _call(session: Any, budget: _Budget, tool: str, args: dict[str, Any]) -> dict:
    budget.spend(tool)
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


def _next_offset(tool: str, page: dict, offset: int) -> int | None:
    """Offset of the next page, None on the last page. A page that says there
    is more without a usable offset is an error, never silently partial data."""
    if tool == "get_my_inventory":
        if not page.get("truncated"):
            return None
        nxt = page.get("nextOffset")
    else:
        more = page.get("more")
        if more is None:
            return None
        nxt = more.get("nextOffset") if isinstance(more, dict) else None
    if isinstance(nxt, bool) or not isinstance(nxt, int) or nxt <= offset:
        msg = f"{tool}: more data at offset {offset} but no usable nextOffset ({nxt!r})"
        raise ArcTrackerError(msg)
    return nxt


async def _pages(
    session: Any, budget: _Budget, tool: str, args: dict[str, Any]
) -> list[dict]:
    pages: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        page = await _call(session, budget, tool, {**args, "offset": offset})
        pages.append(page)
        nxt = _next_offset(tool, page, offset)
        if nxt is None:
            return pages
        offset = nxt
    msg = f"{tool}: still more data after {MAX_PAGES} pages"
    raise ArcTrackerError(msg)


async def _collect(session: Any) -> dict[str, Any]:
    budget = _Budget(MAX_CALLS)
    inventory = await _pages(session, budget, "get_my_inventory", {"limit": 100})
    needs = await _pages(session, budget, "get_my_needs", {"limit": 30})
    tracking = await _pages(session, budget, "get_my_tracking", {"limit": 50})
    entries = [e for page in tracking for e in page.get("entries") or []]
    blueprints = await _call(
        session, budget, "get_my_progress", {"section": "blueprints"}
    )
    workshop = await _call(session, budget, "get_my_progress", {"section": "workshop"})
    return {
        "inventory": inventory,
        "needs": needs,
        "tracking": {"entries": entries},
        "progress_blueprints": blueprints,
        "progress_workshop": workshop,
    }
