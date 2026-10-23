"""Offline demo: the whole story in one command, with no model API key.

    promptise run demo        (or: python demo.py)

What runs, all on this machine:
- the Taskboard REST API, in-process, with its seed data;
- the hand-tuned MCP server, on a real HTTP port on 127.0.0.1;
- the customer's agent, connected to it over MCP with its API key. Its model
  is a ScriptedModel from promptise.testing, so the tool calls are fixed in
  advance. The tools, the server and the API are all real.

Every secret (API tokens, agent keys, the audit key) is generated for this
run and thrown away at the end.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import httpx
import uvicorn
from promptise.mcp.client import MCPClient
from promptise.mcp.server import verify_audit_log
from promptise.testing import ScriptedModel, ToolCall

from customer_agent.agent import create_agent
from mcp_server import Settings, build_server
from taskboard_api.app import create_app

ROOT = Path(__file__).resolve().parent


def step(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 66 - len(title)))


def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def next_friday() -> str:
    today = date.today()
    return (today + timedelta(days=(4 - today.weekday()) % 7 or 7)).isoformat()


async def run_agent(url: str, key: str, script: list, task: str) -> str:
    agent = await create_agent(model=ScriptedModel(script), api_key=key, url=url)
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": task}]})
    finally:
        await agent.shutdown()
    # The model is scripted; the tool results below came from the real server.
    for msg in result["messages"]:
        if msg.type == "tool":
            print(f"  {msg.name} -> {str(msg.content)[:96]}")
    return result["messages"][-1].content


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="taskboard-demo-") as tmp:
        await run_demo(Path(tmp))


async def run_demo(workdir: Path) -> None:
    tokens = {"acme": secrets.token_urlsafe(16), "globex": secrets.token_urlsafe(16)}
    api_app = create_app(tokens=tokens)
    settings = Settings(
        upstream_tokens=tokens,
        keys_file=workdir / "agent_keys.json",
        audit_log=workdir / "audit.jsonl",
        audit_secret=secrets.token_hex(32),
        approval_timeout=60,
    )
    # The MCP server reaches the API in-process; in production this is plain HTTP.
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://taskboard.internal")
    gw = build_server(settings, http_client=upstream)

    ops_key, ops = gw.keys.create(workspace="acme", name="acme-ops-agent", access="admin")
    reader_key, _ = gw.keys.create(workspace="acme", name="acme-report-bot", access="reader")
    dana_key, _ = gw.keys.create(workspace="acme", name="dana@acme", access="approver")

    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    web = uvicorn.Server(uvicorn.Config(gw.server.asgi_app(), host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(web.serve())
    while not web.started:
        await asyncio.sleep(0.05)
    print(f"Taskboard MCP server listening on {url}")

    try:
        step("1. A call without a key is refused")
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"accept": "application/json, text/event-stream"},
            )
        print(f"POST /mcp with no x-api-key -> HTTP {resp.status_code}")

        step("2. A read-only key sees reads only, and cannot write")
        async with MCPClient(url=url, api_key=reader_key) as reader:
            print("tools:", ", ".join(t.name for t in await reader.list_tools()))
            denied = await reader.call_tool("create_task", {"project_id": "prj_101", "title": "Sneaky task"})
            print("create_task ->", json.loads(text_of(denied))["error"]["message"])

        step("3. The customer's agent does the weekly overdue sweep")
        friday = next_friday()
        sweep = [
            ToolCall("find_tasks", {"project_id": "prj_101", "overdue_only": True}),
            [
                ToolCall(
                    "add_comment",
                    {"task_id": "tsk_1001", "body": "@maya this is past due. What is a realistic new date?"},
                ),
                ToolCall(
                    "add_comment",
                    {"task_id": "tsk_1002", "body": "@leo this is past due. Is the CMS export still blocking it?"},
                ),
            ],
            ToolCall(
                "create_task",
                {
                    "project_id": "prj_101",
                    "title": "Review answers on overdue tasks",
                    "assignee": "maya",
                    "due_date": friday,
                },
            ),
            f"Two tasks in Website relaunch were overdue. I asked Maya and Leo for new dates "
            f"and created a review task for Maya due {friday}.",
        ]
        answer = await run_agent(url, ops_key, sweep, "Do the weekly overdue sweep for Website relaunch.")
        print("agent:", answer)

        step("4. Deleting a project waits for a person")
        delete = [ToolCall("delete_project", {"project_id": "prj_103"}), "The 2024 trade show project is deleted."]
        agent_run = asyncio.create_task(run_agent(url, ops_key, delete, "Delete the 2024 trade show project."))
        async with MCPClient(url=url, api_key=dana_key) as dana:
            pending = []
            while not pending:
                await asyncio.sleep(0.2)
                pending = json.loads(text_of(await dana.call_tool("approvals_list", {})))
            p = pending[0]
            print(f"waiting: {p['tool']}({p['arguments']}) asked by {p['client_id']}")
            decision = await dana.call_tool("approvals_decide", {"request_id": p["request_id"], "approve": True})
            print("dana@acme approves ->", text_of(decision))
        print("agent:", await agent_run)

        step("5. Revoke the agent's key while the server runs")
        env = {**os.environ, "TASKBOARD_KEYS_FILE": str(settings.keys_file)}
        out = subprocess.run(
            [sys.executable, "-m", "mcp_server.keys", "revoke", ops["key_id"]],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        print("$ python -m mcp_server.keys revoke", ops["key_id"], "->", out.stdout.strip())
        try:
            async with MCPClient(url=url, api_key=ops_key) as revoked:
                await revoked.list_tools()
            print("unexpected: the revoked key still works")
        except Exception as exc:  # noqa: BLE001 - we want to show whatever the client raises
            status = "HTTP 401 Unauthorized" if "401" in str(exc) else str(exc)[:80]
            print(f"next call with the revoked key -> {type(exc).__name__}: {status}")

    finally:
        web.should_exit = True
        await serving
        await upstream.aclose()

    step("6. The audit log is complete and tamper-evident")
    log = settings.audit_log
    for line in log.read_text().splitlines():
        e = json.loads(line)
        print(f"  #{e['seq']:<2} {e['tool']:<16} {str(e['client_id']):<16} {e['status']:<5} {e.get('error_code', '')}")
    report = verify_audit_log(log, settings.audit_secret)
    print(f"verify: ok={report.ok} entries={report.entries}")

    lines = log.read_text().splitlines(keepends=True)
    i = next(n for n, line in enumerate(lines) if json.loads(line)["tool"] == "delete_project")
    forged = json.loads(lines[i])
    forged["tool"] = "list_projects"  # someone tries to hide the deletion
    lines[i] = json.dumps(forged) + "\n"
    tampered = workdir / "audit-tampered.jsonl"
    tampered.write_text("".join(lines))
    report = verify_audit_log(tampered, settings.audit_secret)
    print(f"after editing one line: ok={report.ok} -> {report.first_problem.kind}: {report.first_problem.message}")


if __name__ == "__main__":
    asyncio.run(main())
