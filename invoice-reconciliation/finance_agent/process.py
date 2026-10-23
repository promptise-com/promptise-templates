"""Wire the agent into an Agent Runtime process: triggers, servers, rules, traces."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from promptise import ApprovalHandler
from promptise.config import StdioServerSpec
from promptise.observability_config import ObservabilityConfig, ObserveLevel, TransporterType
from promptise.runtime import ContextConfig, JournalConfig, ProcessConfig, TriggerConfig

from fake_systems.store import DB_ENV

from .hooks import budget_refusal_hook, denied_refund_recorder
from .instructions import INSTRUCTIONS
from .rules import approval_audit_log, autonomy_budget, refund_approval_policy

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROCESS_NAME = "finance-ops"


@dataclass
class Settings:
    """Everything that differs between the demo, the tests and production."""

    db_path: Path
    """The fake systems' SQLite file."""
    webhook_secret: str
    """Stripe's signing secret (whsec_...) for the webhook endpoint."""
    reviewer: ApprovalHandler
    """Who decides refunds above the threshold."""
    model: Any = "openai:gpt-5-mini"
    """A model id, or a model object (the demo and tests pass a ScriptedModel)."""
    webhook_port: int = 8787
    approval_timeout: float = 1800.0
    """Seconds a refund waits for a person before it is denied."""
    cron_expression: str = "30 4 * * *"
    cron_timezone: str = "Europe/Berlin"
    """04:30 Berlin: after the card networks settle, outside the DST hours."""
    trace_dir: Path = PROJECT_ROOT / ".promptise" / "traces"
    journal_dir: Path | None = PROJECT_ROOT / ".promptise" / "journal"


def mcp_servers(db_path: Path) -> dict[str, StdioServerSpec]:
    """The billing system and the email service, each as its own MCP server."""
    env = {DB_ENV: str(db_path)}
    return {
        name: StdioServerSpec(
            command=sys.executable,
            args=["-m", f"fake_systems.{name}_server"],
            env=env,
            cwd=str(PROJECT_ROOT),
        )
        for name in ("billing", "email")
    }


def build_process_config(settings: Settings) -> ProcessConfig:
    settings.trace_dir.mkdir(parents=True, exist_ok=True)
    return ProcessConfig(
        model=settings.model,
        instructions=INSTRUCTIONS,
        servers=mcp_servers(settings.db_path),
        triggers=[
            TriggerConfig(
                type="cron",
                cron_expression=settings.cron_expression,
                cron_timezone=settings.cron_timezone,
            ),
            TriggerConfig(
                type="webhook",
                webhook_path="/stripe",
                webhook_port=settings.webhook_port,
                hmac_secret=settings.webhook_secret,
                signature_scheme="stripe",  # checks Stripe-Signature, rejects replays
                # Other Stripe events are answered 200 "ignored" and cost nothing.
                filter_expression="payload['type'] == 'invoice.payment_failed'",
            ),
        ],
        # Each event is its own job: no chat history carried between runs.
        context=ContextConfig(conversation_history=False),
        budget=autonomy_budget(),
        approval=refund_approval_policy(
            settings.reviewer,
            timeout=settings.approval_timeout,
            on_decision=[
                approval_audit_log(settings.trace_dir / "approvals.jsonl"),
                denied_refund_recorder(settings.db_path),
            ],
        ),
        # Every LLM turn and tool call, as one JSON line per event.
        observe=ObservabilityConfig(
            level=ObserveLevel.STANDARD,
            session_name=PROCESS_NAME,
            transporters=[TransporterType.STRUCTURED_LOG],
            log_file=str(settings.trace_dir / "finance-ops.jsonl"),
        ),
        # The journal records each run, tool call and refused call, and keeps
        # the daily budget counters across restarts.
        journal=(
            JournalConfig(level="full", path=str(settings.journal_dir))
            if settings.journal_dir
            else JournalConfig(level="none")
        ),
        # A refund the budget refused becomes a finance note, written by code.
        hooks=[budget_refusal_hook(settings.db_path)],
        restart_policy="on_failure",
    )
