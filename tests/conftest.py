"""Shared fixtures: a fake tenant behind httpx2.MockTransport, no live XSOAR needed."""

from __future__ import annotations

import os

import httpx2
import pytest


@pytest.fixture(autouse=True)
def fake_tenant_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XSOAR_FQDN", "test.paloaltonetworks.com")
    monkeypatch.setenv("XSOAR_API_KEY", "dummy-key")
    monkeypatch.setenv("XSOAR_API_KEY_ID", "1234")
    monkeypatch.delenv("XSOAR_TOOLSETS", raising=False)
    monkeypatch.delenv("XSOAR_READ_ONLY", raising=False)


class RecordingMockTransport(httpx2.MockTransport):
    """MockTransport that also records every request that was sent."""

    def __init__(self, handler) -> None:
        self.requests: list[httpx2.Request] = []

        def recording_handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(recording_handler)


@pytest.fixture
def ok_json_handler():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"total": 0, "data": []}, request=request)

    return handler
