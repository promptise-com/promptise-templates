"""Small helpers shared by main.py, demo.py and the tests."""

from __future__ import annotations

import asyncio
import socket

from promptise.runtime import AgentProcess


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for_runs(process: AgentProcess, count: int = 1, timeout: float = 120.0) -> None:
    """Wait until the process has finished ``count`` runs. Raises if a run failed instead."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while process.status()["invocation_count"] < count:
        if process.status()["dead_letter_count"]:
            letter = process.dead_letters[-1]
            raise RuntimeError(f"The run failed and was dead-lettered: {letter}")
        if loop.time() > deadline:
            raise TimeoutError(f"No finished run after {timeout:.0f} s: {process.status()}")
        await asyncio.sleep(0.05)
