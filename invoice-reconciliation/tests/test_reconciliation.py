"""The matching logic finds every mismatch planted in the seed, and only those."""

from __future__ import annotations

import asyncio
import json

from promptise.mcp.server import TestClient
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
from fake_systems import billing_server
from fake_systems.reconcile import reconcile_day
from fake_systems.store import BUSINESS_DAY, connect

PLANTED = {
    ("double_charge", "in_2041"),
    ("double_charge", "in_2042"),
    ("double_charge", "in_2043"),
    ("double_charge", "in_2044"),
    ("double_charge", "in_2047"),
    ("underpaid", "in_2046"),
    ("paid_without_payment", "in_2048"),
    ("payment_failed", "in_2049"),
    ("orphan_payment", None),
}


def test_finds_every_planted_mismatch(db):
    with connect(db) as conn:
        report = reconcile_day(conn, BUSINESS_DAY)

    found = {(m["kind"], m["invoice_id"]) for m in report["mismatches"]}
    assert found == PLANTED
    assert report["invoices_checked"] == 11


def test_double_charge_names_only_the_duplicate(db):
    with connect(db) as conn:
        report = reconcile_day(conn, BUSINESS_DAY)

    large = next(m for m in report["mismatches"] if m["invoice_id"] == "in_2041")
    assert large["payment_ids"] == ["ch_2041a", "ch_2041b"]
    assert large["duplicate_payment_ids"] == ["ch_2041b"]
    assert large["refundable_cents"] == 49000


def test_a_refunded_duplicate_is_not_flagged_again(db, monkeypatch):
    # in_2050 was double charged and refunded by support: no mismatch.
    monkeypatch.setenv("FINANCE_DB", str(db))
    with connect(db) as conn:
        assert "in_2050" not in {
            m["invoice_id"] for m in reconcile_day(conn, BUSINESS_DAY)["mismatches"]
        }

    # Refunding a duplicate resolves it, so a second nightly run won't refund it twice.
    client = TestClient(billing_server.server)
    asyncio.run(
        client.call_tool(
            "refund_duplicate_charge", {"payment_id": "ch_2042b", "amount_cents": 2900}
        )
    )
    with connect(db) as conn:
        again = {m["invoice_id"] for m in reconcile_day(conn, BUSINESS_DAY)["mismatches"]}
    assert "in_2042" not in again
    assert len(again) == len(PLANTED) - 1


def test_a_day_without_invoices_is_clean(db):
    with connect(db) as conn:
        report = reconcile_day(conn, "2026-10-21")
    assert report["mismatch_count"] == 0


def test_nightly_cron_run_reports_the_planted_mismatches(make_settings, db):
    """The cron path end to end: the agent reconciles and opens finance notes."""
    model = ScriptedModel(
        [
            ToolCall("reconcile_day", {"date": BUSINESS_DAY}),
            [
                ToolCall(
                    "create_finance_note",
                    {
                        "subject": "Underpaid: LM-2046",
                        "body": "23.20 of 29.00",
                        "invoice_id": "in_2046",
                    },
                ),
                ToolCall(
                    "create_finance_note",
                    {
                        "subject": "Paid without payment: LM-2048",
                        "body": "No payment",
                        "invoice_id": "in_2048",
                        "priority": "high",
                    },
                ),
            ],
            "Reconciled 2026-10-22: 9 mismatches.",
        ]
    )
    settings = make_settings(model)

    async def scenario():
        async with running(settings) as process:
            await process.inject(NIGHTLY_EVENT)
            await wait_for_runs(process, 1)

    asyncio.run(scenario())

    report = json.loads(tool_results(model)[0])
    assert {(m["kind"], m["invoice_id"]) for m in report["mismatches"]} == PLANTED
    notes = query(db, "SELECT invoice_id, priority FROM finance_notes ORDER BY id")
    assert notes == [
        {"invoice_id": "in_2046", "priority": "normal"},
        {"invoice_id": "in_2048", "priority": "high"},
    ]
    assert agent_refunds(db) == []

    # Every step is in the trace: the run, each LLM turn and each tool call.
    trace = read_jsonl(settings.trace_dir / "finance-ops.jsonl")
    types = [e["event_type"] for e in trace]
    assert types[0] == "agent.input" and types[-1] == "agent.output"
    assert [e["tool_name"] for e in trace if e["event_type"] == "tool.call"] == [
        "reconcile_day",
        "create_finance_note",
        "create_finance_note",
    ]
