"""The offline demo runs end to end and shows every isolation outcome."""

from __future__ import annotations

import asyncio

import main


def test_offline_demo(tmp_path, capsys):
    asyncio.run(main.main(["--offline", "--state-dir", str(tmp_path)]))
    out = capsys.readouterr().out

    assert "get_ticket(ticket_id='ACME-1041') -> TOOL_ERROR" in out
    assert "list_tickets() -> GLX-2204, GLX-2201" in out
    assert "SessionAccessDenied: User 'globex::gina' denied access" in out
    assert "get_order(order_id='GX-7003') -> ACCESS_DENIED" in out
    assert "request_refund(order_id='AC-4988') -> AC-4988 (refunded)" in out
    assert "request_refund(order_id='AC-5001') -> APPROVAL_DENIED" in out
    assert "the session keeps: 'My card [CREDIT_CARD] was charged twice" in out
    assert "list_tickets() -> RATE_LIMIT_EXCEEDED" in out
    assert "list_tickets(status='open') -> ACME-1041" in out
    assert "acme::alice    memory entries: 1" in out
    assert "globex::alice  memory entries: 0" in out
