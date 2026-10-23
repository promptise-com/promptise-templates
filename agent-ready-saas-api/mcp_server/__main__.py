"""Run the Taskboard MCP server over streamable HTTP.

    python -m mcp_server            # http://127.0.0.1:8080/mcp

Agents send their key in the x-api-key header. The settings come from the
environment (see .env.example); `promptise run mcp` loads .env for you.
"""

from __future__ import annotations

from .config import Settings
from .server import build_server


def main() -> None:
    settings = Settings.from_env()
    gateway = build_server(settings)
    print(f"Taskboard MCP server on http://{settings.host}:{settings.port}/mcp (API: {settings.api_url})")
    gateway.server.run(transport="http", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
