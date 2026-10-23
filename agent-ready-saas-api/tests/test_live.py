"""One real end-to-end run with a real model. Skipped without a model key.

    promptise run test-live        (or: pytest -m live)

The agent uses TASKBOARD_AGENT_MODEL (default openai:gpt-5-mini). It does the
weekly sweep, then asks to delete a project, which a reviewer approves over
MCP. About 25k tokens per run.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from promptise.mcp.client import MCPClient

from customer_agent.agent import DEFAULT_MODEL, create_agent
from customer_agent.main import WEEKLY_SWEEP
from local_stack import local_stack

MODEL = os.environ.get("TASKBOARD_AGENT_MODEL", DEFAULT_MODEL)
PROVIDER_KEY = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(MODEL.split(":")[0])

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(bool(PROVIDER_KEY) and not os.environ.get(PROVIDER_KEY), reason=f"{PROVIDER_KEY} is not set"),
]


async def approve_when_asked(url: str, reviewer_key: str) -> dict:
    async with MCPClient(url=url, api_key=reviewer_key) as reviewer:
        while True:
            result = await reviewer.call_tool("approvals_list", {})
            pending = json.loads(result.content[0].text)
            if pending:
                await reviewer.call_tool("approvals_decide", {"request_id": pending[0]["request_id"], "approve": True})
                return pending[0]
            await asyncio.sleep(0.5)


async def test_real_model_does_the_sweep_and_a_deletion_waits_for_approval(tmp_path):
    async with local_stack(tmp_path) as stack:
        store = stack.api_app.state.store
        seed_tasks = set(store.tasks)
        agent = await create_agent(
            model=MODEL, api_key=stack.keys["acme-ops-agent"], url=stack.url, observe=False, remember=False
        )
        try:
            await agent.chat(WEEKLY_SWEEP, session_id="live")
            reviewer = asyncio.create_task(approve_when_asked(stack.url, stack.keys["dana@acme"]))
            answer = await agent.chat("Delete the 2024 trade show project.", session_id="live")
            approved = await asyncio.wait_for(reviewer, 10)
        finally:
            await agent.shutdown()

    # The sweep: one agent comment on each overdue task, and a review task for Maya.
    for task_id in ("tsk_1001", "tsk_1002"):
        assert any(c["author"] == "agent:acme-ops-agent" for c in store.comments[task_id])
    new_tasks = [store.tasks[t] for t in set(store.tasks) - seed_tasks]
    assert [t["assignee"] for t in new_tasks] == ["maya"]
    # The deletion waited for dana@acme, then ran.
    assert approved["tool"] == "delete_project" and approved["arguments"] == {"project_id": "prj_103"}
    assert "prj_103" not in store.projects
    assert "prj_103" in answer
