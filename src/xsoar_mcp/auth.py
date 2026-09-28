"""XSOAR API authentication.

XSOAR's "Standard" API key type is just two static headers - no token exchange, no refresh, no
expiry to manage. ("Advanced" key type adds a nonce+timestamp HMAC per request to guard against
replay; this module only supports Standard for now - see README for how to add Advanced support
if your org requires it.)
"""

from __future__ import annotations

import os

import httpx2

XSOAR_API_BASE_PATH = "/xsoar/public/v1"


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

    return httpx2.AsyncClient(
        base_url=f"https://api-{fqdn}{XSOAR_API_BASE_PATH}",
        headers={
            "Authorization": api_key,
            "x-xdr-auth-id": api_key_id,
        },
        timeout=90.0,
    )
