"""The ops MCP server through its full pipeline: API keys, role guards, rate limits, audit, tools."""

from __future__ import annotations

import asyncio
import json

import pytest
from promptise.mcp.server import TestClient

from ops_systems.server import build_server

AGENT = {"x-api-key": "agent-key"}
VIEWER = {"x-api-key": "viewer-key"}
KEYS = {
    "agent-key": {"client_id": "incident-agent", "roles": ["viewer", "responder", "remediator"]},
    "viewer-key": {"client_id": "status-page", "roles": ["viewer"]},
}
ROLLBACK = {"service": "checkout-service", "to_version": "v2.13.4", "reason": "Deploy v2.14.0 correlates."}


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(build_server(tmp_path, KEYS))


def call(client: TestClient, tool: str, args: dict, headers: dict | None = AGENT) -> dict:
    result = asyncio.run(client.call_tool(tool, args, headers=headers))
    return json.loads(result[0].text)


def test_a_caller_without_a_key_gets_nothing(client):
    error = call(client, "search_logs", {"service": "checkout-service"}, headers={})["error"]
    assert error["code"] == "AUTHENTICATION_ERROR"


def test_a_viewer_can_read_but_not_change_production(client, tmp_path):
    assert call(client, "search_logs", {"service": "checkout-service"}, headers=VIEWER)["matches"] > 0
    error = call(client, "rollback_deploy", ROLLBACK, headers=VIEWER)["error"]

    assert error["code"] == "ACCESS_DENIED"
    assert json.loads((tmp_path / "world.json").read_text())["actions"] == []


def test_a_viewer_cannot_post_in_the_incident_channel(client):
    args = {"alert_name": "HighErrorRate", "service": "checkout-service", "severity": "page", "fingerprint": "f"}
    assert call(client, "open_incident", args, headers=VIEWER)["error"]["code"] == "ACCESS_DENIED"


def test_changes_are_rate_limited_per_client(client):
    for _ in range(3):
        call(client, "restart_service", {"service": "cart-service", "reason": "Runbook: restart on memory leak."})
    error = call(client, "restart_service", {"service": "cart-service", "reason": "A fourth restart this hour."})
    assert error["error"]["code"] == "RATE_LIMIT_EXCEEDED"


def test_logs_are_scoped_to_the_service(client):
    logs = call(client, "search_logs", {"service": "cart-service", "level": "INFO"})
    assert logs["matches"] > 0
    assert {line["service"] for line in logs["lines"]} == {"cart-service"}


def test_unknown_service_is_a_tool_error_the_model_can_read(client):
    error = call(client, "search_logs", {"service": "checkout"})["error"]
    assert error["code"] == "TOOL_ERROR"
    assert "checkout-service" in error["message"]


def test_rollback_fixes_the_error_rate_and_a_restart_does_not(client):
    metric = {"service": "checkout-service", "metric": "error_rate", "minutes": 5}
    assert call(client, "query_metric", metric)["current"] == 11.8

    call(client, "restart_service", {"service": "checkout-service", "reason": "Trying a restart first."})
    assert call(client, "query_metric", metric)["current"] == 11.8

    call(client, "rollback_deploy", ROLLBACK)
    assert call(client, "query_metric", metric)["current"] == 0.4


def test_rollback_to_a_version_that_never_ran_is_refused(client, tmp_path):
    error = call(client, "rollback_deploy", {**ROLLBACK, "to_version": "v1.0.0"})["error"]
    assert "never deployed" in error["message"]
    assert json.loads((tmp_path / "world.json").read_text())["actions"] == []


def test_open_incident_is_idempotent_per_fingerprint(client):
    args = {"alert_name": "HighErrorRate", "service": "checkout-service", "severity": "page", "fingerprint": "abc"}
    first = call(client, "open_incident", args)
    again = call(client, "open_incident", args)
    other = call(client, "open_incident", {**args, "fingerprint": "def"})

    assert (first["incident_id"], first["already_open"]) == ("INC-1042", False)
    assert (again["incident_id"], again["already_open"]) == ("INC-1042", True)
    assert other["incident_id"] == "INC-1043"


def test_every_call_lands_in_the_audit_log_with_the_caller(client, tmp_path):
    call(client, "find_runbook", {"alert_name": "HighErrorRate"})
    call(client, "rollback_deploy", ROLLBACK, headers=VIEWER)

    entries = [json.loads(line) for line in (tmp_path / "ops-audit.jsonl").read_text().splitlines()]
    assert [(e["tool"], e["client_id"], e["status"]) for e in entries] == [
        ("find_runbook", "incident-agent", "ok"),
        ("rollback_deploy", "status-page", "error"),
    ]
    assert entries[1]["error_code"] == "ACCESS_DENIED"
