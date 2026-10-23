"""The fake systems enforce their own invariants, whatever the agent sends.

These run the MCP servers in-process with TestClient: validation, the
handlers and the error envelopes are the same as over stdio.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from promptise.mcp.server import TestClient

from conftest import agent_refunds, query
from fake_systems import billing_server, email_server


@pytest.fixture
def billing(db, monkeypatch):
    monkeypatch.setenv("FINANCE_DB", str(db))
    return TestClient(billing_server.server)


@pytest.fixture
def email(db, monkeypatch):
    monkeypatch.setenv("FINANCE_DB", str(db))
    return TestClient(email_server.server)


def call(client: TestClient, tool: str, args: dict) -> dict:
    return json.loads(asyncio.run(client.call_tool(tool, args))[0].text)


def test_refunding_both_charges_of_an_invoice_is_refused(billing, db):
    first = call(
        billing, "refund_duplicate_charge", {"payment_id": "ch_2042b", "amount_cents": 2900}
    )
    second = call(
        billing, "refund_duplicate_charge", {"payment_id": "ch_2042a", "amount_cents": 2900}
    )

    assert first["status"] == "refunded"
    assert second["error"]["code"] == "NOT_A_DUPLICATE"
    assert len(agent_refunds(db)) == 1


def test_a_single_payment_can_not_be_refunded_as_a_duplicate(billing, db):
    result = call(
        billing, "refund_duplicate_charge", {"payment_id": "ch_2040a", "amount_cents": 4900}
    )
    assert result["error"]["code"] == "NOT_A_DUPLICATE"
    assert agent_refunds(db) == []


def test_refund_can_not_exceed_the_payment(billing):
    result = call(
        billing, "refund_duplicate_charge", {"payment_id": "ch_2042b", "amount_cents": 2901}
    )
    assert result["error"]["code"] == "EXCEEDS_PAYMENT"


def test_refund_amount_must_be_positive(billing):
    result = call(billing, "refund_duplicate_charge", {"payment_id": "ch_2042b", "amount_cents": 0})
    assert result["error"]["code"] == "VALIDATION_ERROR"


def test_customer_lookup_does_not_expose_the_email_address(billing):
    customer = call(billing, "get_customer", {"customer_id": "cus_Yf8tH4g"})
    assert customer["name"] == "Jansen Bakery"
    assert "email" not in customer


def test_email_goes_to_the_address_on_file(email, db):
    result = call(
        email,
        "send_customer_email",
        {"customer_id": "cus_Yf8tH4g", "invoice_id": "in_2049", "template": "payment_failed_retry"},
    )
    assert result["status"] == "sent"
    [message] = query(db, "SELECT * FROM outbox")
    assert message["to_address"] == "hello@jansenbakery.example"
    assert "card declined" in message["body"]
    assert "https://billing.lumen-metrics.example/pay/in_2049" in message["body"]


def test_email_refuses_an_invoice_of_another_customer(email, db):
    result = call(
        email,
        "send_customer_email",
        {"customer_id": "cus_NfA81kq", "invoice_id": "in_2049", "template": "payment_failed_retry"},
    )
    assert result["error"]["code"] == "CUSTOMER_MISMATCH"
    assert query(db, "SELECT * FROM outbox") == []


def test_a_retried_webhook_sends_one_email(email, db):
    args = {
        "customer_id": "cus_Yf8tH4g",
        "invoice_id": "in_2049",
        "template": "payment_failed_retry",
    }
    assert call(email, "send_customer_email", args)["status"] == "sent"
    assert call(email, "send_customer_email", args)["status"] == "already_sent"
    assert len(query(db, "SELECT * FROM outbox")) == 1
