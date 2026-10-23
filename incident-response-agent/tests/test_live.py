"""One real end-to-end incident with a real model. Costs a few cents.

    pytest -m live            # or: promptise run test-live

Skipped when the provider key for INCIDENT_AGENT_MODEL (default openai:gpt-5-mini) is not set.
"""

from __future__ import annotations

import json
import os

import pytest
from conftest import run_incident

from incident_agent.reviewers import AutoReviewer

MODEL = os.environ.get("INCIDENT_AGENT_MODEL", "openai:gpt-5-mini")
KEY = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(MODEL.split(":")[0], "OPENAI_API_KEY")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get(KEY), reason=f"{KEY} is not set"),
]


def test_a_real_model_works_the_incident_end_to_end(world_dir):
    reviewer = AutoReviewer(approve=True, reviewer_id="live-test")
    run = run_incident(world_dir, MODEL, reviewer, run_timeout=600)
    status, answer = run.responses[0]
    assert (status, answer["status"]) == (202, "accepted")

    # It investigated the right service before it asked for a change.
    calls = run.trace.last_run.tool_calls()
    names = [c["tool"] for c in calls]
    assert {"open_incident", "find_runbook", "search_logs", "query_metric", "list_deploys"} <= set(names)
    assert all(c["arguments"]["service"] == "checkout-service" for c in calls if c["tool"] == "search_logs")
    assert names.index("post_status_update") < names.index("rollback_deploy")

    # The only production change went through the approval gate.
    assert [r.tool_name for r in reviewer.requests] == ["rollback_deploy"]
    assert reviewer.requests[0].arguments["to_version"] == "v2.13.4"
    assert [a["action"] for a in run.world["actions"]] == ["rollback"]

    # It verified the fix and wrote the summary.
    rollback_at = names.index("rollback_deploy")
    assert "query_metric" in names[rollback_at:]
    incident = run.world["incidents"]["INC-1042"]
    assert incident["status"] == "mitigated"
    assert (world_dir / incident["summary_path"]).exists()
    lessons = [json.loads(line) for line in (world_dir / "memory.jsonl").read_text().splitlines()]
    assert lessons[-1]["incident_id"] == "INC-1042"
