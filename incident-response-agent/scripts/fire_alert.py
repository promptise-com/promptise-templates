"""Send the sample HighErrorRate alert to the incident agent's webhook, signed.

    promptise run alert                     # a firing alert
    promptise run alert -- --resolved       # the "resolved" notification (ignored by the filter)
    python -m scripts.fire_alert --url http://127.0.0.1:9095/alerts

The signature uses ALERT_WEBHOOK_SECRET, the same secret the agent checks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from incident_agent.alerts import post_alert, sample_alert
from incident_agent.config import DEFAULT_WEBHOOK_PORT, WEBHOOK_PATH


def main() -> None:
    port = int(os.environ.get("ALERT_WEBHOOK_PORT", DEFAULT_WEBHOOK_PORT))
    parser = argparse.ArgumentParser(description="Send the sample alert to the incident agent.")
    parser.add_argument("--url", default=f"http://127.0.0.1:{port}{WEBHOOK_PATH}")
    parser.add_argument("--resolved", action="store_true", help="send the resolved notification instead")
    args = parser.parse_args()

    secret = os.environ.get("ALERT_WEBHOOK_SECRET")
    if not secret:
        sys.exit("Set ALERT_WEBHOOK_SECRET to the agent's secret.")
    payload = sample_alert(status="resolved" if args.resolved else "firing")
    status, answer = post_alert(args.url, payload, secret)
    print(f"POST {args.url} -> {status} {json.dumps(answer)}")


if __name__ == "__main__":
    main()
