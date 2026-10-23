"""The incident agent as a service: it waits for alerts and works each one.

    promptise run                 # needs a model key and ALERT_WEBHOOK_SECRET, see .env.example
    promptise run alert           # in a second terminal: send the sample alert

Risky fixes are asked here, at the terminal. Answer y to approve. Stop with Ctrl+C.
For a run with no API key, use the offline demo instead: promptise run demo
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from promptise.models import ModelSetupError

from incident_agent.config import DEFAULT_WEBHOOK_PORT, PROJECT_ROOT, WEBHOOK_PATH, create_process
from incident_agent.reviewers import TerminalReviewer
from incident_agent.trace import IncidentTrace, RunTrace

DEFAULT_MODEL = "openai:gpt-5-mini"


def report(run: RunTrace, path: Path) -> None:
    print(f"\n{run.final_answer}\nTrace: {path}\n", flush=True)


async def main() -> None:
    secret = os.environ.get("ALERT_WEBHOOK_SECRET")
    if not secret:
        sys.exit("Set ALERT_WEBHOOK_SECRET (copy .env.example to .env). The alerting system signs with it.")
    model = os.environ.get("INCIDENT_AGENT_MODEL", DEFAULT_MODEL)
    port = int(os.environ.get("ALERT_WEBHOOK_PORT", DEFAULT_WEBHOOK_PORT))
    state_dir = Path(os.environ.get("INCIDENT_STATE_DIR", PROJECT_ROOT / "var" / "state"))

    reviewer = TerminalReviewer()
    process = create_process(
        model=model,
        reviewer=reviewer.handler,
        state_dir=state_dir,
        trace=IncidentTrace(state_dir / "traces", on_run_end=report),
        webhook_secret=secret,
        webhook_port=port,
    )
    try:
        await process.start()
    except ModelSetupError as exc:
        sys.exit(str(exc))
    try:
        print(f"Incident agent on {model}, listening on http://127.0.0.1:{port}{WEBHOOK_PATH}", flush=True)
        print("Send the sample alert from another terminal: promptise run alert", flush=True)
        await reviewer.serve()  # answers approval requests until Ctrl+C
    finally:
        await process.stop(drain_timeout=None)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
