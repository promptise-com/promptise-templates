"""Start the billing and email servers as local HTTP services.

``main.py`` and the tests use this.  In production these are services you
already run; point the agent at their URLs instead (see the README).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from promptise.config import HTTPServerSpec

from .security import AGENT_KEY_ENV, AUDIT_LOG_ENV, READONLY_KEY_ENV
from .store import DB_ENV

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class FakeSystems:
    servers: dict[str, HTTPServerSpec]
    """The specs the agent connects with: URL plus its API key."""
    audit_logs: dict[str, Path]
    _procs: list[subprocess.Popen] = field(default_factory=list)

    def stop(self) -> None:
        for proc in self._procs:
            proc.terminate()
        for proc in self._procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _wait_ready(url: str, proc: subprocess.Popen, log: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early, see {log}:\n{log.read_text()[-2000:]}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"server not ready after {timeout:.0f}s: {url}")


def start_fake_systems(
    db_path: Path,
    *,
    agent_key: str,
    audit_secret: str,
    data_dir: Path,
    ports: tuple[int, int] = (8801, 8802),
    readonly_key: str | None = None,
) -> FakeSystems:
    """Start both servers and wait until they answer ``/health/ready``."""
    data_dir.mkdir(parents=True, exist_ok=True)
    systems = FakeSystems(servers={}, audit_logs={})
    try:
        for name, port in zip(("billing", "email"), ports, strict=True):
            audit_log = data_dir / f"audit-{name}.jsonl"
            server_log = data_dir / f"{name}-server.log"
            # Only what the server needs: the model key never reaches it.
            env = {
                "PATH": os.environ.get("PATH", ""),
                DB_ENV: str(db_path),
                AGENT_KEY_ENV: agent_key,
                AUDIT_LOG_ENV: str(audit_log),
                "PROMPTISE_AUDIT_SECRET": audit_secret,
                "PROMPTISE_NO_DOTENV": "1",
            }
            if readonly_key:
                env[READONLY_KEY_ENV] = readonly_key
            with server_log.open("w") as out:
                proc = subprocess.Popen(
                    [sys.executable, "-m", f"fake_systems.{name}_server", "--port", str(port)],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
            systems._procs.append(proc)
            _wait_ready(f"http://127.0.0.1:{port}/health/ready", proc, server_log)
            systems.servers[name] = HTTPServerSpec(
                url=f"http://127.0.0.1:{port}/mcp", api_key=agent_key
            )
            systems.audit_logs[name] = audit_log
    except BaseException:
        systems.stop()
        raise
    return systems
