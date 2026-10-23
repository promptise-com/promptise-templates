"""Start the local ops MCP server as a separate process, the way the real one runs.

``main.py``, the offline demo and the tests use this when ``OPS_MCP_URL`` is
not set. Point ``OPS_MCP_URL`` (and ``OPS_AGENT_API_KEY``) at your own MCP
server to skip it.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from incident_agent.config import PROJECT_ROOT
from incident_agent.runner import free_port


@dataclass
class LocalOps:
    url: str
    api_key: str
    viewer_key: str
    process: subprocess.Popen

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def start_local_ops(state_dir: Path, port: int | None = None, timeout: float = 20.0) -> LocalOps:
    port = port or free_port()
    api_key, viewer_key = secrets.token_hex(24), secrets.token_hex(24)
    env = {**os.environ, "OPS_AGENT_API_KEY": api_key, "OPS_VIEWER_API_KEY": viewer_key}
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    log = open(Path(state_dir) / "ops-server.log", "ab")  # noqa: SIM115 - lives as long as the child
    process = subprocess.Popen(
        [sys.executable, "-m", "ops_systems.server", "--port", str(port), "--state-dir", str(state_dir)],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"The ops server exited early; see {Path(state_dir) / 'ops-server.log'}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return LocalOps(f"http://127.0.0.1:{port}/mcp", api_key, viewer_key, process)
        except OSError:
            time.sleep(0.1)
    process.kill()
    raise TimeoutError(f"The ops server did not listen on port {port} within {timeout:.0f} s")
