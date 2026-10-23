# Incident response agent

An on-call agent that works an alert the way your first responder would. Alertmanager fires
`HighErrorRate` on `checkout-service`. The agent opens an incident and reads the runbook, the logs, the
metrics, the deploy history and its memory of past incidents on that service. It posts what it found in
the incident channel. It asks the on-call engineer before it rolls back the deploy. Then it checks the
error rate again and writes the incident summary with a timeline.

> **This is a reference application.** It shows how this use case can look with Promptise. It is not a
> finished product. The logs, metrics, deploys and chat channel are local stand-ins with sample data,
> not connectors to Prometheus, Loki, your CI/CD or Slack. Replace the adapter files before you point it
> at production.

Built with [Promptise](https://www.promptise.com) 1.7.1: an Agent Runtime process with a signed webhook
trigger, an MCP server with API-key auth and role guards, an approval policy, an autonomy budget, a
journal, per-service memory, prompts as code and observability.

## The business problem

Pages arrive at night. The first 15 minutes of most incidents are the same steps: find the runbook,
pull logs and graphs, check what was deployed, post in the channel. Engineers lose that time while
customers see errors. Handing the whole incident to an agent is not acceptable either, because a
rollback or a restart changes production.

This project splits the work along that line. The agent does the reading and the writing on its own.
Anything that changes production waits for a person, who sees the exact call and the reason. The
person can say no. The agent then stops and hands over.

## Quickstart

```bash
cd incident-response-agent
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # set OPENAI_API_KEY (or another provider) and a random ALERT_WEBHOOK_SECRET
promptise run            # terminal 1: start the agent
promptise run alert      # terminal 2: fire a signed sample alert at it
```

`promptise run` starts two services. The ops MCP server runs as its own HTTP process with fresh API
keys. The agent process listens for alerts on `http://127.0.0.1:9095/alerts`. The alert wakes the agent.
When it wants to roll back, terminal 1 asks you. Answer `y`, or `n` with a reason.

Any provider works: set `INCIDENT_AGENT_MODEL=anthropic:claude-sonnet-4-5` and `ANTHROPIC_API_KEY`, for
example. `promptise run dev` adds the live trace on stderr. State lives in `var/state/`; delete it to
reset the stand-in production world. Memory of past incidents survives a restart.

### A real run

This is a real run on `openai:gpt-5-mini`: one alert, 12 model calls, about 68,000 input and 11,000
output tokens. Terminal 2:

```
$ curl -s -o /dev/null -w "%{http_code}\n" -X POST -d '{"status":"firing"}' http://127.0.0.1:9095/alerts
401
$ promptise run alert-resolved
POST http://127.0.0.1:9095/alerts -> 200 {"status": "ignored", "event_id": "2a0a561f-..."}
$ promptise run alert
POST http://127.0.0.1:9095/alerts -> 202 {"status": "accepted", "event_id": "67af5e68-..."}
```

Terminal 1:

```
WebhookTrigger: rejected request from 127.0.0.1 (missing X-Webhook-Signature header)

The incident agent wants to run rollback_deploy:
{
  "service": "checkout-service",
  "to_version": "v2.13.4",
  "reason": "Rollback: recent deploy v2.14.0 likely introduced client config change causing timeouts"
}
Approve? [y/N] y
-> approved by oncall

At 2026-10-23T16:35:17Z checkout-service 5xx error rate rose to 11.8% (threshold 5%) after a deploy
finished at 2026-10-23T16:34:17Z (v2.14.0, ...). Per the runbook I rolled back checkout-service from
v2.14.0 to v2.13.4 at 2026-10-23T16:50:04Z; checkout-service error_rate is now 0.4% ...
Trace: var/state/traces/20261023T164929Z-f96ae6bf.ndjson
```

The run's timeline (`var/state/traces/*.timeline.md`), shortened:

```
16:49:29  alert received, run started
16:49:31  call open_incident(alert_name='HighErrorRate', service='checkout-service', ...)
16:49:34  call recall_past_incidents(service='checkout-service', query='')
16:49:35  call find_runbook(alert_name='HighErrorRate')
16:49:39  call search_logs(service='checkout-service', minutes=30, level='ERROR', limit=200)
16:49:39  call query_metric(service='checkout-service', metric='error_rate', minutes=30)
16:49:39  call list_deploys(service='checkout-service', hours=24)
16:49:47  call query_metric(service='payments-api', metric='error_rate', minutes=30)
16:49:55  call post_status_update(incident_id='INC-1042', message='Investigating: checkout-service er...)
16:50:02  call rollback_deploy(service='checkout-service', to_version='v2.13.4', reason='Rollback: ...)
16:50:04  approval approved for rollback_deploy by oncall
16:50:13  call query_metric(service='checkout-service', metric='error_rate', minutes=10)
16:50:33  call post_status_update(incident_id='INC-1042', message='Verification: rollback to v2.13.4 ...)
16:50:51  call write_incident_summary(incident_id='INC-1042', status='mitigated', ...)
16:51:11  run finished: 14 tool calls, 12 model calls
```

The journal (`var/state/journal/incident-agent.jsonl`) shows that the webhook event started the run:

```
16:49:29 trigger_event      webhook  event_id=67af5e68-40ba-40aa-b635-6a713ac54cc9
16:49:29 invocation_start            event_id=67af5e68-40ba-40aa-b635-6a713ac54cc9
16:51:11 invocation_result  webhook  event_id=67af5e68-40ba-40aa-b635-6a713ac54cc9  duration_ms=101894.8
```

In a run where the engineer answered `n` with "Payments freeze until 18:00", nothing changed in
production. The agent posted "Rollback to v2.13.4 was DENIED ... A person must take over" and wrote the
summary with status `escalated`.

### No key yet?

`promptise run demo-offline` runs the same application with a scripted model: the ops server, the
signed webhook, the approval gate, the budget, the memory and the trace are all real.
`promptise run demo-offline-deny` shows the denied path.

## Point it at a real Alertmanager

Alertmanager can send a bearer token with its webhooks, but it can't sign the body. Run the relay next to
the agent. It checks Alertmanager's token, signs the body with `ALERT_WEBHOOK_SECRET` and forwards it:

```bash
ALERTMANAGER_TOKEN=... ALERT_WEBHOOK_SECRET=... python -m scripts.alertmanager_relay --host 0.0.0.0 --port 9094
```

```yaml
# alertmanager.yml
route:
  receiver: incident-agent
receivers:
  - name: incident-agent
    webhook_configs:
      - url: https://<relay host>/alertmanager    # put TLS in front of the relay
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/incident-agent-token
```

The agent's webhook keeps listening on loopback. The relay passes the agent's answer back, so
Alertmanager retries when the agent answers `503`. Alertmanager resends a firing alert at every
`repeat_interval`; `open_incident` is idempotent on the alert fingerprint, so a repeat posts one update
and stops. Grafana and PagerDuty payloads work too: change `filter_expression` in
`incident_agent/config.py` and the field names in `incident_agent/prompts.py`.

## How it works

```
Alertmanager ─▶ relay (token) ─▶ signed POST /alerts ─▶ WebhookTrigger ─┐
                                                                        ▼  AgentProcess "incident-agent"
                  prompts.py (blocks) ─▶ ┌──────────────────────────┐
                 memory (per service) ─▶ │  agent: model + tools    │──▶ journal (trigger, runs, tool calls)
   on-call engineer ◀── ApprovalPolicy ──│  budget: 1 change/alert  │──▶ traces: NDJSON, timeline, HTML
                                         └────────────┬─────────────┘
                                                      │ MCP over HTTP, x-api-key
                                         ┌────────────▼─────────────┐
                                         │ ops MCP server           │──▶ ops-audit.jsonl
                                         │ roles: viewer, responder,│
                                         │ remediator · rate limits │
                                         └──────────────────────────┘
```

| Concern | Where | Promptise feature |
|---|---|---|
| What wakes the agent | `incident_agent/config.py` | `TriggerConfig(type="webhook", hmac_secret=..., filter_expression=...)` |
| How it works an incident | `incident_agent/prompts.py` | `promptise.prompts.blocks`: `Identity`, `Rules`, `Section`, `OutputFormat`, `PromptAssembler` |
| What waits for a person | `incident_agent/config.py`, `incident_agent/reviewers.py` | `ApprovalPolicy`, `QueueApprovalHandler`, `CallbackApprovalHandler` |
| How much it may do per alert | `incident_agent/config.py` | `BudgetConfig(max_irreversible_per_run=1, max_tool_calls_per_run=40, max_runs_per_day=100)` |
| What it remembers | `incident_agent/memory.py` | `InMemoryProvider(scope=MemoryScope.PER_USER)` keyed by service, a `@tool`, a `task_completed` hook |
| Who may call what | `ops_systems/server.py` | `MCPServer(require_auth=True)`, `APIKeyAuth`, `roles=[...]`, `rate_limit`, `AuditMiddleware` |
| The record | `incident_agent/config.py`, `incident_agent/trace.py` | `JournalConfig(level="full")`, `ObservabilityConfig` with `CALLBACK` and `HTML` |
| Offline model | `incident_agent/offline_model.py` | `promptise.testing.ScriptedModel` |

The guards stack. The approval policy holds every restart, rollback and scale for a person. The budget
refuses a second production change in the same alert before it reaches the reviewer, then pauses the
process. The ops server only lets the agent's key change production, at most 3 times an hour.

## Adapt it to your systems

| To connect | Change | How |
|---|---|---|
| Logs, metrics, deploys, runbooks, incidents, chat | `ops_systems/store.py` | Replace the methods of `OpsWorld` with calls to Loki, Prometheus, Argo CD, your wiki, incident.io and Slack. Keep the tool names in `ops_systems/server.py`. |
| Your own MCP servers instead | `.env` | Set `OPS_MCP_URL` and `OPS_AGENT_API_KEY`. The agent then skips the local stand-in. |
| API keys and roles | `ops_systems/server.py` (`api_keys_from_env`) | Load keys from your secret store, or switch `APIKeyAuth` for `JwksAuth` and your identity provider. |
| Approvals | `main.py`, `incident_agent/reviewers.py` | Use `WebhookApprovalHandler(url=..., secret=...)` for your approval service, or a `CallbackApprovalHandler` that posts Slack buttons. |
| Memory | `incident_agent/memory.py` | Swap `InMemoryProvider` for `ChromaProvider(persist_directory=..., scope=MemoryScope.PER_USER)` for semantic search over many incidents. |
| Traces | `incident_agent/config.py` | Add `TransporterType.OTLP` to send each run as one OpenTelemetry trace. |

## Project layout

```
incident-response-agent/
├── promptise.toml                 # start, dev, alert, test, test-live, demo-offline, demo-offline-deny
├── main.py                        # the application: agent + local ops server, approvals at the terminal
├── demo.py                        # the same application with a scripted model
├── incident_agent/
│   ├── config.py                  # the process: webhook, approval, budget, journal, memory, trace
│   ├── prompts.py                 # instructions as prompt blocks
│   ├── memory.py                  # past incidents per service; memory_seed.jsonl holds older ones
│   ├── app.py                     # starts memory, the ops server and the process together
│   ├── local_ops.py               # runs the ops MCP server as its own process
│   ├── reviewers.py               # terminal reviewer, demo reviewer
│   ├── trace.py                   # per-run trace export and timeline
│   ├── alerts.py                  # Alertmanager payload, HMAC signing
│   ├── offline_model.py           # ScriptedModel scripts
│   └── runner.py                  # wait for runs, free port
├── ops_systems/
│   ├── server.py                  # ops MCP server: auth, roles, rate limits, audit
│   ├── store.py                   # stand-in production world (replace for real systems)
│   └── seed/                      # services, deploys, logs, runbooks
├── scripts/
│   ├── fire_alert.py              # send the sample alert, signed
│   └── alertmanager_relay.py      # Alertmanager bearer token in, signed webhook out
└── tests/
```

## Tests

```bash
promptise run test         # 35 offline tests, about 30 seconds, no key needed
promptise run test-live    # one real incident end to end with your model key, a few cents
```

The offline tests drive the real process, webhook, relay and ops server with a scripted model. They
check:

- a signed alert wakes the agent, and it reads the logs of the service in the alert;
- a resolved notification and a wrongly signed request never reach the model;
- the journal records the webhook event that started the run;
- restart, rollback and scale never run without approval, and do run after approval;
- an unanswered approval times out as a denial;
- a second production change in one alert is refused by the budget, and the process pauses;
- a viewer key can read but can't change production; changes are rate-limited per client;
- past incidents are recalled per service, and a finished incident is remembered after a restart;
- the summary has an ordered timeline, and the trace holds every tool call;
- the safety rules are in the rendered prompt.

`test-live` runs one alert on a real model with an approving reviewer. It checks that the agent read
`checkout-service` logs, posted before it asked for the fix, rolled back through the approval gate,
verified the error rate and wrote the summary.

## Notes

- The process runs one incident at a time (`concurrency=1`), because `IncidentTrace` attaches approvals
  to the latest run.
- A budget pause holds new alerts in the queue until a person resumes the process (`process.resume()`)
  or restarts the agent.
- Approval denials are counted per tool for the whole process (triggered runs have no user). After three
  denials of a tool within 10 minutes, the gate denies it without asking.
