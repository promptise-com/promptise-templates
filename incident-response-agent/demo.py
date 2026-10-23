"""Offline demo: one alert, end to end, with no API key.

    promptise run demo            # the on-call engineer approves the rollback
    promptise run demo -- --deny  # the on-call engineer denies it

It starts the incident agent process with a scripted model, sends the sample
Alertmanager alert to its webhook over HTTP (signed), answers the approval
request as the on-call engineer, and prints what changed in the fake
production world, the incident summary and the trace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shutil

from incident_agent.alerts import post_alert, sample_alert
from incident_agent.config import PROJECT_ROOT, WEBHOOK_PATH, create_process
from incident_agent.offline_model import approved_run, denied_run
from incident_agent.reviewers import AutoReviewer
from incident_agent.runner import free_port, wait_for_runs
from incident_agent.trace import IncidentTrace
from ops_systems.store import OpsWorld

STATE_DIR = PROJECT_ROOT / "var" / "demo"


def show(title: str, body: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 66 - len(title)))
    print(body.rstrip())


async def main(deny: bool) -> None:
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    world = OpsWorld(STATE_DIR)  # fixes the world's clock before the script reads it
    model = denied_run(world.anchor) if deny else approved_run(world.anchor)
    reviewer = AutoReviewer(approve=not deny, echo=True)
    trace = IncidentTrace(STATE_DIR / "traces")
    secret = secrets.token_hex(16)  # the demo signs its own alert; production reads it from the env
    port = free_port()

    process = create_process(
        model=model,
        reviewer=reviewer,
        state_dir=STATE_DIR,
        trace=trace,
        webhook_secret=secret,
        webhook_port=port,
    )
    async with process:
        url = f"http://127.0.0.1:{port}{WEBHOOK_PATH}"
        status, answer = await asyncio.to_thread(post_alert, url, sample_alert(world.anchor), secret)
        show("Alertmanager -> webhook", f"POST {url} -> {status} {answer['status']}")
        await wait_for_runs(process, 1)

    state = json.loads((STATE_DIR / "world.json").read_text())
    run = trace.last_run
    assert run is not None
    show("What changed in production", json.dumps(state["actions"], indent=2) if state["actions"] else "Nothing.")
    show("Incident channel", "\n".join(f"{p['time']}  {p['message']}" for p in state["status_updates"]))
    summary = next(iter(state["incidents"].values()))["summary_path"]
    show(f"Incident summary ({summary})", (STATE_DIR / summary).read_text())
    show("Trace timeline", "\n".join(run.timeline()))
    show("Agent's final message", run.final_answer)
    files = [*sorted((STATE_DIR / "traces").iterdir()), STATE_DIR / "ops-audit.jsonl"]
    show("Files to replay this run", "\n".join(str(p.relative_to(PROJECT_ROOT)) for p in files))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deny", action="store_true", help="the on-call engineer denies the rollback")
    asyncio.run(main(parser.parse_args().deny))
