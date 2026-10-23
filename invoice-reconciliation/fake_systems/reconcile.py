"""Match one day's invoices against payments and refunds.

This is plain code, not a prompt.  Finding a mismatch is arithmetic, and
arithmetic should give the same answer every night.  The agent's job starts
after this: deciding what to do about each mismatch and telling people.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field

from .store import rows


@dataclass
class Mismatch:
    kind: str
    """double_charge | underpaid | overpaid | paid_without_payment |
    payment_failed | unpaid | currency_mismatch | orphan_payment"""
    invoice_id: str | None
    customer_id: str
    expected_cents: int
    received_cents: int
    detail: str
    payment_ids: list[str] = field(default_factory=list)
    duplicate_payment_ids: list[str] = field(default_factory=list)
    """For a double charge: the payments to refund (all but the first)."""
    refundable_cents: int = 0
    """For a double charge: what refunding the duplicates gives back."""


def reconcile_day(conn: sqlite3.Connection, day: str) -> dict:
    """Reconcile the invoices issued and payments made on *day* (YYYY-MM-DD)."""
    invoices = rows(conn, "SELECT * FROM invoices WHERE issued_on = ? ORDER BY id", day)
    refunded = {
        r["payment_id"]: r["total"]
        for r in rows(
            conn, "SELECT payment_id, SUM(amount_cents) AS total FROM refunds GROUP BY payment_id"
        )
    }
    mismatches: list[Mismatch] = []

    for inv in invoices:
        payments = rows(
            conn, "SELECT * FROM payments WHERE invoice_id = ? ORDER BY created_at", inv["id"]
        )
        succeeded = [p for p in payments if p["status"] == "succeeded"]
        # A refunded payment no longer counts: it was money in, then money out.
        net = {p["id"]: p["amount_cents"] - refunded.get(p["id"], 0) for p in succeeded}
        live = [p for p in succeeded if net[p["id"]] > 0]
        received = sum(net.values())
        expected = inv["amount_cents"]
        base = {
            "invoice_id": inv["id"],
            "customer_id": inv["customer_id"],
            "expected_cents": expected,
            "received_cents": received,
            "payment_ids": [p["id"] for p in payments],
        }

        wrong_currency = [p["id"] for p in live if p["currency"] != inv["currency"]]
        if wrong_currency:
            mismatches.append(
                Mismatch(
                    kind="currency_mismatch",
                    detail=f"Payments {wrong_currency} are not in {inv['currency']}.",
                    **base,
                )
            )
            continue

        if received == expected:
            # One payment, or instalments that add up: nothing to do.
            continue

        if not live:
            failed = [p for p in payments if p["status"] == "failed"]
            if inv["status"] == "paid":
                kind, detail = "paid_without_payment", "Marked paid, but no payment exists."
            elif failed:
                codes = ", ".join(sorted({p["failure_code"] or "unknown" for p in failed}))
                kind, detail = "payment_failed", f"{len(failed)} failed attempt(s): {codes}."
            else:
                kind, detail = "unpaid", "No payment attempt yet."
            mismatches.append(Mismatch(kind=kind, detail=detail, **base))
            continue

        full_charges = [p for p in live if net[p["id"]] == expected]
        if received > expected and len(full_charges) >= 2 and received % expected == 0:
            duplicates = full_charges[1:]
            mismatches.append(
                Mismatch(
                    kind="double_charge",
                    detail=f"Charged {len(full_charges)} times for one invoice.",
                    duplicate_payment_ids=[p["id"] for p in duplicates],
                    refundable_cents=sum(net[p["id"]] for p in duplicates),
                    **base,
                )
            )
        elif received > expected:
            mismatches.append(
                Mismatch(kind="overpaid", detail="Received more than invoiced.", **base)
            )
        else:
            mismatches.append(
                Mismatch(kind="underpaid", detail="Received less than invoiced.", **base)
            )

    orphans = rows(
        conn,
        "SELECT * FROM payments WHERE status = 'succeeded' AND substr(created_at, 1, 10) = ? "
        "AND (invoice_id IS NULL OR invoice_id NOT IN (SELECT id FROM invoices)) ORDER BY id",
        day,
    )
    for p in orphans:
        mismatches.append(
            Mismatch(
                kind="orphan_payment",
                invoice_id=None,
                customer_id=p["customer_id"],
                expected_cents=0,
                received_cents=p["amount_cents"],
                payment_ids=[p["id"]],
                detail="Payment matches no invoice.",
            )
        )

    return {
        "date": day,
        "invoices_checked": len(invoices),
        "mismatch_count": len(mismatches),
        "mismatches": [asdict(m) for m in mismatches],
    }
