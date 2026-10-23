"""The offline demo runs end to end: a real HTTP MCP server and a scripted agent."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_offline_demo_runs_end_to_end():
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    out = subprocess.run(
        [sys.executable, "demo.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    ).stdout

    assert "POST /mcp with no x-api-key -> HTTP 401" in out
    assert "create_task -> Requires any of roles [editor], but client has [reader]" in out
    assert 'add_comment -> {"id": "cmt_' in out
    assert "waiting: delete_project({'project_id': 'prj_103'}) asked by acme-ops-agent" in out
    assert 'delete_project -> {"deleted": "prj_103"' in out
    assert "next call with the revoked key -> MCPConnectionRejectedError: HTTP 401 Unauthorized" in out
    assert "verify: ok=True entries=8" in out
    assert "after editing one line: ok=False" in out
