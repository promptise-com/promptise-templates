"""Memory is keyed by tenant and user: two users called alice never share it."""

from __future__ import annotations

import asyncio

from promptise.memory import owner_key

from agent import ask
from conftest import support_stack

FACT = "Please remember that I prefer to be contacted by phone."
QUESTION = "How do I prefer to be contacted?"


def recalled(model_call) -> str:
    """The memory block the agent injected into one model request, if any."""
    blocks = [str(m.content) for m in model_call.messages if "<memory_context>" in str(m.content)]
    return "\n".join(blocks)


def test_memory_does_not_leak_between_tenants():
    async def scenario():
        async with support_stack(["Noted.", "Nothing on file.", "By phone."]) as stack:
            await ask(stack.agent, stack.login.caller_for("acme", "alice"), FACT)
            await ask(stack.agent, stack.login.caller_for("globex", "alice"), QUESTION)
            await ask(stack.agent, stack.login.caller_for("acme", "alice"), QUESTION)
            return stack

    stack = asyncio.run(scenario())
    _, globex_alice, acme_alice = stack.model.calls
    # Same user_id, other tenant: nothing recalled.
    assert "phone" not in recalled(globex_alice)
    # Same tenant and user: the fact comes back.
    assert "contacted by phone" in recalled(acme_alice)


def test_memory_entries_are_stored_under_the_tenant_qualified_key():
    async def scenario():
        async with support_stack(["Noted."]) as stack:
            await ask(stack.agent, stack.login.caller_for("acme", "alice"), FACT)
            acme = await stack.memory.list_entries(user_id=owner_key("alice", "acme"))
            globex = await stack.memory.list_entries(user_id=owner_key("alice", "globex"))
            # Deleting one tenant's user leaves the other tenant's user alone.
            removed = await stack.memory.purge_user("alice", tenant_id="acme")
            return acme, globex, removed

    acme, globex, removed = asyncio.run(scenario())
    assert owner_key("alice", "acme") == "acme::alice"
    assert len(acme) == 1 and "contacted by phone" in acme[0].content
    assert globex == []
    assert removed == 1
