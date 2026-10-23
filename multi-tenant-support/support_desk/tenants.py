"""The tenant registry: which companies use the support agent, and on which plan.

In production this is your tenants table. The login service reads it to put
the plan's scopes into each user's token, and the support backend reads it to
size each tenant's rate limit. Nothing else decides what a tenant may do.
"""

from __future__ import annotations

from dataclasses import dataclass

TICKETS_READ = "tickets:read"
TICKETS_WRITE = "tickets:write"
ORDERS_READ = "orders:read"


@dataclass(frozen=True)
class Plan:
    name: str
    scopes: frozenset[str]
    calls_per_minute: float  # sustained backend tool calls for the whole tenant
    burst: int  # calls allowed back to back before the rate applies


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    company: str
    plan: str


PLANS: dict[str, Plan] = {
    "business": Plan(
        "business",
        frozenset({TICKETS_READ, TICKETS_WRITE, ORDERS_READ}),
        calls_per_minute=120,
        burst=30,
    ),
    # Trial tenants get tickets but no order lookups, and a small quota.
    "trial": Plan(
        "trial",
        frozenset({TICKETS_READ, TICKETS_WRITE}),
        calls_per_minute=5,
        burst=5,
    ),
}

TENANTS: dict[str, Tenant] = {
    "acme": Tenant("acme", "Acme Robotics", plan="business"),
    "globex": Tenant("globex", "Globex Home", plan="trial"),
}


def plan_for(tenant_id: str | None) -> Plan | None:
    """The tenant's plan, or None for an identity that is not a customer tenant."""
    tenant = TENANTS.get(tenant_id or "")
    return PLANS[tenant.plan] if tenant else None
