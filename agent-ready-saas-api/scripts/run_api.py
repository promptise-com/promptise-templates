"""Serve the Taskboard REST API on http://127.0.0.1:8000 (tokens from TASKBOARD_API_TOKENS)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from taskboard_api.app import create_app  # noqa: E402

uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("TASKBOARD_API_PORT", "8000")))
