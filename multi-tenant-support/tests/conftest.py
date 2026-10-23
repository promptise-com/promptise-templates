"""Shared helpers. Every test runs offline: no API key, no network beyond 127.0.0.1."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from promptise import PromptiseAgent
from promptise.mcp.server import AuditMiddleware, MCPServer, TestClient
from promptise.memory import InMemoryProvider, MemoryScope
from promptise.testing import ScriptedModel

from agent import create_agent
from support_desk.backend import build_server
from support_desk.identity import LoginService
from support_desk.serving import running_backend


@pytest.fixture
def login() -> LoginService:
    return LoginService()


@pytest.fixture
def server() -> MCPServer:
    """A fresh backend per test: fresh data and fresh rate-limit buckets."""
    return build_server()


def as_user(server: MCPServer, token: str) -> TestClient:
    return TestClient(server, meta={"authorization": f"Bearer {token}"})


async def call(client: TestClient, tool: str, args: dict[str, Any] | None = None) -> Any:
    """Call a backend tool and return its decoded JSON result."""
    result = await client.call_tool(tool, args or {})
    return json.loads(result[0].text)


@dataclass
class Stack:
    agent: PromptiseAgent
    model: ScriptedModel
    audit: AuditMiddleware
    memory: InMemoryProvider
    login: LoginService


@contextlib.asynccontextmanager
async def support_stack(replies: list[Any]) -> AsyncIterator[Stack]:
    """The real backend over HTTP on loopback, and the agent on a scripted model."""
    login = LoginService()
    audit = AuditMiddleware(include_args=True, signed=False)
    memory = InMemoryProvider(scope=MemoryScope.PER_USER)
    model = ScriptedModel(replies)
    async with running_backend(build_server(audit=audit)) as url:
        agent = await create_agent(backend_url=url, model=model, memory=memory, login=login)
        try:
            yield Stack(agent, model, audit, memory, login)
        finally:
            await agent.shutdown()


def everything_the_model_saw(model: ScriptedModel) -> str:
    """All text in every request the model received, tool results included."""
    return "\n".join(str(m.content) for c in model.calls for m in c.messages)
