"""Shared helpers. Every test runs offline: no API key, no network beyond 127.0.0.1."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from promptise import PromptiseAgent
from promptise.conversations import InMemoryConversationStore
from promptise.mcp.server import AuditMiddleware, MCPServer, TestClient
from promptise.memory import InMemoryProvider, MemoryScope
from promptise.testing import ScriptedModel

from agent import create_agent
from support_desk.backend import RefundReview, build_server
from support_desk.identity import LoginService
from support_desk.records import TicketDesk
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
    model: Any
    audit: AuditMiddleware
    memory: InMemoryProvider
    conversations: InMemoryConversationStore
    refunds: RefundReview
    login: LoginService


@contextlib.asynccontextmanager
async def support_stack(replies: list[Any] | None = None, *, model: Any = None) -> AsyncIterator[Stack]:
    """The real backend over HTTP on loopback, and the agent on a scripted (or real) model."""
    login = LoginService()
    desk = TicketDesk()
    audit = AuditMiddleware(include_args=True, signed=False)
    refunds = RefundReview(desk)
    memory = InMemoryProvider(scope=MemoryScope.PER_USER)
    conversations = InMemoryConversationStore()
    model = model if model is not None else ScriptedModel(replies or [])
    async with running_backend(build_server(desk, audit=audit, refunds=refunds)) as url:
        agent = await create_agent(
            backend_url=url, model=model, memory=memory, conversations=conversations, login=login
        )
        try:
            yield Stack(agent, model, audit, memory, conversations, refunds, login)
        finally:
            await agent.shutdown()


def everything_the_model_saw(model: ScriptedModel) -> str:
    """All text in every request the model received, tool results included."""
    return "\n".join(str(m.content) for c in model.calls for m in c.messages)


def tool_results(model: ScriptedModel) -> dict[str, str]:
    """Each tool's full result, as the model received it (last call per tool wins)."""
    names: dict[str, str] = {}
    results: dict[str, str] = {}
    for call in model.calls:
        for message in call.messages:
            if message.type == "ai":
                names.update({c["id"]: c["name"] for c in message.tool_calls})
            elif message.type == "tool" and message.tool_call_id in names:
                results[names[message.tool_call_id]] = str(message.content)
    return results
