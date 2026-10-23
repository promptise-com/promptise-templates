"""Every call lands in the HMAC-chained audit log, and editing the log is detected."""

from __future__ import annotations

import asyncio
import json

from promptise.mcp.server import verify_audit_log

from conftest import Harness


def entries(h: Harness) -> list[dict]:
    return [json.loads(line) for line in h.settings.audit_log.read_text().splitlines()]


async def run_some_calls(h: Harness) -> None:
    await h.call("list_projects")  # no key
    await h.call("create_task", {"project_id": "prj_101", "title": "x"}, as_="acme-report-bot")  # wrong role
    await h.call("add_comment", {"task_id": "tsk_1001", "body": "New date?"}, as_="acme-writer-bot")
    call = asyncio.create_task(h.call("delete_project", {"project_id": "prj_103"}, as_="acme-ops-agent"))
    (pending,) = await h.wait_for_pending()
    await h.call("approvals_decide", {"request_id": pending["request_id"], "approve": True}, as_="dana@acme")
    await call


async def test_audit_log_records_allowed_and_refused_calls(h: Harness):
    await run_some_calls(h)
    log = entries(h)

    assert [(e["tool"], e["client_id"], e["status"], e.get("error_code")) for e in log] == [
        ("list_projects", None, "error", "AUTHENTICATION_ERROR"),
        ("create_task", "acme-report-bot", "error", "ACCESS_DENIED"),
        ("add_comment", "acme-writer-bot", "ok", None),
        ("approvals_decide", "dana@acme", "ok", None),
        ("delete_project", "acme-ops-agent", "ok", None),
    ]
    delete = log[-1]
    assert delete["args"] == {"project_id": "prj_103"}
    assert delete["identity"]["tenant_id"] == "acme"
    # Keys never end up in the log.
    text = h.settings.audit_log.read_text()
    assert not any(key in text for key in h.key.values())


async def test_untouched_log_verifies(h: Harness):
    await run_some_calls(h)
    report = verify_audit_log(h.settings.audit_log, h.settings.audit_secret)
    assert report.ok and report.entries == 5


async def test_editing_an_entry_is_detected(h: Harness):
    await run_some_calls(h)
    lines = h.settings.audit_log.read_text().splitlines(keepends=True)
    entry = json.loads(lines[4])
    entry["client_id"] = "someone-else"  # pin the deletion on another agent
    lines[4] = json.dumps(entry) + "\n"
    h.settings.audit_log.write_text("".join(lines))

    report = verify_audit_log(h.settings.audit_log, h.settings.audit_secret)
    assert not report.ok
    assert report.first_problem.line == 5


async def test_deleting_an_entry_is_detected(h: Harness):
    await run_some_calls(h)
    lines = h.settings.audit_log.read_text().splitlines(keepends=True)
    del lines[1]  # hide the refused create_task
    h.settings.audit_log.write_text("".join(lines))

    report = verify_audit_log(h.settings.audit_log, h.settings.audit_secret)
    assert not report.ok
    assert report.first_problem.kind == "deleted"


async def test_a_forged_log_fails_without_the_key(h: Harness):
    await run_some_calls(h)
    assert not verify_audit_log(h.settings.audit_log, "not-the-key").ok
