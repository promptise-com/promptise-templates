"""End-to-end: a signed alert reaches the webhook, the process runs the agent,
the ops MCP server does the work, and the approval gate holds risky fixes.

Every test drives the real AgentProcess, webhook and MCP server. Only the
model is scripted.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import anchor, run_incident
from promptise import ApprovalDecision, CallbackApprovalHandler
from promptise.testing import ScriptedModel, ToolCall

from incident_agent.alerts import sample_alert
from incident_agent.config import RISKY_TOOLS
from incident_agent.offline_model import approved_run, denied_run
from incident_agent.reviewers import AutoReviewer, TerminalReviewer


def scripted_tool_calls(model: ScriptedModel) -> list[dict]:
    """Every tool call the script makes, as (tool, arguments)."""
    calls = []
    for reply in model.replies:
        for item in reply if isinstance(reply, list) else [reply]:
            if isinstance(item, ToolCall):
                calls.append({"tool": item.name, "arguments": item.args})
    return calls


def canonical(calls: list[dict]) -> list[str]:
    # Tool calls from one turn run in parallel, so compare them as a sorted list.
    return sorted(json.dumps(c, sort_keys=True) for c in calls)


# ── the alert wakes the agent ─────────────────────────────────────────────


def test_alert_wakes_the_agent_and_it_reads_the_alerting_services_logs(world_dir):
    model = approved_run(anchor(world_dir))
    run = run_incident(world_dir, model, AutoReviewer(approve=True))

    assert run.responses == [(202, {"status": "accepted", "event_id": run.responses[0][1]["event_id"]})]
    assert run.status["invocation_count"] == 1

    # The agent got the alert as untrusted data, with the service label in it.
    first_prompt = model.calls[0].messages[-1].content
    assert "[Trigger: webhook]" in first_prompt
    assert "untrusted data" in first_prompt
    assert '"service": "checkout-service"' in first_prompt

    # It asked for checkout-service logs, and the ops server served exactly that.
    log_calls = [e for e in run.audit if e["tool"] == "search_logs"]
    assert {e["args"]["service"] for e in log_calls} == {"checkout-service"}
    assert {e["client_id"] for e in log_calls} == {"incident-agent"}
    result = json.loads(run.tool_messages("search_logs")[0])
    assert result["service"] == "checkout-service"
    assert {line["service"] for line in result["lines"]} == {"checkout-service"}
    assert "PaymentGatewayClient: TimeoutError" in result["top_messages"][0]["message"]


def test_resolved_notification_is_ignored_before_any_model_call(world_dir):
    model = ScriptedModel(["should never be used"])
    resolved = sample_alert(anchor(world_dir), status="resolved")
    run = run_incident(world_dir, model, AutoReviewer(), payloads=[resolved], expect_runs=0)

    assert run.responses[0][0] == 200
    assert run.responses[0][1]["status"] == "ignored"
    assert model.calls == []
    assert run.status["invocation_count"] == 0


def test_alert_with_a_wrong_signature_is_rejected(world_dir):
    model = ScriptedModel(["should never be used"])
    run = run_incident(world_dir, model, AutoReviewer(), sign_with="not-the-secret", expect_runs=0)

    assert run.responses[0][0] == 401
    assert model.calls == []


# ── risky fixes wait for a person ─────────────────────────────────────────


def test_rollback_runs_only_after_approval(world_dir):
    seen_before_decision = {}

    async def on_call(request):
        # At the moment the person is asked, production must be untouched.
        world = json.loads((world_dir / "world.json").read_text())
        seen_before_decision["actions"] = list(world["actions"])
        seen_before_decision["version"] = world["versions"]["checkout-service"]
        return ApprovalDecision(approved=True, reviewer_id="alice")

    reviewer = CallbackApprovalHandler(on_call)
    run = run_incident(world_dir, approved_run(anchor(world_dir)), reviewer)

    assert seen_before_decision == {"actions": [], "version": "v2.14.0"}
    world = run.world
    assert world["versions"]["checkout-service"] == "v2.13.4"
    assert [a["action"] for a in world["actions"]] == ["rollback"]
    assert json.loads(run.tool_messages("rollback_deploy")[0])["rolled_back"] is True


def test_denied_rollback_never_reaches_production(world_dir):
    reviewer = AutoReviewer(approve=False)
    run = run_incident(world_dir, denied_run(anchor(world_dir)), reviewer)

    assert [r.tool_name for r in reviewer.requests] == ["rollback_deploy"]
    assert run.world["actions"] == []
    assert run.world["versions"]["checkout-service"] == "v2.14.0"
    # The ops server was never asked to roll back.
    assert "rollback_deploy" not in [e["tool"] for e in run.audit]
    # The model was told, and handed over.
    assert run.tool_messages("rollback_deploy")[0].startswith("DENIED: Not during the payments freeze")
    assert run.world["incidents"]["INC-1042"]["status"] == "escalated"


def test_terminal_reviewer_approves_at_the_keyboard(world_dir, monkeypatch):
    answers = iter(["y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    reviewer = TerminalReviewer(reviewer_id="bob")
    run = run_incident(world_dir, approved_run(anchor(world_dir)), reviewer.handler, serve_reviewer=reviewer.serve)

    assert run.world["versions"]["checkout-service"] == "v2.13.4"
    [decision] = [e for e in run.trace.last_run.entries if e["event_type"] == "approval.decision"]
    assert decision["metadata"]["reviewer"] == "bob"


def test_unanswered_approval_times_out_as_a_denial(world_dir):
    async def nobody_answers(request):
        await asyncio.sleep(3600)

    run = run_incident(
        world_dir, denied_run(anchor(world_dir)), CallbackApprovalHandler(nobody_answers), approval_timeout=0.5
    )

    assert run.world["actions"] == []
    assert run.tool_messages("rollback_deploy")[0].startswith("DENIED")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("restart_service", {"service": "checkout-service", "reason": "Runbook says restart on pool exhaustion."}),
        ("rollback_deploy", {"service": "checkout-service", "to_version": "v2.13.4", "reason": "Deploy correlates."}),
        ("scale_service", {"service": "checkout-service", "replicas": 9, "reason": "Runbook says scale up by 50%."}),
    ],
)
@pytest.mark.parametrize("approve", [False, True])
def test_every_risky_fix_is_gated(world_dir, tool, args, approve):
    reviewer = AutoReviewer(approve=approve)
    run = run_incident(world_dir, ScriptedModel([ToolCall(tool, args), "done"]), reviewer)

    assert [r.tool_name for r in reviewer.requests] == [tool]
    assert reviewer.requests[0].arguments == args
    executed = [e["tool"] for e in run.audit]
    assert (tool in executed) is approve
    assert len(run.world["actions"]) == (1 if approve else 0)


def test_safe_steps_run_without_asking(world_dir):
    reviewer = AutoReviewer(approve=True)
    run = run_incident(world_dir, approved_run(anchor(world_dir)), reviewer)

    asked = {r.tool_name for r in reviewer.requests}
    assert asked == {"rollback_deploy"}
    assert asked <= set(RISKY_TOOLS)
    executed = {e["tool"] for e in run.audit}
    assert {"search_logs", "query_metric", "list_deploys", "find_runbook", "post_status_update"} <= executed


# ── the summary and the trace ─────────────────────────────────────────────


def test_incident_summary_is_written_with_its_timeline(world_dir):
    run = run_incident(world_dir, approved_run(anchor(world_dir)), AutoReviewer(approve=True))

    incident = run.world["incidents"]["INC-1042"]
    assert incident["status"] == "mitigated"
    text = (world_dir / incident["summary_path"]).read_text()
    assert text.startswith("# INC-1042: HighErrorRate on checkout-service")
    assert "PAYMENTS_TIMEOUT_MS" in text
    events = [row["event"] for row in incident["timeline"]]
    # The agent's findings and the incident tool's own record, in time order.
    assert events[0].startswith("Deploy v2.14.0 of checkout-service finished")
    assert "Rolled back checkout-service from v2.14.0 to v2.13.4" in events
    assert events.index("Rolled back checkout-service from v2.14.0 to v2.13.4") < len(events) - 1
    assert events[-1].startswith("Status update: Mitigated")
    assert [row["time"] for row in incident["timeline"]] == sorted(row["time"] for row in incident["timeline"])


def test_trace_contains_every_tool_call(world_dir):
    model = approved_run(anchor(world_dir))
    run = run_incident(world_dir, model, AutoReviewer(approve=True))
    traced = run.trace.last_run.tool_calls()

    # Every call the model made is in the trace, with its arguments ...
    assert canonical(traced) == canonical(scripted_tool_calls(model))
    # ... and every MCP call matches what the ops server says it executed (memory is a local tool).
    audited = [{"tool": e["tool"], "arguments": e["args"]} for e in run.audit]
    remote = [c for c in traced if c["tool"] != "recall_past_incidents"]
    assert sorted(c["tool"] for c in remote) == sorted(c["tool"] for c in audited)
    for call in remote:
        # The audit log writes nested models as text, so match on the scalar arguments.
        scalars = {k: v for k, v in call["arguments"].items() if isinstance(v, (str, int, float, bool))}
        assert any(a["tool"] == call["tool"] and scalars.items() <= a["arguments"].items() for a in audited)

    # The exported file holds the same calls plus the approval decision.
    [exported] = (world_dir / "traces").glob("*.ndjson")
    entries = [json.loads(line) for line in exported.read_text().splitlines()]
    assert sum(e["event_type"] == "tool.call" for e in entries) == len(traced)
    [decision] = [e for e in entries if e["event_type"] == "approval.decision"]
    assert decision["metadata"]["tool_name"] == "rollback_deploy"
    assert decision["metadata"]["approved"] is True
    assert entries[0]["event_type"] == "agent.input"
    assert entries[-1]["event_type"] == "agent.output"
    assert list((world_dir / "traces").glob("*.timeline.md"))


def test_a_repeated_alert_reuses_the_open_incident(world_dir):
    alert = sample_alert(anchor(world_dir))
    model = ScriptedModel(
        [
            ToolCall(
                "open_incident",
                {
                    "alert_name": "HighErrorRate",
                    "service": "checkout-service",
                    "severity": "page",
                    "fingerprint": alert["alerts"][0]["fingerprint"],
                },
            ),
            "first",
            ToolCall(
                "open_incident",
                {
                    "alert_name": "HighErrorRate",
                    "service": "checkout-service",
                    "severity": "page",
                    "fingerprint": alert["alerts"][0]["fingerprint"],
                },
            ),
            "second",
        ]
    )
    run = run_incident(world_dir, model, AutoReviewer(), payloads=[alert, alert], expect_runs=2)

    assert list(run.world["incidents"]) == ["INC-1042"]
    second = json.loads(run.tool_messages("open_incident")[-1])
    assert second["already_open"] is True


# ── the runtime's record, budget and memory ───────────────────────────────


def test_the_journal_shows_the_webhook_event_that_started_the_run(world_dir):
    run = run_incident(world_dir, approved_run(anchor(world_dir)), AutoReviewer(approve=True))
    event_id = run.responses[0][1]["event_id"]

    kinds = [(e["entry_type"], e["data"].get("event_id")) for e in run.journal]
    assert ("trigger_event", event_id) in kinds
    assert ("invocation_start", event_id) in kinds
    assert ("invocation_result", event_id) in kinds
    [trigger] = [e for e in run.journal if e["entry_type"] == "trigger_event"]
    assert trigger["data"]["trigger_type"] == "webhook"
    assert trigger["data"]["payload"]["commonLabels"]["service"] == "checkout-service"
    journaled_tools = [e["data"]["tool"] for e in run.journal if e["entry_type"] == "tool_call"]
    assert "rollback_deploy" in journaled_tools


def test_budget_allows_one_production_change_per_alert(world_dir):
    reviewer = AutoReviewer(approve=True)
    model = ScriptedModel(
        [
            ToolCall(
                "rollback_deploy",
                {"service": "checkout-service", "to_version": "v2.13.4", "reason": "Deploy correlates."},
            ),
            ToolCall("restart_service", {"service": "checkout-service", "reason": "And a restart, to be sure."}),
            "done",
        ]
    )
    run = run_incident(world_dir, model, reviewer)

    # The second change never reached the reviewer or production.
    assert [r.tool_name for r in reviewer.requests] == ["rollback_deploy"]
    assert [a["action"] for a in run.world["actions"]] == ["rollback"]
    assert "max_irreversible_per_run" in run.tool_messages("restart_service")[0]
    # The process paused itself until a person looks.
    assert run.status["state"] == "suspended"
    assert run.status["suspend_reason"] == "budget"


def test_past_incidents_are_recalled_per_service(world_dir):
    model = ScriptedModel(
        [
            [
                ToolCall("recall_past_incidents", {"service": "checkout-service"}),
                ToolCall("recall_past_incidents", {"service": "cart-service"}),
            ],
            "done",
        ]
    )
    run = run_incident(world_dir, model, AutoReviewer())
    checkout, cart = (json.loads(m) for m in run.tool_messages("recall_past_incidents"))

    assert {e["incident"].split(" ")[0] for e in checkout} == {"INC-0987", "INC-0912"}
    assert [e["incident"].split(" ")[0] for e in cart] == ["INC-1001"]


def test_a_finished_incident_is_remembered_for_the_next_alert(world_dir):
    run_incident(world_dir, approved_run(anchor(world_dir)), AutoReviewer(approve=True))
    lessons = [json.loads(line) for line in (world_dir / "memory.jsonl").read_text().splitlines()]
    assert [(r["service"], r["incident_id"], r["status"]) for r in lessons] == [
        ("checkout-service", "INC-1042", "mitigated")
    ]

    # A new process on the same state directory recalls it first.
    later = ScriptedModel([ToolCall("recall_past_incidents", {"service": "checkout-service"}), "done"])
    run = run_incident(world_dir, later, AutoReviewer(), payloads=[sample_alert(anchor(world_dir))])
    newest = json.loads(run.tool_messages("recall_past_incidents")[0])[0]["incident"]
    assert newest.startswith("INC-1042")
    assert "PAYMENTS_TIMEOUT_MS" in newest
