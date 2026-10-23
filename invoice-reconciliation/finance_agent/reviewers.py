"""The human side of the approval workflow.

``QueueApprovalHandler`` hands each refund above the threshold to code in
this process.  ``console_reviewer`` asks at the terminal.  In production,
pass a ``WebhookApprovalHandler`` instead and answer from Slack, your
back office or a ticket queue (see the README).
"""

from __future__ import annotations

import asyncio

from promptise import ApprovalDecision, ApprovalRequest, QueueApprovalHandler


def describe(request: ApprovalRequest) -> str:
    args = request.arguments
    cents = args.get("amount_cents")
    amount = f"{cents / 100:.2f} EUR" if isinstance(cents, int) else "?"
    return (
        f"{request.agent_id} wants to call {request.tool_name}: refund {amount} "
        f"on payment {args.get('payment_id')} ({args.get('note') or 'no note'})"
    )


def decide(
    handler: QueueApprovalHandler, request: ApprovalRequest, decision: ApprovalDecision
) -> bool:
    """Submit a decision; False when the request already timed out."""
    try:
        handler.submit_decision(request.request_id, decision)
        return True
    except KeyError:  # timed out (denied) or already decided
        return False


async def console_reviewer(handler: QueueApprovalHandler) -> None:
    """Ask a person at the terminal about every pending refund, one at a time."""
    while True:
        request = await handler.request_queue.get()
        print(f"\n[approval needed] {describe(request)}")
        answer = await asyncio.to_thread(input, "Approve this refund? [y/N] ")
        approved = answer.strip().lower() in {"y", "yes"}
        decision = ApprovalDecision(
            approved=approved,
            reviewer_id="console",
            reason=None if approved else "Declined by finance at the console.",
        )
        if not decide(handler, request, decision):
            print("Too late: the request timed out and was denied.")
