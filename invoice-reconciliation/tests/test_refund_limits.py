"""The refund rules hold in code: three refunds per run, a person above 100 EUR.

The scripted model asks for exactly what a careless or manipulated model
might ask for.  The assertions check what reached the ledger.
"""

from __future__ import annotations

import asyncio

import pytest
from promptise import ApprovalDecision, ApprovalRequest, QueueApprovalHandler
from promptise.testing import ScriptedModel, ToolCall

from conftest import (
    NIGHTLY_EVENT,
    agent_refunds,
    query,
    read_jsonl,
    running,
    tool_results,
    wait_for_runs,
)
from finance_agent.rules import MAX_REFUNDS_PER_RUN, refund_within_auto_limit


def refund(payment_id: str, cents: int) -> ToolCall:
    return ToolCall("refund_duplicate_charge", {"payment_id": payment_id, "amount_cents": cents})


SMALL_DUPLICATES = [("ch_2042b", 2900), ("ch_2043b", 9900), ("ch_2044b", 1200), ("ch_2047b", 4900)]


def test_the_fourth_refund_in_a_run_is_blocked(make_settings, db):
    model = ScriptedModel([*(refund(p, c) for p, c in SMALL_DUPLICATES), "Done."])
    settings = make_settings(model)

    async def scenario():
        async with running(settings) as process:
            await process.inject(NIGHTLY_EVENT)
            await wait_for_runs(process, 1)
            return process.status()

    status = asyncio.run(scenario())

    assert [r["payment_id"] for r in agent_refunds(db)] == ["ch_2042b", "ch_2043b", "ch_2044b"]
    fourth = tool_results(model)[MAX_REFUNDS_PER_RUN]
    assert "max_irreversible_per_run" in fourth and "was not executed" in fourth
    # Hitting the limit pauses the process until finance resumes it.
    assert status["state"] == "suspended" and status["suspend_reason"] == "budget"
    denied = [
        e
        for e in read_jsonl(settings.journal_dir / "finance-ops.jsonl")
        if e["entry_type"] == "tool_denied"
    ]
    assert [d["data"]["args"]["payment_id"] for d in denied] == ["ch_2047b"]
    # Code, not the model, tells finance what was left undone.
    [note] = query(db, "SELECT subject, priority FROM finance_notes")
    assert note == {"subject": "Refund not made: ch_2047b", "priority": "high"}


def test_parallel_refunds_are_capped_too(make_settings, db):
    """Four refunds in one model turn: the runtime checks them one by one."""
    model = ScriptedModel([[refund(p, c) for p, c in SMALL_DUPLICATES], "Done."])

    async def scenario():
        async with running(make_settings(model)) as process:
            await process.inject(NIGHTLY_EVENT)
            await wait_for_runs(process, 1)

    asyncio.run(scenario())
    assert len(agent_refunds(db)) == MAX_REFUNDS_PER_RUN


def test_the_limit_is_per_run(make_settings, db):
    """After finance resumes the agent, the next run may refund again."""
    model = ScriptedModel(
        [
            *(refund(p, c) for p, c in SMALL_DUPLICATES),
            "Limit reached.",  # run 1
            refund("ch_2047b", 4900),
            "Refunded the last one.",  # run 2
        ]
    )

    async def scenario():
        async with running(make_settings(model)) as process:
            await process.inject(NIGHTLY_EVENT)
            await wait_for_runs(process, 1)
            await process.resume()
            await process.inject(NIGHTLY_EVENT)
            await wait_for_runs(process, 2)

    asyncio.run(scenario())
    assert len(agent_refunds(db)) == 4


def test_a_small_refund_needs_no_person(make_settings, db):
    reviewer = QueueApprovalHandler()
    model = ScriptedModel([refund("ch_2043b", 9900), "Refunded."])  # 99.00 EUR, under the threshold
    settings = make_settings(model, reviewer)

    async def scenario():
        async with running(settings) as process:
            await process.inject(NIGHTLY_EVENT)
            await wait_for_runs(process, 1)

    asyncio.run(scenario())
    assert [r["amount_cents"] for r in agent_refunds(db)] == [9900]
    assert reviewer.request_queue.empty()
    [decision] = read_jsonl(settings.trace_dir / "approvals.jsonl")
    assert decision["decided_by"] == "classifier" and decision["approved"]


async def _pending_request(reviewer: QueueApprovalHandler) -> ApprovalRequest:
    return await asyncio.wait_for(reviewer.request_queue.get(), 20)


def test_a_large_refund_waits_and_does_not_run_when_denied(make_settings, db):
    reviewer = QueueApprovalHandler()
    model = ScriptedModel([refund("ch_2041b", 49000), "Finance denied the refund."])
    settings = make_settings(model, reviewer)

    async def scenario():
        async with running(settings) as process:
            await process.inject(NIGHTLY_EVENT)
            request = await _pending_request(reviewer)
            # The run is parked on the decision: no refund, run not finished.
            await asyncio.sleep(0.5)
            assert agent_refunds(db) == []
            assert process.status()["invocation_count"] == 0
            assert request.arguments["amount_cents"] == 49000

            reviewer.submit_decision(
                request.request_id,
                ApprovalDecision(
                    approved=False, reviewer_id="maria", reason="Customer asked for credit."
                ),
            )
            await wait_for_runs(process, 1)

    asyncio.run(scenario())
    assert agent_refunds(db) == []
    assert tool_results(model)[0].startswith("DENIED: Customer asked for credit.")
    [note] = query(db, "SELECT subject, body FROM finance_notes")
    assert note["subject"] == "Refund not made: ch_2041b" and "490.00 EUR" in note["body"]


def test_a_large_refund_is_denied_when_nobody_answers(make_settings, db):
    model = ScriptedModel([refund("ch_2041b", 49000), "No decision came."])
    settings = make_settings(model, approval_timeout=1)

    async def scenario():
        async with running(settings) as process:
            await process.inject(NIGHTLY_EVENT)
            await wait_for_runs(process, 1)

    asyncio.run(scenario())
    assert agent_refunds(db) == []
    [decision] = read_jsonl(settings.trace_dir / "approvals.jsonl")
    assert decision["approved"] is False and decision["decided_by"] == "gate"


def test_a_large_refund_runs_after_a_person_approves(make_settings, db):
    reviewer = QueueApprovalHandler()
    model = ScriptedModel([refund("ch_2041b", 49000), "Refunded after approval."])
    settings = make_settings(model, reviewer)

    async def scenario():
        async with running(settings) as process:
            await process.inject(NIGHTLY_EVENT)
            request = await _pending_request(reviewer)
            reviewer.submit_decision(
                request.request_id, ApprovalDecision(approved=True, reviewer_id="maria")
            )
            await wait_for_runs(process, 1)

    asyncio.run(scenario())
    assert [r["amount_cents"] for r in agent_refunds(db)] == [49000]
    [decision] = read_jsonl(settings.trace_dir / "approvals.jsonl")
    assert decision["reviewer"] == "maria" and decision["decided_by"] == "reviewer"


@pytest.mark.parametrize(
    ("arguments", "auto"),
    [
        ({"amount_cents": 10_000}, True),  # exactly 100.00 EUR
        ({"amount_cents": 10_001}, False),
        ({"amount_cents": "500"}, False),  # not an integer: a person decides
        ({}, False),
    ],
)
def test_threshold_rule(arguments, auto):
    request = ApprovalRequest(
        request_id="r", tool_name="refund_duplicate_charge", arguments=arguments
    )
    assert asyncio.run(refund_within_auto_limit(request)) is auto
