"""Who may call the billing and email servers, and the audit trail of every call.

Both servers run over HTTP with ``require_auth=True``: a request without a
valid API key is refused before it reaches a tool.  Each key carries roles,
and each tool names the role it needs, so the agent's key can refund while
a read-only reporting key cannot.  ``AuditMiddleware`` writes an
HMAC-chained log of every call; ``verify_audit_log`` detects any later edit.
"""

from __future__ import annotations

import os

from promptise.mcp.server import APIKeyAuth, AuditMiddleware, AuthMiddleware, MCPServer

AGENT_KEY_ENV = "FINANCE_AGENT_API_KEY"
READONLY_KEY_ENV = "FINANCE_READONLY_API_KEY"
AUDIT_LOG_ENV = "AUDIT_LOG"

# Roles the tools check.  One key per caller, one role per kind of action.
READ = "billing.read"
NOTES = "notes.write"
REFUNDS = "refunds.write"
EMAIL = "email.send"


def current_keys() -> dict[str, dict]:
    """The keys valid right now, read on every request (rotate without a restart)."""
    keys: dict[str, dict] = {}
    if agent_key := os.environ.get(AGENT_KEY_ENV):
        keys[agent_key] = {"client_id": "finance-ops-agent", "roles": [READ, NOTES, REFUNDS, EMAIL]}
    if readonly_key := os.environ.get(READONLY_KEY_ENV):
        keys[readonly_key] = {"client_id": "finance-dashboard", "roles": [READ]}
    return keys


def secure(server: MCPServer) -> MCPServer:
    """Add the audit trail and API-key auth to *server*.

    Audit goes first, so a call refused by a role check is in the log too.
    The log is signed with ``PROMPTISE_AUDIT_SECRET``; without it the server
    refuses to start rather than write a log nobody can verify.
    """
    server.add_middleware(
        AuditMiddleware(log_path=os.environ.get(AUDIT_LOG_ENV), include_args=True)
    )
    server.add_middleware(AuthMiddleware(APIKeyAuth(keys=current_keys)))
    return server
