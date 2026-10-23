"""Fixtures for the generated tests: a fake upstream API and clients through the full pipeline.

Generated with the plan; put your own tests in another file under tests/ —
conftest.py and test_tools.py are rewritten on every regeneration.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from promptise.mcp.server import TestClient

from taskboard_mcp import build_server


class FakeUpstream:
    """Records every request the server sends and answers 200 with a JSON body."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True, "path": request.url.path})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "the server sent no request"
        return self.requests[-1]


def error_of(content: list[Any]) -> dict[str, Any] | None:
    """The structured error a call returned, or None on success."""
    try:
        return json.loads(content[0].text).get("error")
    except (AttributeError, IndexError, ValueError, TypeError):
        return None


def path_of(request: httpx.Request) -> str:
    """The path as sent on the wire: percent-encoded, without the query string.

    ``request.url.path`` is decoded (``/users/ada%40example.com`` reads back as
    ``/users/ada@example.com``); the tests compare what the upstream receives.
    """
    return request.url.raw_path.split(b"?", 1)[0].decode()


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials for auth mode 'api-key', in the environment the server reads."""
    monkeypatch.setenv(
        "MCPCAST_CLIENT_KEYS",
        json.dumps({"test-key": {"client_id": "tests", "tenant_id": "test-tenant", "roles": []}}),
    )
    monkeypatch.setenv("MCPCAST_UPSTREAM_TOKENS", json.dumps({"test-tenant": "Bearer test-token"}))


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def approvals() -> list[Any]:
    """Every approval request the gate raised during the test."""
    return []


def make_client(upstream: FakeUpstream, approvals: list[Any], *, approve: bool) -> TestClient:
    """A client whose gated calls are approved (or denied) by a recording handler."""

    def handler(request: Any) -> bool:
        approvals.append(request)
        return approve

    server = build_server(approval_handler=handler, http_client=upstream.client())
    return TestClient(server, meta={"x-api-key": "test-key"})


@pytest.fixture
def client(upstream: FakeUpstream, approvals: list[Any]) -> TestClient:
    """Every call goes through validation, guards, the approval gate (approving) and the tool."""
    return make_client(upstream, approvals, approve=True)


@pytest.fixture
def denying_client(upstream: FakeUpstream, approvals: list[Any]) -> TestClient:
    """The same, with a human who says no."""
    return make_client(upstream, approvals, approve=False)
