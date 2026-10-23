"""Run the customer's agent on one task against the running Taskboard MCP server.

    promptise run agent                                   # the weekly overdue sweep
    promptise run agent "Delete the 2024 trade show project"
    promptise run agent-reader "Close the homepage copy task"   # a read-only key

Needs a running `promptise run api` and `promptise run mcp`, an agent key
(TASKBOARD_AGENT_KEY, written by `promptise run setup`) and the model
provider's key (OPENAI_API_KEY for the default model).

Runs in the same --session share one conversation, so a follow-up such as
"When is Maya's review task due?" knows what the sweep did.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from promptise.mcp.client import MCPConnectionRejectedError  # noqa: E402

from customer_agent.agent import DATA, DEFAULT_MODEL, create_agent, observability  # noqa: E402

WEEKLY_SWEEP = (
    "Do the weekly overdue sweep for the Website relaunch project. "
    "Comment on each overdue task asking the assignee for a new due date. "
    "Then create one task for Maya, due this Friday, to review the answers."
)


def show(entry) -> None:
    """Print each tool call as it happens, so a run waiting for approval is visible."""
    kind, meta = entry.event_type.value, entry.metadata or {}
    if kind == "tool.call":
        args = meta.get("arguments", {})
        print(f"  -> {meta.get('tool_name')}({args if isinstance(args, str) else json.dumps(args)})", flush=True)
    elif kind == "tool.result":
        print(f"  <- {meta.get('tool_name')}: {str(meta.get('result_preview', ''))[:110]}", flush=True)
    elif kind == "tool.error":
        print(f"  <- {meta.get('tool_name')} failed: {str(meta.get('error', ''))[:110]}", flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Taskboard ops agent on one task.")
    parser.add_argument("task", nargs="*", help="what to do (default: the weekly overdue sweep)")
    parser.add_argument("--key-env", default="TASKBOARD_AGENT_KEY", help="variable that holds the agent key")
    parser.add_argument("--session", default="weekly-ops", help="conversation to continue")
    args = parser.parse_args()

    model = os.environ.get("TASKBOARD_AGENT_MODEL", DEFAULT_MODEL)
    if model.startswith("openai:") and not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY, or TASKBOARD_AGENT_MODEL for another provider. No key? See demo-offline.")
    api_key = os.environ.get(args.key_env)
    if not api_key:
        sys.exit(f"{args.key_env} is not set. Run `promptise run setup` first.")

    task = " ".join(args.task) or WEEKLY_SWEEP
    print(f"> {task}")
    try:
        agent = await create_agent(api_key=api_key, observe=observability(on_event=show))
    except MCPConnectionRejectedError:
        sys.exit("The Taskboard MCP server refused this agent key (HTTP 401). It is unknown or was revoked.")
    try:
        answer = await agent.chat(task, session_id=args.session)
        print(f"\n{answer}\n")
        stats = agent.get_stats()
        print(
            f"[{stats['llm_call_count']} model calls, {stats['tool_call_count']} tool calls, "
            f"{stats['total_tokens']} tokens. Trace: {(DATA / 'agent-trace.jsonl').relative_to(DATA.parent)}]"
        )
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
