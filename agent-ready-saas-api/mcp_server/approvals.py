"""Four-eyes review of destructive calls, scoped to the reviewer's workspace.

A tool declared with ``requires_approval=True`` parks in the PendingApprover
until a person with the ``approver`` role decides. The SDK's stock
``approvals_list`` / ``approvals_decide`` tools are server-wide, which on a
multi-tenant server would show Acme's pending calls to Globex's reviewers.
These two tools only ever show and decide calls from the reviewer's own
workspace, and nobody may approve a call they made themselves.
"""

from __future__ import annotations

from typing import Annotated, Any

from promptise.mcp.server import MCPServer, PendingApprover, RequestContext, ToolError
from pydantic import Field


def register_approval_tools(server: MCPServer, approver: PendingApprover) -> None:
    @server.tool(roles=["approver"], tags=["approvals"], read_only_hint=True)
    async def approvals_list(ctx: RequestContext) -> list[dict[str, Any]]:
        """List calls from your workspace that wait for a person's approval:
        tool, arguments, which agent asked, and how long ago."""
        return [p for p in approver.pending() if p["tenant_id"] == ctx.client.tenant_id]

    @server.tool(roles=["approver"], tags=["approvals"])
    async def approvals_decide(
        ctx: RequestContext,
        request_id: Annotated[str, Field(description="The request_id from approvals_list.")],
        approve: Annotated[bool, Field(description="True runs the call; False denies it.")],
        reason: Annotated[str, Field(max_length=500, description="Shown to the agent on a denial.")] = "",
    ) -> dict[str, Any]:
        """Approve or deny one pending call from your workspace."""
        entry = next((p for p in approver.pending() if p["request_id"] == request_id), None)
        # Another workspace's request looks exactly like an unknown one.
        if entry is None or entry["tenant_id"] != ctx.client.tenant_id:
            raise ToolError(f"No pending request {request_id!r} in your workspace.", code="NOT_FOUND")
        if approve and entry["client_id"] == ctx.client_id:
            raise ToolError("You cannot approve a call you made yourself.", code="ACCESS_DENIED")
        resolved = approver.decide(request_id, approve, reviewer_id=ctx.client_id, reason=reason or None)
        return {"request_id": request_id, "approved": approve, "resolved": resolved}
