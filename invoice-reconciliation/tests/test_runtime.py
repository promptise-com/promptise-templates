"""The Agent Runtime parts: the cron trigger fires, memory survives a restart,
and the prompt states the limit the code enforces."""

from __future__ import annotations

import asyncio

from promptise.runtime import AgentProcess
from promptise.testing import ScriptedModel, ToolCall

import finance_agent.prompt as prompt_module
from conftest import NIGHTLY_EVENT, read_jsonl, running, tool_results, wait_for_runs
from fake_systems.store import BUSINESS_DAY
from finance_agent.process import PROCESS_NAME, build_process_config


def test_the_cron_trigger_fires_the_process(make_settings):
    """A real schedule, every 2 seconds (the sixth field is seconds); no inject()."""
    model = ScriptedModel(
        [ToolCall("reconcile_day", {"date": BUSINESS_DAY}), "Reconciled."],
        on_exhausted="repeat_last",  # later ticks get the plain answer
    )
    settings = make_settings(model, cron_expression="* * * * * */2")

    async def scenario():
        async with running(settings) as process:
            await wait_for_runs(process, 1, timeout=15)

    asyncio.run(scenario())

    events = [
        e["data"]
        for e in read_jsonl(settings.journal_dir / "finance-ops.jsonl")
        if e["entry_type"] == "trigger_event"
    ]
    assert events[0]["trigger_type"] == "cron"
    assert events[0]["trigger_id"].startswith("cron-")
    assert events[0]["payload"]["cron_expression"] == "* * * * * */2"
    # The run that tick started called the tools, as recorded in the trace.
    trace = read_jsonl(settings.trace_dir / "finance-ops.jsonl")
    assert "reconcile_day" in [e.get("tool_name") for e in trace if e["event_type"] == "tool.call"]
    assert "[Trigger: cron]" in model.calls[0].messages[-1].content


def test_follow_up_memory_survives_a_restart(make_settings):
    entry = f"{BUSINESS_DAY} cus_Yf8tH4g in_2049 payment_failed_retry"
    first = ScriptedModel(
        [ToolCall("update_state", {"key": "followups", "value": [entry]}), "Saved."]
    )
    second = ScriptedModel(["Nothing to do."])

    async def scenario():
        async with running(make_settings(first)) as process:
            await process.inject(NIGHTLY_EVENT)
            await wait_for_runs(process, 1)
        # A new process object, as after a crash or a redeploy: same name, same journal.
        restarted = AgentProcess(PROCESS_NAME, build_process_config(make_settings(second)))
        await restarted.start()
        try:
            assert "followups" in restarted.recovered["context_keys"]
            await restarted.inject(NIGHTLY_EVENT)
            await wait_for_runs(restarted, restarted.recovered["invocation_count"] + 1)
        finally:
            await restarted.stop()

    asyncio.run(scenario())
    # The agent sees what it did before, at the start of the next run.
    shown = [m.content for m in second.calls[0].messages if m.type == "system"]
    assert any("[Context State]" in text and entry in text for text in shown)


def test_the_agent_can_only_write_its_follow_up_memory(make_settings):
    model = ScriptedModel(
        [ToolCall("update_state", {"key": "refund_limit", "value": 99}), "Tried."]
    )

    async def scenario():
        async with running(make_settings(model)) as process:
            await process.inject(NIGHTLY_EVENT)
            await wait_for_runs(process, 1)
            return process.context.state_snapshot()

    state = asyncio.run(scenario())
    assert "refund_limit" not in state
    assert tool_results(model)[0].startswith("Error")


def test_the_prompt_states_the_threshold_the_gate_enforces(monkeypatch):
    assert "Refunds above 100.00 EUR wait for a person" in prompt_module.INSTRUCTIONS
    monkeypatch.setattr(prompt_module, "REFUND_APPROVAL_THRESHOLD_CENTS", 25_000)
    assert "Refunds above 250.00 EUR wait for a person" in prompt_module.build_instructions()
