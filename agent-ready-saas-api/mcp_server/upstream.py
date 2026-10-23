"""The HTTP client the MCP server uses to call the Taskboard REST API.

The workspace always comes from the caller's verified API key, never from a
tool argument, so an agent cannot reach another customer's data by passing
a different id. API errors become structured tool errors the agent can act
on, without leaking the upstream token or stack traces.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from promptise.mcp.server import ToolError


class TaskboardAPI:
    def __init__(
        self,
        base_url: str,
        tokens: Mapping[str, str],
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._tokens = dict(tokens)
        self._http = http_client or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(
        self,
        workspace: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        token = self._tokens.get(workspace)
        if token is None:
            # A key for a workspace this deployment holds no token for.
            raise ToolError("This workspace is not connected to the agent gateway.", code="WORKSPACE_NOT_CONNECTED")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._http.request(
                method, path, params=params, json=json, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.TimeoutException:
            raise ToolError("Taskboard did not answer in time.", code="UPSTREAM_TIMEOUT", retryable=True) from None
        except httpx.HTTPError:
            raise ToolError("Taskboard is unreachable.", code="UPSTREAM_UNREACHABLE", retryable=True) from None

        if resp.status_code == 204:
            return None
        if resp.status_code < 400:
            return resp.json()
        detail = _detail(resp)
        if resp.status_code == 404:
            raise ToolError(detail, code="NOT_FOUND", suggestion="List the projects or tasks to find a valid id.")
        if resp.status_code == 422:
            raise ToolError(detail, code="INVALID_INPUT")
        if resp.status_code in (401, 403):
            # Our own credential was refused: an operator problem, not the agent's.
            raise ToolError("The gateway's Taskboard credential was refused.", code="UPSTREAM_AUTH")
        raise ToolError(
            f"Taskboard error {resp.status_code}.", code="UPSTREAM_ERROR", retryable=resp.status_code >= 500
        )


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return f"Taskboard error {resp.status_code}."
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(f"{'.'.join(map(str, d.get('loc', [])[1:]))}: {d.get('msg')}" for d in detail)
    return str(detail)[:500]
