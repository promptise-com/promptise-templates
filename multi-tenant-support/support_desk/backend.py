"""The support backend: tickets and orders as an MCP server, one per SaaS.

Every tenant talks to the same server. Isolation comes from the token on each
request, never from tool arguments:

* ``AuthMiddleware`` verifies the user's JWT and puts its ``org`` claim on
  ``ctx.client.tenant_id``. ``require_tenant=True`` refuses any call without one.
* ``HasScope`` guards decide which tools a tenant's plan includes, and
  ``hide_unauthorized_tools=True`` lists only those tools to that tenant.
* ``RateLimitMiddleware`` keeps one token bucket per tenant, sized by its plan.
* ``AuditMiddleware`` records every call with the caller's tenant.

No tool takes a tenant parameter, so a prompt that says "show me Globex's
tickets" has nothing to change: the server reads the tenant from the token.

Run it on its own with ``python -m support_desk.backend`` (set
SUPPORT_JWT_SECRET to the secret your login service signs with).
"""

from __future__ import annotations

import argparse
import os
from typing import Annotated, Any, Literal

from promptise.mcp.server import (
    AuditMiddleware,
    AuthMiddleware,
    ClientContext,
    HasScope,
    JWTAuth,
    MCPServer,
    RateLimitMiddleware,
    RequestContext,
    TokenBucketLimiter,
    ToolError,
)
from pydantic import Field

from .identity import AUDIENCE, ISSUER, TENANT_CLAIM, signing_secret
from .records import TenantRecords, TicketDesk
from .tenants import ORDERS_READ, PLANS, TICKETS_READ, TICKETS_WRITE, plan_for


class PlanLimiter:
    """One token bucket per tenant, sized by the tenant's plan.

    ``RateLimitMiddleware`` hands the limiter only the bucket key, so the key
    carries the plan name and the tenant: ``"trial|globex"``.
    """

    def __init__(self) -> None:
        self._by_plan = {
            name: TokenBucketLimiter(rate_per_minute=plan.calls_per_minute, burst=plan.burst)
            for name, plan in PLANS.items()
        }
        # Identities outside a customer tenant (the agent's own credential).
        self._fallback = TokenBucketLimiter(rate_per_minute=10, burst=5)

    def consume(self, key: str) -> tuple[bool, float]:
        plan, _, _tenant = key.partition("|")
        return self._by_plan.get(plan, self._fallback).consume(key)


def tenant_bucket(ctx: RequestContext) -> str:
    """Bucket key: every user of a tenant draws from the same quota."""
    return f"{ctx.client.extra.get('plan', 'none')}|{ctx.client.tenant_id}"


def attach_plan(client: ClientContext, ctx: RequestContext) -> None:
    """Runs after each successful authentication: look up the tenant's plan."""
    plan = plan_for(client.tenant_id)
    client.extra["plan"] = plan.name if plan else "none"


def build_server(
    desk: TicketDesk | None = None,
    *,
    audit: AuditMiddleware | None = None,
    secret: str | None = None,
) -> MCPServer:
    """Build the support backend. Each call gets fresh rate-limit buckets.

    Pass ``audit`` to read the audit trail afterwards (the demo prints it).
    """
    desk = desk or TicketDesk()
    audit = audit or AuditMiddleware(include_args=True, signed=False)
    server = MCPServer(
        "support-desk",
        instructions="Tickets and orders for the signed-in end user's company.",
        require_tenant=True,
        hide_unauthorized_tools=True,
    )
    # Order matters: identify the caller, record the call, then apply the quota.
    server.add_middleware(
        AuthMiddleware(
            JWTAuth(secret=secret or signing_secret(), audience=AUDIENCE, issuer=ISSUER),
            tenant_claim=TENANT_CLAIM,
            on_authenticate=attach_plan,
        )
    )
    server.add_middleware(audit)
    server.add_middleware(RateLimitMiddleware(PlanLimiter(), key_func=tenant_bucket))

    # Called inside each handler, after AuthMiddleware and the guards ran.
    # (A Depends() provider would run before authentication, with no tenant.)
    def caller_records(ctx: RequestContext) -> TenantRecords:
        """The verified caller's tenant records. Tools never pick a tenant."""
        records = desk.for_tenant(ctx.client.tenant_id)
        if records is None:
            raise ToolError("This organisation has no support workspace.")
        return records

    @server.tool(read_only_hint=True, guards=[HasScope(TICKETS_READ)])
    async def list_tickets(
        ctx: RequestContext,
        status: Literal["open", "pending_customer", "solved"] | None = None,
    ) -> list[dict[str, Any]]:
        """List the signed-in customer's support tickets, newest first.

        Args:
            status: Only tickets with this status.
        """
        rows = caller_records(ctx).tickets(requester=ctx.client.client_id, status=status)
        return sorted(rows, key=lambda t: t["created"], reverse=True)

    @server.tool(read_only_hint=True, guards=[HasScope(TICKETS_READ)])
    async def get_ticket(
        ctx: RequestContext,
        ticket_id: Annotated[str, Field(description="Ticket ID, for example ACME-1041.")],
    ) -> dict[str, Any]:
        """Get one of the signed-in customer's tickets by its ID."""
        ticket = caller_records(ctx).ticket(ticket_id.strip().upper(), requester=ctx.client.client_id)
        if ticket is None:
            # Same answer whether the ticket belongs to another customer, another
            # tenant, or nobody: the reply must not confirm that an ID exists.
            raise ToolError(f"Ticket {ticket_id} was not found for this customer.")
        return ticket

    @server.tool(guards=[HasScope(TICKETS_WRITE)])
    async def open_ticket(
        ctx: RequestContext,
        subject: Annotated[str, Field(min_length=5, max_length=120)],
        description: Annotated[str, Field(min_length=10, max_length=2000)],
        priority: Literal["low", "normal", "high"] = "normal",
    ) -> dict[str, Any]:
        """Open a new support ticket for the signed-in customer.

        Args:
            subject: One-line summary of the problem.
            description: What happened, in the customer's words.
            priority: How urgent the problem is.
        """
        return caller_records(ctx).open_ticket(ctx.client.client_id, subject, description, priority)

    @server.tool(read_only_hint=True, guards=[HasScope(ORDERS_READ)])
    async def get_order(
        ctx: RequestContext,
        order_id: Annotated[str, Field(description="Order number, for example AC-5001.")],
    ) -> dict[str, Any]:
        """Get the status and tracking of one of the signed-in customer's orders."""
        order = caller_records(ctx).order(order_id.strip().upper(), customer=ctx.client.client_id)
        if order is None:
            raise ToolError(f"Order {order_id} was not found for this customer.")
        return order

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the support backend MCP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not os.environ.get("SUPPORT_JWT_SECRET"):
        raise SystemExit("Set SUPPORT_JWT_SECRET to the secret your login service signs tokens with.")
    build_server().run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
