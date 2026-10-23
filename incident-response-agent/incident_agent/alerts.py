"""Alertmanager-style webhook payloads, and how to send one signed."""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

SAMPLE_FINGERPRINT = "c4a7e1f09b2d3e55"


def _iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sample_alert(now: datetime | None = None, *, status: str = "firing") -> dict[str, Any]:
    """The payload Alertmanager (webhook_configs, version 4) sends for HighErrorRate on checkout-service."""
    now = now or datetime.now(timezone.utc)
    labels = {
        "alertname": "HighErrorRate",
        "service": "checkout-service",
        "severity": "page",
        "team": "payments",
        "env": "prod",
    }
    annotations = {
        "summary": "checkout-service 5xx rate above 5% for 5 minutes",
        "description": "Error rate is 11.8% (threshold 5%).",
        "runbook_url": "https://runbooks.example.com/HighErrorRate",
    }
    return {
        "version": "4",
        "groupKey": '{}/{severity="page"}:{alertname="HighErrorRate", service="checkout-service"}',
        "truncatedAlerts": 0,
        "status": status,
        "receiver": "incident-agent",
        "groupLabels": {"alertname": "HighErrorRate", "service": "checkout-service"},
        "commonLabels": labels,
        "commonAnnotations": annotations,
        "externalURL": "https://alertmanager.example.com",
        "alerts": [
            {
                "status": status,
                "labels": labels,
                "annotations": annotations,
                "startsAt": _iso(now - timedelta(minutes=8)),
                "endsAt": "0001-01-01T00:00:00Z" if status == "firing" else _iso(now),
                "generatorURL": "https://prometheus.example.com/graph?g0.expr=job%3Ahttp_5xx_ratio%3Arate5m",
                "fingerprint": SAMPLE_FINGERPRINT,
            }
        ],
    }


def sign(body: bytes, secret: str) -> str:
    """The ``X-Webhook-Signature`` value the webhook trigger checks (generic scheme)."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_alert(url: str, payload: dict[str, Any], secret: str, timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
    """POST a signed alert. Returns the HTTP status and the JSON answer (202 accepted, 200 ignored)."""
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": sign(body, secret)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
