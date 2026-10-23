"""The Taskboard MCP server: what customers' agents connect to.

Built with the Promptise MCP server SDK. Every call runs through this
pipeline, in order:

1. AuditMiddleware        every call, allowed or refused, into an HMAC-chained log
2. AuthMiddleware         an agent API key -> client id, workspace, roles
3. RateLimitMiddleware    one token bucket per agent key
4. ApprovalGateMiddleware deletions wait for a person in the same workspace
5. the tool's own guard   roles=[...] on each tool below

Each tool calls the Taskboard REST API for the caller's workspace only.
GET /health and GET /health/ready serve container and load-balancer probes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal

import httpx
from promptise.mcp.server import (
    APIKeyAuth,
    ApprovalGateMiddleware,
    AuditMiddleware,
    AuthMiddleware,
    HealthCheck,
    MCPServer,
    PendingApprover,
    RateLimitMiddleware,
    RequestContext,
    TokenBucketLimiter,
)
from pydantic import Field

from .approvals import register_approval_tools
from .config import Settings
from .keys import KeyStore
from .upstream import TaskboardAPI

INSTRUCTIONS = (
    "Taskboard project management for one customer workspace. "
    "Every call acts in the workspace of your API key. "
    "Find ids with list_projects and find_tasks before you change anything. "
    "Deleting a project waits for a person to approve it."
)

ProjectId = Annotated[str, Field(pattern=r"^prj_\d+$", description="Project id, for example 'prj_101'.")]
TaskId = Annotated[str, Field(pattern=r"^tsk_\d+$", description="Task id, for example 'tsk_1001'.")]
Handle = Annotated[str, Field(max_length=40, description="Member handle from list_members, for example 'maya'.")]
TaskStatus = Literal["todo", "in_progress", "done"]


@dataclass
class Gateway:
    """The server plus the parts tests and operators need to reach."""

    server: MCPServer
    approver: PendingApprover
    keys: KeyStore
    audit: AuditMiddleware
    api: TaskboardAPI


def build_server(settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> Gateway:
    if not settings.audit_secret or "<" in settings.audit_secret:
        raise RuntimeError("PROMPTISE_AUDIT_SECRET is not set: the audit log needs a key you keep.")
    if any("<" in token for token in (settings.upstream_tokens or {}).values()):
        raise RuntimeError("TASKBOARD_UPSTREAM_TOKENS still holds a <placeholder> from .env.example.")

    # require_tenant: every tool needs an authenticated caller with a workspace.
    # hide_unauthorized_tools: a reader key does not even see the write tools.
    server = MCPServer(
        name="taskboard",
        version="1.0.0",
        instructions=INSTRUCTIONS,
        require_tenant=True,
        hide_unauthorized_tools=True,
    )

    keys = KeyStore(settings.keys_file)
    settings.audit_log.parent.mkdir(parents=True, exist_ok=True)
    audit = AuditMiddleware(
        log_path=str(settings.audit_log),
        hmac_secret=settings.audit_secret,
        include_args=True,
    )
    approver = PendingApprover(
        approver_role="approver",
        max_pending=200,
        max_pending_per_tenant=20,
        max_pending_per_client=5,
    )

    # Audit goes first so refused calls (bad key, wrong role, rate limited,
    # approval denied) are in the log too.
    server.add_middleware(audit)
    server.add_middleware(AuthMiddleware(APIKeyAuth(lookup=keys.lookup)))
    server.add_middleware(
        RateLimitMiddleware(TokenBucketLimiter(rate_per_minute=settings.rate_per_minute, burst=settings.rate_burst))
    )
    server.add_middleware(ApprovalGateMiddleware(approver, timeout=settings.approval_timeout))
    register_approval_tools(server, approver)

    api = TaskboardAPI(settings.api_url, settings.upstream_tokens or {}, http_client=http_client)
    server.on_shutdown(api.aclose)

    # GET /health answers while the process runs. GET /health/ready answers 503
    # while the Taskboard API is down, so a load balancer stops sending agents here.
    health = HealthCheck()
    health.add_check("taskboard_api", api.healthy, required_for_ready=True)
    health.register_resources(server)

    def ws(ctx: RequestContext) -> str:
        return ctx.client.tenant_id

    # -- reads: any key with the reader role ------------------------------------

    @server.tool(roles=["reader"], read_only_hint=True)
    async def list_projects(
        ctx: RequestContext,
        status: Literal["active", "archived", "all"] = "active",
    ) -> list[dict[str, Any]]:
        """List the workspace's projects with their open and overdue task counts. Start here to find a project id."""
        rows = await api.call(ws(ctx), "GET", "/projects", params={"status": None if status == "all" else status})
        return [_project(p) for p in rows]

    @server.tool(roles=["reader"], read_only_hint=True)
    async def find_tasks(
        ctx: RequestContext,
        project_id: ProjectId | None = None,
        status: TaskStatus | None = None,
        assignee: Handle | None = None,
        overdue_only: Annotated[bool, Field(description="Only open tasks past their due date.")] = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> list[dict[str, Any]]:
        """Find tasks, across all projects or in one, filtered by status, assignee or overdue. Sorted by due date."""
        params = {"project_id": project_id, "status": status, "assignee": assignee, "limit": limit}
        if overdue_only:
            params["overdue"] = "true"
        return [_task(t) for t in await api.call(ws(ctx), "GET", "/tasks", params=params)]

    @server.tool(roles=["reader"], read_only_hint=True)
    async def get_task(ctx: RequestContext, task_id: TaskId) -> dict[str, Any]:
        """Get one task with its full description and every comment on it."""
        task = await api.call(ws(ctx), "GET", f"/tasks/{task_id}")
        comments = await api.call(ws(ctx), "GET", f"/tasks/{task_id}/comments")
        return {**_task(task), "description": task["description"], "comments": [_comment(c) for c in comments]}

    @server.tool(roles=["reader"], read_only_hint=True)
    async def list_members(ctx: RequestContext) -> list[dict[str, Any]]:
        """List the people in the workspace. Use a member's handle to assign tasks."""
        return await api.call(ws(ctx), "GET", "/members")

    # -- writes: the editor role. Reversible, so no approval, but rate limited --

    @server.tool(roles=["editor"], rate_limit="30/min")
    async def create_task(
        ctx: RequestContext,
        project_id: ProjectId,
        title: Annotated[str, Field(min_length=1, max_length=200)],
        assignee: Handle | None = None,
        due_date: Annotated[date | None, Field(description="ISO date, for example '2025-03-01'.")] = None,
        description: Annotated[str, Field(max_length=4000)] = "",
    ) -> dict[str, Any]:
        """Create a task in a project. Returns the new task with its id."""
        body = {
            "title": title,
            "assignee": assignee,
            "description": description,
            "due_date": due_date.isoformat() if due_date else None,
        }
        task = await api.call(ws(ctx), "POST", f"/projects/{project_id}/tasks", json=_drop_none(body))
        return _task(task)

    @server.tool(roles=["editor"], rate_limit="30/min", idempotent_hint=True)
    async def update_task(
        ctx: RequestContext,
        task_id: TaskId,
        status: TaskStatus | None = None,
        assignee: Handle | None = None,
        due_date: Annotated[date | None, Field(description="ISO date, for example '2025-03-01'.")] = None,
        title: Annotated[str | None, Field(min_length=1, max_length=200)] = None,
    ) -> dict[str, Any]:
        """Change a task's status, assignee, due date or title. Only the fields you pass change."""
        body = _drop_none(
            {
                "status": status,
                "assignee": assignee,
                "title": title,
                "due_date": due_date.isoformat() if due_date else None,
            }
        )
        return _task(await api.call(ws(ctx), "PATCH", f"/tasks/{task_id}", json=body))

    @server.tool(roles=["editor"], rate_limit="30/min")
    async def add_comment(
        ctx: RequestContext,
        task_id: TaskId,
        body: Annotated[str, Field(min_length=1, max_length=4000, description="The comment text.")],
    ) -> dict[str, Any]:
        """Comment on a task. The comment is signed with your agent's name, so people can see an agent wrote it."""
        # The author comes from the verified key, never from the agent.
        comment = await api.call(
            ws(ctx), "POST", f"/tasks/{task_id}/comments", json={"body": body, "author": f"agent:{ctx.client_id}"}
        )
        return _comment(comment)

    @server.tool(roles=["editor"], rate_limit="10/min")
    async def create_project(
        ctx: RequestContext,
        name: Annotated[str, Field(min_length=1, max_length=120)],
        description: Annotated[str, Field(max_length=2000)] = "",
    ) -> dict[str, Any]:
        """Create a new, empty project."""
        return _project(await api.call(ws(ctx), "POST", "/projects", json={"name": name, "description": description}))

    @server.tool(roles=["editor"], rate_limit="10/min", idempotent_hint=True)
    async def archive_project(ctx: RequestContext, project_id: ProjectId) -> dict[str, Any]:
        """Archive a finished project. It keeps its tasks and can be reactivated in Taskboard.
        Prefer this over deleting."""
        return _project(await api.call(ws(ctx), "PATCH", f"/projects/{project_id}", json={"status": "archived"}))

    # -- destructive: the admin role AND a person's approval ---------------------

    @server.tool(roles=["admin"], requires_approval=True, destructive_hint=True, rate_limit="5/min")
    async def delete_project(ctx: RequestContext, project_id: ProjectId) -> dict[str, Any]:
        """Permanently delete a project with all its tasks and comments.
        A person in the workspace must approve it first; the call waits for that decision.
        Use archive_project unless the user asked to delete."""
        project = await api.call(ws(ctx), "GET", f"/projects/{project_id}")
        await api.call(ws(ctx), "DELETE", f"/projects/{project_id}")
        return {"deleted": project_id, "name": project["name"]}

    return Gateway(server=server, approver=approver, keys=keys, audit=audit, api=api)


# The agent sees these fields. Workspace ids and timestamps stay out of its context.


def _project(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p[k] for k in ("id", "name", "description", "status", "open_tasks", "overdue_tasks")}


def _task(t: dict[str, Any]) -> dict[str, Any]:
    return {k: t[k] for k in ("id", "project_id", "title", "assignee", "status", "due_date", "overdue")}


def _comment(c: dict[str, Any]) -> dict[str, Any]:
    return {k: c[k] for k in ("id", "author", "body", "created_at")}


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}
