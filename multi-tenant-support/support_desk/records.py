"""The ticketing and order system behind the support backend (a local fake).

Data is stored per tenant, and the only way in is ``TicketDesk.for_tenant()``.
The backend calls it with the tenant from the verified token, so a tool
handler holds one tenant's records and nothing else. Swap this module for
your Zendesk, Freshdesk, Shopify or database client; keep the per-tenant
entry point.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import date
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "records"
TICKET_PREFIX = {"acme": "ACME", "globex": "GLX"}


class TenantRecords:
    """One tenant's tickets and orders."""

    def __init__(self, tenant_id: str, data: dict[str, Any], lock: threading.Lock) -> None:
        self.tenant_id = tenant_id
        self._data = data
        self._lock = lock

    def tickets(self, requester: str, status: str | None = None) -> list[dict[str, Any]]:
        rows = [t for t in self._data["tickets"] if t["requester"] == requester]
        if status:
            rows = [t for t in rows if t["status"] == status]
        return copy.deepcopy(rows)

    def ticket(self, ticket_id: str, requester: str) -> dict[str, Any] | None:
        for row in self._data["tickets"]:
            if row["ticket_id"] == ticket_id and row["requester"] == requester:
                return copy.deepcopy(row)
        return None

    def order(self, order_id: str, customer: str) -> dict[str, Any] | None:
        for row in self._data["orders"]:
            if row["order_id"] == order_id and row["customer"] == customer:
                return copy.deepcopy(row)
        return None

    def open_ticket(self, requester: str, subject: str, description: str, priority: str) -> dict[str, Any]:
        with self._lock:
            prefix = TICKET_PREFIX.get(self.tenant_id, self.tenant_id.upper())
            numbers = [int(t["ticket_id"].rsplit("-", 1)[1]) for t in self._data["tickets"]]
            ticket = {
                "ticket_id": f"{prefix}-{max(numbers, default=1000) + 1}",
                "requester": requester,
                "subject": subject,
                "description": description,
                "status": "open",
                "priority": priority,
                "created": date.today().isoformat(),
                "last_update": "Ticket received. An agent replies within one business day.",
            }
            self._data["tickets"].append(ticket)
            return copy.deepcopy(ticket)


class TicketDesk:
    """All tenants' records, loaded from ``data/records/<tenant>.json``."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._tenants: dict[str, dict[str, Any]] = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(data_dir.glob("*.json"))
        }
        self._locks = {tenant_id: threading.Lock() for tenant_id in self._tenants}

    def for_tenant(self, tenant_id: str | None) -> TenantRecords | None:
        """The records of exactly one tenant, or None when it has none."""
        if not tenant_id or tenant_id not in self._tenants:
            return None
        return TenantRecords(tenant_id, self._tenants[tenant_id], self._locks[tenant_id])
