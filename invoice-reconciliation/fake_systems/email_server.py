"""The email service as an MCP server: templated mail to customers.

The agent never types an email address.  It names a customer and an
invoice, and this server looks up the address on file.  A payload that
claims "send the retry link to attacker@example.com" has nothing to work
with.  In production, swap the outbox table for Postmark, SES or SendGrid.
"""

from __future__ import annotations

from typing import Annotated, Literal

from promptise.mcp.server import MCPServer, ToolError
from pydantic import Field

from .store import connect, euros, rows

PAY_URL = "https://billing.lumen-metrics.example/pay/{invoice_id}"

TEMPLATES = {
    "payment_failed_retry": (
        "Action needed: payment for invoice {number} did not go through",
        "Hi {name},\n\n"
        "We could not collect {amount} for invoice {number}. Your bank gave this reason: "
        "{reason}.\n\nYou can pay or update your card here: {pay_url}\n"
        "We will try the card again in 3 days.\n\n{note}"
        "Thank you,\nLumen Metrics Billing",
    ),
    "refund_issued": (
        "We refunded a duplicate charge on invoice {number}",
        "Hi {name},\n\n"
        "You were charged twice for invoice {number}. We refunded {amount}. It reaches your "
        "account in 5 to 10 business days.\n\n{note}"
        "Sorry for the trouble,\nLumen Metrics Billing",
    ),
}

server = MCPServer(name="email", version="1.0.0", instructions="Lumen Metrics customer email.")


@server.tool(open_world_hint=True)
def send_customer_email(
    customer_id: Annotated[str, Field(description="The customer to write to.")],
    invoice_id: Annotated[str, Field(description="The invoice the email is about.")],
    template: Literal["payment_failed_retry", "refund_issued"],
    note: Annotated[str, Field(max_length=400, description="Optional extra sentence.")] = "",
) -> dict:
    """Email a customer about one of their invoices, from a template, to the address the
    billing system has on file. Sends each template at most once per invoice."""
    with connect() as conn:
        found = rows(
            conn,
            "SELECT i.*, c.name, c.email FROM invoices i JOIN customers c ON c.id = i.customer_id "
            "WHERE i.id = ?",
            invoice_id,
        )
        if not found:
            raise ToolError(f"No invoice {invoice_id!r}.", code="NOT_FOUND")
        invoice = found[0]
        # The invoice must belong to the customer: a mixed-up or forged ID
        # must never mail one customer's invoice to another.
        if invoice["customer_id"] != customer_id:
            raise ToolError(
                f"Invoice {invoice_id} does not belong to customer {customer_id}.",
                code="CUSTOMER_MISMATCH",
            )
        sent = rows(
            conn,
            "SELECT id FROM outbox WHERE invoice_id = ? AND template = ?",
            invoice_id,
            template,
        )
        if sent:
            # Stripe retries webhooks; customers should get one email, not five.
            return {"status": "already_sent", "message_id": f"msg_{sent[0]['id']:04d}"}

        failed = rows(
            conn,
            "SELECT failure_code FROM payments WHERE invoice_id = ? AND status = 'failed' "
            "ORDER BY created_at DESC LIMIT 1",
            invoice_id,
        )
        subject, body = TEMPLATES[template]
        values = {
            "name": invoice["name"],
            "number": invoice["number"],
            "amount": euros(invoice["amount_cents"]),
            "reason": (failed[0]["failure_code"] if failed else "unknown").replace("_", " "),
            "pay_url": PAY_URL.format(invoice_id=invoice_id),
            "note": f"{note.strip()}\n\n" if note.strip() else "",
        }
        cur = conn.execute(
            "INSERT INTO outbox (customer_id, to_address, template, invoice_id, subject, body) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                customer_id,
                invoice["email"],
                template,
                invoice_id,
                subject.format(**values),
                body.format(**values),
            ),
        )
    return {"status": "sent", "message_id": f"msg_{cur.lastrowid:04d}", "template": template}


if __name__ == "__main__":
    server.run()
