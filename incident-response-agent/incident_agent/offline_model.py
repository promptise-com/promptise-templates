"""A scripted model for running the incident agent with no API key.

``ScriptedModel`` plays back the turns a capable model takes on the sample
HighErrorRate alert. Everything around it is real: the signed webhook, the
runtime process, the MCP server and its data, the approval gate and the
trace. A script cannot read tool results, so there is one script for an
approved rollback and one for a denied one.

``anchor`` is the ops world's clock (``OpsWorld.anchor``). The script reads
the deploy and first-error times from it, as a model would read them from
the tool results.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from promptise.testing import ScriptedModel, ToolCall

from incident_agent.alerts import SAMPLE_FINGERPRINT

INCIDENT = "INC-1042"
SERVICE = "checkout-service"


def _at(anchor: datetime, minutes_ago: int) -> str:
    return (anchor - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _investigation() -> list:
    return [
        ToolCall(
            "open_incident",
            {
                "alert_name": "HighErrorRate",
                "service": SERVICE,
                "severity": "page",
                "fingerprint": SAMPLE_FINGERPRINT,
            },
        ),
        ToolCall("find_runbook", {"alert_name": "HighErrorRate"}),
        # Independent reads go out in one turn; the agent runs them in parallel.
        [
            ToolCall("search_logs", {"service": SERVICE, "minutes": 30, "level": "ERROR"}),
            ToolCall("query_metric", {"service": SERVICE, "metric": "error_rate", "minutes": 30}),
            ToolCall("query_metric", {"service": SERVICE, "metric": "latency_p95_ms", "minutes": 30}),
            ToolCall("list_deploys", {"service": SERVICE, "hours": 24}),
        ],
        # Runbook step 5: is the dependency itself unhealthy?
        [
            ToolCall("query_metric", {"service": "payments-api", "metric": "error_rate", "minutes": 30}),
            ToolCall("query_metric", {"service": "payments-api", "metric": "latency_p95_ms", "minutes": 30}),
        ],
        ToolCall(
            "post_status_update",
            {
                "incident_id": INCIDENT,
                "message": (
                    "Investigating. checkout-service 5xx rate is 11.8% (baseline 0.4%) and p95 is 930 ms. "
                    "Errors are timeouts to payments-api /v2/authorize after 800 ms. They began 1 minute after "
                    "deploy v2.14.0, which lowered PAYMENTS_TIMEOUT_MS from 3000 to 800. payments-api is healthy "
                    "(0.2% errors, p95 1450 ms). Runbook says: roll back. Asking on-call for approval."
                ),
            },
        ),
        ToolCall(
            "rollback_deploy",
            {
                "service": SERVICE,
                "to_version": "v2.13.4",
                "reason": "HighErrorRate runbook, Mitigate: errors began 1 minute after deploy v2.14.0.",
            },
        ),
    ]


def _findings(anchor: datetime) -> list[dict[str, str]]:
    return [
        {"time": _at(anchor, 15), "event": "Deploy v2.14.0 of checkout-service finished (commit 9f3c2ab)"},
        {"time": _at(anchor, 14), "event": "First payments-api timeout after 800 ms in checkout-service logs"},
    ]


def approved_run(anchor: datetime) -> ScriptedModel:
    """The rollback is approved, verified and written up."""
    return ScriptedModel(
        [
            *_investigation(),
            ToolCall("query_metric", {"service": SERVICE, "metric": "error_rate", "minutes": 5}),
            ToolCall(
                "post_status_update",
                {
                    "incident_id": INCIDENT,
                    "message": (
                        "Mitigated. Rolled back checkout-service to v2.13.4. Error rate is now 0.4%, below the 1% "
                        "threshold in the runbook. Watching for 15 minutes before resolving."
                    ),
                },
            ),
            ToolCall(
                "write_incident_summary",
                {
                    "incident_id": INCIDENT,
                    "status": "mitigated",
                    "summary": (
                        "checkout-service failed 11.8% of checkouts for about 15 minutes after deploy v2.14.0. "
                        "A rollback to v2.13.4 brought the error rate back to 0.4%."
                    ),
                    "root_cause": (
                        "v2.14.0 moved the payments client to a new config loader whose default "
                        "PAYMENTS_TIMEOUT_MS is 800. payments-api answers in about 1.3 s at p95, so most "
                        "authorizations timed out."
                    ),
                    "findings": _findings(anchor),
                    "follow_ups": [
                        "payments team: set PAYMENTS_TIMEOUT_MS explicitly to 3000 before redeploying v2.14.x",
                        "Add a canary step that watches checkout error rate for 10 minutes",
                    ],
                },
            ),
            "INC-1042 mitigated: deploy v2.14.0 cut the payments timeout to 800 ms and checkouts failed. "
            "Rolled back to v2.13.4 with on-call approval; the error rate is back to 0.4%. "
            "Follow-ups are in the incident summary.",
        ]
    )


def denied_run(anchor: datetime) -> ScriptedModel:
    """The rollback is denied: the agent stops changing things and hands over."""
    return ScriptedModel(
        [
            *_investigation(),
            ToolCall(
                "post_status_update",
                {
                    "incident_id": INCIDENT,
                    "message": (
                        "Rollback of checkout-service was denied by on-call. I am not changing anything else. "
                        "A person needs to take over; findings so far are above."
                    ),
                },
            ),
            ToolCall(
                "write_incident_summary",
                {
                    "incident_id": INCIDENT,
                    "status": "escalated",
                    "summary": (
                        "checkout-service is failing 11.8% of checkouts since deploy v2.14.0. "
                        "The runbook rollback was denied, so the incident is handed to on-call."
                    ),
                    "root_cause": "Likely the PAYMENTS_TIMEOUT_MS=800 default from v2.14.0 (not yet confirmed).",
                    "findings": _findings(anchor),
                    "follow_ups": ["On-call: decide the mitigation; the rollback to v2.13.4 is ready"],
                },
            ),
            "INC-1042 escalated: checkout errors began right after deploy v2.14.0. The rollback was denied, "
            "so nothing was changed. On-call has the findings in the incident channel.",
        ]
    )
