"""Post a signed Stripe event to the running agent, the way Stripe does.

    python scripts/send_test_webhook.py                  # the sample event
    python scripts/send_test_webhook.py my_event.json --port 8787

The secret comes from STRIPE_WEBHOOK_SECRET, the same variable the agent
reads.  With the Stripe CLI you can forward real test events instead:
``stripe listen --forward-to localhost:8787/stripe``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fake_systems.stripe_sender import SAMPLE_EVENT, post_event  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Post a signed Stripe event to the agent.")
    parser.add_argument("event", nargs="?", default=str(SAMPLE_EVENT), help="JSON event file")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEBHOOK_PORT", "8787")))
    args = parser.parse_args()

    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        print("Set STRIPE_WEBHOOK_SECRET to the secret the agent uses.", file=sys.stderr)
        return 2
    event = json.loads(Path(args.event).read_text())
    status, answer = post_event(f"http://127.0.0.1:{args.port}/stripe", event, secret)
    print(status, answer)
    return 0 if status in (200, 202) else 1


if __name__ == "__main__":
    sys.exit(main())
