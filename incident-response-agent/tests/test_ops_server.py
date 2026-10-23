"""The fake ops systems, through the MCP server pipeline (no agent, no subprocess)."""

from __future__ import annotations

import asyncio
import json

from promptise.mcp.server import TestClient

from ops_systems.server import build_server


def call(client: TestClient, tool: str, args: dict) -> dict:
    result = asyncio.run(client.call_tool(tool, args))
    return json.loads(result[0].text)


def test_logs_are_scoped_to_the_service(tmp_path):
    client = TestClient(build_server(tmp_path))
    logs = call(client, "search_logs", {"service": "cart-service", "level": "INFO"})

    assert logs["matches"] > 0
    assert {line["service"] for line in logs["lines"]} == {"cart-service"}


def test_unknown_service_is_a_tool_error_the_model_can_read(tmp_path):
    client = TestClient(build_server(tmp_path))
    error = call(client, "search_logs", {"service": "checkout"})["error"]

    assert error["code"] == "TOOL_ERROR"
    assert "checkout-service" in error["message"]


def test_rollback_fixes_the_error_rate_and_a_restart_does_not(tmp_path):
    client = TestClient(build_server(tmp_path))
    metric = {"service": "checkout-service", "metric": "error_rate", "minutes": 5}
    assert call(client, "query_metric", metric)["current"] == 11.8

    call(client, "restart_service", {"service": "checkout-service", "reason": "Trying a restart first."})
    assert call(client, "query_metric", metric)["current"] == 11.8

    call(
        client,
        "rollback_deploy",
        {
            "service": "checkout-service",
            "to_version": "v2.13.4",
            "reason": "Deploy v2.14.0 correlates with the errors.",
        },
    )
    assert call(client, "query_metric", metric)["current"] == 0.4


def test_rollback_to_a_version_that_never_ran_is_refused(tmp_path):
    client = TestClient(build_server(tmp_path))
    error = call(
        client,
        "rollback_deploy",
        {
            "service": "checkout-service",
            "to_version": "v1.0.0",
            "reason": "Going back to a version that never existed.",
        },
    )["error"]

    assert "never deployed" in error["message"]
    assert json.loads((tmp_path / "world.json").read_text())["actions"] == []


def test_open_incident_is_idempotent_per_fingerprint(tmp_path):
    client = TestClient(build_server(tmp_path))
    args = {"alert_name": "HighErrorRate", "service": "checkout-service", "severity": "page", "fingerprint": "abc"}
    first = call(client, "open_incident", args)
    again = call(client, "open_incident", args)
    other = call(client, "open_incident", {**args, "fingerprint": "def"})

    assert (first["incident_id"], first["already_open"]) == ("INC-1042", False)
    assert (again["incident_id"], again["already_open"]) == ("INC-1042", True)
    assert other["incident_id"] == "INC-1043"


def test_every_call_lands_in_the_audit_log(tmp_path):
    client = TestClient(build_server(tmp_path))
    call(client, "find_runbook", {"alert_name": "HighErrorRate"})
    call(client, "list_deploys", {"service": "checkout-service"})

    entries = [json.loads(line) for line in (tmp_path / "ops-audit.jsonl").read_text().splitlines()]
    assert [(e["tool"], e["status"]) for e in entries] == [("find_runbook", "ok"), ("list_deploys", "ok")]
    assert entries[1]["args"] == {"service": "checkout-service", "hours": 24}
