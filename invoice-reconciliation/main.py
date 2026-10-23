"""Run the finance operations agent.

    promptise run demo                 # offline: scripted model, no API key
    promptise run                      # live: a real model, cron + Stripe webhook
    promptise run main.py -- --reconcile-now   # live, and reconcile once right away

Live mode still talks to the fake billing system and outbox in .data/.
Point fake_systems at your real services to go further (see the README).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from promptise import QueueApprovalHandler, WebhookApprovalHandler
from promptise.runtime import AgentRuntime, OrchestrationAPI, TriggerEvent

from fake_systems.store import seed
from finance_agent.process import PROCESS_NAME, PROJECT_ROOT, Settings, build_process_config
from finance_agent.reviewers import console_reviewer

PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def _missing_settings(model: str) -> list[str]:
    missing = [] if os.environ.get("STRIPE_WEBHOOK_SECRET") else ["STRIPE_WEBHOOK_SECRET"]
    key = PROVIDER_KEYS.get(model.split(":", 1)[0])
    if key and not os.environ.get(key):
        missing.append(key)
    return missing


async def run_live(reconcile_now: bool) -> int:
    model = os.environ.get("FINANCE_AGENT_MODEL", "openai:gpt-5-mini")
    missing = _missing_settings(model)
    if missing:
        print(
            f"Set {', '.join(missing)} (see .env.example), or run the offline demo.",
            file=sys.stderr,
        )
        return 2

    db = PROJECT_ROOT / ".data" / "finance.sqlite"
    if not db.exists():
        seed(db)

    # A webhook reviewer when one is configured, else a person at this terminal.
    reviewer_task = None
    if os.environ.get("APPROVAL_WEBHOOK_URL"):
        reviewer = WebhookApprovalHandler(
            url=os.environ["APPROVAL_WEBHOOK_URL"],
            secret=os.environ.get("APPROVAL_WEBHOOK_SECRET"),
        )
    else:
        reviewer = QueueApprovalHandler()
        reviewer_task = asyncio.create_task(console_reviewer(reviewer))

    settings = Settings(
        db_path=db,
        webhook_secret=os.environ["STRIPE_WEBHOOK_SECRET"],
        reviewer=reviewer,
        model=model,
        webhook_port=int(os.environ.get("WEBHOOK_PORT", "8787")),
    )
    runtime = AgentRuntime()
    process = await runtime.add_process(PROCESS_NAME, build_process_config(settings))
    await runtime.start_all()
    if reconcile_now:
        # The same payload the cron trigger sends, stamped now.
        now = datetime.now(ZoneInfo(settings.cron_timezone)).isoformat(timespec="seconds")
        await process.inject(
            TriggerEvent(trigger_id="manual", trigger_type="cron", payload={"scheduled_time": now})
        )
    # The control API lets finance resume the agent after a budget pause.
    api = None
    if os.environ.get("RUNTIME_API_TOKEN"):
        api = OrchestrationAPI(runtime, port=9100, auth_token=os.environ["RUNTIME_API_TOKEN"])
        await api.start()
    print(
        f"{PROCESS_NAME}: cron {settings.cron_expression} ({settings.cron_timezone}), "
        f"webhook http://127.0.0.1:{settings.webhook_port}/stripe. Ctrl+C stops."
    )
    try:
        await runtime.run_forever()  # start_all() already ran: this waits for Ctrl+C
    finally:
        if api is not None:
            await api.stop()
        if reviewer_task:
            reviewer_task.cancel()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Lumen Metrics finance operations agent")
    parser.add_argument("--demo", action="store_true", help="offline demo with a scripted model")
    parser.add_argument(
        "--reconcile-now", action="store_true", help="live: reconcile once at start"
    )
    args = parser.parse_args()
    if args.demo:
        from finance_agent.demo import run_demo

        asyncio.run(run_demo())
        return 0
    return asyncio.run(run_live(args.reconcile_now))


if __name__ == "__main__":
    sys.exit(main())
