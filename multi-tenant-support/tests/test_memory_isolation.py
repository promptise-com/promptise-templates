"""Memory is keyed by tenant and user: two users called alice never share it."""

from __future__ import annotations

import asyncio

from promptise.conversations import generate_session_id
from promptise.memory import owner_key
from promptise.testing import ToolCall

from agent import ask
from conftest import support_stack

FACT = "Please remember that I prefer to be contacted by phone."
QUESTION = "How do I prefer to be contacted?"


def recalled(model_call) -> str:
    """The memory block the agent injected into one model request, if any."""
    blocks = [str(m.content) for m in model_call.messages if "<memory_context>" in str(m.content)]
    return "\n".join(blocks)


def test_memory_does_not_leak_between_tenants():
    replies = [
        ToolCall("remember_preference", {"preference": "Prefers to be contacted by phone."}),
        "Noted.",
        "Nothing on file.",
        "By phone.",
    ]

    async def scenario():
        async with support_stack(replies) as stack:
            # Three separate chat sessions: memory is what carries the fact across.
            await ask(
                stack.agent, stack.login.caller_for("acme", "alice"), FACT, session_id=generate_session_id()
            )
            for tenant in ("globex", "acme"):
                caller = stack.login.caller_for(tenant, "alice")
                await ask(stack.agent, caller, QUESTION, session_id=generate_session_id())
            return stack

    stack = asyncio.run(scenario())
    globex_alice, acme_alice = stack.model.calls[2], stack.model.calls[3]
    # Same user_id, other tenant: nothing recalled.
    assert "phone" not in recalled(globex_alice)
    # Same tenant and user, new session: the preference comes back.
    assert "contacted by phone" in recalled(acme_alice)


def test_preferences_are_stored_under_the_tenant_qualified_key():
    replies = [ToolCall("remember_preference", {"preference": "Prefers to be contacted by phone."}), "Noted."]

    async def scenario():
        async with support_stack(replies) as stack:
            alice = stack.login.caller_for("acme", "alice")
            await ask(stack.agent, alice, FACT, session_id=generate_session_id())
            acme = await stack.memory.list_entries(user_id=owner_key("alice", "acme"))
            globex = await stack.memory.list_entries(user_id=owner_key("alice", "globex"))
            # Deleting one tenant's user leaves the other tenant's user alone.
            removed = await stack.memory.purge_user("alice", tenant_id="acme")
            return acme, globex, removed

    acme, globex, removed = asyncio.run(scenario())
    assert owner_key("alice", "acme") == "acme::alice"
    assert [e.content for e in acme] == ["Prefers to be contacted by phone."]
    assert globex == []
    assert removed == 1
