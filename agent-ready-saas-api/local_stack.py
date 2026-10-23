"""The whole product in one process, for the offline demo and the live test.

The Taskboard API runs in-process. The MCP server runs on a real HTTP port on
127.0.0.1, so agents connect to it exactly as they do in production. Every
secret is generated for this run and thrown away at the end.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn

from mcp_server import Gateway, Settings, build_server
from taskboard_api.app import create_app


@dataclass
class Stack:
    url: str
    gateway: Gateway
    settings: Settings
    api_app: object
    keys: dict[str, str]
    key_ids: dict[str, str]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def local_stack(workdir: Path, *, approval_timeout: float = 60) -> AsyncIterator[Stack]:
    tokens = {"acme": secrets.token_urlsafe(16), "globex": secrets.token_urlsafe(16)}
    api_app = create_app(tokens=tokens)
    settings = Settings(
        upstream_tokens=tokens,
        keys_file=workdir / "agent_keys.json",
        audit_log=workdir / "audit.jsonl",
        audit_secret=secrets.token_hex(32),
        approval_timeout=approval_timeout,
    )
    # The MCP server reaches the API in-process; in production this is plain HTTP.
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://taskboard.internal")
    gw = build_server(settings, http_client=upstream)
    keys, key_ids = {}, {}
    for name, access in [("acme-ops-agent", "admin"), ("acme-report-bot", "reader"), ("dana@acme", "approver")]:
        keys[name], record = gw.keys.create(workspace="acme", name=name, access=access)
        key_ids[name] = record["key_id"]

    port = _free_port()
    web = uvicorn.Server(uvicorn.Config(gw.server.asgi_app(), host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(web.serve())
    while not web.started:
        await asyncio.sleep(0.05)
    try:
        yield Stack(f"http://127.0.0.1:{port}/mcp", gw, settings, api_app, keys, key_ids)
    finally:
        web.should_exit = True
        await serving
        await upstream.aclose()
