"""Shared test helpers: run one alert through the real process, offline.

Each test gets its own state directory, webhook port and secret. The model
is a ScriptedModel, so no test needs an API key or the network beyond
127.0.0.1.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from incident_agent.alerts import post_alert, sample_alert
from incident_agent.config import WEBHOOK_PATH, create_process
from incident_agent.runner import free_port, wait_for_runs
from incident_agent.trace import IncidentTrace
from ops_systems.store import OpsWorld


@dataclass
class IncidentRun:
    state_dir: Path
    trace: IncidentTrace
    model: Any
    reviewer: Any
    responses: list[tuple[int, dict]] = field(default_factory=list)
    status: dict = field(default_factory=dict)

    @property
    def world(self) -> dict:
        return json.loads((self.state_dir / "world.json").read_text())

    @property
    def audit(self) -> list[dict]:
        """What the ops MCP server itself recorded: one entry per tool call it executed."""
        path = self.state_dir / "ops-audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def tool_messages(self, tool: str) -> list[str]:
        """The results of ``tool`` as the model received them."""
        ids = {
            call["id"]
            for model_call in self.model.calls
            if model_call.reply is not None
            for call in model_call.reply.tool_calls
            if call["name"] == tool
        }
        # Long tool loops are compacted, so a result may only be in the calls right after it.
        found: dict[str, str] = {}
        for model_call in self.model.calls:
            for msg in model_call.messages:
                if msg.type == "tool" and msg.tool_call_id in ids:
                    found.setdefault(msg.tool_call_id, msg.content)
        return list(found.values())


@pytest.fixture
def world_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    OpsWorld(state_dir)  # sets the world's clock
    return state_dir


def anchor(state_dir: Path):
    return OpsWorld(state_dir).anchor


def run_incident(
    state_dir: Path,
    model: Any,
    reviewer: Any,
    *,
    payloads: list[dict] | None = None,
    expect_runs: int = 1,
    sign_with: str | None = None,
    approval_timeout: float = 30,
    serve_reviewer: Callable[[], Awaitable[None]] | None = None,
) -> IncidentRun:
    """Start the process, POST the alert(s) to its webhook, wait for the runs, stop."""
    trace = IncidentTrace(state_dir / "traces")
    secret = secrets.token_hex(16)
    port = free_port()
    run = IncidentRun(state_dir=state_dir, trace=trace, model=model, reviewer=reviewer)

    async def go() -> None:
        process = create_process(
            model=model,
            reviewer=reviewer,
            state_dir=state_dir,
            trace=trace,
            webhook_secret=secret,
            webhook_port=port,
            approval_timeout=approval_timeout,
        )
        async with process:
            reviewer_task = asyncio.create_task(serve_reviewer()) if serve_reviewer else None
            url = f"http://127.0.0.1:{port}{WEBHOOK_PATH}"
            for payload in payloads or [sample_alert(anchor(state_dir))]:
                run.responses.append(await asyncio.to_thread(post_alert, url, payload, sign_with or secret))
            if expect_runs:
                await wait_for_runs(process, expect_runs, timeout=60)
            else:
                await asyncio.sleep(0.5)  # time for a run that should not happen
            run.status = process.status()
            if reviewer_task is not None:
                reviewer_task.cancel()

    asyncio.run(go())
    return run
