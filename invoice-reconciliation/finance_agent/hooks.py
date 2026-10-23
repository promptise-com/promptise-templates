"""Tell finance about every refund that was not made, in code.

When the budget refuses a refund, the model is told to stop calling tools.
So the follow-up can't depend on the model: these two callbacks write the
finance note themselves.  One is a runtime hook for budget refusals, the
other the approval gate's ``on_decision`` for denials and timeouts.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from promptise import ApprovalDecision, ApprovalRequest
from promptise.runtime import HookConfig, HookContext

from fake_systems.store import connect, euros

from .rules import REFUND_TOOL


def note_refund_not_made(db_path: Path, arguments: dict, why: str) -> None:
    payment = arguments.get("payment_id")
    cents = arguments.get("amount_cents")
    amount = euros(cents) if isinstance(cents, int) else "an unknown amount"
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO finance_notes (subject, body, invoice_id, priority) "
            "VALUES (?, ?, NULL, 'high')",
            (
                f"Refund not made: {payment}",
                f"The agent did not refund {amount} on payment {payment} because {why}. "
                "Check it and refund it by hand if it is a real duplicate.",
            ),
        )


def budget_refusal_hook(db_path: Path) -> HookConfig:
    """A ``permission_denied`` hook: a refund the budget refused becomes a note."""

    async def on_permission_denied(ctx: HookContext) -> None:
        data = ctx.data
        if data.get("kind") == "budget" and data.get("tool_name") == REFUND_TOOL:
            note_refund_not_made(
                db_path,
                data.get("arguments") or {},
                f"the per-run limit ({data['limit_name']} = {data['limit_value']}) was reached. "
                "The agent is paused until someone resumes it, and the next reconciliation "
                "lists every double charge still open",
            )

    return HookConfig(event="permission_denied", callback=on_permission_denied)


def denied_refund_recorder(db_path: Path) -> Callable[[ApprovalRequest, ApprovalDecision], None]:
    """An ``on_decision`` callback: a refund a person or the gate denied becomes a note."""

    def record(request: ApprovalRequest, decision: ApprovalDecision) -> None:
        if not decision.approved:
            who = decision.reviewer_id or decision.decided_by
            note_refund_not_made(
                db_path,
                request.arguments,
                f"it was denied by {who}: {decision.reason or 'no reason'}",
            )

    return record
