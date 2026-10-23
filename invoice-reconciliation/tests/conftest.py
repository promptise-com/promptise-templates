"""Shared fixtures: a fresh database per test and a running agent process.

The tests use promptise.testing.ScriptedModel in place of a model provider,
so they need no API key and no network.  The MCP servers, triggers, budget
and approval gate are the real ones.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from promptise import ApprovalHandler, QueueApprovalHandler  # noqa: E402
from promptise.runtime import AgentProcess, TriggerEvent  # noqa: E402

from fake_systems.store import connect, rows, seed  # noqa: E402
from finance_agent.process import PROCESS_NAME, Settings, build_process_config  # noqa: E402

NIGHTLY_EVENT = TriggerEvent(
    trigger_id="test-cron",
    trigger_type="cron",
    payload={"scheduled_time": "2026-10-23T04:30:00+02:00", "timezone": "Europe/Berlin"},
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return seed(tmp_path / "finance.sqlite")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def make_settings(tmp_path: Path, db: Path):
    def _make(model, reviewer: ApprovalHandler | None = None, **overrides) -> Settings:
        values = dict(
            db_path=db,
            webhook_secret="whsec_test_only",
            reviewer=reviewer or QueueApprovalHandler(),
            model=model,
            webhook_port=free_port(),
            approval_timeout=10,
            trace_dir=tmp_path / "traces",
            journal_dir=tmp_path / "journal",
        )
        values.update(overrides)
        return Settings(**values)

    return _make


@asynccontextmanager
async def running(settings: Settings) -> AsyncIterator[AgentProcess]:
    process = AgentProcess(PROCESS_NAME, build_process_config(settings))
    await process.start()
    try:
        yield process
    finally:
        await process.stop()


async def wait_for_runs(process: AgentProcess, count: int, timeout: float = 30.0) -> None:
    async def _poll() -> None:
        while process.status()["invocation_count"] < count:
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_poll(), timeout)


def query(db: Path, sql: str, *params: object) -> list[dict]:
    with connect(db) as conn:
        return rows(conn, sql, *params)


def agent_refunds(db: Path) -> list[dict]:
    """Refunds made during the test (re_0001 is in the seed)."""
    return query(db, "SELECT * FROM refunds WHERE id != 're_0001' ORDER BY id")


def tool_results(model) -> list[str]:
    """Every tool result the model was sent, in order."""
    seen: dict[str, str] = {}
    for call in model.calls:
        for message in call.messages:
            if message.type == "tool":
                seen[message.tool_call_id] = str(message.content)
    return list(seen.values())


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
