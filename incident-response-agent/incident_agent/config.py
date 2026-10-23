"""The incident agent as an Agent Runtime process.

Everything that decides how the agent behaves in production is in this
file: what wakes it (a signed webhook from the alerting system), which
systems it can reach (the ops MCP server, with its own API key), which
tools wait for a person (the approval policy), how much it may do per
alert (the budget), what it remembers (past incidents per service) and
where the record goes (journal and trace).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from promptise import ApprovalPolicy
from promptise.observability_config import ObservabilityConfig, ObserveLevel, TransporterType
from promptise.runtime import (
    AgentProcess,
    BudgetConfig,
    ContextConfig,
    HookConfig,
    JournalConfig,
    ProcessConfig,
    ToolCostAnnotation,
    TriggerConfig,
)

from incident_agent.memory import IncidentMemory
from incident_agent.prompts import render_instructions
from incident_agent.trace import IncidentTrace

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Fixes that change production. Each one waits for the on-call engineer.
RISKY_TOOLS = ["restart_service", "rollback_deploy", "scale_service"]

WEBHOOK_PATH = "/alerts"
DEFAULT_WEBHOOK_PORT = 9095


def build_config(
    *,
    model: Any,
    reviewer: Any,
    state_dir: Path,
    trace: IncidentTrace,
    memory: IncidentMemory,
    ops_url: str,
    ops_api_key: str,
    webhook_secret: str,
    webhook_port: int = DEFAULT_WEBHOOK_PORT,
    approval_timeout: float = 900,  # 15 minutes to answer a page, then the fix is denied
) -> ProcessConfig:
    return ProcessConfig(
        model=model,
        instructions=render_instructions().text,
        # The ops MCP server checks this key and gives the agent its roles.
        servers={"ops": {"url": ops_url, "api_key": ops_api_key}},
        extra_tools=[memory.recall_tool()],
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
        budget=BudgetConfig(
            enabled=True,
            # One production change per alert. A second one is refused before it reaches the reviewer.
            max_irreversible_per_run=1,
            tool_costs={name: ToolCostAnnotation(cost_weight=5.0, irreversible=True) for name in RISKY_TOOLS},
            # A runaway loop stops long before it hurts; a normal incident takes 12 to 20 calls.
            max_tool_calls_per_run=40,
            # An alert storm can't run up the model bill.
            max_runs_per_day=100,
            # Any of these crossed: the process pauses until a person resumes it.
            on_exceeded="pause",
        ),
        # Every trigger event, run and tool call, in an append-only journal for recovery and audit.
        journal=JournalConfig(level="full", path=str(state_dir / "journal")),
        observe=ObservabilityConfig(
            level=ObserveLevel.FULL,
            session_name="incident-agent",
            transporters=[TransporterType.CALLBACK, TransporterType.HTML],
            on_event=trace.on_event,
            output_dir=str(state_dir / "traces"),
        ),
        # Learn from each finished incident: store its summary in the per-service memory.
        hooks=[HookConfig(event="task_completed", callback=memory.lesson_hook(trace))],
        # Each alert is its own investigation: no chat history from the last incident.
        context=ContextConfig(conversation_history=False),
        # One incident at a time keeps approvals and traces in order.
        concurrency=1,
        max_invocation_time=1800,
    )


def create_process(**kwargs: Any) -> AgentProcess:
    return AgentProcess("incident-agent", build_config(**kwargs))
