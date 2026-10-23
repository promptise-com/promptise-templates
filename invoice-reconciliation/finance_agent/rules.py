"""The business rules, as code.

A prompt can ask the model to be careful with refunds.  Only code can make
sure it is.  Each limit here is enforced by the runtime before a tool runs,
whatever the model writes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from promptise import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalRule,
    AutoApprovalClassifier,
)
from promptise.runtime import BudgetConfig, ToolCostAnnotation

REFUND_TOOL = "refund_duplicate_charge"

REFUND_APPROVAL_THRESHOLD_CENTS = 10_000
"""Refunds up to 100.00 EUR go through on their own.  Larger ones wait for a person."""

MAX_REFUNDS_PER_RUN = 3
"""More than three double charges in one night means something upstream broke.
The fourth refund is refused and the agent pauses until finance looks."""


async def refund_within_auto_limit(request: ApprovalRequest) -> bool:
    """True when a refund is small enough to skip the human reviewer.

    Anything unexpected (a missing or non-integer amount) returns False, so
    the call goes to a person instead of through.
    """
    amount = request.arguments.get("amount_cents")
    return isinstance(amount, int) and 0 < amount <= REFUND_APPROVAL_THRESHOLD_CENTS


def approval_audit_log(path: Path) -> Callable[[ApprovalRequest, ApprovalDecision], None]:
    """An ``on_decision`` callback that appends every refund decision to *path*.

    It sees every decision: the rule's, the reviewer's and the gate's own
    (a timeout, a handler error), so the log answers "who approved this?".
    """

    def record(request: ApprovalRequest, decision: ApprovalDecision) -> None:
        line = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(decision.timestamp)),
            "tool": request.tool_name,
            "arguments": request.arguments,
            "approved": decision.approved,
            "decided_by": decision.decided_by,
            "reviewer": decision.reviewer_id,
            "reason": decision.reason,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

    return record


def refund_approval_policy(
    reviewer: ApprovalHandler,
    *,
    timeout: float,
    on_decision: Sequence[Callable[[ApprovalRequest, ApprovalDecision], None]] = (),
) -> ApprovalPolicy:
    """Gate every refund: small ones are approved by a rule, the rest by *reviewer*.

    Each *on_decision* callback sees every decision, including the gate's own.
    """

    def decided(request: ApprovalRequest, decision: ApprovalDecision) -> None:
        for callback in on_decision:
            callback(request, decision)

    classifier = AutoApprovalClassifier(
        allow_rules=[
            ApprovalRule(
                tool=REFUND_TOOL,
                predicate=refund_within_auto_limit,
                reason=f"duplicate refund up to {REFUND_APPROVAL_THRESHOLD_CENTS // 100} EUR",
            )
        ],
        # The refund tool is the only gated tool, and it is never read-only.
        read_only_auto_allow=False,
        fallback=reviewer,
    )
    return ApprovalPolicy(
        tools=[REFUND_TOOL],
        handler=classifier,
        timeout=timeout,
        on_timeout="deny",  # nobody answered: no money moves
        on_decision=decided,
    )


def autonomy_budget() -> BudgetConfig:
    """Limits per run and per day.  Only refunds count as irreversible."""
    return BudgetConfig(
        enabled=True,
        max_irreversible_per_run=MAX_REFUNDS_PER_RUN,
        max_tool_calls_per_run=40,
        max_runs_per_day=200,  # a webhook storm stops here, not at the model bill
        tool_costs={
            REFUND_TOOL: ToolCostAnnotation(cost_weight=5.0, irreversible=True),
            "send_customer_email": ToolCostAnnotation(cost_weight=2.0),
        },
        # The run that hits a limit finishes, then the process pauses until
        # someone calls resume().  Webhook events queue up meanwhile.
        on_exceeded="pause",
    )
