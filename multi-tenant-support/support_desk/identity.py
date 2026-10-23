"""A stand-in for your login service.

Your web app already knows who is signed in and which company they belong to.
This module turns that into the two things the agent needs per request: a
signed token for the support backend, and a ``CallerContext`` for the agent's
own isolation (memory, help center). Both come from the same user record, so
the tenant the agent sees and the tenant the backend sees cannot disagree.

Replace ``LoginService`` with your identity provider (Auth0, Okta, Cognito,
Keycloak...). Keep the tenant claim and the scopes; the backend checks both.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from promptise import CallerContext
from promptise.mcp.server import JWTAuth

from .tenants import PLANS, TENANTS

AUDIENCE = "api://support-desk"
ISSUER = "https://login.example-saas.test"
TENANT_CLAIM = "org"

# One signing secret per process unless you set one. The demo runs the login
# service and the backend in one process, so a random secret is enough there.
# A backend that runs on its own needs SUPPORT_JWT_SECRET set to the same value.
_SECRET = os.environ.get("SUPPORT_JWT_SECRET") or secrets.token_hex(32)


def signing_secret() -> str:
    return _SECRET


@dataclass(frozen=True)
class EndUser:
    tenant_id: str
    user_id: str
    name: str
    email: str


# Note that both tenants have a user called "alice". Every isolation key in
# this project includes the tenant, so the two never share anything.
USERS: dict[tuple[str, str], EndUser] = {
    ("acme", "alice"): EndUser("acme", "alice", "Alice Moreau", "alice@moreau-family.test"),
    ("acme", "ben"): EndUser("acme", "ben", "Ben Okafor", "ben.okafor@mail.test"),
    ("globex", "gina"): EndUser("globex", "gina", "Gina Patel", "gina.patel@mail.test"),
    ("globex", "alice"): EndUser("globex", "alice", "Alice Brandt", "a.brandt@mail.test"),
}


class LoginService:
    """Issues the tokens your identity provider would issue."""

    def __init__(self, secret: str | None = None) -> None:
        self._jwt = JWTAuth(secret=secret or signing_secret(), audience=AUDIENCE, issuer=ISSUER)

    def token_for(self, tenant_id: str, user_id: str, *, expires_in: int = 900) -> str:
        user = USERS[(tenant_id, user_id)]
        plan = PLANS[TENANTS[tenant_id].plan]
        return self._jwt.create_token(
            {
                "sub": user.user_id,
                TENANT_CLAIM: user.tenant_id,
                "scope": " ".join(sorted(plan.scopes)),
                "email": user.email,
            },
            expires_in=expires_in,
        )

    def caller_for(self, tenant_id: str, user_id: str) -> CallerContext:
        """The identity one agent invocation runs as."""
        user = USERS[(tenant_id, user_id)]
        return CallerContext(
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            bearer_token=self.token_for(tenant_id, user_id),
            metadata={"name": user.name},
        )

    def service_token(self) -> str:
        """The agent's own credential, used to discover the backend's tools.

        It holds every scope, so the agent learns about every tool. Each tool
        call is then made with the end user's token and checked against that
        user's tenant and scopes. It belongs to no customer tenant, so a call
        made with it reads no customer data.
        """
        all_scopes = set().union(*(plan.scopes for plan in PLANS.values()))
        return self._jwt.create_token(
            {"sub": "support-agent", TENANT_CLAIM: "support-platform", "scope": " ".join(sorted(all_scopes))},
            expires_in=24 * 3600,
        )
