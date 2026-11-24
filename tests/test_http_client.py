"""Unit tests for the shared XSOAR HTTP client (auth.build_http_client / XsoarApiClient).

All tests run against httpx2.MockTransport - no live tenant or credentials needed - except the
real-socket framing regression test, which talks to a local stdlib http.server.
"""

from __future__ import annotations

import hashlib
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


async def test_303_without_location_header_still_reports_route_unavailable():
    """The live tenant's 303 for unserved routes carries no Location header."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(303, text="<html>404</html>")

    client, _ = make_client(handler)
    async with client:
        with pytest.raises(PublicApiRouteUnavailableError) as excinfo:
            await client.post("/investigations/search", json={"size": 5})
    assert "not available on the XSOAR public API" in str(excinfo.value)


async def test_non_redirect_3xx_gets_generic_message_not_route_hint():
    """A 304 is not a 'route not served' signal; the route-unavailable hint must not claim it."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(304)

    client, _ = make_client(handler)
    async with client:
        with pytest.raises(PublicApiRouteUnavailableError) as excinfo:
            await client.post("/x", json={})
    message = str(excinfo.value)
    assert "304" in message
    assert "not available on the XSOAR public API" not in message


async def test_follow_redirects_true_is_still_not_followed():
    """The client forces follow_redirects=False even when the caller asks for redirects."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "/somewhere"})

    client, transport = make_client(handler)
    async with client:
        with pytest.raises(PublicApiRouteUnavailableError):
            await client.post("/any/route", json={}, follow_redirects=True)
    assert len(transport.requests) == 1


async def test_advanced_auth_headers_are_still_signed_per_request():
    """The advanced-key signature must be exactly sha256(api_key + nonce + timestamp), and
    signing must also apply to requests whose body was injected by build_request."""
    client, transport = make_client(lambda req: httpx2.Response(200, json={}))
    async with client:
        await client.post("/x", json={})
        await client.post("/x")  # bodyless: the injected-body path
    api_key = os.environ["XSOAR_API_KEY"]
    for request in transport.requests:
        nonce = request.headers["x-xdr-nonce"]
        timestamp = request.headers["x-xdr-timestamp"]
        expected = hashlib.sha256(f"{api_key}{nonce}{timestamp}".encode("utf-8")).hexdigest()
        assert request.headers["authorization"] == expected
        assert request.headers["x-xdr-auth-id"] == os.environ["XSOAR_API_KEY_ID"]
    assert (
        transport.requests[0].headers["x-xdr-nonce"] != transport.requests[1].headers["x-xdr-nonce"]
    )


async def test_injected_body_clears_stale_content_length_from_fastmcp_headers():
    """Regression for the FastMCP request path (openapi/components.py:207).

    FastMCP rebuilds requests with headers=list(directed_request.headers.raw), which already
    carries 'content-length: 0' for a bodyless directed request. Without stripping it, the
    injected b'{}' body goes out under Content-Length: 0 and h11 aborts the connection with
    'Too much data for declared Content-Length'.
    """
    client, transport = make_client(lambda req: httpx2.Response(200, json={}))
    async with client:
        # Exactly the kwargs FastMCP's OpenAPI tool passes for a no-arg search tool.
        request = client.build_request(
            "POST",
            "/automation/search",
            params=None,
            headers=[(b"content-length", b"0"), (b"accept", b"*/*")],
            content=b"",
        )
        assert request.headers["content-type"] == "application/json"
        assert request.content == b"{}"
        assert request.headers["content-length"] == str(len(request.content))
        await client.send(request)
    sent = transport.requests[0]
    assert sent.headers["content-length"] == "2"
    assert sent.content == b"{}"


async def test_injected_body_arrives_over_a_real_socket():
    """MockTransport never enforces HTTP framing, so prove framing against a real socket."""

    received: dict = {}

    class EchoHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", 0))
            received["body"] = self.rfile.read(length)
            received["content_type"] = self.headers.get("content-type")
            payload = b'{"ok": true}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:  # keep pytest output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = XsoarApiClient(base_url=f"http://127.0.0.1:{server.server_port}/xsoar/public/v1")
        async with client:
            request = client.build_request(
                "POST",
                "/automation/search",
                headers=[(b"content-length", b"0")],  # stale header from FastMCP's rebuild
                content=b"",
            )
            response = await client.send(request)
        assert response.status_code == 200
    finally:
        server.shutdown()
        server.server_close()
    assert received["body"] == b"{}"
    assert received["content_type"] == "application/json"
