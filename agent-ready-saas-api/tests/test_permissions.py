"""Each tool declares the role it needs, and every call stays in the key's workspace."""

from __future__ import annotations

import pytest

from conftest import Harness, error_code

WRITES = [
    ("create_task", {"project_id": "prj_101", "title": "Sneaky task"}),
    ("update_task", {"task_id": "tsk_1001", "status": "done"}),
    ("add_comment", {"task_id": "tsk_1001", "body": "hi"}),
    ("create_project", {"name": "Sneaky project"}),
    ("archive_project", {"project_id": "prj_101"}),
    ("delete_project", {"project_id": "prj_101"}),
]


@pytest.mark.parametrize(("tool", "args"), WRITES)
async def test_read_only_key_cannot_create_change_or_delete(h: Harness, tool: str, args: dict):
    tasks_before = dict(h.store.tasks)
    projects_before = {k: dict(v) for k, v in h.store.projects.items()}

    reply = await h.call(tool, args, as_="acme-report-bot")

    assert error_code(reply) == "ACCESS_DENIED"
    assert h.store.tasks.keys() == tasks_before.keys()
    assert h.store.projects == projects_before
    # A denied delete never reaches a reviewer.
    assert h.gw.approver.pending() == []


async def test_read_only_key_can_read(h: Harness):
    overdue = await h.call("find_tasks", {"project_id": "prj_101", "overdue_only": True}, as_="acme-report-bot")
    assert [t["id"] for t in overdue] == ["tsk_1001", "tsk_1002"]


async def test_tools_list_shows_each_key_only_what_it_may_call(h: Harness):
    async def names(who: str) -> set[str]:
        return {t.name for t in await h.client.list_tools(headers={"x-api-key": h.key[who]})}

    reads = {"list_projects", "find_tasks", "get_task", "list_members"}
    assert await names("acme-report-bot") == reads
    assert "delete_project" not in await names("acme-writer-bot")
    assert "delete_project" in await names("acme-ops-agent")
    assert "approvals_decide" not in await names("acme-ops-agent")
    assert "approvals_decide" in await names("dana@acme")


async def test_editor_can_write_but_not_delete(h: Harness):
    task = await h.call(
        "create_task", {"project_id": "prj_101", "title": "Check the sitemap", "assignee": "leo"}, as_="acme-writer-bot"
    )
    assert task["project_id"] == "prj_101" and task["assignee"] == "leo"

    reply = await h.call("delete_project", {"project_id": "prj_101"}, as_="acme-writer-bot")
    assert error_code(reply) == "ACCESS_DENIED"


async def test_a_key_never_reaches_another_workspace(h: Harness):
    acme_ids = {p["id"] for p in await h.call("list_projects", {"status": "all"}, as_="acme-ops-agent")}
    globex_ids = {p["id"] for p in await h.call("list_projects", {"status": "all"}, as_="globex-agent")}
    assert acme_ids == {"prj_101", "prj_102", "prj_103"}
    assert globex_ids == {"prj_201"}

    # Globex's agent asking for an Acme task gets "not found", not the task.
    assert error_code(await h.call("get_task", {"task_id": "tsk_1001"}, as_="globex-agent")) == "NOT_FOUND"
    reply = await h.call("add_comment", {"task_id": "tsk_1001", "body": "hi"}, as_="globex-agent")
    assert error_code(reply) == "NOT_FOUND"


async def test_comment_author_comes_from_the_key_not_the_agent(h: Harness):
    tools = {t.name: t for t in await h.client.list_tools(headers={"x-api-key": h.key["acme-writer-bot"]})}
    assert "author" not in tools["add_comment"].input_schema["properties"]

    comment = await h.call("add_comment", {"task_id": "tsk_1001", "body": "New date?"}, as_="acme-writer-bot")
    assert comment["author"] == "agent:acme-writer-bot"


async def test_bad_arguments_are_rejected_before_the_api(h: Harness):
    reply = await h.call("get_task", {"task_id": "../projects/prj_201"}, as_="acme-ops-agent")
    assert error_code(reply) == "VALIDATION_ERROR"
