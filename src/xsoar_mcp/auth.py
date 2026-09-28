"""XSOAR API authentication.

Two key types exist, selected when you generate the key in the tenant UI:

- **Standard**: a static "Authorization: {api_key}" header. Simple, no per-request computation.
- **Advanced**: guards against replay by requiring a fresh nonce and timestamp on every request,
  with "Authorization" set to sha256(api_key + nonce + timestamp) instead of the raw key. Since
  those values must be fresh per call (not fixed once on the client), this is implemented as an
  httpx2.Auth subclass rather than static client headers.

Set XSOAR_API_KEY_TYPE=standard|advanced to pick. Defaults to advanced, since that's the type
Palo Alto's own docs frame as the recommended default and a 401 against a set of static
Standard-style headers is the most likely reason to end up reading this file's diff.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time

import httpx2

XSOAR_API_BASE_PATH = "/xsoar/public/v1"


def _resolve_base_url(fqdn: str) -> str:
    """Accept either a bare FQDN ("zhaw.crtx.ch.paloaltonetworks.com") or a full URL a user
    copy-pasted from their tenant's console ("https://api-zhaw.crtx.ch.paloaltonetworks.com") -
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


def build_http_client() -> httpx2.AsyncClient:
    """Build the authenticated httpx client FastMCP will use to call the XSOAR API."""
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
        return httpx2.AsyncClient(
            base_url=base_url,
            auth=AdvancedKeyAuth(api_key, api_key_id),
            timeout=90.0,
        )
    elif key_type == "standard":
        return httpx2.AsyncClient(
            base_url=base_url,
            headers={"Authorization": api_key, "x-xdr-auth-id": api_key_id},
            timeout=90.0,
        )
    else:
        raise RuntimeError(
            f"XSOAR_API_KEY_TYPE={key_type!r} is not valid - use 'standard' or 'advanced' "
            "(matching whichever type you picked when generating the key)."
        )
