"""Run the support backend on a loopback port for the demo and the tests.

In production the backend is its own service (``python -m support_desk.backend``)
and the agent gets its URL from SUPPORT_BACKEND_URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import socket
from collections.abc import AsyncIterator

from promptise.mcp.server import MCPServer


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until_listening(port: int, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def running_backend(server: MCPServer) -> AsyncIterator[str]:
    """Serve *server* over Streamable HTTP on 127.0.0.1 and yield its MCP URL."""
    port = free_port()
    # The server prints a startup banner and uvicorn logs every request. Both
    # are useful for a standalone service and noise inside the demo, so the
    # startup output is captured and uvicorn keeps only warnings and errors.
    startup_output = io.StringIO()
    with contextlib.redirect_stdout(startup_output), contextlib.redirect_stderr(startup_output):
        task = asyncio.create_task(server.run_async(transport="http", host="127.0.0.1", port=port))
        await _wait_until_listening(port)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        # Stopping by cancellation makes uvicorn log a traceback for its
        # lifespan; it is expected here, so keep it out of the output.
        logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
