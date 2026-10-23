"""Serve the Taskboard MCP server on http://127.0.0.1:8080/mcp (settings from .env)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.__main__ import main  # noqa: E402

main()
