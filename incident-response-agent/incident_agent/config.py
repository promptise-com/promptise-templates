"""The incident agent as an Agent Runtime process.

Everything that decides how the agent behaves in production is in this
file: what wakes it (a signed webhook from the alerting system), which
systems it can reach (the ops MCP server), which tools wait for a person
(the approval policy) and where the trace goes (observability).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from promptise import ApprovalPolicy
from promptise.observability_config import ObservabilityConfig, ObserveLevel, TransporterType
from promptise.runtime import AgentProcess, ContextConfig, ProcessConfig, TriggerConfig

from incident_agent.trace import IncidentTrace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text()

# Fixes that change production. Each one waits for the on-call engineer.
RISKY_TOOLS = ["restart_service", "rollback_deploy", "scale_service"]

WEBHOOK_PATH = "/alerts"
DEFAULT_WEBHOOK_PORT = 9095


def ops_server(state_dir: Path) -> dict[str, Any]:
    """The ops MCP server, started by the process over stdio.

    To use your real systems, replace this entry (or add entries) with your
    own MCP servers, for example ``{"url": "https://logs-mcp.internal/mcp"}``.
    """
    return {
        "command": sys.executable,
        "args": ["-m", "ops_systems.server"],
        "cwd": str(PROJECT_ROOT),
        "env": {"INCIDENT_STATE_DIR": str(state_dir)},
    }


def build_config(
    *,
    model: Any,
    reviewer: Any,
    state_dir: Path,
    trace: IncidentTrace,
    webhook_secret: str,
    webhook_port: int = DEFAULT_WEBHOOK_PORT,
    approval_timeout: float = 900,  # 15 minutes to answer a page, then the fix is denied
) -> ProcessConfig:
    return ProcessConfig(
        model=model,
        instructions=INSTRUCTIONS,
        servers={"ops": ops_server(state_dir)},
        triggers=[
            TriggerConfig(
                type="webhook",
                webhook_path=WEBHOOK_PATH,
                webhook_port=webhook_port,
                hmac_secret=webhook_secret,
                # Alertmanager also sends "resolved" notifications; only a firing alert needs work.
                filter_expression="payload.status == 'firing'",
            )
        ],
        approval=ApprovalPolicy(
            tools=RISKY_TOOLS,
            handler=reviewer,
            timeout=approval_timeout,
            on_timeout="deny",
            on_decision=trace.on_decision,
        ),
        observe=ObservabilityConfig(
            level=ObserveLevel.FULL,
            session_name="incident-agent",
            transporters=[TransporterType.CALLBACK, TransporterType.HTML],
            on_event=trace.on_event,
            output_dir=str(state_dir / "traces"),
        ),
        # Each alert is its own investigation: no chat history from the last incident.
        context=ContextConfig(conversation_history=False),
        # One incident at a time keeps approvals and traces in order.
        concurrency=1,
        max_invocation_time=1800,
    )


def create_process(**kwargs: Any) -> AgentProcess:
    return AgentProcess("incident-agent", build_config(**kwargs))
