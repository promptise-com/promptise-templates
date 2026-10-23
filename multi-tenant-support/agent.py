"""The support agent: built once, serves every tenant.

Per request, your app passes a ``CallerContext`` (user, tenant, the user's
token). From it the agent scopes memory to ``tenant::user``, searches only
that tenant's help center, and sends the user's token on every backend tool
call so the backend applies that tenant's data, scopes and quota.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from promptise import (
    CallerContext,
    CredentialDetector,
    PIICategory,
    PIIDetector,
    PromptiseAgent,
    PromptiseSecurityScanner,
    build_agent,
)
from promptise.config import HTTPServerSpec
from promptise.conversations import ConversationStore, InMemoryConversationStore
from promptise.memory import InMemoryProvider, MemoryProvider, MemoryScope
from promptise.observability_config import ObservabilityConfig, ObserveLevel, TransporterType

from support_desk.help_center import HelpCenters, help_center_tool
from support_desk.identity import LoginService
from support_desk.instructions import SUPPORT_BLOCKS, support_instructions
from support_desk.preferences import preference_tool

DEFAULT_MODEL = "openai:gpt-5-mini"


def payment_data_guardrail() -> PromptiseSecurityScanner:
    """Redact card data, passwords and secrets before the model sees them.

    Customers paste these into support chats. With ``redact_input=True`` the
    redacted text is what reaches the model, the conversation store and memory.
    Regex detectors only: no model download. IBAN detection is left out on
    purpose: as of 1.9.0 it also matches parcel tracking numbers such as
    JD0146000039902, which this help desk must be able to show.
    """
    return PromptiseSecurityScanner(
        detectors=[
            PIIDetector(
                categories={
                    PIICategory.CREDIT_CARDS,
                    PIICategory.CVV,
                    PIICategory.CARD_EXPIRY,
                    PIICategory.BANK_ACCOUNT,
                    PIICategory.PASSWORD,
                }
            ),
            CredentialDetector(),
        ],
        redact_input=True,
    )


def tracing(trace_dir: str | None) -> ObservabilityConfig:
    """Every run, LLM turn and tool call, attributed to the caller's tenant::user."""
    return ObservabilityConfig(
        level=ObserveLevel.STANDARD,
        session_name="multi-tenant-support",
        transporters=[TransporterType.JSON] if trace_dir else [],
        output_dir=trace_dir or ".promptise/traces",
    )


async def create_agent(
    *,
    backend_url: str,
    model: Any = None,
    memory: MemoryProvider | None = None,
    conversations: ConversationStore | None = None,
    help_centers: HelpCenters | None = None,
    login: LoginService | None = None,
    trace_dir: str | None = None,
) -> PromptiseAgent:
    """Build the one agent every tenant shares."""
    login = login or LoginService()
    memory = memory or InMemoryProvider(scope=MemoryScope.PER_USER)
    if help_centers is None:
        help_centers = HelpCenters()
        await help_centers.index()
    return await build_agent(
        # `promptise run --model ...` overrides this for every agent it runs.
        model=model or os.environ.get("SUPPORT_MODEL") or DEFAULT_MODEL,
        # Rules once in the reasoning node; the tenant part re-rendered per request.
        graph_blocks=SUPPORT_BLOCKS,
        instructions=support_instructions,
        servers={
            # The service token only discovers the tools. Each call is sent
            # with the invoking user's CallerContext.bearer_token instead.
            "support": HTTPServerSpec(url=backend_url, bearer_token=login.service_token()),
        },
        extra_tools=[help_center_tool(help_centers), preference_tool(memory)],
        # PER_USER + a caller with tenant_id: searched as "acme::alice" before each
        # answer. Only stated preferences are stored (remember_preference), not
        # every exchange: the chat history already lives in the conversation store.
        memory=memory,
        memory_max_results=3,
        # Sessions are owned by tenant::user: another tenant's user gets SessionAccessDenied.
        conversation_store=conversations or InMemoryConversationStore(),
        conversation_max_messages=20,
        guardrails=payment_data_guardrail(),
        observe=tracing(trace_dir),
        # Support questions are short tool loops: the default ReAct pattern fits.
        agent_pattern="react",
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


async def ask(agent: PromptiseAgent, caller: CallerContext, question: str, *, session_id: str) -> Turn:
    """Answer one end-user message in their chat session, as that user."""
    # Traces take the session from the caller's metadata; with it, every event
    # of this turn can be traced back to the session and so to the tenant.
    caller = dataclasses.replace(caller, metadata={**caller.metadata, "session_id": session_id})
    since = time.time()
    answer = await agent.chat(question, session_id=session_id, caller=caller)
    return Turn(answer=answer, tools=tool_uses(agent, since, session_id))


def tool_uses(agent: PromptiseAgent, since: float, session_id: str) -> list[ToolUse]:
    """The tool calls of one chat turn, read back from the agent's trace."""
    uses: list[ToolUse] = []
    for entry in agent.collector.get_timeline():
        if entry.timestamp < since or entry.session_id != session_id:
            continue
        meta = entry.metadata
        if entry.event_type.value == "tool.call":
            uses.append(ToolUse(meta.get("tool_name", ""), _arguments(meta.get("arguments"))))
        elif entry.event_type.value in ("tool.result", "tool.error") and uses:
            pending = next((u for u in uses if u.name == meta.get("tool_name") and not u.result), None)
            if pending is not None:
                error = entry.event_type.value == "tool.error"
                pending.result = str(meta.get("error") if error else meta.get("result_preview", ""))
                pending.ok = not error
    return uses


def _arguments(recorded: Any) -> dict[str, Any]:
    """Tool arguments as the trace recorded them: a dict (Promptise 1.9.0 and later), or the
    JSON or repr text older versions recorded."""
    if isinstance(recorded, dict):
        return recorded
    for parse in (json.loads, ast.literal_eval):
        try:
            parsed = parse(recorded or "{}")
        except (TypeError, ValueError, SyntaxError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"arguments": recorded}
