"""Alertmanager -> relay -> signed webhook -> agent run."""

from __future__ import annotations

import asyncio
import json
import secrets
import urllib.request

from aiohttp import web
from conftest import anchor
from promptise.testing import ScriptedModel, ToolCall

from incident_agent.alerts import sample_alert
from incident_agent.app import running_agent
from incident_agent.reviewers import AutoReviewer
from incident_agent.runner import free_port, wait_for_runs
from scripts.alertmanager_relay import build_app


def post(url: str, body: bytes, token: str) -> int:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_alertmanager_reaches_the_agent_through_the_relay(world_dir):
    secret, token = secrets.token_hex(16), secrets.token_hex(16)
    model = ScriptedModel([ToolCall("find_runbook", {"alert_name": "HighErrorRate"}), "done"])
    body = json.dumps(sample_alert(anchor(world_dir))).encode()

    async def go() -> list[int]:
        async with running_agent(
            model=model, reviewer=AutoReviewer(), state_dir=world_dir, webhook_secret=secret, webhook_port=free_port()
        ) as agent:
            runner = web.AppRunner(build_app(token, secret, agent.webhook_url))
            await runner.setup()
            relay_port = free_port()
            await web.TCPSite(runner, "127.0.0.1", relay_port).start()
            url = f"http://127.0.0.1:{relay_port}/alertmanager"
            try:
                codes = [
                    await asyncio.to_thread(post, url, body, "wrong-token"),
                    await asyncio.to_thread(post, url, body, token),
                ]
                await wait_for_runs(agent.process, 1)
            finally:
                await runner.cleanup()
            return codes

    assert asyncio.run(go()) == [401, 202]
    assert model.calls[0].tools  # the run happened
