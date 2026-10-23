"""Agents must sign in, and a revoked key stops working at once."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx

from conftest import Harness, error_code
from mcp_server.keys import KeyStore

ROOT = Path(__file__).resolve().parent.parent


async def test_unauthenticated_call_is_refused(h: Harness):
    reply = await h.call("add_comment", {"task_id": "tsk_1001", "body": "hello"})
    assert error_code(reply) == "AUTHENTICATION_ERROR"
    # The handler never ran: no comment reached the API.
    assert len(h.store.comments["tsk_1001"]) == 1


async def test_unknown_and_malformed_keys_are_refused(h: Harness):
    for bad in ["tbk_deadbeef.not-the-secret", "sk-something-else", h.key["acme-ops-agent"] + "x"]:
        result = await h.client.call_tool("list_projects", {}, headers={"x-api-key": bad})
        assert "AUTHENTICATION_ERROR" in result[0].text


async def test_http_transport_answers_401_without_a_key(h: Harness):
    # require_tenant implies the transport-level gate: no key, no MCP session.
    app = h.gw.server.asgi_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        headers = {"accept": "application/json, text/event-stream"}
        assert (await http.post("/mcp", json=body, headers=headers)).status_code == 401
        headers["x-api-key"] = "tbk_deadbeef.wrong"
        assert (await http.post("/mcp", json=body, headers=headers)).status_code == 401


async def test_revoked_key_stops_working_without_a_restart(h: Harness):
    assert error_code(await h.call("list_projects", as_="acme-ops-agent")) is None

    # Revoke through a second store on the same file, as the admin UI or the
    # CLI in another process would. The running server is not told.
    assert KeyStore(h.settings.keys_file).revoke(h.key_id["acme-ops-agent"])

    assert error_code(await h.call("list_projects", as_="acme-ops-agent")) == "AUTHENTICATION_ERROR"
    # Other keys are unaffected.
    assert error_code(await h.call("list_projects", as_="acme-writer-bot")) is None


async def test_revoke_from_the_cli_while_the_server_runs(h: Harness):
    env = {**os.environ, "TASKBOARD_KEYS_FILE": str(h.settings.keys_file)}
    subprocess.run(
        [sys.executable, "-m", "mcp_server.keys", "revoke", h.key_id["acme-writer-bot"]],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    reply = await h.call("create_task", {"project_id": "prj_101", "title": "x"}, as_="acme-writer-bot")
    assert error_code(reply) == "AUTHENTICATION_ERROR"


def test_keys_are_stored_hashed(h: Harness):
    stored = h.settings.keys_file.read_text()
    for key in h.key.values():
        assert key not in stored
        assert key.split(".", 1)[1] not in stored
