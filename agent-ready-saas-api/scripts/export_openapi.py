"""Write the Taskboard API's OpenAPI document to openapi.json.

FastAPI builds the document from the code, so this file is always in step
with taskboard_api/app.py. MCPcast reads it to draft the first MCP server.

    python scripts/export_openapi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taskboard_api.app import create_app  # noqa: E402

spec = create_app(tokens={}).openapi()
# FastAPI leaves `servers` out; MCPcast needs a base URL for the draft.
spec["servers"] = [{"url": "http://127.0.0.1:8000"}]
(ROOT / "openapi.json").write_text(json.dumps(spec, indent=2) + "\n")
print(f"wrote openapi.json: {len(spec['paths'])} paths")
