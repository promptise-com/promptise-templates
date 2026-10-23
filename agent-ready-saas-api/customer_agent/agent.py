"""The customer's agent: Acme's operations assistant.

This file is what a customer writes on their side. All it needs from the
SaaS company is the MCP server URL and an agent API key from the workspace
settings. Everything it may do is decided by that key on the server.
"""

from __future__ import annotations

import os
from typing import Any

from promptise import PromptiseAgent, build_agent
from promptise.config import HTTPServerSpec
from promptise.run import RunContext

DEFAULT_MODEL = "openai:gpt-5-mini"

INSTRUCTIONS = """\
You are the operations assistant for the Acme Robotics team in Taskboard.
Look things up before you change them: use list_projects and find_tasks to get ids.
Keep comments short, friendly and specific to the task.
Archive finished projects. Delete a project only when the user asks for deletion;
that call waits for a person to approve it.
If a tool returns an error, explain it to the user instead of guessing."""


async def create_agent(
    ctx: RunContext | None = None,
    *,
    model: Any = None,
    api_key: str | None = None,
    url: str | None = None,
) -> PromptiseAgent:
    """Build the agent. `promptise run` passes ctx; the demo passes the rest."""
    model = model or (ctx.model if ctx else None) or os.environ.get("TASKBOARD_AGENT_MODEL", DEFAULT_MODEL)
    api_key = api_key or os.environ["TASKBOARD_AGENT_KEY"]
    url = url or os.environ.get("TASKBOARD_MCP_URL", "http://127.0.0.1:8080/mcp")
    return await build_agent(
        model=model,
        instructions=INSTRUCTIONS,
        # The key travels as the x-api-key header on every MCP request.
        servers={"taskboard": HTTPServerSpec(url=url, api_key=api_key)},
    )
