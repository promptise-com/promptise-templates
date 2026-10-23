"""The customer's agent: an operations assistant for one Taskboard workspace.

This file is what a customer writes on their side. All it needs from the
SaaS company is the MCP server URL and an agent API key from the workspace
settings. Everything the agent may do is decided by that key, on the server.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from promptise import PromptiseAgent, build_agent
from promptise.config import HTTPServerSpec
from promptise.conversations import SQLiteConversationStore
from promptise.observability_config import ObservabilityConfig, ObserveLevel, TransporterType
from promptise.run import RunContext

from customer_agent.prompts import ops_assistant

DEFAULT_MODEL = "openai:gpt-5-mini"
DATA = Path(__file__).resolve().parent.parent / ".data"


def observability(on_event: Callable[[Any], None] | None = None) -> ObservabilityConfig:
    """Traces of every run: an HTML report, JSON log lines, and an optional live callback."""
    transporters = [TransporterType.HTML, TransporterType.STRUCTURED_LOG]
    if on_event is not None:
        transporters.append(TransporterType.CALLBACK)
    return ObservabilityConfig(
        level=ObserveLevel.STANDARD,
        session_name="taskboard-ops-agent",
        transporters=transporters,
        output_dir=str(DATA / "reports"),
        log_file=str(DATA / "agent-trace.jsonl"),
        on_event=on_event,
    )


async def create_agent(
    ctx: RunContext | None = None,
    *,
    model: Any = None,
    api_key: str | None = None,
    url: str | None = None,
    observe: Any = None,
    remember: bool = True,
) -> PromptiseAgent:
    """Build the agent. `promptise run` passes ctx; main.py, the tests and the demo pass the rest."""
    model = model or (ctx.model if ctx else None) or os.environ.get("TASKBOARD_AGENT_MODEL", DEFAULT_MODEL)
    api_key = api_key or os.environ["TASKBOARD_AGENT_KEY"]
    url = url or os.environ.get("TASKBOARD_MCP_URL", "http://127.0.0.1:8080/mcp")
    DATA.mkdir(exist_ok=True)
    return await build_agent(
        model=model,
        # A Prompt object, rendered again on every run (see prompts.py).
        instructions=ops_assistant,
        # The key travels as the x-api-key header on every MCP request.
        servers={"taskboard": HTTPServerSpec(url=url, api_key=api_key)},
        # A plain tool loop: look up, act, answer. The task needs nothing more.
        agent_pattern="react",
        max_agent_iterations=15,
        # agent.chat(..., session_id=...) keeps the conversation across runs.
        conversation_store=SQLiteConversationStore(str(DATA / "conversations.db")) if remember else None,
        observe=observe if observe is not None else observability(),
    )
