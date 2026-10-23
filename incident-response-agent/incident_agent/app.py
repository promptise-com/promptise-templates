"""Wire the pieces together: memory, the ops MCP server, the trace and the process.

``main.py``, ``demo.py`` and the tests all start the agent through
:func:`running_agent`, so they run the same application.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from promptise.runtime import AgentProcess

from incident_agent.config import DEFAULT_WEBHOOK_PORT, WEBHOOK_PATH, create_process
from incident_agent.local_ops import LocalOps, start_local_ops
from incident_agent.memory import IncidentMemory
from incident_agent.trace import IncidentTrace, RunTrace


@dataclass
class RunningAgent:
    process: AgentProcess
    trace: IncidentTrace
    memory: IncidentMemory
    webhook_url: str
    ops_url: str
    local_ops: LocalOps | None


@asynccontextmanager
async def running_agent(
    *,
    model: Any,
    reviewer: Any,
    state_dir: Path,
    webhook_secret: str,
    webhook_port: int = DEFAULT_WEBHOOK_PORT,
    approval_timeout: float = 900,
    on_run_end: Callable[[RunTrace, Path], None] | None = None,
) -> AsyncIterator[RunningAgent]:
    """Start the agent process (and the local ops server unless ``OPS_MCP_URL`` is set); stop both on exit."""
    state_dir.mkdir(parents=True, exist_ok=True)
    memory = IncidentMemory(state_dir)
    await memory.load()
    trace = IncidentTrace(state_dir / "traces", on_run_end=on_run_end)

    local_ops = None
    ops_url = os.environ.get("OPS_MCP_URL")
    if ops_url:
        ops_key = os.environ["OPS_AGENT_API_KEY"]
    else:
        local_ops = start_local_ops(state_dir)
        ops_url, ops_key = local_ops.url, local_ops.api_key

    process = create_process(
        model=model,
        reviewer=reviewer,
        state_dir=state_dir,
        trace=trace,
        memory=memory,
        ops_url=ops_url,
        ops_api_key=ops_key,
        webhook_secret=webhook_secret,
        webhook_port=webhook_port,
        approval_timeout=approval_timeout,
    )
    try:
        await process.start()
        yield RunningAgent(
            process=process,
            trace=trace,
            memory=memory,
            webhook_url=f"http://127.0.0.1:{webhook_port}{WEBHOOK_PATH}",
            ops_url=ops_url,
            local_ops=local_ops,
        )
    finally:
        await process.stop(drain_timeout=None)
        if local_ops is not None:
            local_ops.stop()
