"""The MCPcast first draft, and what the hand-tuned server changed on purpose."""

from __future__ import annotations

from pathlib import Path

from promptise.mcpcast import MCPcastPlan

from conftest import Harness

PLAN = Path(__file__).resolve().parent.parent / "mcpcast_draft" / "mcpcast.plan.yaml"


def test_the_draft_gates_every_operation_that_changes_data():
    plan = MCPcastPlan.load(PLAN)
    changes = {t.name for t in plan.tools if t.risk.value != "read"}
    assert changes == {t.name for t in plan.gated_tools}
    assert plan.tool("delete_project").risk.value == "destructive"


def test_the_draft_lets_the_agent_choose_the_comment_author():
    # One reason the draft is not what ships: an agent could sign as anyone.
    assert "author" in MCPcastPlan.load(PLAN).tool("add_comment").params


async def test_the_shipped_server_narrows_the_draft(h: Harness):
    draft = set(MCPcastPlan.load(PLAN).tool_names)
    shipped = {t.name for t in await h.client.list_tools(headers={"x-api-key": h.key["acme-ops-agent"]})}
    # Not exposed to agents at all.
    assert {"health_check", "delete_task"} <= draft - shipped
    # Merged into get_task, which returns the comments too.
    assert "list_comments" in draft and "list_comments" not in shipped
