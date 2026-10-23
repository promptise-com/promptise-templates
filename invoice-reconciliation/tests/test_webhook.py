"""The failed-payment path, through the real webhook trigger over HTTP.

Each test posts a Stripe-signed event to the process's webhook port, as
Stripe would, and checks who got email.
"""

from __future__ import annotations

import asyncio
import copy
import json
import urllib.error
import urllib.request

from promptise.testing import ScriptedModel, ToolCall

from conftest import query, running, tool_results, wait_for_runs
from fake_systems.stripe_sender import post_event, sample_event, stripe_signature

FOLLOW_UP = [
    [
        ToolCall("get_invoice", {"invoice_id": "in_2049"}),
        ToolCall("get_customer", {"customer_id": "cus_Yf8tH4g"}),
    ],
    ToolCall(
        "send_customer_email",
        {"customer_id": "cus_Yf8tH4g", "invoice_id": "in_2049", "template": "payment_failed_retry"},
    ),
    ToolCall(
        "create_finance_note",
        {"subject": "Card declined: LM-2049", "body": "Customer emailed.", "invoice_id": "in_2049"},
    ),
    "Emailed Jansen Bakery and opened a note.",
]


def url(settings) -> str:
    return f"http://127.0.0.1:{settings.webhook_port}/stripe"


async def post(settings, event: dict, secret: str | None = None) -> tuple[int, dict]:
    return await asyncio.to_thread(
        post_event, url(settings), event, secret or settings.webhook_secret
    )


def test_payment_failed_emails_the_right_customer(make_settings, db):
    model = ScriptedModel(FOLLOW_UP)
    settings = make_settings(model)

    async def scenario():
        async with running(settings) as process:
            status, answer = await post(settings, sample_event())
            assert (status, answer["status"]) == (202, "accepted")
            await wait_for_runs(process, 1)

    asyncio.run(scenario())

    [message] = query(db, "SELECT * FROM outbox")
    assert message["to_address"] == "hello@jansenbakery.example"
    assert (message["template"], message["invoice_id"]) == ("payment_failed_retry", "in_2049")
    assert query(db, "SELECT invoice_id FROM finance_notes") == [{"invoice_id": "in_2049"}]
    # The agent got the payload as untrusted data, inside a delimited block.
    first_prompt = model.calls[0].messages[-1].content
    assert "[Trigger: webhook]" in first_prompt and "untrusted" in first_prompt


def test_a_bad_signature_is_rejected_before_the_agent_runs(make_settings, db):
    model = ScriptedModel(FOLLOW_UP)
    settings = make_settings(model)

    async def scenario():
        async with running(settings) as process:
            status, _ = await post(settings, sample_event(), secret="whsec_wrong")
            await asyncio.sleep(0.3)
            return status, process.status()["invocation_count"]

    assert asyncio.run(scenario()) == (401, 0)
    assert model.calls == []
    assert query(db, "SELECT * FROM outbox") == []


def test_a_replayed_delivery_is_rejected(make_settings):
    model = ScriptedModel(FOLLOW_UP)
    settings = make_settings(model)
    body = json.dumps(sample_event()).encode()
    headers = {
        "Content-Type": "application/json",
        "Stripe-Signature": stripe_signature(body, settings.webhook_secret),
    }

    def send() -> int:
        request = urllib.request.Request(url(settings), data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    async def scenario():
        async with running(settings) as process:
            first = await asyncio.to_thread(send)
            second = await asyncio.to_thread(send)  # the same signed request again
            await wait_for_runs(process, 1)
            return first, second

    assert asyncio.run(scenario()) == (202, 401)


def test_other_stripe_events_cost_nothing(make_settings):
    model = ScriptedModel(FOLLOW_UP)
    settings = make_settings(model)
    event = copy.deepcopy(sample_event())
    event["type"] = "invoice.paid"

    async def scenario():
        async with running(settings) as process:
            status, answer = await post(settings, event)
            await asyncio.sleep(0.3)
            return status, answer["status"], process.status()["invocation_count"]

    assert asyncio.run(scenario()) == (200, "ignored", 0)
    assert model.calls == []


def test_a_forged_customer_in_the_payload_gets_no_email(make_settings, db):
    """The payload names another customer.  A model that trusts it is stopped by code."""
    event = copy.deepcopy(sample_event())
    event["data"]["object"]["customer"] = "cus_NfA81kq"
    model = ScriptedModel(
        [
            ToolCall(
                "send_customer_email",
                {
                    "customer_id": "cus_NfA81kq",
                    "invoice_id": "in_2049",
                    "template": "payment_failed_retry",
                },
            ),
            "Could not send.",
        ]
    )
    settings = make_settings(model)

    async def scenario():
        async with running(settings) as process:
            await post(settings, event)
            await wait_for_runs(process, 1)

    asyncio.run(scenario())
    assert query(db, "SELECT * FROM outbox") == []
    assert "CUSTOMER_MISMATCH" in tool_results(model)[0]
