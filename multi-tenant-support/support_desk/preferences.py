"""Customer preferences the agent remembers across chat sessions.

The agent saves a preference only when the customer states one, instead of
storing every exchange. The memory provider is ``PER_USER``, and entries are
keyed by ``CallerContext.isolation_key`` (``"acme::alice"``), the same key the
agent uses when it searches memory before each answer. So Acme's alice and
Globex's alice never see each other's preferences.
"""

from __future__ import annotations

from promptise import CallerContext, tool
from promptise.memory import MemoryProvider


def preference_tool(memory: MemoryProvider):
    @tool
    async def remember_preference(preference: str, caller: CallerContext | None) -> str:
        """Save a lasting preference the customer just stated, for future chats.

        Use it for things like the preferred contact channel or language. Do not
        use it for one-off details of a ticket or order.

        Args:
            preference: One short sentence, for example "Prefers to be contacted by phone."
        """
        if caller is None or not caller.tenant_id:
            return "No signed-in customer for this request; nothing was saved."
        await memory.add(preference, metadata={"kind": "preference"}, user_id=caller.isolation_key)
        return "Saved."

    return remember_preference
