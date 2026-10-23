"""Multi-tenant support agent: run the demo, or ask one question as one user.

    python main.py --offline                  # the demo, no API key needed
    python main.py                            # the demo with a real model
    python main.py --as acme/alice "Where is my order AC-5001?"

``promptise run demo`` and ``promptise run`` do the same through promptise.toml.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from typing import Any

from promptise.mcp.server import AuditMiddleware
from promptise.memory import InMemoryProvider, MemoryScope, owner_key

from agent import ToolUse, ask, create_agent
from demo import STEPS, offline_model
from support_desk.backend import build_server
from support_desk.identity import USERS, LoginService
from support_desk.serving import running_backend

PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--offline", action="store_true", help="use the scripted model (no API key)")
    parser.add_argument("--model", help="model string, e.g. openai:gpt-5-mini (default: SUPPORT_MODEL)")
    parser.add_argument("--as", dest="who", metavar="TENANT/USER", help="ask one question as this user")
    parser.add_argument("question", nargs="*", help="the question to ask with --as")
    return parser.parse_args()


def check_model_key(model: str) -> None:
    provider = model.split(":", 1)[0]
    key = PROVIDER_KEYS.get(provider)
    if key and not os.environ.get(key):
        sys.exit(f"{model} needs {key}. Copy .env.example to .env and set it, or run with --offline.")


def summarize(use: ToolUse) -> str:
    """One line per tool call: what came back, without the payload."""
    args = ", ".join(f"{k}={v!r}" for k, v in use.args.items() if k != "description")
    head = f"{use.name}({args})"
    try:
        data = json.loads(use.result)
    except ValueError:
        titles = [line[3:] for line in use.result.splitlines() if line.startswith("## ")]
        return f"{head} -> articles: {', '.join(titles) or use.result[:80]}"
    if isinstance(data, dict) and "error" in data:
        return f"{head} -> {data['error']['code']}: {data['error']['message']}"
    if isinstance(data, list):
        return f"{head} -> {', '.join(row.get('ticket_id', '?') for row in data) or 'no results'}"
    ident = data.get("ticket_id") or data.get("order_id")
    return f"{head} -> {ident} ({data.get('status')})"


async def run_demo(agent: Any, login: LoginService) -> None:
    for step in STEPS:
        if step.title:
            print(f"\n=== {step.title} ===")
        turn = await ask(agent, login.caller_for(step.tenant, step.user), step.question)
        print(f"\n[{step.tenant}/{step.user}] {step.question}")
        for use in turn.tools:
            print(f"   tool  {summarize(use)}")
        print(f"   agent {turn.answer}")


async def isolation_report(audit: AuditMiddleware, memory: InMemoryProvider) -> None:
    print("\n=== What the backend saw (audit log, per tenant) ===")
    outcomes: dict[str, Counter[str]] = {}
    for entry in audit.entries:
        tenant = entry.get("identity", {}).get("tenant_id", "?")
        outcomes.setdefault(tenant, Counter())[entry.get("error_code", "OK")] += 1
    for tenant, counts in sorted(outcomes.items()):
        print(f"   {tenant:7} " + ", ".join(f"{code} x{n}" for code, n in sorted(counts.items())))

    print("\n=== Memory entries per isolation key ===")
    for tenant, user in sorted(USERS):
        key = owner_key(user, tenant)
        entries = await memory.list_entries(user_id=key)
        print(f"   {key:14} {len(entries)} {'entry' if len(entries) == 1 else 'entries'}")


async def main() -> None:
    args = parse_args()
    model: Any = offline_model() if args.offline else (args.model or os.environ.get("SUPPORT_MODEL"))
    if args.question and not args.who:
        sys.exit("Say who is asking: --as TENANT/USER, for example --as acme/alice.")
    if args.who and args.offline:
        sys.exit("The offline model only knows the demo script. Use a real model to ask your own question.")
    if not args.offline:
        model = model or "openai:gpt-5-mini"
        check_model_key(model)

    login = LoginService()
    audit = AuditMiddleware(include_args=True, signed=False)
    memory = InMemoryProvider(scope=MemoryScope.PER_USER)
    backend_url = os.environ.get("SUPPORT_BACKEND_URL")

    async def run(url: str) -> None:
        agent = await create_agent(backend_url=url, model=model, memory=memory, login=login)
        try:
            if args.who:
                tenant, _, user = args.who.partition("/")
                if (tenant, user) not in USERS:
                    sys.exit(f"Unknown user {args.who}. Known: {', '.join(f'{t}/{u}' for t, u in USERS)}")
                turn = await ask(agent, login.caller_for(tenant, user), " ".join(args.question))
                for use in turn.tools:
                    print(f"tool  {summarize(use)}")
                print(turn.answer)
            else:
                await run_demo(agent, login)
                if not backend_url:
                    await isolation_report(audit, memory)
        finally:
            await agent.shutdown()

    if backend_url:
        await run(backend_url)
    else:
        async with running_backend(build_server(audit=audit)) as url:
            await run(url)


if __name__ == "__main__":
    asyncio.run(main())
