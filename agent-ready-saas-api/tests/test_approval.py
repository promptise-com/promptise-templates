"""Deleting a project waits for a person in the same workspace."""

from __future__ import annotations

import asyncio

from conftest import Harness, error_code


async def test_delete_waits_for_approval_then_runs(h: Harness):
    call = asyncio.create_task(h.call("delete_project", {"project_id": "prj_103"}, as_="acme-ops-agent"))

    (pending,) = await h.wait_for_pending()
    assert pending["tool"] == "delete_project"
    assert pending["arguments"] == {"project_id": "prj_103"}
    assert pending["client_id"] == "acme-ops-agent"
    assert not call.done()
    assert "prj_103" in h.store.projects  # nothing deleted yet

    listed = await h.call("approvals_list", as_="dana@acme")
    assert [p["request_id"] for p in listed] == [pending["request_id"]]
    decision = await h.call("approvals_decide", {"request_id": pending["request_id"], "approve": True}, as_="dana@acme")
    assert decision["resolved"] is True

    assert await asyncio.wait_for(call, 5) == {"deleted": "prj_103", "name": "2024 trade show"}
    assert "prj_103" not in h.store.projects
    assert "tsk_1007" not in h.store.tasks


async def test_denied_delete_does_not_run(h: Harness):
    call = asyncio.create_task(h.call("delete_project", {"project_id": "prj_101"}, as_="acme-ops-agent"))
    (pending,) = await h.wait_for_pending()

    await h.call(
        "approvals_decide",
        {"request_id": pending["request_id"], "approve": False, "reason": "Website relaunch is still running"},
        as_="dana@acme",
    )

    reply = await asyncio.wait_for(call, 5)
    assert error_code(reply) == "APPROVAL_DENIED"
    assert "still running" in reply["error"]["message"]
    assert "prj_101" in h.store.projects


async def test_nobody_approves_their_own_call(h: Harness):
    # Olivia's owner key holds both admin and approver.
    call = asyncio.create_task(h.call("delete_project", {"project_id": "prj_103"}, as_="olivia@acme"))
    (pending,) = await h.wait_for_pending()

    own = await h.call("approvals_decide", {"request_id": pending["request_id"], "approve": True}, as_="olivia@acme")
    assert error_code(own) == "ACCESS_DENIED"
    assert not call.done()

    await h.call("approvals_decide", {"request_id": pending["request_id"], "approve": True}, as_="dana@acme")
    assert (await asyncio.wait_for(call, 5))["deleted"] == "prj_103"


async def test_reviewers_only_see_their_own_workspace(h: Harness):
    call = asyncio.create_task(h.call("delete_project", {"project_id": "prj_103"}, as_="acme-ops-agent"))
    (pending,) = await h.wait_for_pending()

    assert await h.call("approvals_list", as_="ines@globex") == []
    reply = await h.call("approvals_decide", {"request_id": pending["request_id"], "approve": True}, as_="ines@globex")
    assert error_code(reply) == "NOT_FOUND"
    assert not call.done()

    await h.call("approvals_decide", {"request_id": pending["request_id"], "approve": False}, as_="dana@acme")
    await asyncio.wait_for(call, 5)


async def test_agents_cannot_approve(h: Harness):
    reply = await h.call("approvals_list", as_="acme-ops-agent")
    assert error_code(reply) == "ACCESS_DENIED"


async def test_no_decision_in_time_means_denied(tmp_path):
    h = Harness(tmp_path, approval_timeout=0.3)
    try:
        reply = await h.call("delete_project", {"project_id": "prj_103"}, as_="acme-ops-agent")
        assert error_code(reply) == "APPROVAL_DENIED"
        assert "prj_103" in h.store.projects
    finally:
        await h.http.aclose()
