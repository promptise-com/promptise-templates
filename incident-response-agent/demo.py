"""Offline run of the whole application with a scripted model (no API key).

    promptise run demo-offline         # the on-call engineer approves the rollback
    promptise run demo-offline-deny    # the on-call engineer denies it

Everything is the real application except the model: the ops MCP server runs
as its own HTTP service with API keys, the sample alert is POSTed to the
webhook signed, the approval gate, budget, memory, journal and trace all run.
The model is a ScriptedModel that plays back a real model's turns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shutil

from incident_agent.alerts import post_alert, sample_alert
from incident_agent.app import running_agent
from incident_agent.config import PROJECT_ROOT
from incident_agent.offline_model import approved_run, denied_run
from incident_agent.reviewers import AutoReviewer
from incident_agent.runner import free_port, wait_for_runs
from ops_systems.store import OpsWorld

STATE_DIR = PROJECT_ROOT / "var" / "demo"


def show(title: str, body: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 66 - len(title)))
    print(body.rstrip())


async def main(deny: bool) -> None:
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    world = OpsWorld(STATE_DIR)  # fixes the world's clock before the script reads it
    model = denied_run(world.anchor) if deny else approved_run(world.anchor)
    secret = secrets.token_hex(16)  # the demo signs its own alert; the real app reads it from the env

    async with running_agent(
        model=model,
        reviewer=AutoReviewer(approve=not deny, echo=True),
        state_dir=STATE_DIR,
        webhook_secret=secret,
        webhook_port=free_port(),
    ) as agent:
        status, answer = await asyncio.to_thread(post_alert, agent.webhook_url, sample_alert(world.anchor), secret)
        show("Alertmanager -> webhook", f"POST {agent.webhook_url} -> {status} {answer['status']}")
        await wait_for_runs(agent.process, 1)
        trace = agent.trace

    state = json.loads((STATE_DIR / "world.json").read_text())
    run = trace.last_run
    assert run is not None
    show("What changed in production", json.dumps(state["actions"], indent=2) if state["actions"] else "Nothing.")
    show("Incident channel", "\n".join(f"{p['time']}  {p['message']}" for p in state["status_updates"]))
    summary = next(iter(state["incidents"].values()))["summary_path"]
    show(f"Incident summary ({summary})", (STATE_DIR / summary).read_text())
    show("Trace timeline", "\n".join(run.timeline()))
    show("Agent's final message", run.final_answer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deny", action="store_true", help="the on-call engineer denies the rollback")
    asyncio.run(main(parser.parse_args().deny))
