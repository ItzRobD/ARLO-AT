import asyncio
import json
import types

import httpx2
import pytest

from arc_helper.arctracker import mcp_client
from arc_helper.arctracker.mcp_client import ArcTrackerAuthError
from arc_helper.arctracker.mcp_client import ArcTrackerClient
from arc_helper.arctracker.mcp_client import ArcTrackerError

KEY = "FAKEKEY-not-a-real-one"
URL = "https://mcp.example.invalid/mcp"


class FakeSession:
    """call_tool returning canned pages; `pages(tool, args)` builds each one."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def call_tool(self, tool, args):
        self.calls.append((tool, dict(args)))
        text = json.dumps(self.pages(tool, args))
        return types.SimpleNamespace(
            is_error=False, content=[types.SimpleNamespace(text=text)]
        )


def _collect(pages):
    session = FakeSession(pages)
    try:
        raw = asyncio.run(mcp_client._collect(session))  # noqa: SLF001
    except ArcTrackerError as exc:
        exc.session = session
        raise
    return raw, session


def _ok(tool, args):
    if tool == "get_my_inventory":
        off = args["offset"]
        return {"items": [], "truncated": off == 0, "nextOffset": 100}
    if tool == "get_my_needs":
        return {"groups": []} if args["offset"] else {"more": {"nextOffset": 30}}
    if tool == "get_my_tracking":
        return {"entries": [{"id": f"t{args['offset']}"}]}
    return {}


def test_collect_pages_within_budget():
    raw, session = _collect(_ok)
    assert len(raw["inventory"]) == 2
    assert len(raw["needs"]) == 2
    assert raw["tracking"] == {"entries": [{"id": "t0"}]}
    assert len(session.calls) == 7
    limits = {tool: args["limit"] for tool, args in session.calls if "limit" in args}
    assert limits == {
        "get_my_inventory": 100,
        "get_my_needs": 30,
        "get_my_tracking": 50,
    }


def test_budget_and_pages_are_consistent():
    assert mcp_client.MAX_CALLS == 20
    # one tool paging to MAX_PAGES still leaves a call for each other tool
    assert mcp_client.MAX_PAGES + 4 <= mcp_client.MAX_CALLS


def test_large_account_fits_budget():
    """~300 stash items (3 pages), 4 needs pages, 2 tracking pages: 11 calls."""

    def pages(tool, args):
        off = args.get("offset", 0)
        if tool == "get_my_inventory":
            return {"truncated": off < 200, "nextOffset": off + 100}
        if tool == "get_my_needs":
            return {"more": {"nextOffset": off + 30}} if off < 90 else {}
        if tool == "get_my_tracking":
            return {"entries": [], "more": {"nextOffset": 50}} if off == 0 else {}
        return {}

    raw, session = _collect(pages)
    assert (len(raw["inventory"]), len(raw["needs"])) == (3, 4)
    assert len(session.calls) == 11


def test_call_budget_enforced_when_always_truncated():
    def pages(tool, args):
        off = args.get("offset", 0)
        if tool == "get_my_inventory":  # 5 pages, each under MAX_PAGES
            return {"truncated": off < 400, "nextOffset": off + 100}
        return {"more": {"nextOffset": off + 30}}  # every paged tool never ends

    with pytest.raises(ArcTrackerError, match="tool calls") as info:
        _collect(pages)
    assert len(info.value.session.calls) == mcp_client.MAX_CALLS


def test_max_pages_while_truncated_is_an_error(monkeypatch):
    monkeypatch.setattr(mcp_client, "MAX_PAGES", 3)

    def pages(_tool, args):
        return {"truncated": True, "nextOffset": args["offset"] + 100}

    with pytest.raises(ArcTrackerError, match="after 3 pages"):
        _collect(pages)


@pytest.mark.parametrize("bad", [None, "200", True, 0])
def test_truncated_without_usable_next_offset_is_an_error(bad):
    def pages(tool, _args):
        page = {"truncated": True}
        if bad is not None:
            page["nextOffset"] = bad
        return page if tool == "get_my_inventory" else {}

    with pytest.raises(ArcTrackerError, match="nextOffset"):
        _collect(pages)


def test_more_without_next_offset_is_an_error():
    def pages(tool, _args):
        if tool == "get_my_needs":
            return {"more": {"groups": 3}}
        return {}

    with pytest.raises(ArcTrackerError, match="get_my_needs"):
        _collect(pages)


def _client(handler):
    return ArcTrackerClient(KEY, URL, transport=httpx2.MockTransport(handler))


def _chain(exc):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


@pytest.mark.parametrize(
    ("status", "www_auth", "auth"),
    [
        (401, 'Bearer error="invalid_token"', True),
        (401, "", True),
        (403, 'Bearer error="invalid_token"', True),
        (403, 'Bearer error="insufficient_scope"', False),
        (403, "", False),
    ],
)
def test_http_status_mapping(status, www_auth, auth):
    def handler(request):
        assert request.headers["authorization"] == f"Bearer {KEY}"
        headers = {"www-authenticate": www_auth} if www_auth else {}
        return httpx2.Response(status, headers=headers, text="denied")

    with pytest.raises(ArcTrackerError) as info:
        _client(handler).fetch_raw()
    assert isinstance(info.value, ArcTrackerAuthError) is auth
    assert str(status) in str(info.value)


def test_transport_error_with_key_is_redacted():
    def handler(request):
        msg = f"proxy refused {request.headers['authorization']} ({KEY})"
        raise RuntimeError(msg)

    client = _client(handler)
    assert KEY not in repr(client)
    with pytest.raises(ArcTrackerError) as info:
        client.fetch_raw()
    err = info.value
    assert not isinstance(err, ArcTrackerAuthError)
    assert "***" in str(err)
    assert err.__cause__ is None
    assert err.__context__ is None
    for e in _chain(err):
        assert KEY not in str(e)
        assert KEY not in repr(e)
        assert KEY not in repr(e.args)
