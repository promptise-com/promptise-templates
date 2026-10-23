"""The fake billing system and email outbox: one SQLite file with seed data.

Lumen Metrics is a made-up EU analytics SaaS that bills its customers every
month or year.  The seed covers one business day (2026-10-22) and plants
the kinds of mismatches a finance team finds every week: double charges,
an underpayment, an invoice marked paid with no money behind it, a failed
card payment and a payment that belongs to no invoice.

Amounts are integer cents, as in Stripe.  Floats and money don't mix.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DB_ENV = "FINANCE_DB"
"""The environment variable both MCP servers read the database path from."""

BUSINESS_DAY = "2026-10-22"
"""The day the fixed seed covers (tests and the offline demo).

The live application seeds yesterday instead, so tonight's run has data.
"""

SCHEMA = """
CREATE TABLE customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    plan TEXT NOT NULL,
    country TEXT NOT NULL
);
CREATE TABLE invoices (
    id TEXT PRIMARY KEY,
    number TEXT NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,            -- open | paid
    issued_on TEXT NOT NULL
);
CREATE TABLE payments (
    id TEXT PRIMARY KEY,
    invoice_id TEXT,                 -- NULL: money that matches no invoice
    customer_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,            -- succeeded | failed
    failure_code TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE refunds (
    id TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL REFERENCES payments(id),
    amount_cents INTEGER NOT NULL,
    reason TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE finance_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    invoice_id TEXT,
    priority TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    to_address TEXT NOT NULL,
    template TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CUSTOMERS = [
    # id, name, email, plan, country
    ("cus_NfA81kq", "Anouk de Vries", "anouk@devries-design.example", "Team monthly", "NL"),
    (
        "cus_PzR02mt",
        "Brightline Tours GmbH",
        "billing@brightline-tours.example",
        "Team annual",
        "DE",
    ),
    ("cus_Lq7sV3c", "Casa Moreno", "finance@casamoreno.example", "Starter monthly", "ES"),
    ("cus_Hk2wE9d", "Delta Freight Oy", "ap@deltafreight.example", "Business monthly", "FI"),
    ("cus_Tm4xB6r", "Elif Kaya Studio", "elif@kaya-studio.example", "Solo monthly", "AT"),
    (
        "cus_Gv9pQ1n",
        "Fjord Analytics AS",
        "invoices@fjord-analytics.example",
        "Business monthly",
        "NO",
    ),
    ("cus_Rb5yU8s", "Greta Lindholm", "greta@lindholm.example", "Starter monthly", "SE"),
    ("cus_Wc3jM7a", "Harbor & Pine Ltd", "accounts@harborpine.example", "Team monthly", "IE"),
    (
        "cus_Xd6kN2f",
        "Ines Carvalho Consulting",
        "ines@carvalho-consult.example",
        "Solo monthly",
        "PT",
    ),
    ("cus_Yf8tH4g", "Jansen Bakery", "hello@jansenbakery.example", "Starter monthly", "BE"),
    ("cus_Zh1rK5p", "Kestrel Labs", "billing@kestrel-labs.example", "Team monthly", "DK"),
]

D = BUSINESS_DAY
INVOICES = [
    # id, number, customer, cents, currency, status, issued_on
    ("in_2040", "LM-2040", "cus_NfA81kq", 4900, "EUR", "paid", D),  # clean
    ("in_2041", "LM-2041", "cus_PzR02mt", 49000, "EUR", "paid", D),  # double charge, large
    ("in_2042", "LM-2042", "cus_Lq7sV3c", 2900, "EUR", "paid", D),  # double charge
    ("in_2043", "LM-2043", "cus_Hk2wE9d", 9900, "EUR", "paid", D),  # double charge
    ("in_2044", "LM-2044", "cus_Tm4xB6r", 1200, "EUR", "paid", D),  # double charge
    ("in_2045", "LM-2045", "cus_Gv9pQ1n", 9900, "EUR", "paid", D),  # clean
    ("in_2046", "LM-2046", "cus_Rb5yU8s", 2900, "EUR", "paid", D),  # underpaid
    ("in_2047", "LM-2047", "cus_Wc3jM7a", 4900, "EUR", "paid", D),  # double charge
    ("in_2048", "LM-2048", "cus_Xd6kN2f", 1200, "EUR", "paid", D),  # paid, but no payment
    ("in_2049", "LM-2049", "cus_Yf8tH4g", 2900, "EUR", "open", D),  # card declined
    (
        "in_2050",
        "LM-2050",
        "cus_Zh1rK5p",
        4900,
        "EUR",
        "paid",
        D,
    ),  # double charge, already refunded
]

PAYMENTS = [
    # id, invoice, customer, cents, currency, status, failure_code, created_at
    ("ch_2040a", "in_2040", "cus_NfA81kq", 4900, "EUR", "succeeded", None, f"{D}T06:01:12Z"),
    ("ch_2041a", "in_2041", "cus_PzR02mt", 49000, "EUR", "succeeded", None, f"{D}T06:01:40Z"),
    ("ch_2041b", "in_2041", "cus_PzR02mt", 49000, "EUR", "succeeded", None, f"{D}T06:03:02Z"),
    ("ch_2042a", "in_2042", "cus_Lq7sV3c", 2900, "EUR", "succeeded", None, f"{D}T06:02:05Z"),
    ("ch_2042b", "in_2042", "cus_Lq7sV3c", 2900, "EUR", "succeeded", None, f"{D}T06:02:51Z"),
    ("ch_2043a", "in_2043", "cus_Hk2wE9d", 9900, "EUR", "succeeded", None, f"{D}T06:02:33Z"),
    ("ch_2043b", "in_2043", "cus_Hk2wE9d", 9900, "EUR", "succeeded", None, f"{D}T06:04:10Z"),
    ("ch_2044a", "in_2044", "cus_Tm4xB6r", 1200, "EUR", "succeeded", None, f"{D}T06:03:15Z"),
    ("ch_2044b", "in_2044", "cus_Tm4xB6r", 1200, "EUR", "succeeded", None, f"{D}T06:03:59Z"),
    ("ch_2045a", "in_2045", "cus_Gv9pQ1n", 9900, "EUR", "succeeded", None, f"{D}T06:04:41Z"),
    ("ch_2046a", "in_2046", "cus_Rb5yU8s", 2320, "EUR", "succeeded", None, f"{D}T06:05:02Z"),
    ("ch_2047a", "in_2047", "cus_Wc3jM7a", 4900, "EUR", "succeeded", None, f"{D}T06:05:30Z"),
    ("ch_2047b", "in_2047", "cus_Wc3jM7a", 4900, "EUR", "succeeded", None, f"{D}T06:06:44Z"),
    (
        "ch_2049a",
        "in_2049",
        "cus_Yf8tH4g",
        2900,
        "EUR",
        "failed",
        "card_declined",
        f"{D}T06:07:18Z",
    ),
    ("ch_2050a", "in_2050", "cus_Zh1rK5p", 4900, "EUR", "succeeded", None, f"{D}T06:07:55Z"),
    ("ch_2050b", "in_2050", "cus_Zh1rK5p", 4900, "EUR", "succeeded", None, f"{D}T06:08:20Z"),
    # A bank transfer nobody could match to an invoice.
    ("py_9001", None, "cus_Rb5yU8s", 580, "EUR", "succeeded", None, f"{D}T14:22:09Z"),
]

REFUNDS = [
    # The in_2050 duplicate was refunded by hand the same day: not a mismatch any more.
    (
        "re_0001",
        "ch_2050b",
        4900,
        "duplicate",
        "Refunded by support (ticket 5531)",
        f"{D}T11:40:00Z",
    ),
]


def db_path() -> Path:
    """The database path from ``FINANCE_DB``, or the default one."""
    return Path(os.environ.get(DB_ENV, ".data/finance.sqlite"))


@contextmanager
def connect(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Open the database, commit on success and always close it."""
    conn = sqlite3.connect(str(path or db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _on_day(table: list[tuple], day: str) -> list[tuple]:
    """Move seed rows from BUSINESS_DAY to *day*."""
    return [
        tuple(v.replace(BUSINESS_DAY, day) if isinstance(v, str) else v for v in row)
        for row in table
    ]


def seed(path: Path | str, day: str = BUSINESS_DAY) -> Path:
    """Create a fresh database at *path* with the Lumen Metrics seed data for *day*."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", CUSTOMERS)
        conn.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?,?)", _on_day(INVOICES, day))
        conn.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?)", _on_day(PAYMENTS, day))
        conn.executemany("INSERT INTO refunds VALUES (?,?,?,?,?,?)", _on_day(REFUNDS, day))
    return path


def rows(conn: sqlite3.Connection, sql: str, *params: object) -> list[dict]:
    """Run a query and return plain dicts."""
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def euros(cents: int) -> str:
    """Format cents for people: 49000 -> '490.00 EUR'."""
    return f"{cents / 100:.2f} EUR"
