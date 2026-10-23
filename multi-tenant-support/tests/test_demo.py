"""The offline demo runs end to end and shows every isolation outcome."""

from __future__ import annotations

import asyncio
import sys

import main


def test_offline_demo(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["main.py", "--offline"])
    asyncio.run(main.main())
    out = capsys.readouterr().out

    assert "get_ticket(ticket_id='ACME-1041') -> TOOL_ERROR" in out
    assert "list_tickets() -> GLX-2204, GLX-2201" in out
    assert "get_order(order_id='GX-7003') -> ACCESS_DENIED" in out
    assert "list_tickets() -> RATE_LIMIT_EXCEEDED" in out
    assert "list_tickets(status='open') -> ACME-1044, ACME-1041" in out
    assert "globex  ACCESS_DENIED x1, OK x3, RATE_LIMIT_EXCEEDED x1, TOOL_ERROR x1" in out
