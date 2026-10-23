"""Send Stripe-style webhook events: the signature scheme and a tiny HTTP client.

Stripe signs each delivery with ``Stripe-Signature: t=<unix ts>,v1=<hex>``,
where the hex is HMAC-SHA256 of ``"<ts>.<raw body>"`` with the endpoint's
signing secret.  The agent's webhook trigger checks exactly this.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

SAMPLE_EVENT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "sample_events"
    / ("invoice.payment_failed.json")
)


def stripe_signature(body: bytes, secret: str, timestamp: int | None = None) -> str:
    """The Stripe-Signature header value for *body*."""
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def post_event(url: str, event: dict, secret: str) -> tuple[int, dict]:
    """Sign and POST *event*; return the status code and the JSON answer."""
    body = json.dumps(event).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": stripe_signature(body, secret),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def sample_event() -> dict:
    """The sample ``invoice.payment_failed`` event for invoice in_2049."""
    return json.loads(SAMPLE_EVENT.read_text())
