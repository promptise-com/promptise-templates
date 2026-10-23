"""Shared fixtures: the real REST API and the real MCP server, wired in-process.

Nothing here needs a network or a model key. The MCP server's calls to the
API go through httpx.ASGITransport, and tests call the MCP server through
TestClient, which runs the full pipeline: auth, rate limits, the approval
gate, guards, audit and the handler.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from promptise.mcp.server import TestClient  # noqa: E402

from mcp_server import Settings, build_server  # noqa: E402
from taskboard_api.app import create_app  # noqa: E402

TOKENS = {"acme": "test-acme-token", "globex": "test-globex-token"}


class Harness:
    """One server, its keys, and helpers to call it as a given key."""

    def __init__(self, tmp_path: Path, **overrides: Any) -> None:
        self.api_app = create_app(tokens=TOKENS)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api_app), base_url="http://taskboard.test")
        self.settings = Settings(
            upstream_tokens=TOKENS,
            keys_file=tmp_path / "agent_keys.json",
            audit_log=tmp_path / "audit.jsonl",
            audit_secret=secrets.token_hex(32),
            **{"approval_timeout": 5.0, **overrides},
        )
        self.gw = build_server(self.settings, http_client=self.http)
        self.client = TestClient(self.gw.server)
        self.key: dict[str, str] = {}
        self.key_id: dict[str, str] = {}
        for name, workspace, access in [
            ("acme-report-bot", "acme", "reader"),
            ("acme-writer-bot", "acme", "editor"),
            ("acme-ops-agent", "acme", "admin"),
            ("dana@acme", "acme", "approver"),
            ("olivia@acme", "acme", "owner"),
            ("globex-agent", "globex", "admin"),
            ("ines@globex", "globex", "approver"),
        ]:
            self.key[name], record = self.gw.keys.create(workspace=workspace, name=name, access=access)
            self.key_id[name] = record["key_id"]

    @property
    def store(self):
        return self.api_app.state.store

    async def call(self, tool: str, args: dict[str, Any] | None = None, *, as_: str | None = None) -> Any:
        """Call a tool as one of the keys above (or with no key) and decode the JSON reply."""
        headers = {"x-api-key": self.key[as_]} if as_ else None
        result = await self.client.call_tool(tool, args or {}, headers=headers)
        return json.loads(result[0].text)

    async def wait_for_pending(self, count: int = 1) -> list[dict[str, Any]]:
        for _ in range(500):
            if len(self.gw.approver.pending()) >= count:
                return self.gw.approver.pending()
            await asyncio.sleep(0.01)
        raise AssertionError("no call reached the approval queue")


def error_code(reply: Any) -> str | None:
    return reply.get("error", {}).get("code") if isinstance(reply, dict) else None


@pytest.fixture
async def h(tmp_path: Path):
    harness = Harness(tmp_path)
    yield harness
    await harness.http.aclose()
