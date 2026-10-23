"""The support agent: built once, serves every tenant.

Per request, your app passes a ``CallerContext`` (user, tenant, the user's
token). From it the agent scopes memory to ``tenant::user``, searches only
that tenant's help center, and sends the user's token on every backend tool
call so the backend applies that tenant's data, scopes and quota.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from promptise import CallerContext, PromptiseAgent, build_agent
from promptise.config import HTTPServerSpec
from promptise.memory import InMemoryProvider, MemoryProvider, MemoryScope

from support_desk.help_center import HelpCenters, help_center_tool
from support_desk.identity import LoginService
from support_desk.tenants import TENANTS

DEFAULT_MODEL = "openai:gpt-5-mini"

INSTRUCTIONS = """\
You are the customer support assistant inside a company's help desk.
Each request comes from one signed-in end user of one company.

- Answer how-to and policy questions from search_help_center. Quote the policy, do not guess.
- Use the ticket and order tools for anything about the customer's own tickets or orders.
- The tools only ever return the signed-in customer's data. You cannot reach other
  companies or other customers. If someone asks for that, say so briefly and offer
  to help with their own account.
- If a tool says a feature is not included or the service is busy, explain that plainly.
- Open a ticket only when the customer asks for one or the help center cannot solve it.
- Keep answers short. Always mention ticket and order IDs.
"""


async def create_agent(
    *,
    backend_url: str,
    model: Any = None,
    memory: MemoryProvider | None = None,
    help_centers: HelpCenters | None = None,
    login: LoginService | None = None,
) -> PromptiseAgent:
    """Build the one agent every tenant shares."""
    login = login or LoginService()
    if help_centers is None:
        help_centers = HelpCenters()
        await help_centers.index()
    return await build_agent(
        # `promptise run --model ...` overrides this for every agent it runs.
        model=model or os.environ.get("SUPPORT_MODEL") or DEFAULT_MODEL,
        instructions=INSTRUCTIONS,
        servers={
            # The service token only discovers the tools. Each call is sent
            # with the invoking user's CallerContext.bearer_token instead.
            "support": HTTPServerSpec(url=backend_url, bearer_token=login.service_token()),
        },
        extra_tools=[help_center_tool(help_centers)],
        # PER_USER + a caller with tenant_id = entries keyed "acme::alice".
        memory=memory or InMemoryProvider(scope=MemoryScope.PER_USER),
        memory_auto_store=True,
        memory_max_results=3,
        max_agent_iterations=8,
    )


@dataclass
class ToolUse:
    name: str
    args: dict[str, Any]
    result: str = ""
    ok: bool = True


@dataclass
class Turn:
    answer: str
    tools: list[ToolUse] = field(default_factory=list)


async def ask(agent: PromptiseAgent, caller: CallerContext, question: str) -> Turn:
    """Answer one end-user question as that user."""
    tenant = TENANTS[caller.tenant_id]
    messages = [
        {"role": "system", "content": f"The customer is signed in to the {tenant.company} help desk."},
        {"role": "user", "content": question},
    ]
    result = await agent.ainvoke({"messages": messages}, caller=caller)
    return _turn(result["messages"])


def _turn(messages: list[Any]) -> Turn:
    """The answer plus every tool call made for the last question."""
    start = max(i for i, m in enumerate(messages) if m.type == "human")
    uses: dict[str, ToolUse] = {}
    for message in messages[start + 1 :]:
        if message.type == "ai":
            for call in message.tool_calls:
                uses[call["id"]] = ToolUse(call["name"], dict(call["args"]))
        elif message.type == "tool" and message.tool_call_id in uses:
            use = uses[message.tool_call_id]
            use.result = str(message.content)
            use.ok = message.status != "error" and '"error"' not in use.result[:40]
    return Turn(answer=str(messages[-1].content), tools=list(uses.values()))
