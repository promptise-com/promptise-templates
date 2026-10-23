"""Multi-tenant support agent: run the demo, or ask one question as one user.

    promptise run                                   # the demo with a real model
    promptise run ask -- --as acme/alice "Where is my order AC-5001?"
    promptise run demo-offline                      # no API key: scripted model

The model comes from SUPPORT_MODEL (default openai:gpt-5-mini) or --model.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from promptise.conversations import SessionAccessDenied, SQLiteConversationStore, generate_session_id
from promptise.mcp.client import MCPClient
from promptise.mcp.server import AuditMiddleware
from promptise.memory import InMemoryProvider, MemoryScope, owner_key

from agent import ToolUse, Turn, ask, create_agent
from demo import SCENES, offline_model
from support_desk.backend import RefundReview, build_server
from support_desk.identity import USERS, LoginService
from support_desk.records import TicketDesk
from support_desk.serving import running_backend

PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
IDS = re.compile(r"\b(?:ACME|GLX|AC|GX)-\d{4}\b")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--offline", action="store_true", help="use the scripted model (no API key)")
    parser.add_argument("--model", help="model string, e.g. openai:gpt-5-mini (default: SUPPORT_MODEL)")
    parser.add_argument("--as", dest="who", metavar="TENANT/USER", help="ask one question as this user")
    parser.add_argument(
        "--state-dir", default=".promptise", help="conversations and traces (default: .promptise)"
    )
    parser.add_argument("question", nargs="*", help="the question to ask with --as")
    return parser.parse_args(argv)


def check_model_key(model: str) -> None:
    key = PROVIDER_KEYS.get(model.split(":", 1)[0])
    if key and not os.environ.get(key):
        sys.exit(
            f"{model} needs {key}. Copy .env.example to .env and set it, or run: promptise run demo-offline"
        )


def summarize(use: ToolUse) -> str:
    """One line per tool call: what came back, without the payload."""
    args = ", ".join(
        f"{k}={v!r}" for k, v in use.args.items() if k not in ("description", "comment", "reason")
    )
    head = f"{use.name}({args})"
    if not use.ok:
        return f"{head} -> {use.result[:150]}"
    titles = re.findall(r"^## (.+)$", use.result, flags=re.M)
    if titles:
        return f"{head} -> articles: {', '.join(titles)}"
    status = re.search(r'"status": "(\w+)"', use.result)
    ids = list(dict.fromkeys(IDS.findall(use.result)))
    if use.name == "list_tickets":
        return f"{head} -> {', '.join(ids) or 'no tickets'}"
    if ids and status:
        return f"{head} -> {ids[0]} ({status.group(1)})"
    return f"{head} -> {use.result[:80]}"


def show(tenant: str, user: str, text: str, turn: Turn) -> None:
    print(f"\n[{tenant}/{user}] {text}")
    for use in turn.tools:
        print(f"   tool  {summarize(use)}")
    print("   agent " + turn.answer.strip().replace("\n", "\n         "))


async def spend_quota(url: str, login: LoginService, tenant: str, user: str) -> None:
    """The tenant's own API integration calls the backend until its quota is gone."""
    async with MCPClient(url=url, bearer_token=login.token_for(tenant, user)) as client:
        for calls in range(1, 21):
            result = await client.call_tool("list_tickets", {})
            if "RATE_LIMIT_EXCEEDED" in result.content[0].text:
                spent = f"{calls - 1} backend calls, then RATE_LIMIT_EXCEEDED"
                print(f"\n   app   {tenant}'s own API integration: {spent}")
                return


async def run_demo(
    agent: Any, login: LoginService, url: str, store: SQLiteConversationStore
) -> dict[str, str]:
    """Run every scene. Returns session id -> tenant, to group the traces."""
    sessions: dict[str, str] = {}
    latest: dict[str, str] = {}  # tenant -> its most recent session id
    for chat in SCENES:
        if chat.title:
            print(f"\n=== {chat.title} ===")
        if chat.before == "burst":
            await spend_quota(url, login, chat.tenant, chat.user)
        if chat.before == "hijack":
            # Replay another tenant's session id, as a stolen link would.
            session_id = next(sid for tenant, sid in latest.items() if tenant != chat.tenant)
        else:
            session_id = generate_session_id()
        caller = login.caller_for(chat.tenant, chat.user)
        for message in chat.messages:
            try:
                turn = await ask(agent, caller, message.text, session_id=session_id)
            except SessionAccessDenied as denied:
                print(
                    f"\n[{chat.tenant}/{chat.user}] {message.text}  (sent with another tenant's session id)"
                )
                print(f"   app   SessionAccessDenied: {denied}")
                break
            sessions.setdefault(session_id, chat.tenant)
            show(chat.tenant, chat.user, message.text, turn)
            if "4242" in message.text:
                await show_stored_message(store, session_id)
        if chat.before != "hijack":
            latest[chat.tenant] = session_id
    return sessions


async def show_stored_message(store: SQLiteConversationStore, session_id: str) -> None:
    stored = [m for m in await store.load_messages(session_id) if m.role == "user"]
    print(f"   store the session keeps: {stored[-1].content!r}")


async def reports(
    agent: Any,
    audit: AuditMiddleware,
    refunds: RefundReview,
    memory: InMemoryProvider,
    sessions: dict[str, str],
) -> None:
    print("\n=== What the backend saw (audit log, per tenant) ===")
    outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in audit.entries:
        outcomes[entry.get("identity", {}).get("tenant_id", "?")][entry.get("error_code", "OK")] += 1
    for tenant, counts in sorted(outcomes.items()):
        print(f"   {tenant:7} " + ", ".join(f"{code} x{n}" for code, n in sorted(counts.items())))

    print("\n=== Refunds waiting for a support lead ===")
    for item in refunds.queue or [{"tenant_id": "-", "order_id": "none"}]:
        print(f"   {item['tenant_id']:7} {item['order_id']}")

    print("\n=== Traces per tenant (tokens, model calls, tool calls) ===")
    totals: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in agent.collector.get_timeline():
        tenant = sessions.get(entry.session_id or "")
        if tenant and entry.event_type.value == "agent.output":
            for key in ("total_tokens", "llm_call_count", "tool_call_count"):
                totals[tenant][key] += int(entry.metadata.get(key) or 0)
    for tenant, t in sorted(totals.items()):
        calls = f"{t['llm_call_count']} model calls, {t['tool_call_count']} tool calls"
        print(f"   {tenant:7} {t['total_tokens']} tokens, {calls}")

    print("\n=== Memory entries and chat sessions per tenant::user ===")
    for tenant, user in sorted(USERS):
        key = owner_key(user, tenant)
        entries = await memory.list_entries(user_id=key)
        owned = await agent.list_sessions(user_id=key)
        print(f"   {key:14} memory entries: {len(entries)}, chat sessions: {len(owned)}")


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.question and not args.who:
        sys.exit("Say who is asking: --as TENANT/USER, for example --as acme/alice.")
    if args.who and args.offline:
        sys.exit("The offline model only knows the demo script. Use a real model to ask your own question.")
    model: Any = (
        offline_model()
        if args.offline
        else (args.model or os.environ.get("SUPPORT_MODEL") or "openai:gpt-5-mini")
    )
    if not args.offline:
        check_model_key(model)

    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    login = LoginService()
    desk = TicketDesk()
    audit = AuditMiddleware(include_args=True, signed=False)
    refunds = RefundReview(desk)
    memory = InMemoryProvider(scope=MemoryScope.PER_USER)
    conversations = SQLiteConversationStore(str(state / "conversations.db"))

    async with running_backend(build_server(desk, audit=audit, refunds=refunds)) as url:
        agent = await create_agent(
            backend_url=url,
            model=model,
            memory=memory,
            conversations=conversations,
            login=login,
            trace_dir=str(state / "traces"),
        )
        try:
            if args.who:
                tenant, _, user = args.who.partition("/")
                if (tenant, user) not in USERS:
                    sys.exit(f"Unknown user {args.who}. Known: {', '.join(f'{t}/{u}' for t, u in USERS)}")
                question = " ".join(args.question)
                show(
                    tenant,
                    user,
                    question,
                    await ask(
                        agent, login.caller_for(tenant, user), question, session_id=generate_session_id()
                    ),
                )
            else:
                print(f"Model: {model if isinstance(model, str) else 'scripted (offline)'}")
                sessions = await run_demo(agent, login, url, conversations)
                await reports(agent, audit, refunds, memory, sessions)
        finally:
            await agent.shutdown()
            await conversations.close()


if __name__ == "__main__":
    asyncio.run(main())
