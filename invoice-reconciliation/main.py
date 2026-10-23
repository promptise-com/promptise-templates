"""Run the finance operations agent.

    promptise run start                # the agent with a real model: cron + Stripe webhook
    promptise run reconcile            # the same, and reconcile yesterday once right away
    promptise run demo-offline         # no API key yet: a scripted model plays one day

The billing system and the email service are local stand-ins (fake_systems/)
that this program starts on 127.0.0.1.  Replace them with your real services
before you use real data (see the README).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from promptise import Model, QueueApprovalHandler, WebhookApprovalHandler
from promptise.mcp.server import verify_audit_log
from promptise.runtime import AgentRuntime, OrchestrationAPI, TriggerEvent

from fake_systems.launch import start_fake_systems
from fake_systems.security import AGENT_KEY_ENV
from fake_systems.store import seed
from finance_agent.process import PROCESS_NAME, PROJECT_ROOT, Settings, build_process_config
from finance_agent.reviewers import console_reviewer

PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
TIMEZONE = "Europe/Berlin"
DATA_DIR = PROJECT_ROOT / ".data"


def _missing_settings(model: str) -> list[str]:
    needed = ["STRIPE_WEBHOOK_SECRET", "PROMPTISE_AUDIT_SECRET"]
    if key := PROVIDER_KEYS.get(model.split(":", 1)[0]):
        needed.append(key)
    return [name for name in needed if not os.environ.get(name)]


def _model(spec: str) -> str | Model:
    """The model to run.  OpenAI reasoning models get a reasoning effort.

    With the default effort, gpt-5-mini spent 3,000 to 5,000 reasoning tokens
    and about 30 seconds on each step of the nightly run.  The steps are
    spelled out in the prompt, so "low" is enough and much cheaper.
    """
    provider, _, name = spec.partition(":")
    if provider == "openai" and name.startswith(("gpt-5", "o3", "o4")):
        effort = os.environ.get("FINANCE_AGENT_REASONING") or "low"
        return Model(name, provider="openai", extra={"reasoning_effort": effort})
    return spec


def _yesterday() -> str:
    return (datetime.now(ZoneInfo(TIMEZONE)) - timedelta(days=1)).date().isoformat()


async def run_live(*, reconcile_now: bool, reseed: bool) -> int:
    model = os.environ.get("FINANCE_AGENT_MODEL", "openai:gpt-5-mini")
    if missing := _missing_settings(model):
        print(f"Set {', '.join(missing)} (see .env.example).", file=sys.stderr)
        print("No key yet? `promptise run demo-offline` plays a scripted day.", file=sys.stderr)
        return 2

    db = DATA_DIR / "finance.sqlite"
    if reseed or not db.exists():
        # The stand-in billing data covers yesterday, so tonight's run has work to do.
        seed(db, day=_yesterday())
    systems = start_fake_systems(
        db,
        # The key the agent presents to the billing and email servers.
        agent_key=os.environ.get(AGENT_KEY_ENV) or secrets.token_urlsafe(32),
        audit_secret=os.environ["PROMPTISE_AUDIT_SECRET"],
        data_dir=DATA_DIR / "systems",
    )

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
        servers=systems.servers,
        webhook_secret=os.environ["STRIPE_WEBHOOK_SECRET"],
        reviewer=reviewer,
        model=_model(model),
        webhook_port=int(os.environ.get("WEBHOOK_PORT", "8787")),
        # FINANCE_CRON="* * * * *" fires every minute, to watch the schedule work.
        cron_expression=os.environ.get("FINANCE_CRON") or "30 4 * * *",
        cron_timezone=TIMEZONE,
    )
    runtime = AgentRuntime()
    process = await runtime.add_process(PROCESS_NAME, build_process_config(settings))
    api = None
    try:
        await runtime.start_all()
        if reconcile_now:
            # The same payload the cron trigger sends, stamped now.
            now = datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds")
            await process.inject(
                TriggerEvent(
                    trigger_id="manual", trigger_type="cron", payload={"scheduled_time": now}
                )
            )
        # The control API lets finance resume the agent after a budget pause.
        if os.environ.get("RUNTIME_API_TOKEN"):
            api = OrchestrationAPI(runtime, port=9100, auth_token=os.environ["RUNTIME_API_TOKEN"])
            await api.start()
        print(
            f"{PROCESS_NAME} ({model}): cron '{settings.cron_expression}' {TIMEZONE}, "
            f"webhook http://127.0.0.1:{settings.webhook_port}/stripe. Ctrl+C stops.",
            flush=True,
        )
        await runtime.run_forever()  # start_all() already ran: this waits for Ctrl+C
    finally:
        if api is not None:
            await api.stop()
        if reviewer_task:
            reviewer_task.cancel()
        await runtime.stop_all()
        systems.stop()
        for name, log in systems.audit_logs.items():
            if log.exists():
                report = verify_audit_log(log, os.environ["PROMPTISE_AUDIT_SECRET"])
                verdict = "intact" if report.ok else f"BROKEN: {report.first_problem}"
                print(f"audit log {name}: {report.entries} entries, {verdict}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Lumen Metrics finance operations agent")
    parser.add_argument("--reconcile-now", action="store_true", help="reconcile once at start")
    parser.add_argument(
        "--reseed", action="store_true", help="reset the stand-in data to yesterday"
    )
    parser.add_argument("--demo", action="store_true", help="offline: a scripted model, no key")
    args = parser.parse_args()
    if args.demo:
        from finance_agent.demo import run_demo

        asyncio.run(run_demo())
        return 0
    return asyncio.run(run_live(reconcile_now=args.reconcile_now, reseed=args.reseed))


if __name__ == "__main__":
    sys.exit(main())
