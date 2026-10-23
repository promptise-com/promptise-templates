"""Who answers the agent's approval requests.

``TerminalReviewer`` asks the person running ``main.py`` at the keyboard.
``AutoReviewer`` stands in for the on-call engineer in the offline demo and
in tests: it answers every request the same way and remembers what it saw.

In production, replace both with a handler that reaches the on-call
engineer where they are: ``WebhookApprovalHandler`` pointed at your
approval service, or a ``CallbackApprovalHandler`` that posts Slack
buttons. See README.md, "Adapt it to your systems".
"""

from __future__ import annotations

import asyncio
import json

from promptise import ApprovalDecision, ApprovalRequest, CallbackApprovalHandler, QueueApprovalHandler


def describe(request: ApprovalRequest) -> str:
    args = json.dumps(request.arguments, indent=2)
    return f"The incident agent wants to run {request.tool_name}:\n{args}"


class AutoReviewer(CallbackApprovalHandler):
    """Approves (or denies) every request, and keeps the requests it answered."""

    def __init__(self, approve: bool = True, *, reviewer_id: str = "demo-oncall", echo: bool = False) -> None:
        self.approve = approve
        self.reviewer_id = reviewer_id
        self.echo = echo
        self.requests: list[ApprovalRequest] = []
        super().__init__(self._decide)

    async def _decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        reason = None if self.approve else "Not during the payments freeze. I will take it from here."
        if self.echo:
            verdict = "APPROVED" if self.approve else "DENIED"
            print(f"\n[approval] {describe(request)}\n[approval] {verdict} by {self.reviewer_id}\n", flush=True)
        return ApprovalDecision(approved=self.approve, reviewer_id=self.reviewer_id, reason=reason)


class TerminalReviewer:
    """Prompts at the terminal for each request in the queue."""

    def __init__(self, reviewer_id: str = "oncall") -> None:
        self.reviewer_id = reviewer_id
        self.handler = QueueApprovalHandler()

    async def serve(self) -> None:
        """Answer requests until cancelled. Run it as a task next to the agent process."""
        while True:
            request = await self.handler.request_queue.get()
            print(f"\n{describe(request)}", flush=True)
            answer = await self._ask("Approve? [y/N] ")
            if answer.strip().lower() in {"y", "yes"}:
                decision = ApprovalDecision(approved=True, reviewer_id=self.reviewer_id)
            else:
                reason = await self._ask("Reason (shown to the agent): ")
                decision = ApprovalDecision(
                    approved=False, reviewer_id=self.reviewer_id, reason=reason.strip() or "Denied by on-call."
                )
            print(f"\n-> {'approved' if decision.approved else 'denied'} by {self.reviewer_id}", flush=True)
            try:
                self.handler.submit_decision(request.request_id, decision)
            except KeyError:
                print("Too late: the request timed out and was denied.", flush=True)

    @staticmethod
    async def _ask(prompt: str) -> str:
        try:
            return await asyncio.to_thread(input, prompt)
        except EOFError:
            return ""  # no terminal attached: treat as "no", never as "yes"
