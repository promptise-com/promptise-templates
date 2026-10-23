"""Create a local .env with fresh development secrets and two agent keys.

    promptise run setup

Writes .env (git-ignored, readable only by you) with random API tokens for
the two seed workspaces, an audit key, an agent key for acme-ops-agent
(admin access) and a reviewer key for dana@acme (approver). It never
prints a secret and refuses to touch an existing .env.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_server.keys import KeyStore  # noqa: E402

env_path = ROOT / ".env"
if env_path.exists():
    sys.exit(".env already exists; delete it first to start over (this also needs a new .data/).")

tokens = json.dumps({"acme": secrets.token_urlsafe(24), "globex": secrets.token_urlsafe(24)})
store = KeyStore(ROOT / ".data" / "agent_keys.json")
agent_key, agent = store.create(workspace="acme", name="acme-ops-agent", access="admin")
reviewer_key, reviewer = store.create(workspace="acme", name="dana@acme", access="approver")

lines = [
    "# Written by `promptise run setup`. Development values only.",
    f"TASKBOARD_API_TOKENS={tokens}",
    f"TASKBOARD_UPSTREAM_TOKENS={tokens}",
    f"PROMPTISE_AUDIT_SECRET={secrets.token_hex(32)}",
    f"TASKBOARD_AGENT_KEY={agent_key}",
    f"TASKBOARD_APPROVER_KEY={reviewer_key}",
    "# The customer agent's model. Set the provider key here or in your shell:",
    "# OPENAI_API_KEY=",
    "",
]
fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as fh:
    fh.write("\n".join(lines))
print("wrote .env and .data/agent_keys.json")
print(f"agent key    {agent['key_id']}  acme-ops-agent  (admin)     -> TASKBOARD_AGENT_KEY")
print(f"reviewer key {reviewer['key_id']}  dana@acme       (approver)  -> TASKBOARD_APPROVER_KEY")
