"""Mint, list and revoke agent keys: promptise run keys list | create ... | revoke <key_id>."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.keys import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
