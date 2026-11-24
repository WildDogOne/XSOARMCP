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


# --- playbook execution tool (overlay op: GET /inv-playbook/{investigationId}) --------------------


_FULL_EXECUTION = {
    "id": "999001",
    "playbookId": "Cortex XDR - Isolate Endpoint",
    "state": "completed",
    "startTaskId": "0",
    "startDate": "2026-11-24T10:00:00Z",
    "modified": "2026-11-24T10:01:02Z",
    "netDurationMs": 62_000,
    "runIndex": 3,
    "pbHistory": [{"playbookId": "old-version"}],
    "Dirty": False,
    "view": {"Root": {"base": 0, "obj": {"0": {"style": {"shape": "rectangle"}}}}},
    "tasks": {
        "0": {
            "id": "0",
            "taskId": "0",
            "type": "start",
            "state": "Completed",
            "startDate": "0001-01-01T00:00:00Z",
            "completedDate": "2026-11-24T10:00:00Z",
            "completedBy": "DBot",
            "dueDate": "",
            "hasErrorEntries": False,
            "retryCount": 0,
            # live shape: nextTasks is a map of branch label -> next task ids, never a list
            "nextTasks": {"#none#": ["1"]},
            "task": {"name": "Start", "script": "", "taskparams": {"a": [1, 2, 3]}},
            "evidenceData": {"numberofevidence": 0},
            "view": {"id": "0"},
        },
        "1": {
            "id": "1",
            "taskId": "1",
            "type": "condition",
            "state": "Completed",
            "startDate": "2026-11-24T10:00:01Z",
            "completedDate": "2026-11-24T10:00:02Z",
            "completedBy": "DBot",
            "hasErrorEntries": False,
            "retryCount": 0,
            "nextTasks": {"#default#": ["2"], "yes": ["4"]},
            "task": {"name": "Is XDR incident high severity?", "type": "condition", "conditions": []},
        },
        "3": {
            "id": "3",
            "taskId": "3",
            "type": "regular",
            "state": "WillNotBeExecuted",
            # live shape: a task that ends the flow (or never runs) carries nextTasks: null
            "nextTasks": None,
            "task": {"name": "xdr-host-isolate-endpoint", "script": "xdr-host-isolate-endpoint"},
        },
    },
}


def _execution_handler(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=_FULL_EXECUTION)


async def test_playbook_execution_tool_is_registered(mock_server):
    server, _ = mock_server
    names = {tool.name for tool in await server.list_tools()}
    assert "getPlaybookExecution" in names


async def test_get_playbook_execution_substitutes_path_param(ok_json_handler, monkeypatch):
    """GET /inv-playbook/{id}: the investigation id goes into the path, never the query."""
    server, transport = _server_with(ok_json_handler, monkeypatch)
    async with Client(server) as client:
        result = await client.call_tool("getPlaybookExecution", {"investigationId": "999001"})
    assert not result.is_error
    request = transport.requests[0]
    assert request.method == "GET"
    assert request.url.path.endswith("/xsoar/public/v1/inv-playbook/999001")
    assert not request.url.query


async def test_get_playbook_execution_encodes_reserved_path_chars(ok_json_handler, monkeypatch):
    """An investigation id with '/' or '..' must be percent-encoded so it stays one path segment."""
    server, transport = _server_with(ok_json_handler, monkeypatch)
    async with Client(server) as client:
        result = await client.call_tool("getPlaybookExecution", {"investigationId": "11/2../3"})
    assert not result.is_error
    raw = transport.requests[0].url.raw_path
    assert b"%2F" in raw and b"%2E" in raw
    assert raw.endswith(b"/inv-playbook/11%2F2%2E%2E%2F3")


async def test_get_playbook_execution_trims_run_and_tasks(monkeypatch):
    """The run's metadata and every entry of the tasks map are trimmed to the compact defaults:
    runIndex/pbHistory/view/Dirty and each task's taskId/dueDate/evidenceData/view/task body go."""
    server, _ = _server_with(_execution_handler, monkeypatch)
    async with Client(server) as client:
        result = await client.call_tool("getPlaybookExecution", {"investigationId": "999001"})
    assert not result.is_error
    payload = _result_json(result)
    assert payload == {
        "id": "999001",
        "playbookId": "Cortex XDR - Isolate Endpoint",
        "state": "completed",
        "startTaskId": "0",
        "startDate": "2026-11-24T10:00:00Z",
        "modified": "2026-11-24T10:01:02Z",
        "netDurationMs": 62_000,
        "tasks": {
            "0": {
                "id": "0",
                "type": "start",
                "state": "Completed",
                "startDate": "0001-01-01T00:00:00Z",
                "completedDate": "2026-11-24T10:00:00Z",
                "completedBy": "DBot",
                "hasErrorEntries": False,
                "retryCount": 0,
                "nextTasks": {"#none#": ["1"]},
                "task": {"name": "Start"},
            },
            "1": {
                "id": "1",
                "type": "condition",
                "state": "Completed",
                "startDate": "2026-11-24T10:00:01Z",
                "completedDate": "2026-11-24T10:00:02Z",
                "completedBy": "DBot",
                "hasErrorEntries": False,
                "retryCount": 0,
                "nextTasks": {"#default#": ["2"], "yes": ["4"]},
                "task": {"name": "Is XDR incident high severity?"},
            },
            "3": {
                "id": "3",
                "type": "regular",
                "state": "WillNotBeExecuted",
                "nextTasks": None,
                "task": {"name": "xdr-host-isolate-endpoint"},
            },
        },
    }


async def test_get_playbook_execution_fields_passthrough_and_custom(monkeypatch):
    """fields=["*"] returns the run untrimmed; tasks.-prefixed names pick per-task fields and bare
    names pick the run's own fields."""
    server, _ = _server_with(_execution_handler, monkeypatch)
    async with Client(server) as client:
        star = await client.call_tool(
            "getPlaybookExecution", {"investigationId": "999001", "fields": ["*"]}
        )
        states = await client.call_tool(
            "getPlaybookExecution",
            {"investigationId": "999001", "fields": ["state", "tasks.state", "tasks.task.name"]},
        )
        whole_tasks = await client.call_tool(
            "getPlaybookExecution", {"investigationId": "999001", "fields": ["id", "tasks"]}
        )
    assert not star.is_error and not states.is_error and not whole_tasks.is_error
    assert _result_json(star) == _FULL_EXECUTION
    assert _result_json(states) == {
        "state": "completed",
        "tasks": {
            "0": {"state": "Completed", "task": {"name": "Start"}},
            "1": {"state": "Completed", "task": {"name": "Is XDR incident high severity?"}},
            "3": {"state": "WillNotBeExecuted", "task": {"name": "xdr-host-isolate-endpoint"}},
        },
    }
    assert _result_json(whole_tasks)["tasks"] == _FULL_EXECUTION["tasks"]


async def test_get_playbook_execution_tasks_star_passes_map_through(monkeypatch):
    """fields=["tasks.*"] is the tasks-map equivalent of ["*"]: every entry stays untrimmed."""
    server, _ = _server_with(_execution_handler, monkeypatch)
    async with Client(server) as client:
        result = await client.call_tool(
            "getPlaybookExecution", {"investigationId": "999001", "fields": ["tasks.*"]}
        )
        with_run = await client.call_tool(
            "getPlaybookExecution",
            {"investigationId": "999001", "fields": ["id", "tasks.*"]},
        )
    assert not result.is_error and not with_run.is_error
    assert _result_json(result) == {"tasks": _FULL_EXECUTION["tasks"]}
    assert _result_json(with_run) == {"id": "999001", "tasks": _FULL_EXECUTION["tasks"]}


async def test_get_playbook_execution_omits_tasks_without_task_fields(monkeypatch):
    """Selecting only run-level fields drops the tasks map entirely - it must not come back as one
    empty object per task."""
    server, _ = _server_with(_execution_handler, monkeypatch)
    async with Client(server) as client:
        result = await client.call_tool(
            "getPlaybookExecution",
            {"investigationId": "999001", "fields": ["state", "playbookId", "netDurationMs"]},
        )
    assert not result.is_error
    assert _result_json(result) == {
        "state": "completed",
        "playbookId": "Cortex XDR - Isolate Endpoint",
        "netDurationMs": 62_000,
    }


async def test_get_playbook_execution_unknown_investigation_is_empty_object(monkeypatch):
    """An id with no playbook run answers 200 with {} (verified live) - the tool passes that through
    instead of inventing keys, so 'no run' stays distinguishable."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={})

    server, transport = _server_with(handler, monkeypatch)
    async with Client(server) as client:
        result = await client.call_tool("getPlaybookExecution", {"investigationId": "999999999"})
    assert not result.is_error
    assert _result_json(result) == {}
    assert transport.requests[0].url.path.endswith("/xsoar/public/v1/inv-playbook/999999999")


async def test_playbook_execution_survives_read_only_and_investigations_toolset(
    ok_json_handler, monkeypatch
):
    """It is a read tool in the `investigations` toolset: read-only mode and XSOAR_TOOLSETS=
    investigations keep it, XSOAR_TOOLSETS=content drops it (the playbook definition tools stay)."""
    monkeypatch.setenv("XSOAR_READ_ONLY", "true")
    server, _ = _server_with(ok_json_handler, monkeypatch)
    names = {tool.name for tool in await server.list_tools()}
    assert "getPlaybookExecution" in names

    monkeypatch.setenv("XSOAR_TOOLSETS", "investigations")
    server2, _ = _server_with(ok_json_handler, monkeypatch)
    names2 = {tool.name for tool in await server2.list_tools()}
    assert "getPlaybookExecution" in names2
    assert {"searchPlaybooks", "getPlaybookById"}.isdisjoint(names2)


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
