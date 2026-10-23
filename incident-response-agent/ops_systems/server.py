"""Ops MCP server: the systems an on-call engineer uses, as tools.

One server stands in for several real ones: the log store (Loki, Elastic),
the metrics store (Prometheus), the deploy system (Argo CD, Spinnaker), the
runbook wiki, the incident tool (PagerDuty, incident.io) and the team chat.
In production each of these is usually its own MCP server; the agent code
does not change, only the server list in ``incident_agent/config.py``.

Run it on its own with ``python -m ops_systems.server`` (stdio) from the
project root. The agent process starts it for you.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from promptise.mcp.server import AuditMiddleware, MCPServer, ToolError
from pydantic import BaseModel, Field

from ops_systems.store import OpsError, OpsWorld

Service = Annotated[str, Field(description='Service name, for example "checkout-service".')]
Reason = Annotated[str, Field(min_length=10, description="Why, in one sentence, for the change log.")]
Metric = Literal["error_rate", "latency_p95_ms", "request_rate", "cpu", "memory"]


class Finding(BaseModel):
    time: str = Field(description="ISO 8601 UTC time, copied from a tool result (a log line, a deploy).")
    event: str = Field(description="What happened then, in one short sentence.")


def build_server(state_dir: str | os.PathLike[str]) -> MCPServer:
    world = OpsWorld(state_dir)
    server = MCPServer(name="ops", version="1.0.0")
    # Every call, with its arguments, in an append-only log next to the world state.
    # Set PROMPTISE_AUDIT_SECRET to sign the log as an HMAC chain.
    signed = bool(os.environ.get("PROMPTISE_AUDIT_SECRET"))
    server.add_middleware(
        AuditMiddleware(log_path=str(Path(state_dir) / "ops-audit.jsonl"), signed=signed, include_args=True)
    )

    def run(fn: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
        # OpsError carries a message the model can act on; ToolError hands it over as the result.
        try:
            return fn(*args)
        except OpsError as exc:
            raise ToolError(str(exc)) from None

    # ── read-only: safe for the agent to call on its own ───────────────────

    @server.tool(read_only_hint=True)
    async def search_logs(
        service: Service,
        minutes: Annotated[int, Field(ge=1, le=240, description="How far back to search.")] = 30,
        level: Annotated[Literal["INFO", "WARN", "ERROR"], Field(description="Lowest level returned.")] = "ERROR",
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict:
        """Search a service's logs. Returns the matching lines and the most frequent messages."""
        return run(world.search_logs, service, minutes, level, limit)

    @server.tool(read_only_hint=True)
    async def query_metric(
        service: Service,
        metric: Metric,
        minutes: Annotated[int, Field(ge=1, le=240, description="How far back the series goes.")] = 30,
    ) -> dict:
        """Read one metric of a service: a per-minute series, the baseline and the current value."""
        return run(world.query_metric, service, metric, minutes)

    @server.tool(read_only_hint=True)
    async def list_deploys(
        service: Service,
        hours: Annotated[int, Field(ge=1, le=168)] = 24,
    ) -> dict:
        """List a service's deploys in the last hours, newest first, and the version it runs now."""
        return run(world.list_deploys, service, hours)

    @server.tool(read_only_hint=True)
    async def find_runbook(
        alert_name: Annotated[str, Field(description='The alert name, for example "HighErrorRate".')],
    ) -> dict:
        """Get the runbook for an alert: how to investigate, mitigate and verify."""
        return run(world.find_runbook, alert_name)

    # ── incident tool and chat: visible to people, but safe to repeat ──────

    @server.tool(idempotent_hint=True)
    async def open_incident(
        alert_name: str,
        service: Service,
        severity: Annotated[str, Field(description='The alert\'s severity label, for example "page".')],
        fingerprint: Annotated[str, Field(description="The alert's fingerprint from Alertmanager.")],
    ) -> dict:
        """Open an incident for a firing alert, or return the open one with the same fingerprint."""
        return run(world.open_incident, alert_name, service, severity, fingerprint)

    @server.tool()
    async def post_status_update(
        incident_id: Annotated[str, Field(description='For example "INC-1042".')],
        message: Annotated[str, Field(min_length=1, max_length=2000)],
    ) -> dict:
        """Post a status update in the incident's chat channel."""
        return run(world.post_status_update, incident_id, message)

    @server.tool()
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

    # ── risky: the agent's approval policy holds these for a person ────────

    @server.tool(destructive_hint=True)
    async def restart_service(service: Service, reason: Reason) -> dict:
        """Restart every replica of a service (rolling). Needs approval."""
        return run(world.restart_service, service, reason)

    @server.tool(destructive_hint=True)
    async def rollback_deploy(
        service: Service,
        to_version: Annotated[str, Field(description='The version to go back to, for example "v2.13.4".')],
        reason: Reason,
    ) -> dict:
        """Roll a service back to an earlier deployed version. Needs approval."""
        return run(world.rollback_deploy, service, to_version, reason)

    @server.tool(destructive_hint=True)
    async def scale_service(
        service: Service,
        replicas: Annotated[int, Field(ge=1, le=50)],
        reason: Reason,
    ) -> dict:
        """Set the number of replicas of a service. Needs approval."""
        return run(world.scale_service, service, replicas, reason)

    return server


if __name__ == "__main__":
    build_server(os.environ.get("INCIDENT_STATE_DIR", "var/state")).run()
