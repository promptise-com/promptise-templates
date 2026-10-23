"""One real run with a real model.  Skipped unless you ask for it with a key set.

    OPENAI_API_KEY=... promptise run test-live      # about 0.03 USD with gpt-5-mini

The assertions check outcomes the code guarantees, whatever the model writes:
the right customer is emailed, at most three refunds land, every refund is a
real duplicate, the large one only after a person approved it, and every
refused refund leaves a finance note.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from promptise import ApprovalDecision, CallbackApprovalHandler

from conftest import NIGHTLY_EVENT, agent_refunds, query, read_jsonl, running, wait_for_runs
from fake_systems.stripe_sender import post_event, sample_event
from main import _model

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY"),
]

DUPLICATES = {"ch_2041b", "ch_2042b", "ch_2043b", "ch_2044b", "ch_2047b"}


def test_a_real_model_runs_the_whole_day(make_settings, db):
    reviewed: list[str] = []

    async def finance_reviewer(request):
        # Stands in for the person at the console: approve, and remember what was asked.
        reviewed.append(request.arguments["payment_id"])
        return ApprovalDecision(approved=True, reviewer_id="live-test")

    model = _model(os.environ.get("FINANCE_AGENT_MODEL", "openai:gpt-5-mini"))
    settings = make_settings(model, CallbackApprovalHandler(finance_reviewer), approval_timeout=120)

    async def scenario():
        async with running(settings) as process:
            url = f"http://127.0.0.1:{settings.webhook_port}/stripe"
            status, _ = await asyncio.to_thread(
                post_event, url, sample_event(), settings.webhook_secret
            )
            assert status == 202
            await wait_for_runs(process, 1, timeout=180)
            await process.inject(NIGHTLY_EVENT)
            await wait_for_runs(process, 2, timeout=600)

    asyncio.run(scenario())

    # Webhook path: one email, to the address on file for the failed invoice's customer.
    failed = query(db, "SELECT to_address FROM outbox WHERE template = 'payment_failed_retry'")
    assert failed == [{"to_address": "hello@jansenbakery.example"}]

    # Nightly path: the code-enforced rules held.
    refunds = {r["payment_id"] for r in agent_refunds(db)}
    assert refunds <= DUPLICATES
    assert len(refunds) <= 3
    assert ("ch_2041b" in refunds) == ("ch_2041b" in reviewed)  # 490 EUR only via a person
    assert all(p == "ch_2041b" for p in reviewed)  # small refunds never reached the person
    refused = [
        e
        for e in read_jsonl(settings.journal_dir / "finance-ops.jsonl")
        if e["entry_type"] == "tool_denied"
    ]
    notes = {n["subject"] for n in query(db, "SELECT subject FROM finance_notes")}
    for entry in refused:
        assert f"Refund not made: {entry['data']['args']['payment_id']}" in notes
    assert any(
        "in_2048" in (n["invoice_id"] or "")
        for n in query(db, "SELECT invoice_id FROM finance_notes")
    )
