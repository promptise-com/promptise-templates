"""The reviewer's console: approve or deny the agents' destructive calls.

    promptise run approvals

Connects to the MCP server with TASKBOARD_APPROVER_KEY (a key with the
approver role) and asks you about each call that waits for a decision. In a
real product this is a page in your admin UI that calls the same two tools.
"""

from __future__ import annotations

import asyncio
import json
import os

from promptise.mcp.client import MCPClient


def text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def main() -> None:
    url = os.environ.get("TASKBOARD_MCP_URL", "http://127.0.0.1:8080/mcp")
    seen: set[str] = set()
    async with MCPClient(url=url, api_key=os.environ["TASKBOARD_APPROVER_KEY"]) as client:
        print(f"Watching {url} for calls that need your approval. Ctrl+C to stop.", flush=True)
        while True:
            for p in json.loads(text(await client.call_tool("approvals_list", {}))):
                if p["request_id"] in seen:
                    continue
                seen.add(p["request_id"])
                print(f"\n{p['client_id']} wants to run {p['tool']} with {json.dumps(p['arguments'])}", flush=True)
                answer = await asyncio.to_thread(input, "Approve? [y/N] ")
                approve = answer.strip().lower() == "y"
                reason = "" if approve else await asyncio.to_thread(input, "Reason for the agent (optional): ")
                result = await client.call_tool(
                    "approvals_decide", {"request_id": p["request_id"], "approve": approve, "reason": reason}
                )
                print(f"\n{'approved' if approve else 'denied'}: {text(result)}", flush=True)
            await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
