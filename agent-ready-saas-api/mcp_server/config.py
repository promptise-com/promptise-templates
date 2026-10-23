"""Settings for the Taskboard MCP server, read from the environment.

Secrets (the audit key and the per-workspace API tokens) only ever come from
the environment. See .env.example for every variable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    api_url: str = "http://127.0.0.1:8000"
    # workspace id -> that workspace's Taskboard API token
    upstream_tokens: dict[str, str] | None = None
    keys_file: Path = ROOT / ".data" / "agent_keys.json"
    audit_log: Path = ROOT / ".data" / "audit.jsonl"
    audit_secret: str = ""
    # How long a deletion waits for a person before it is denied.
    approval_timeout: float = 300.0
    # Per agent key: sustained calls per minute, and the short burst allowed above it.
    rate_per_minute: int = 60
    rate_burst: int = 20
    host: str = "127.0.0.1"
    port: int = 8080

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            api_url=env.get("TASKBOARD_API_URL", cls.api_url),
            upstream_tokens=json.loads(env.get("TASKBOARD_UPSTREAM_TOKENS") or "{}"),
            keys_file=Path(env.get("TASKBOARD_KEYS_FILE", cls.keys_file)),
            audit_log=Path(env.get("TASKBOARD_AUDIT_LOG", cls.audit_log)),
            audit_secret=env.get("PROMPTISE_AUDIT_SECRET", ""),
            approval_timeout=float(env.get("TASKBOARD_APPROVAL_TIMEOUT", cls.approval_timeout)),
            rate_per_minute=int(env.get("TASKBOARD_RATE_LIMIT_PER_MINUTE", cls.rate_per_minute)),
            rate_burst=int(env.get("TASKBOARD_RATE_LIMIT_BURST", cls.rate_burst)),
            host=env.get("TASKBOARD_MCP_HOST", cls.host),
            port=int(env.get("TASKBOARD_MCP_PORT", cls.port)),
        )
