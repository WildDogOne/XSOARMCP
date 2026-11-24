"""XSOAR API authentication and the shared HTTP client.

Two key types exist, selected when you generate the key in the tenant UI:

- **Standard**: a static "Authorization: {api_key}" header. Simple, no per-request computation.
- **Advanced**: guards against replay by requiring a fresh nonce and timestamp on every request,
  with "Authorization" set to sha256(api_key + nonce + timestamp) instead of the raw key. Since
  those values must be fresh per call (not fixed once on the client), this is implemented as an
  httpx2.Auth subclass rather than static client headers.

Set XSOAR_API_KEY_TYPE=standard|advanced to pick. Defaults to advanced, since that's the type
Palo Alto's own docs frame as the recommended default and a 401 against a set of static
Standard-style headers is the most likely reason to end up reading this file's diff.

The module also builds the shared HTTP client (XsoarApiClient), which fixes two live-tenant
failure modes centrally: XSOAR's 415 on POSTs without Content-Type: application/json, and the
303-to-/#/404 HTML redirect the public API answers for routes it does not serve.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time

import httpx2

XSOAR_API_BASE_PATH = "/xsoar/public/v1"

# Methods that must always carry a JSON body on the XSOAR public API. XSOAR answers a POST
# without "Content-Type: application/json" with HTTP 415 unsupported_media_type, even when the
# endpoint is a read (its search endpoints are all POSTs).
_JSON_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class PublicApiRouteUnavailableError(RuntimeError):
    """Raised when the public API answers a redirect (301/302/303/307/308) instead of the route.

    XSOAR Cloud's public API base path (/xsoar/public/v1) does not serve every route listed in
    Palo Alto's OpenAPI spec - the spec covers the full private API too. An unserved route does
    not 404: the tenant's front-end answers with a 303 to /#/404 and an HTML body, which httpx
    would either surface as a raw redirect error or follow into useless HTML. Raising here gives
    the MCP client an actionable message instead.
    """


def _has_content_type(headers) -> bool:
    if not headers:
        return False
    if isinstance(headers, dict):
        names = headers.keys()
    else:
        names = [entry[0] for entry in headers]
    return any(
        (name.decode("ascii") if isinstance(name, bytes) else name).lower() == "content-type"
        for name in names
    )


class XsoarApiClient(httpx2.AsyncClient):
    """httpx client hardened for the XSOAR public API base path.

    - build_request: POST/PUT/PATCH built without any body and without a Content-Type get
      `json={}` instead, so FastMCP's bodyless search POSTs (tools called with no body
      arguments) don't come back as 415 unsupported_media_type.
    - send: never follows redirects. On the public API a redirect always means "route not
      served here" (303 -> /#/404 with an HTML body), so it raises a clear error instead.
    """

    def build_request(self, method: str, url, **kwargs):
        if method.upper() in _JSON_BODY_METHODS:
            bodyless = all(
                kwargs.get(key) is None or kwargs.get(key) == b""
                for key in ("json", "content", "data", "files")
            )
            if bodyless and not _has_content_type(kwargs.get("headers")):
                kwargs.pop("content", None)
                kwargs["json"] = {}  # httpx then sends Content-Type: application/json + b"{}"
        return super().build_request(method, url, **kwargs)

    async def send(self, request, *args, **kwargs):
        kwargs["follow_redirects"] = False
        response = await super().send(request, *args, **kwargs)
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            raise PublicApiRouteUnavailableError(
                f"HTTP {response.status_code} redirect from {request.method} "
                f"{request.url.raw_path.decode('ascii', 'replace').split('?')[0]} "
                f"(-> {location or 'no Location header'}): this route is not available on the "
                f"XSOAR public API ({XSOAR_API_BASE_PATH}). The bundled OpenAPI spec covers the "
                "full XSOAR API; routes like this are private-API only and are not callable with "
                "an API key."
            )
        return response


def _resolve_base_url(fqdn: str) -> str:
    """Accept either a bare FQDN ("company.crtx.ch.paloaltonetworks.com") or a full URL a user
    copy-pasted from their tenant's console ("https://api-company.crtx.ch.paloaltonetworks.com") -
    the latter is an easy mistake since "FQDN" is ambiguous once you're staring at a URL bar, and
    silently mis-building the base URL from it produces a malformed request that's hard to
    diagnose from the resulting error alone (a proxy or the server just rejects it outright).
    """
    fqdn = fqdn.strip().rstrip("/")
    base = fqdn if fqdn.startswith(("http://", "https://")) else f"https://api-{fqdn}"
    return base if base.endswith(XSOAR_API_BASE_PATH) else base + XSOAR_API_BASE_PATH


class AdvancedKeyAuth(httpx2.Auth):
    """Computes a fresh nonce + timestamp + sha256 signature on every request."""

    def __init__(self, api_key: str, api_key_id: str) -> None:
        self._api_key = api_key
        self._api_key_id = api_key_id

    def _sign(self) -> dict[str, str]:
        nonce = secrets.token_hex(32)  # 64 hex chars, matches Palo Alto's documented length
        timestamp = str(int(time.time() * 1000))  # current UTC time in milliseconds
        signature = hashlib.sha256(
            f"{self._api_key}{nonce}{timestamp}".encode("utf-8")
        ).hexdigest()
        return {
            "x-xdr-auth-id": self._api_key_id,
            "x-xdr-nonce": nonce,
            "x-xdr-timestamp": timestamp,
            "Authorization": signature,
        }

    def sync_auth_flow(self, request: httpx2.Request):
        request.headers.update(self._sign())
        yield request

    async def async_auth_flow(self, request: httpx2.Request):
        request.headers.update(self._sign())
        yield request


def build_http_client(transport: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncClient:
    """Build the authenticated httpx client FastMCP will use to call the XSOAR API.

    `transport` is only for tests (httpx2.MockTransport against a fake tenant); production uses
    the default networking transport.
    """
    try:
        fqdn = os.environ["XSOAR_FQDN"]
        api_key = os.environ["XSOAR_API_KEY"]
        api_key_id = os.environ["XSOAR_API_KEY_ID"]
    except KeyError as exc:
        raise RuntimeError(
            f"Missing required environment variable {exc}. "
            "Copy .env.example to .env and fill in your XSOAR API key, key ID, and tenant FQDN."
        ) from exc

    key_type = os.environ.get("XSOAR_API_KEY_TYPE", "advanced").strip().lower()
    base_url = _resolve_base_url(fqdn)

    if key_type == "advanced":
        return XsoarApiClient(
            base_url=base_url,
            auth=AdvancedKeyAuth(api_key, api_key_id),
            timeout=90.0,
            follow_redirects=False,
            transport=transport,
        )
    elif key_type == "standard":
        return XsoarApiClient(
            base_url=base_url,
            headers={"Authorization": api_key, "x-xdr-auth-id": api_key_id},
            timeout=90.0,
            follow_redirects=False,
            transport=transport,
        )
    else:
        raise RuntimeError(
            f"XSOAR_API_KEY_TYPE={key_type!r} is not valid - use 'standard' or 'advanced' "
            "(matching whichever type you picked when generating the key)."
        )
