"""Server-level tests: the MCP tool list and end-to-end tool calls against a fake tenant.

build_server() is wired to an httpx2.MockTransport instead of a live XSOAR tenant.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from fastmcp import Client

import xsoar_mcp.server as server_module
from xsoar_mcp.auth import build_http_client
from xsoar_mcp.openapi import (
    OverlayCollisionError,
    load_generated,
    load_merged,
    load_overlay,
    merge,
)
from xsoar_mcp.toolsets import classification_drift

from conftest import RecordingMockTransport


@pytest.fixture
def mock_server(ok_json_handler):
    """The real generated server, with its HTTP client pointed at a mock transport."""
    transport = RecordingMockTransport(ok_json_handler)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        server_module, "build_http_client", lambda: build_http_client(transport=transport)
    )
    yield server_module.build_server(), transport
    monkey.undo()


async def test_routes_unavailable_on_public_api_are_not_registered(mock_server):
    server, _ = mock_server
    names = {tool.name for tool in await server.list_tools()}
    assert "searchInvestigations" not in names
    assert "getIncidentsFieldsByIncidentType" not in names
    # neighbouring tools in the same toolsets must survive
    assert "searchIncidents" in names
    assert "searchEvidence" in names
    assert "getAutomationScripts" in names


async def test_removed_tools_cannot_be_called_by_name(mock_server):
    server, transport = mock_server
    async with Client(server) as client:
        with pytest.raises(Exception, match="searchInvestigations"):
            await client.call_tool("searchInvestigations", {"size": 5})
        with pytest.raises(Exception, match="getIncidentsFieldsByIncidentType"):
            await client.call_tool("getIncidentsFieldsByIncidentType", {"type": "Incident"})
    assert transport.requests == []


async def test_search_tool_called_without_body_args_sends_json_post(mock_server):
    """Regression for the live 415: getAutomationScripts called with no body arguments."""
    server, transport = mock_server
    async with Client(server) as client:
        result = await client.call_tool("getAutomationScripts", {})
    assert not result.is_error
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/automation/search")
    assert request.headers["content-type"] == "application/json"
    assert request.content == b"{}"


async def test_search_tool_with_body_args_sends_json_post(mock_server):
    server, transport = mock_server
    async with Client(server) as client:
        result = await client.call_tool("searchIncidents", {"filter": {"size": 10}})
    assert not result.is_error
    request = transport.requests[0]
    assert request.headers["content-type"] == "application/json"
    assert b'"size"' in request.content


async def test_303_surfaces_as_clear_tool_error_not_raw_redirect(ok_json_handler):
    """A route the public API does not serve must raise an actionable error, not HTML noise."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(303, headers={"location": "/#/404"}, text="<html>404</html>")

    transport = RecordingMockTransport(handler)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        server_module, "build_http_client", lambda: build_http_client(transport=transport)
    )
    try:
        server = server_module.build_server()
        async with Client(server) as client:
            with pytest.raises(Exception) as excinfo:
                await client.call_tool("searchIncidents", {"filter": {"size": 10}})
        message = str(excinfo.value)
        assert "303" in message
        assert "/xsoar/public/v1/incidents/search" in message
        assert "not available on the XSOAR public API" in message
        assert "<html>" not in message
    finally:
        monkey.undo()


# --- playbook read tools (overlay ops: POST /playbook/search, GET /playbook/{playbookId}) --------


def _result_json(result) -> dict:
    return json.loads(next(c.text for c in result.content if hasattr(c, "text")))


def _server_with(handler, monkeypatch):
    """build_server() wired to a mock transport; the monkeypatch fixture reverts the client."""
    transport = RecordingMockTransport(handler)
    monkeypatch.setattr(
        server_module, "build_http_client", lambda: build_http_client(transport=transport)
    )
    return server_module.build_server(), transport


_FULL_PLAYBOOK = {
    "id": "pb-1",
    "name": "Playbook Under Test",
    "version": 49,
    "packName": "",
    "modified": "2024-01-01T00:00:00Z",
    "sizeInBytes": 1234,
    "tasks": {"0": {"taskId": "x"}, "1": {"taskId": "y"}},
    "inputs": [{"key": "Email"}],
}


def _search_handler(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json={"playbooks": [_FULL_PLAYBOOK], "tags": ["xdr"], "total": 1})


async def test_playbook_tools_are_registered(mock_server):
    server, _ = mock_server
    names = {tool.name for tool in await server.list_tools()}
    assert {"searchPlaybooks", "getPlaybookById"} <= names


async def test_search_playbooks_sends_json_post_and_trims_records(ok_json_handler, monkeypatch):
    """searchPlaybooks posts to /playbook/search and projects the bulky playbook objects down to
    the compact default fields (the per-record task graph must be projected away)."""
    server, transport = _server_with(_search_handler, monkeypatch)
    async with Client(server) as client:
        result = await client.call_tool("searchPlaybooks", {"query": "Playbook Under"})
    assert not result.is_error
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/xsoar/public/v1/playbook/search")
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"query": "Playbook Under"}
    payload = _result_json(result)
    assert payload["total"] == 1
    record = payload["playbooks"][0]
    assert "tasks" not in record and "inputs" not in record
    assert record == {
        "id": "pb-1",
        "name": "Playbook Under Test",
        "version": 49,
        "packName": "",
        "modified": "2024-01-01T00:00:00Z",
        "sizeInBytes": 1234,
    }


async def test_search_playbooks_fields_passthrough_and_custom(monkeypatch):
    """fields=["*"] passes the full record through (task graph kept); a custom list trims to it."""
    server, _ = _server_with(_search_handler, monkeypatch)
    async with Client(server) as client:
        star = await client.call_tool("searchPlaybooks", {"query": "x", "fields": ["*"]})
        custom = await client.call_tool("searchPlaybooks", {"query": "x", "fields": ["id", "name"]})
    assert not star.is_error and not custom.is_error
    assert "tasks" in _result_json(star)["playbooks"][0]
    assert _result_json(custom)["playbooks"][0] == {
        "id": "pb-1",
        "name": "Playbook Under Test",
    }


async def test_get_playbook_by_id_substitutes_path_param(ok_json_handler, monkeypatch):
    """getPlaybookById issues GET /playbook/{id} with the id placed in the path (spaces encoded)."""
    server, transport = _server_with(ok_json_handler, monkeypatch)
    async with Client(server) as client:
        result = await client.call_tool(
            "getPlaybookById", {"playbookId": "Cortex XDR - Isolate Endpoint"}
        )
    assert not result.is_error
    request = transport.requests[0]
    assert request.method == "GET"
    # httpx percent-encodes the path segment on the wire (raw_path); .path gives the decoded form.
    assert request.url.path.endswith("/xsoar/public/v1/playbook/Cortex XDR - Isolate Endpoint")
    assert b"Cortex%20XDR%20-%20Isolate%20Endpoint" in request.url.raw_path


async def test_get_playbook_by_id_encodes_reserved_path_chars(ok_json_handler, monkeypatch):
    """A playbook id containing '/' and '..' must be percent-encoded so it cannot escape the path
    segment (no traversal): '/' -> %2F and '.' -> %2E in the wire path."""
    server, transport = _server_with(ok_json_handler, monkeypatch)
    async with Client(server) as client:
        result = await client.call_tool("getPlaybookById", {"playbookId": "a/b../c"})
    assert not result.is_error
    raw = transport.requests[0].url.raw_path
    assert b"%2F" in raw and b"%2E" in raw
    assert raw.endswith(b"/playbook/a%2Fb%2E%2E%2Fc")


async def test_playbook_tools_survive_read_only_mode(ok_json_handler, monkeypatch):
    """Both playbook tools are reads, so XSOAR_READ_ONLY=true keeps them registered."""
    monkeypatch.setenv("XSOAR_READ_ONLY", "true")
    server, _ = _server_with(ok_json_handler, monkeypatch)
    names = {tool.name for tool in await server.list_tools()}
    assert {"searchPlaybooks", "getPlaybookById"} <= names


async def test_playbook_tools_toolset_filtering(ok_json_handler, monkeypatch):
    """XSOAR_TOOLSETS=content keeps both; =incidents drops them (but keeps searchIncidents)."""
    monkeypatch.setenv("XSOAR_TOOLSETS", "content")
    server, _ = _server_with(ok_json_handler, monkeypatch)
    names = {tool.name for tool in await server.list_tools()}
    assert {"searchPlaybooks", "getPlaybookById"} <= names

    monkeypatch.setenv("XSOAR_TOOLSETS", "incidents")
    server2, _ = _server_with(ok_json_handler, monkeypatch)
    names2 = {tool.name for tool in await server2.list_tools()}
    assert {"searchPlaybooks", "getPlaybookById"}.isdisjoint(names2)
    assert "searchIncidents" in names2


# --- overlay merge integrity ---------------------------------------------------------------------


def _operation_ids(paths: dict) -> set[str]:
    return {
        op["operationId"]
        for methods in paths.values()
        for op in methods.values()
        if isinstance(op, dict) and "operationId" in op
    }


def test_no_overlay_collision_and_no_classification_drift():
    """The overlay shares no path or operationId with the generated spec, and the merged spec has
    no classification drift (every operation, overlay included, is classified in toolsets.py)."""
    gen_paths = load_generated().get("paths", {})
    ovl_paths = load_overlay().get("paths", {})
    assert set(ovl_paths) & set(gen_paths) == set()
    assert _operation_ids(ovl_paths) & _operation_ids(gen_paths) == set()
    unclassified, stale = classification_drift(load_merged())
    assert unclassified == [] and stale == []


def test_merge_raises_on_operationid_collision():
    """merge() refuses to silently shadow a generated operationId."""
    spec = {"paths": {"/x": {"post": {"operationId": "searchPlaybooks"}}}}
    overlay = {"paths": {"/y": {"post": {"operationId": "searchPlaybooks"}}}}
    try:
        merge(spec, overlay)
    except OverlayCollisionError as exc:
        assert "searchPlaybooks" in str(exc)
        assert spec["paths"] == {"/x": {"post": {"operationId": "searchPlaybooks"}}}  # untouched
    else:
        raise AssertionError("merge() should have raised OverlayCollisionError")
