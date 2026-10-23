"""Relay between Alertmanager and the incident agent's signed webhook.

Alertmanager can send a bearer token with its webhooks, but it can't sign the
body. The agent's webhook wants an HMAC signature. This relay checks the
token, signs the body with ALERT_WEBHOOK_SECRET and forwards it unchanged.

    ALERTMANAGER_TOKEN=... ALERT_WEBHOOK_SECRET=... python -m scripts.alertmanager_relay --port 9094

Alertmanager side (alertmanager.yml):

    receivers:
      - name: incident-agent
        webhook_configs:
          - url: http://<relay host>:9094/alertmanager
            send_resolved: true
            http_config:
              authorization:
                type: Bearer
                credentials_file: /etc/alertmanager/incident-agent-token
"""

from __future__ import annotations

import argparse
import hmac
import os

from aiohttp import ClientSession, web

from incident_agent.alerts import sign
from incident_agent.config import DEFAULT_WEBHOOK_PORT, WEBHOOK_PATH


def build_app(token: str, secret: str, agent_url: str) -> web.Application:
    expected = f"Bearer {token}"

    async def relay(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            return web.json_response({"status": "error", "message": "bad token"}, status=401)
        body = await request.read()
        headers = {"Content-Type": "application/json", "X-Webhook-Signature": sign(body, secret)}
        async with ClientSession() as session, session.post(agent_url, data=body, headers=headers) as upstream:
            # Pass the agent's answer back, so Alertmanager retries on a 503.
            return web.Response(status=upstream.status, body=await upstream.read(), content_type="application/json")

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_post("/alertmanager", relay)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Relay Alertmanager webhooks to the incident agent, signed.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9094)
    port = int(os.environ.get("ALERT_WEBHOOK_PORT", DEFAULT_WEBHOOK_PORT))
    parser.add_argument("--agent-url", default=f"http://127.0.0.1:{port}{WEBHOOK_PATH}")
    args = parser.parse_args()
    token, secret = os.environ.get("ALERTMANAGER_TOKEN"), os.environ.get("ALERT_WEBHOOK_SECRET")
    if not token or not secret:
        raise SystemExit("Set ALERTMANAGER_TOKEN and ALERT_WEBHOOK_SECRET.")
    web.run_app(build_app(token, secret, args.agent_url), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
