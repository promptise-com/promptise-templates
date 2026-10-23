"""The billing system as an MCP server: invoices, payments, refunds, notes.

In production this is a thin MCP server in front of Stripe (or Chargebee,
Recurly, your own ledger).  The tool names and arguments stay the same, so
the agent does not change when you swap the backend.

It runs over HTTP and every tool needs an API key with the right role (see
``security.py``).  ``python -m fake_systems.billing_server --port 8801``
starts it on its own; ``main.py`` starts it for you.
"""

from __future__ import annotations

import argparse
from typing import Annotated, Literal

from promptise.mcp.server import MCPServer, ToolError
from pydantic import Field

from .reconcile import reconcile_day as _reconcile_day
from .security import NOTES, READ, REFUNDS, secure
from .store import connect, euros, rows

MAX_REFUND_CENTS = 100_000
"""A hard ceiling the billing system itself enforces (1,000.00 EUR).

The agent's approval rule sits in front of it, but this check does not
depend on the agent being configured correctly.
"""


def reconcile_day(
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="Day, YYYY-MM-DD.")],
) -> dict:
    """Match the invoices issued on a day against payments and refunds, and list every
    mismatch: double_charge, underpaid, overpaid, paid_without_payment, payment_failed,
    unpaid, currency_mismatch and orphan_payment. Amounts are in cents."""
    with connect() as conn:
        return _reconcile_day(conn, date)


def get_invoice(
    invoice_id: Annotated[str, Field(description='Invoice ID, for example "in_2049".')],
) -> dict:
    """Get an invoice with its payments and refunds. Amounts are in cents."""
    with connect() as conn:
        found = rows(conn, "SELECT * FROM invoices WHERE id = ?", invoice_id)
        if not found:
            raise ToolError(f"No invoice {invoice_id!r}.", code="NOT_FOUND")
        invoice = found[0]
        invoice["payments"] = rows(
            conn, "SELECT * FROM payments WHERE invoice_id = ? ORDER BY created_at", invoice_id
        )
        invoice["refunds"] = rows(
            conn,
            "SELECT r.* FROM refunds r JOIN payments p ON p.id = r.payment_id "
            "WHERE p.invoice_id = ? ORDER BY r.created_at",
            invoice_id,
        )
        return invoice


def get_customer(
    customer_id: Annotated[str, Field(description='Customer ID, for example "cus_Yf8tH4g".')],
) -> dict:
    """Get a customer's name, plan and country. The email address stays in the billing
    system: send email with send_customer_email, which looks it up itself."""
    with connect() as conn:
        found = rows(
            conn, "SELECT id, name, plan, country FROM customers WHERE id = ?", customer_id
        )
    if not found:
        raise ToolError(f"No customer {customer_id!r}.", code="NOT_FOUND")
    return {**found[0], "email_on_file": True}


def refund_duplicate_charge(
    payment_id: Annotated[str, Field(description="The duplicate payment to refund.")],
    amount_cents: Annotated[int, Field(gt=0, description="Amount to refund, in cents.")],
    note: Annotated[str, Field(max_length=300, description="Why, for the audit trail.")] = "",
) -> dict:
    """Refund a payment that charged a customer twice for the same invoice. Only works
    for a duplicate: the invoice must still be fully paid after the refund."""
    with connect() as conn:
        found = rows(conn, "SELECT * FROM payments WHERE id = ?", payment_id)
        if not found:
            raise ToolError(f"No payment {payment_id!r}.", code="NOT_FOUND")
        payment = found[0]
        if payment["status"] != "succeeded" or payment["invoice_id"] is None:
            raise ToolError(
                f"{payment_id} is not a settled invoice payment.", code="NOT_REFUNDABLE"
            )
        if amount_cents > MAX_REFUND_CENTS:
            raise ToolError(
                f"Refunds above {euros(MAX_REFUND_CENTS)} are done by finance, not by tools.",
                code="ABOVE_HARD_LIMIT",
            )

        already = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM refunds WHERE payment_id = ?", (payment_id,)
        ).fetchone()[0]
        if amount_cents > payment["amount_cents"] - already:
            left = euros(payment["amount_cents"] - already)
            raise ToolError(
                f"Only {left} of {payment_id} is left to refund.", code="EXCEEDS_PAYMENT"
            )

        # A duplicate refund must not leave the invoice underpaid.  This is
        # what stops a refund of the one real payment.
        invoice = rows(conn, "SELECT * FROM invoices WHERE id = ?", payment["invoice_id"])[0]
        received = conn.execute(
            "SELECT COALESCE(SUM(p.amount_cents), 0) - COALESCE((SELECT SUM(r.amount_cents) "
            "FROM refunds r JOIN payments q ON q.id = r.payment_id WHERE q.invoice_id = ?), 0) "
            "FROM payments p WHERE p.invoice_id = ? AND p.status = 'succeeded'",
            (invoice["id"], invoice["id"]),
        ).fetchone()[0]
        if received - amount_cents < invoice["amount_cents"]:
            raise ToolError(
                f"Refusing: {invoice['id']} would be underpaid after this refund. "
                "This is not a duplicate charge; open a finance note instead.",
                code="NOT_A_DUPLICATE",
            )

        count = conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
        refund_id = f"re_{count + 1:04d}"
        conn.execute(
            "INSERT INTO refunds VALUES (?, ?, ?, 'duplicate', ?, datetime('now'))",
            (refund_id, payment_id, amount_cents, note),
        )
    return {
        "refund_id": refund_id,
        "payment_id": payment_id,
        "invoice_id": invoice["id"],
        "customer_id": invoice["customer_id"],
        "amount": euros(amount_cents),
        "status": "refunded",
    }


def create_finance_note(
    subject: Annotated[str, Field(max_length=120)],
    body: Annotated[str, Field(max_length=2000)],
    priority: Literal["low", "normal", "high"] = "normal",
    invoice_id: str | None = None,
) -> dict:
    """Open a note in the finance team's queue for something a person must look at."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO finance_notes (subject, body, invoice_id, priority) VALUES (?, ?, ?, ?)",
            (subject, body, invoice_id, priority),
        )
        note_id = f"FN-{cur.lastrowid:04d}"
    return {"note_id": note_id, "priority": priority, "invoice_id": invoice_id}


def create_server() -> MCPServer:
    """The billing server, with auth and audit read from the environment."""
    server = MCPServer(
        name="billing",
        version="1.0.0",
        instructions="Lumen Metrics billing: invoices, payments, refunds and finance notes.",
        require_auth=True,  # no key, no tool: checked before the MCP layer
    )
    secure(server)
    server.tool(roles=[READ], read_only_hint=True)(reconcile_day)
    server.tool(roles=[READ], read_only_hint=True)(get_invoice)
    server.tool(roles=[READ], read_only_hint=True)(get_customer)
    server.tool(roles=[REFUNDS], destructive_hint=True, idempotent_hint=False)(
        refund_duplicate_charge
    )
    server.tool(roles=[NOTES])(create_finance_note)
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="The fake billing MCP server")
    parser.add_argument("--port", type=int, default=8801)
    create_server().run(transport="http", host="127.0.0.1", port=parser.parse_args().port)
