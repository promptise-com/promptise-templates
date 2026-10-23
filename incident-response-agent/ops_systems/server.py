"""Ops MCP server: the systems an on-call engineer uses, as tools.

One server stands in for several real ones: the log store (Loki, Elastic),
the metrics store (Prometheus), the deploy system (Argo CD, Spinnaker), the
runbook wiki, the incident tool (PagerDuty, incident.io) and the team chat.
In production each of these is usually its own MCP server; the agent code
does not change, only the server entry in ``incident_agent/config.py``.

It runs as its own service over HTTP, like a real internal API:

    OPS_AGENT_API_KEY=... python -m ops_systems.server --port 8765

Every caller presents an API key. The key decides the caller's roles:

- ``viewer``: read logs, metrics, deploys, runbooks (a status page, a chatbot);
- ``responder``: also open incidents and post in the incident channel;
- ``remediator``: also restart, roll back and scale.

The incident agent holds all three. Its approval policy still holds every
remediation for a person; the roles make sure no other client can skip that.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from promptise.mcp.server import APIKeyAuth, AuditMiddleware, AuthMiddleware, MCPServer, ToolError
from pydantic import BaseModel, Field

from ops_systems.store import OpsError, OpsWorld

Service = Annotated[str, Field(description='Service name, for example "checkout-service".')]
Reason = Annotated[str, Field(min_length=10, description="Why, in one sentence, for the change log.")]
Metric = Literal["error_rate", "latency_p95_ms", "request_rate", "cpu", "memory"]

READ = ["viewer", "responder", "remediator"]
RESPOND = ["responder"]
REMEDIATE = ["remediator"]
# A blast-radius limit per client, on top of approvals: no caller changes production more often than this.
CHANGE_RATE = "3/hour"


class Finding(BaseModel):
    time: str = Field(description="ISO 8601 UTC time, copied from a tool result (a log line, a deploy).")
    event: str = Field(description="What happened then, in one short sentence.")


def api_keys_from_env() -> dict[str, dict[str, Any]]:
    """The keys this server accepts, from the environment (never from a file in the repo)."""
    keys: dict[str, dict[str, Any]] = {}
    agent_key = os.environ.get("OPS_AGENT_API_KEY")
    if agent_key:
        keys[agent_key] = {"client_id": "incident-agent", "roles": ["viewer", "responder", "remediator"]}
    viewer_key = os.environ.get("OPS_VIEWER_API_KEY")
    if viewer_key:
        keys[viewer_key] = {"client_id": "status-page", "roles": ["viewer"]}
    if not keys:
        raise SystemExit("Set OPS_AGENT_API_KEY: the ops server accepts no anonymous callers.")
    return keys


def build_server(state_dir: str | os.PathLike[str], api_keys: dict[str, dict[str, Any]]) -> MCPServer:
    world = OpsWorld(state_dir)
    server = MCPServer(name="ops", version="1.0.0", require_auth=True)
    # Audit first, so refused calls (bad key, wrong role, rate limit) are recorded too.
    # Set PROMPTISE_AUDIT_SECRET to sign the log as an HMAC chain.
    signed = bool(os.environ.get("PROMPTISE_AUDIT_SECRET"))
    server.add_middleware(
        AuditMiddleware(log_path=str(Path(state_dir) / "ops-audit.jsonl"), signed=signed, include_args=True)
    )
    server.add_middleware(AuthMiddleware(APIKeyAuth(keys=api_keys)))

    def run(fn: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
        # OpsError carries a message the model can act on; ToolError hands it over as the result.
        try:
            return fn(*args)
        except OpsError as exc:
            raise ToolError(str(exc)) from None

    # ── read-only: safe for the agent to call on its own ───────────────────

    @server.tool(roles=READ, read_only_hint=True)
    async def search_logs(
        service: Service,
        minutes: Annotated[int, Field(ge=1, le=240, description="How far back to search.")] = 30,
        level: Annotated[Literal["INFO", "WARN", "ERROR"], Field(description="Lowest level returned.")] = "ERROR",
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict:
        """Search a service's logs. Returns the matching lines and the most frequent messages."""
        return run(world.search_logs, service, minutes, level, limit)

    @server.tool(roles=READ, read_only_hint=True)
    async def query_metric(
        service: Service,
        metric: Metric,
        minutes: Annotated[int, Field(ge=1, le=240, description="How far back the series goes.")] = 30,
    ) -> dict:
        """Read one metric of a service: a per-minute series, the baseline and the current value."""
        return run(world.query_metric, service, metric, minutes)

    @server.tool(roles=READ, read_only_hint=True)
    async def list_deploys(
        service: Service,
        hours: Annotated[int, Field(ge=1, le=168)] = 24,
    ) -> dict:
        """List a service's deploys in the last hours, newest first, and the version it runs now."""
        return run(world.list_deploys, service, hours)

    @server.tool(roles=READ, read_only_hint=True)
    async def find_runbook(
        alert_name: Annotated[str, Field(description='The alert name, for example "HighErrorRate".')],
    ) -> dict:
        """Get the runbook for an alert: how to investigate, mitigate and verify."""
        return run(world.find_runbook, alert_name)

    # ── incident tool and chat: visible to people, but safe to repeat ──────

    @server.tool(roles=RESPOND, idempotent_hint=True)
    async def open_incident(
        alert_name: str,
        service: Service,
        severity: Annotated[str, Field(description='The alert\'s severity label, for example "page".')],
        fingerprint: Annotated[str, Field(description="The alert's fingerprint from Alertmanager.")],
    ) -> dict:
        """Open an incident for a firing alert, or return the open one with the same fingerprint."""
        return run(world.open_incident, alert_name, service, severity, fingerprint)

    @server.tool(roles=RESPOND)
    async def post_status_update(
        incident_id: Annotated[str, Field(description='For example "INC-1042".')],
        message: Annotated[str, Field(min_length=1, max_length=2000)],
    ) -> dict:
        """Post a status update in the incident's chat channel."""
        return run(world.post_status_update, incident_id, message)

    @server.tool(roles=RESPOND)
    async def write_incident_summary(
        incident_id: str,
        status: Literal["mitigated", "escalated", "investigating"],
        summary: Annotated[str, Field(description="Two or three sentences: what broke, impact, what fixed it.")],
        root_cause: str,
        findings: Annotated[list[Finding], Field(description="Key moments you found: deploys, first errors.")],
        follow_ups: list[str],
    ) -> dict:
        """Write the incident summary. The timeline merges your findings with the incident's own record of
        status updates and changes, so do not repeat those. Call it once, at the end."""
        rows = [finding.model_dump() for finding in findings]
        return run(world.write_incident_summary, incident_id, status, summary, root_cause, rows, follow_ups)

    # ── risky: role-guarded and rate-limited here, held for a person by the agent ──

    @server.tool(roles=REMEDIATE, rate_limit=CHANGE_RATE, destructive_hint=True)
    async def restart_service(service: Service, reason: Reason) -> dict:
        """Restart every replica of a service (rolling). Needs approval."""
        return run(world.restart_service, service, reason)

    @server.tool(roles=REMEDIATE, rate_limit=CHANGE_RATE, destructive_hint=True)
    async def rollback_deploy(
        service: Service,
        to_version: Annotated[str, Field(description='The version to go back to, for example "v2.13.4".')],
        reason: Reason,
    ) -> dict:
        """Roll a service back to an earlier deployed version. Needs approval."""
        return run(world.rollback_deploy, service, to_version, reason)

    @server.tool(roles=REMEDIATE, rate_limit=CHANGE_RATE, destructive_hint=True)
    async def scale_service(
        service: Service,
        replicas: Annotated[int, Field(ge=1, le=50)],
        reason: Reason,
    ) -> dict:
        """Set the number of replicas of a service. Needs approval."""
        return run(world.scale_service, service, replicas, reason)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ops MCP server over HTTP.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPS_MCP_PORT", "8765")))
    parser.add_argument("--state-dir", default=os.environ.get("INCIDENT_STATE_DIR", "var/state"))
    args = parser.parse_args()
    build_server(args.state_dir, api_keys_from_env()).run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
