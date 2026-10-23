"""Run the customer's agent on one task against a running Taskboard MCP server.

    promptise run agent                       # the default weekly sweep
    promptise run agent "Archive the 2024 trade show project"

Needs TASKBOARD_AGENT_KEY (an agent key), the model provider's key
(OPENAI_API_KEY for the default model) and a running `promptise run mcp`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from customer_agent.agent import DEFAULT_MODEL, create_agent  # noqa: E402

WEEKLY_SWEEP = (
    "Do the weekly overdue sweep for the Website relaunch project. "
    "Comment on each overdue task asking the assignee for a new due date. "
    "Then create one task for Maya, due this Friday, to review the answers. "
    "Finish with a short summary of what you did."
)


async def main() -> None:
    task = " ".join(sys.argv[1:]) or WEEKLY_SWEEP
    model = os.environ.get("TASKBOARD_AGENT_MODEL", DEFAULT_MODEL)
    if model.startswith("openai:") and not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY, or TASKBOARD_AGENT_MODEL for another provider. No key? Run: promptise run demo")
    agent = await create_agent()
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": task}]})
        print(result["messages"][-1].content)
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
