"""The offline demo: one scripted business day, for when you have no model API key yet.

A ``ScriptedModel`` plays the model's part: it sends the tool calls a good
model would send, in order.  Everything else is real: the two MCP servers,
the webhook trigger with Stripe signatures, the approval gate, the budget
that refuses the fourth refund, and the trace file.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
from pathlib import Path

from promptise import ApprovalDecision, QueueApprovalHandler
from promptise.runtime import AgentProcess, TriggerEvent
from promptise.testing import ScriptedModel, ToolCall

from fake_systems.launch import start_fake_systems
from fake_systems.store import BUSINESS_DAY, connect, euros, rows, seed
from fake_systems.stripe_sender import post_event, sample_event

from .process import PROCESS_NAME, PROJECT_ROOT, Settings, build_process_config
from .reviewers import decide, describe

DEMO_DIR = PROJECT_ROOT / ".data" / "demo"


def _note(subject: str, body: str, invoice_id: str | None, priority: str = "normal") -> ToolCall:
    args = {"subject": subject, "body": body, "priority": priority}
    if invoice_id:
        args["invoice_id"] = invoice_id
    return ToolCall("create_finance_note", args)


def _refund(payment_id: str, cents: int, invoice: str) -> ToolCall:
    return ToolCall(
        "refund_duplicate_charge",
        {"payment_id": payment_id, "amount_cents": cents, "note": f"Duplicate charge on {invoice}"},
    )


def _refund_email(customer_id: str, invoice_id: str) -> ToolCall:
    return ToolCall(
        "send_customer_email",
        {"customer_id": customer_id, "invoice_id": invoice_id, "template": "refund_issued"},
    )


WEBHOOK_RUN = [
    # Check the payload against the billing system before acting on it.
    [
        ToolCall("get_invoice", {"invoice_id": "in_2049"}),
        ToolCall("get_customer", {"customer_id": "cus_Yf8tH4g"}),
    ],
    ToolCall(
        "send_customer_email",
        {"customer_id": "cus_Yf8tH4g", "invoice_id": "in_2049", "template": "payment_failed_retry"},
    ),
    _note(
        "Card declined: LM-2049 (Jansen Bakery)",
        "29.00 EUR for invoice in_2049 failed with card_declined. The customer got the "
        "payment link. Stripe retries the card in 3 days.",
        "in_2049",
    ),
    ToolCall(
        "update_state",
        {"key": "followups", "value": [f"{BUSINESS_DAY} cus_Yf8tH4g in_2049 payment_failed_retry"]},
    ),
    "Emailed Jansen Bakery a payment link for LM-2049 (29.00 EUR, card declined) "
    "and opened a finance note.",
]

NIGHTLY_RUN = [
    ToolCall("reconcile_day", {"date": BUSINESS_DAY}),
    # Notes first, so nothing is lost if a limit stops the run later.
    [
        _note(
            "Underpaid: LM-2046 (Greta Lindholm)",
            "Invoiced 29.00 EUR, received 23.20 EUR. Bank transfer py_9001 of 5.80 EUR from "
            "the same customer matches no invoice and is probably the rest.",
            "in_2046",
        ),
        _note(
            "Marked paid without a payment: LM-2048",
            "Invoice in_2048 (12.00 EUR, Ines Carvalho Consulting) is marked paid, but no "
            "payment exists. Check who marked it paid.",
            "in_2048",
            priority="high",
        ),
        _note(
            "Payment failed: LM-2049 (Jansen Bakery)",
            "29.00 EUR still open after card_declined. The customer was emailed yesterday.",
            "in_2049",
        ),
        _note(
            "Unmatched bank transfer py_9001",
            "5.80 EUR from cus_Rb5yU8s matches no invoice. Likely the missing part of LM-2046.",
            None,
        ),
    ],
    _refund("ch_2041b", 49000, "in_2041"),  # 490.00 EUR: waits for a person
    _refund_email("cus_PzR02mt", "in_2041"),
    _refund("ch_2042b", 2900, "in_2042"),
    _refund_email("cus_Lq7sV3c", "in_2042"),
    _refund("ch_2043b", 9900, "in_2043"),
    _refund_email("cus_Hk2wE9d", "in_2043"),
    _refund("ch_2044b", 1200, "in_2044"),  # the fourth refund: the budget refuses it
    "Reconciled 2026-10-22: 9 mismatches. Refunded 3 double charges (490.00, 29.00 and "
    "99.00 EUR). The refund limit stopped the fourth; in_2044 and in_2047 are left for "
    "finance. Opened 4 finance notes.",
]


def _say(text: str = "") -> None:
    print(text, flush=True)


async def _wait_for_runs(process: AgentProcess, count: int, timeout: float = 60.0) -> None:
    async def _poll() -> None:
        while process.status()["invocation_count"] < count:
            await asyncio.sleep(0.1)

    await asyncio.wait_for(_poll(), timeout)


async def _demo_reviewer(handler: QueueApprovalHandler, db: Path) -> None:
    """Play the finance reviewer: look at the request, wait a moment, approve."""
    while True:
        request = await handler.request_queue.get()
        _say(f"  [approval] {describe(request)}")
        await asyncio.sleep(1.5)
        with connect(db) as conn:
            done = rows(conn, "SELECT id FROM refunds WHERE payment_id = ?", "ch_2041b")
        _say(f"  [approval] while it waits, refunds on ch_2041b in the ledger: {len(done)}")
        decide(
            handler,
            request,
            ApprovalDecision(
                approved=True, reviewer_id="maria@finance", reason="Checked in Stripe."
            ),
        )
        _say("  [approval] maria@finance approved it")


def _print_ledger(db: Path) -> None:
    with connect(db) as conn:
        refunds = rows(conn, "SELECT * FROM refunds WHERE id != 're_0001' ORDER BY id")
        outbox = rows(conn, "SELECT * FROM outbox ORDER BY id")
        notes = rows(conn, "SELECT * FROM finance_notes ORDER BY id")
    _say("\nRefunds made by the agent:")
    for r in refunds:
        _say(f"  {r['id']}  {r['payment_id']:<9} {euros(r['amount_cents']):>11}")
    _say("\nEmails in the outbox:")
    for m in outbox:
        _say(f"  {m['to_address']:<36} {m['template']:<21} {m['invoice_id']}")
    _say("\nFinance notes:")
    for n in notes:
        _say(f"  FN-{n['id']:04d} [{n['priority']:<6}] {n['subject']}")


def _print_audit(settings: Settings) -> None:
    """What the three records say: the trace, the approval log, the journal."""
    trace = settings.trace_dir / "finance-ops.jsonl"
    calls: dict[str, int] = {}
    lines = trace.read_text().splitlines()
    for line in lines:
        entry = json.loads(line)
        if entry["event_type"] == "tool.call":
            calls[entry["tool_name"]] = calls.get(entry["tool_name"], 0) + 1
    _say(f"\nTrace ({trace.relative_to(PROJECT_ROOT)}): {len(lines)} events")
    for name, count in sorted(calls.items()):
        _say(f"  {name:<26} {count} call(s)")

    _say("\nApproval decisions (approvals.jsonl):")
    for line in (settings.trace_dir / "approvals.jsonl").read_text().splitlines():
        d = json.loads(line)
        verdict = "approved" if d["approved"] else "denied"
        _say(f"  {d['arguments']['amount_cents'] / 100:>7.2f} EUR  {verdict} by {d['reviewer']}")

    _say("\nRefused before running (journal):")
    journal = settings.journal_dir / f"{PROCESS_NAME}.jsonl"
    for line in journal.read_text().splitlines():
        entry = json.loads(line)
        if entry["entry_type"] == "tool_denied":
            data = entry["data"]
            _say(f"  {data['tool']}({data['args']['payment_id']}): {data['reason']}")


async def run_demo() -> None:
    shutil.rmtree(DEMO_DIR, ignore_errors=True)
    db = seed(DEMO_DIR / "finance.sqlite")
    # Throwaway secrets for this run only: webhook signing, the agent's API key, the audit chain.
    secret = secrets.token_hex(16)
    systems = start_fake_systems(
        db,
        agent_key=secrets.token_urlsafe(24),
        audit_secret=secrets.token_hex(32),
        data_dir=DEMO_DIR / "systems",
    )
    reviewer = QueueApprovalHandler()
    model = ScriptedModel(WEBHOOK_RUN + NIGHTLY_RUN)
    settings = Settings(
        db_path=db,
        servers=systems.servers,
        webhook_secret=secret,
        reviewer=reviewer,
        model=model,
        approval_timeout=60,
        trace_dir=DEMO_DIR / "traces",
        journal_dir=DEMO_DIR / "journal",
    )
    process = AgentProcess(PROCESS_NAME, build_process_config(settings))
    reviewer_task = asyncio.create_task(_demo_reviewer(reviewer, db))
    await process.start()
    try:
        _say(f"Lumen Metrics finance agent, offline demo (model: {type(model).__name__})\n")

        _say("14:02  Stripe: invoice.payment_failed for in_2049")
        url = f"http://127.0.0.1:{settings.webhook_port}/stripe"
        status, answer = await asyncio.to_thread(post_event, url, sample_event(), secret)
        _say(f"  webhook answered {status} {answer['status']}")
        await _wait_for_runs(process, 1)

        _say(f"\n04:30  cron: reconcile {BUSINESS_DAY}")
        await process.inject(
            TriggerEvent(
                trigger_id="demo-nightly",
                trigger_type="cron",
                payload={
                    "scheduled_time": "2026-10-23T04:30:00+02:00",
                    "timezone": "Europe/Berlin",
                },
            )
        )
        await _wait_for_runs(process, 2, timeout=120)

        status = process.status()
        _say(f"\nProcess state: {status['state']} (reason: {status['suspend_reason']})")
        _say("  The fourth refund was refused before it ran. The agent waits for finance.")
        _print_ledger(db)
        _print_audit(settings)
        await process.resume()
        _say("\nFinance resumed the agent. Demo done.")
    finally:
        reviewer_task.cancel()
        await process.stop()
        systems.stop()
