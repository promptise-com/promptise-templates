# Incident response agent

An on-call agent that works an alert the way your first responder would. Alertmanager fires
`HighErrorRate` on `checkout-service`. The agent opens an incident, reads the runbook, the logs, the
metrics and the deploy history, and posts what it found. It asks the on-call engineer before it rolls
back the deploy. Then it checks the error rate again and writes the incident summary with its timeline.
Every model turn, tool call and approval is in a trace you can replay.

Built with [Promptise](https://www.promptise.com) 1.7.1: an Agent Runtime process with a signed webhook
trigger, an MCP server built with the Promptise MCP server SDK, an approval policy and observability.

## The business problem

Pages arrive at night. The first 15 minutes of most incidents are the same steps: find the runbook,
pull logs and graphs, check what was deployed, post in the channel. Engineers lose that time while
customers see errors. Handing the whole incident to an agent is not acceptable either, because a
rollback or a restart changes production.

This project splits the work along that line. The agent does the reading and the writing on its own.
Anything that changes production waits for a person, who sees the exact call and the reason. The
person can say no, and the agent then stops and hands over.

## What it does

1. Alertmanager POSTs its webhook payload to `http://127.0.0.1:9095/alerts`, signed with HMAC-SHA256.
   Unsigned or wrongly signed requests get `401`. `resolved` notifications are ignored before any
   model call.
2. The Agent Runtime turns the request into a run of the agent. The payload reaches the model as
   untrusted data, so text inside an alert cannot give it orders.
3. The agent investigates with read-only tools: `find_runbook`, `search_logs`, `query_metric`,
   `list_deploys`. It opens the incident and posts status updates in the incident channel.
4. It calls the fix the runbook prescribes, here `rollback_deploy`. The approval policy holds the call
   and asks the on-call engineer. `restart_service` and `scale_service` are held the same way.
5. Approved: the rollback runs, the agent verifies the error rate and posts the result. Denied or no
   answer within 15 minutes: nothing changes, and the agent escalates.
6. The agent writes the incident summary. The incident tool merges the agent's findings with its own
   record of status posts and changes, so the timeline does not depend on the model's memory.
7. The trace of the run is written to `var/<state>/traces/`: one NDJSON file, a readable timeline and
   an HTML report.

## Architecture

```
Alertmanager ──signed POST /alerts──▶ WebhookTrigger ─┐
                                                      │  AgentProcess "incident-agent"
                                                      ▼  (promptise.runtime)
                                          ┌──────────────────────────┐
                 instructions.md ────────▶│  agent (model + tools)   │──▶ observability ─▶ traces/*.ndjson
                                          │                          │                    traces/*.timeline.md
     on-call engineer ◀── ApprovalPolicy ─┤ restart / rollback /     │                    HTML report
     (terminal, Slack, webhook)           │ scale wait for a person  │
                                          └────────────┬─────────────┘
                                                       │ MCP (stdio)
                                          ┌────────────▼─────────────┐
                                          │ ops MCP server           │──▶ ops-audit.jsonl
                                          │ logs · metrics · deploys │
                                          │ runbooks · incidents ·   │
                                          │ chat · remediation       │
                                          └──────────────────────────┘
```

| Piece | File | Promptise feature |
|---|---|---|
| Process, trigger, approval, trace config | `incident_agent/config.py` | `AgentProcess`, `TriggerConfig(type="webhook")`, `ApprovalPolicy`, `ObservabilityConfig` |
| How the agent works an incident | `incident_agent/instructions.md` | Process instructions |
| Fake ops systems as tools | `ops_systems/server.py`, `ops_systems/store.py`, `ops_systems/seed/` | `MCPServer`, `AuditMiddleware`, `ToolError` |
| Who answers approvals | `incident_agent/reviewers.py` | `QueueApprovalHandler`, `CallbackApprovalHandler` |
| Per-run trace export | `incident_agent/trace.py` | `TransporterType.CALLBACK`, `on_decision` |
| Offline model | `incident_agent/offline_model.py` | `promptise.testing.ScriptedModel` |

## Quickstart

```bash
cd incident-response-agent
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Offline demo (no API key)

```bash
promptise run demo          # the on-call engineer approves the rollback
promptise run demo-deny     # the on-call engineer denies it
promptise run demo-trace    # the same run with the live trace on stderr
```

The demo starts the real process, sends the sample alert to the webhook over HTTP, answers the
approval request, and prints what changed in production, the incident channel, the incident summary
and the trace. The model is a `ScriptedModel` that plays back the turns a capable model takes on this
alert. The webhook, the runtime, the MCP server, the approval gate and the trace are the real thing.

Excerpt of `promptise run demo`:

```
== Alertmanager -> webhook ===========================================
POST http://127.0.0.1:53447/alerts -> 202 accepted

[approval] The incident agent wants to run rollback_deploy:
{
  "service": "checkout-service",
  "to_version": "v2.13.4",
  "reason": "HighErrorRate runbook, Mitigate: errors began 1 minute after deploy v2.14.0."
}
[approval] APPROVED by demo-oncall

== Trace timeline ====================================================
14:17:12  alert received, run started
14:17:12  call open_incident(alert_name='HighErrorRate', service='checkout-service', ...)
14:17:12  call find_runbook(alert_name='HighErrorRate')
14:17:12  call search_logs(service='checkout-service', minutes=30, level='ERROR')
...
14:17:12  call rollback_deploy(service='checkout-service', to_version='v2.13.4', reason=...)
14:17:12  approval approved for rollback_deploy by demo-oncall
14:17:12    -> rollback_deploy returned in 3 ms
14:17:12  call query_metric(service='checkout-service', metric='error_rate', minutes=5)
...
14:17:12  run finished: 13 tool calls, 10 model calls
```

The files of the run are in `var/demo/`: `world.json` (the fake production state),
`incidents/INC-1042.md`, `ops-audit.jsonl` and `traces/`.

### With a real model

```bash
cp .env.example .env        # set OPENAI_API_KEY and a long random ALERT_WEBHOOK_SECRET
promptise run               # terminal 1: the agent waits for alerts
promptise run alert         # terminal 2: send the sample alert
```

Terminal 1 asks you to approve the rollback. Answer `y`, or `n` with a reason, and watch the agent
react. Any provider string works: set `INCIDENT_AGENT_MODEL=anthropic:claude-sonnet-4-5` and
`ANTHROPIC_API_KEY`, for example. `promptise run dev` adds the live trace. State is kept in `var/state/`;
delete that folder to reset the fake production world.

## Adapt it to your systems

| To connect | Change | How |
|---|---|---|
| Your alerting | `incident_agent/config.py` (`TriggerConfig`) | Alertmanager can't sign its webhook. Put a small relay or your reverse proxy in front: it checks Alertmanager's `http_config.authorization` bearer token, then signs the body with `ALERT_WEBHOOK_SECRET` (see `incident_agent/alerts.py: sign`). Grafana, PagerDuty and Opsgenie payloads work too; change `filter_expression` and the field names in `instructions.md`. |
| Logs, metrics, deploys | `incident_agent/config.py` (`ops_server`) | Replace the `ops` entry with your own MCP servers (`{"url": "https://loki-mcp.internal/mcp"}`), or keep `ops_systems/server.py` and replace the methods of `OpsWorld` in `ops_systems/store.py` with calls to Loki, Prometheus and Argo CD. Keep the tool names, or update `RISKY_TOOLS`. |
| Runbooks | `ops_systems/seed/runbooks/` | Point `find_runbook` at your wiki. The agent follows what the runbook says, so write the Mitigate section as explicit conditions. |
| Incident tool and chat | `ops_systems/store.py` (`open_incident`, `post_status_update`, `write_incident_summary`) | Call incident.io, PagerDuty or Slack. Keep `open_incident` idempotent on the alert fingerprint. |
| Approvals | `main.py` and `incident_agent/reviewers.py` | Use `WebhookApprovalHandler(url=..., secret=...)` for your approval service, or a `CallbackApprovalHandler` that posts Slack buttons and waits for the click. |
| Traces | `incident_agent/config.py` (`ObservabilityConfig`) | Add `TransporterType.OTLP` to send each run as one OpenTelemetry trace, or `STRUCTURED_LOG` for your log pipeline. |
| The model | `.env` | `INCIDENT_AGENT_MODEL` takes any Promptise model string. |

Before you run it against production, give the process its own identity on each MCP server, and let
the servers enforce approval too (`@server.tool(requires_approval=True)` with `ApprovalGateMiddleware`).
Then a different client can't skip the gate.

## Project layout

```
incident-response-agent/
├── promptise.toml              # scripts: demo, demo-deny, demo-trace, alert, dev, test
├── main.py                     # the service: waits for alerts, asks approvals at the terminal
├── demo.py                     # offline end-to-end run with a scripted model
├── incident_agent/
│   ├── config.py               # AgentProcess: webhook trigger, MCP server, approval, observability
│   ├── instructions.md         # investigate → act → verify → report
│   ├── reviewers.py            # terminal reviewer, demo reviewer
│   ├── trace.py                # per-run trace export and timeline
│   ├── alerts.py               # Alertmanager payload, HMAC signing
│   ├── offline_model.py        # ScriptedModel scripts (approved, denied)
│   └── runner.py               # wait for runs, free port
├── ops_systems/
│   ├── server.py               # ops MCP server (Promptise MCP server SDK)
│   ├── store.py                # fake world: state, metrics, incidents, remediation
│   └── seed/                   # services, deploys, logs, runbooks
├── scripts/fire_alert.py       # send the sample alert, signed
└── tests/                      # pytest, offline
```

## Tests

```bash
promptise run test          # or: pytest -q
```

23 tests, offline, about 20 seconds. They drive the real process, webhook and MCP server with a
scripted model, and check:

- a signed alert wakes the agent, and it reads the logs of the service in the alert;
- a resolved notification and a wrongly signed request never reach the model;
- `rollback_deploy`, `restart_service` and `scale_service` never run without approval, and do run
  after approval; production is untouched while the person decides;
- an unanswered approval times out as a denial;
- read-only steps run without asking;
- the incident summary is written with a timeline in time order;
- the trace holds every tool call the model made, matching the ops server's audit log, plus the
  approval decision;
- a repeated alert reuses the open incident.

## Notes

- The process runs one incident at a time (`concurrency=1`). Raise it only with one reviewer queue
  per incident, because `IncidentTrace` attaches approvals to the latest run.
- Approval denials are counted per tool for the whole process (triggered runs have no user). After
  three denials of a tool within 10 minutes, the gate denies it without asking.
