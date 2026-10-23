"""Generated tests — one per tool: it is listed, it calls the right upstream operation,
and, when it changes data, it does not run without a human's approval. Every tool
advertises the plan's JSON Schema for each of its parameters.

Regenerated with the plan; add your own tests in another file.
"""

from __future__ import annotations

from conftest import FakeUpstream, error_of, path_of
from promptise.mcp.server import TestClient

from taskboard_mcp.tools import MODULES


async def test_every_tool_is_listed(client: TestClient) -> None:
    names = {tool.name for tool in await client.list_tools()}
    assert names >= {
        "add_comment",
        "create_project",
        "create_task",
        "delete_project",
        "delete_task",
        "get_project",
        "get_task",
        "health_check",
        "list_comments",
        "list_members",
        "list_projects",
        "list_tasks",
        "update_project",
        "update_task",
    }


async def test_every_parameter_advertises_its_schema(client: TestClient) -> None:
    listed = {
        tool.name: tool.model_dump(by_alias=True)["inputSchema"]
        for tool in await client.list_tools()
    }
    for module in MODULES:
        for tool, params in module.PARAM_SCHEMAS.items():
            for name, schema in params.items():
                advertised = listed[tool]["properties"][name]
                assert {key: advertised.get(key) for key in schema} == schema, (tool, name)


async def test_health_check_calls_get(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("health_check", {})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "GET"
    assert path_of(upstream.last) == "/health"


async def test_list_members_calls_get(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("list_members", {})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "GET"
    assert path_of(upstream.last) == "/members"


async def test_list_projects_calls_get(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("list_projects", {})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "GET"
    assert path_of(upstream.last) == "/projects"


async def test_create_project_calls_post(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("create_project", {"name": "example"})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "POST"
    assert path_of(upstream.last) == "/projects"


async def test_create_project_needs_approval(
    denying_client: TestClient, upstream: FakeUpstream, approvals: list
) -> None:
    reply = await denying_client.call_tool("create_project", {"name": "example"})
    assert (error_of(reply) or {}).get("code") == "APPROVAL_DENIED"
    assert upstream.requests == []  # nothing reached the API
    assert approvals, "the gate asked nobody"
    assert approvals[-1].tool_name == "create_project"


async def test_get_project_calls_get(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("get_project", {"project_id": "123"})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "GET"
    assert path_of(upstream.last) == "/projects/123"


async def test_update_project_calls_patch(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("update_project", {"project_id": "123"})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "PATCH"
    assert path_of(upstream.last) == "/projects/123"


async def test_update_project_needs_approval(
    denying_client: TestClient, upstream: FakeUpstream, approvals: list
) -> None:
    reply = await denying_client.call_tool("update_project", {"project_id": "123"})
    assert (error_of(reply) or {}).get("code") == "APPROVAL_DENIED"
    assert upstream.requests == []  # nothing reached the API
    assert approvals, "the gate asked nobody"
    assert approvals[-1].tool_name == "update_project"


async def test_delete_project_calls_delete(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("delete_project", {"project_id": "123"})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "DELETE"
    assert path_of(upstream.last) == "/projects/123"


async def test_delete_project_needs_approval(
    denying_client: TestClient, upstream: FakeUpstream, approvals: list
) -> None:
    reply = await denying_client.call_tool("delete_project", {"project_id": "123"})
    assert (error_of(reply) or {}).get("code") == "APPROVAL_DENIED"
    assert upstream.requests == []  # nothing reached the API
    assert approvals, "the gate asked nobody"
    assert approvals[-1].tool_name == "delete_project"


async def test_list_tasks_calls_get(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("list_tasks", {})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "GET"
    assert path_of(upstream.last) == "/tasks"


async def test_create_task_calls_post(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("create_task", {"project_id": "123", "title": "string"})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "POST"
    assert path_of(upstream.last) == "/projects/123/tasks"


async def test_create_task_needs_approval(
    denying_client: TestClient, upstream: FakeUpstream, approvals: list
) -> None:
    reply = await denying_client.call_tool("create_task", {"project_id": "123", "title": "string"})
    assert (error_of(reply) or {}).get("code") == "APPROVAL_DENIED"
    assert upstream.requests == []  # nothing reached the API
    assert approvals, "the gate asked nobody"
    assert approvals[-1].tool_name == "create_task"


async def test_get_task_calls_get(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("get_task", {"task_id": "123"})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "GET"
    assert path_of(upstream.last) == "/tasks/123"


async def test_update_task_calls_patch(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("update_task", {"task_id": "123"})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "PATCH"
    assert path_of(upstream.last) == "/tasks/123"


async def test_update_task_needs_approval(
    denying_client: TestClient, upstream: FakeUpstream, approvals: list
) -> None:
    reply = await denying_client.call_tool("update_task", {"task_id": "123"})
    assert (error_of(reply) or {}).get("code") == "APPROVAL_DENIED"
    assert upstream.requests == []  # nothing reached the API
    assert approvals, "the gate asked nobody"
    assert approvals[-1].tool_name == "update_task"


async def test_delete_task_calls_delete(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("delete_task", {"task_id": "123"})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "DELETE"
    assert path_of(upstream.last) == "/tasks/123"


async def test_delete_task_needs_approval(
    denying_client: TestClient, upstream: FakeUpstream, approvals: list
) -> None:
    reply = await denying_client.call_tool("delete_task", {"task_id": "123"})
    assert (error_of(reply) or {}).get("code") == "APPROVAL_DENIED"
    assert upstream.requests == []  # nothing reached the API
    assert approvals, "the gate asked nobody"
    assert approvals[-1].tool_name == "delete_task"


async def test_list_comments_calls_get(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool("list_comments", {"task_id": "123"})
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "GET"
    assert path_of(upstream.last) == "/tasks/123/comments"


async def test_add_comment_calls_post(client: TestClient, upstream: FakeUpstream) -> None:
    reply = await client.call_tool(
        "add_comment",
        {"task_id": "123", "body": "string", "author": "string"},
    )
    assert error_of(reply) is None, reply[0].text
    assert upstream.last.method == "POST"
    assert path_of(upstream.last) == "/tasks/123/comments"


async def test_add_comment_needs_approval(
    denying_client: TestClient, upstream: FakeUpstream, approvals: list
) -> None:
    reply = await denying_client.call_tool(
        "add_comment",
        {"task_id": "123", "body": "string", "author": "string"},
    )
    assert (error_of(reply) or {}).get("code") == "APPROVAL_DENIED"
    assert upstream.requests == []  # nothing reached the API
    assert approvals, "the gate asked nobody"
    assert approvals[-1].tool_name == "add_comment"
