"""Unit tests for the shared XSOAR HTTP client (auth.build_http_client / XsoarApiClient).

All tests run against httpx2.MockTransport - no live tenant or credentials needed.
"""

from __future__ import annotations

import httpx2
import pytest

from xsoar_mcp.auth import PublicApiRouteUnavailableError, XsoarApiClient, build_http_client

from conftest import RecordingMockTransport


def make_client(handler) -> tuple[XsoarApiClient, RecordingMockTransport]:
    transport = RecordingMockTransport(handler)
    client = build_http_client(transport=transport)
    assert isinstance(client, XsoarApiClient)
    return client, transport


async def test_bodyless_post_gets_json_content_type_and_empty_object_body():
    """XSOAR answers a POST without Content-Type with 415; FastMCP sends search tools called
    without body arguments as bodyless POSTs, so the client must add header + `{}` body."""
    client, transport = make_client(
        lambda req: httpx2.Response(200, json={"total": 0, "scripts": []})
    )
    async with client:
        response = await client.post("/automation/search")
    assert response.status_code == 200
    request = transport.requests[0]
    assert request.headers["content-type"] == "application/json"
    assert request.content == b"{}"
    assert request.headers["content-length"] == "2"


async def test_post_with_json_body_keeps_content_type_and_body():
    client, transport = make_client(lambda req: httpx2.Response(200, json={}))
    async with client:
        await client.post("/investigations/search", json={"size": 5})
    request = transport.requests[0]
    assert request.headers["content-type"] == "application/json"
    assert request.content == b'{"size":5}'


async def test_multipart_upload_content_type_is_untouched():
    client, transport = make_client(lambda req: httpx2.Response(200, json={}))
    async with client:
        await client.post("/incident/file/upload", files={"file": ("a.txt", b"hello")})
    request = transport.requests[0]
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b"hello" in request.content


async def test_explicit_content_type_header_is_not_overwritten():
    client, transport = make_client(lambda req: httpx2.Response(200))
    async with client:
        await client.post(
            "/x", content=b"raw", headers={"Content-Type": "application/xml"}
        )
    request = transport.requests[0]
    assert request.headers["content-type"] == "application/xml"
    assert request.content == b"raw"


async def test_get_requests_are_not_given_a_body():
    client, transport = make_client(lambda req: httpx2.Response(200, json={}))
    async with client:
        await client.get("/incidentfields/associatedTypes/Incident")
    request = transport.requests[0]
    assert "content-type" not in request.headers
    assert request.content == b""


async def test_303_raises_clear_error_and_is_not_followed():
    """XSOAR Cloud answers routes the public API does not serve with 303 -> /#/404 (HTML)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(303, headers={"location": "/#/404"}, text="<html>404</html>")

    client, transport = make_client(handler)
    async with client:
        with pytest.raises(PublicApiRouteUnavailableError) as excinfo:
            await client.get("/incident/fields")
    message = str(excinfo.value)
    assert "303" in message
    assert "/xsoar/public/v1/incident/fields" in message
    assert "not available on the XSOAR public API" in message
    # The redirect must not be followed: exactly one request reached the transport.
    assert len(transport.requests) == 1


async def test_redirect_response_never_reaches_the_caller_even_if_following():
    client, transport = make_client(
        lambda req: httpx2.Response(302, headers={"location": "/somewhere"})
    )
    async with client:
        with pytest.raises(PublicApiRouteUnavailableError):
            await client.post("/any/route", json={})


async def test_advanced_auth_headers_are_still_signed_per_request():
    client, transport = make_client(lambda req: httpx2.Response(200, json={}))
    async with client:
        await client.post("/x", json={})
        await client.post("/x", json={})
    first, second = transport.requests
    for request in (first, second):
        assert request.headers["authorization"]
        assert request.headers["x-xdr-nonce"]
        assert request.headers["x-xdr-timestamp"]
    assert first.headers["x-xdr-nonce"] != second.headers["x-xdr-nonce"]
