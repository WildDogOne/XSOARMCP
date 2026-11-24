"""Server-level tests: the MCP tool list and end-to-end tool calls against a fake tenant.

build_server() is wired to an httpx2.MockTransport instead of a live XSOAR tenant.
"""

from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client

import xsoar_mcp.server as server_module
from xsoar_mcp.auth import build_http_client

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
