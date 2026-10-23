"""The incident agent: it waits for alerts and works each one.

    promptise run             # terminal 1: needs a model key and ALERT_WEBHOOK_SECRET (see .env.example)
    promptise run alert       # terminal 2: send the sample alert, signed

Fixes that change production are asked here, at the terminal: answer y to approve.
Stop with Ctrl+C or SIGTERM. With no key yet, try: promptise run demo-offline
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from promptise.models import ModelSetupError

from incident_agent.app import running_agent
from incident_agent.config import DEFAULT_WEBHOOK_PORT, PROJECT_ROOT
from incident_agent.reviewers import TerminalReviewer
from incident_agent.trace import RunTrace

DEFAULT_MODEL = "openai:gpt-5-mini"


def report(run: RunTrace, path: Path) -> None:
    print(f"\n{run.final_answer}\nTrace: {path.relative_to(PROJECT_ROOT)}\n", flush=True)


async def main() -> None:
    secret = os.environ.get("ALERT_WEBHOOK_SECRET")
    if not secret:
        sys.exit("Set ALERT_WEBHOOK_SECRET (copy .env.example to .env). The alerting side signs with it.")
    model = os.environ.get("INCIDENT_AGENT_MODEL", DEFAULT_MODEL)
    port = int(os.environ.get("ALERT_WEBHOOK_PORT", DEFAULT_WEBHOOK_PORT))
    state_dir = Path(os.environ.get("INCIDENT_STATE_DIR", PROJECT_ROOT / "var" / "state"))

    # SIGTERM (docker stop, systemd) shuts down like Ctrl+C: runs in flight get drain time.
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)

    reviewer = TerminalReviewer()
    try:
        async with running_agent(
            model=model,
            reviewer=reviewer.handler,
            state_dir=state_dir,
            webhook_secret=secret,
            webhook_port=port,
            on_run_end=report,
        ) as agent:
            print(f"Incident agent on {model}", flush=True)
            print(f"Alerts: POST {agent.webhook_url} (HMAC-signed)", flush=True)
            print(f"Ops MCP server: {agent.ops_url}", flush=True)
            print("Send the sample alert from another terminal: promptise run alert\n", flush=True)
            await reviewer.serve()  # answers approval requests until stopped
    except ModelSetupError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
