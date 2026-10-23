---
title: "How to Build an AI Incident Response Agent with Runbooks"
description: "Build an AI incident response agent in Python. An Alertmanager webhook wakes it. It follows your runbook. Rollbacks and restarts wait for a person to approve."
slug: incident-response-agent
queries:
  - ai incident response agent
  - ai on-call agent python
  - runbook automation with llm agent
  - alertmanager webhook ai agent
  - human approval for ai agent remediation
---

# How to Build an AI Incident Response Agent with Runbooks

You can have an agent take the first 15 minutes of every page. An alert wakes it. It reads the runbook, the logs, the metrics, the deploy history and what went wrong on that service before. It posts its findings in the incident channel. It asks the on-call engineer before it rolls anything back. Then it checks the fix and writes the incident summary.

> **This is a reference application.** It shows how this use case can look with Promptise. It is not a finished product. The logs, metrics, deploys and chat channel are local stand-ins with sample data, not connectors to Prometheus, Loki, your CI/CD or Slack. Replace the adapter files before you point it at production.

The complete project is in the [promptise-templates repository](https://github.com/promptise-com/promptise-templates/tree/main/incident-response-agent).

## The problem: the first minutes of an incident are always the same

Most pages start with the same steps. Find the runbook. Pull the error logs. Open the graphs. Check what was deployed in the last hour. Post a first update. An engineer woken at 3 a.m. does this slowly, while customers see errors.

An agent can do these steps in a minute or two. The hard part is trust. A rollback, a restart or a scale-down changes production. You want the agent to read freely. You want a person to decide every change.

## How the incident agent works

The agent is an [Agent Runtime](https://docs.promptise.com/runtime/) process:

1. **A signed webhook wakes it.** Each accepted alert becomes one run.
2. **An ops MCP server gives it tools.** Logs, metrics, deploys, runbooks, the incident tool and the chat channel sit behind API keys and roles.
3. **An approval policy holds every change.** Restart, rollback and scale wait for the on-call engineer.
4. **A budget caps each alert.** One production change per alert. A second one is refused.
5. **Memory recalls past incidents.** It is kept per service.
6. **A journal and a trace record everything.** You can replay any run.

## Run it first

```bash
pip install -r requirements.txt
cp .env.example .env     # set OPENAI_API_KEY (or another provider) and ALERT_WEBHOOK_SECRET
promptise run            # terminal 1: start the agent
promptise run alert      # terminal 2: fire a signed sample alert
```

This is a real run on `openai:gpt-5-mini`. Unsigned requests are refused. A resolved notification is ignored. The signed alert starts a run:

```text
$ curl -s -o /dev/null -w "%{http_code}\n" -X POST -d '{"status":"firing"}' http://127.0.0.1:9095/alerts
401
$ promptise run alert-resolved
POST http://127.0.0.1:9095/alerts -> 200 {"status": "ignored", "event_id": "2a0a561f-..."}
$ promptise run alert
POST http://127.0.0.1:9095/alerts -> 202 {"status": "accepted", "event_id": "67af5e68-..."}
```

Half a minute later, terminal 1 asks for the rollback:

```text
The incident agent wants to run rollback_deploy:
{
  "service": "checkout-service",
  "to_version": "v2.13.4",
  "reason": "Rollback: recent deploy v2.14.0 likely introduced client config change causing timeouts"
}
Approve? [y/N] y
-> approved by oncall
```

The run's timeline, shortened:

```text
16:49:31  call open_incident(alert_name='HighErrorRate', service='checkout-service', ...)
16:49:34  call recall_past_incidents(service='checkout-service', query='')
16:49:35  call find_runbook(alert_name='HighErrorRate')
16:49:39  call search_logs(service='checkout-service', minutes=30, level='ERROR', limit=200)
16:49:47  call query_metric(service='payments-api', metric='error_rate', minutes=30)
16:49:55  call post_status_update(incident_id='INC-1042', message='Investigating: checkout-service er...)
16:50:02  call rollback_deploy(service='checkout-service', to_version='v2.13.4', reason='Rollback: ...)
16:50:04  approval approved for rollback_deploy by oncall
16:50:13  call query_metric(service='checkout-service', metric='error_rate', minutes=10)
16:50:51  call write_incident_summary(incident_id='INC-1042', status='mitigated', ...)
16:51:11  run finished: 14 tool calls, 12 model calls
```

In another run the engineer denied the rollback. Nothing changed in production. The agent posted that a person must take over. It wrote the summary with status `escalated`.

**No key yet?** `promptise run demo-offline` runs the same application with a scripted model from `promptise.testing`.

## Step 1: Wake the agent with a signed webhook

The process config is in `incident_agent/config.py`:

```python file=incident_agent/config.py
        triggers=[
            TriggerConfig(
                type="webhook",
                webhook_path=WEBHOOK_PATH,
                webhook_port=webhook_port,
                hmac_secret=webhook_secret,
                # Alertmanager also sends "resolved" notifications; only a firing alert needs work.
                filter_expression="payload.status == 'firing'",
            )
        ],
```

The runtime wraps the payload in a block marked as untrusted data. Text inside an alert is information, not an order. With `JournalConfig(level="full")`, the journal records a `trigger_event` with the webhook's event id. Then come the run's tool calls and its result.

Alertmanager can send a bearer token, but it can't sign the body. `scripts/alertmanager_relay.py` checks the token. Then it signs the body and forwards it. Point Alertmanager's `webhook_configs` at the relay, with `http_config.authorization` set to the token.

## Step 2: Put your ops systems behind an authenticated MCP server

One MCP server built with the Promptise MCP server SDK stands in for Loki, Prometheus, Argo CD, the runbook wiki, the incident tool and Slack. It runs as its own HTTP service. Every caller presents an API key. The key decides the roles. This is from `ops_systems/server.py`:

```python file=ops_systems/server.py
    server = MCPServer(name="ops", version="1.0.0", require_auth=True)
    # Audit first, so refused calls (bad key, wrong role, rate limit) are recorded too.
    # Set PROMPTISE_AUDIT_SECRET to sign the log as an HMAC chain.
    signed = bool(os.environ.get("PROMPTISE_AUDIT_SECRET"))
    server.add_middleware(
        AuditMiddleware(log_path=str(Path(state_dir) / "ops-audit.jsonl"), signed=signed, include_args=True)
    )
    server.add_middleware(AuthMiddleware(APIKeyAuth(keys=api_keys)))
```

The tools that change production need the `remediator` role. They are also rate-limited per client:

```python file=ops_systems/server.py
    @server.tool(roles=REMEDIATE, rate_limit=CHANGE_RATE, destructive_hint=True)
    async def rollback_deploy(
        service: Service,
        to_version: Annotated[str, Field(description='The version to go back to, for example "v2.13.4".')],
        reason: Reason,
    ) -> dict:
        """Roll a service back to an earlier deployed version. Needs approval."""
        return run(world.rollback_deploy, service, to_version, reason)
```

A status page holding a `viewer` key can read logs. It gets `ACCESS_DENIED` on a rollback. The agent connects with its own key: `servers={"ops": {"url": ops_url, "api_key": ops_api_key}}`.

## Step 3: Hold every change for a person

```python file=incident_agent/config.py
        approval=ApprovalPolicy(
            tools=RISKY_TOOLS,
            handler=reviewer,
            timeout=approval_timeout,
            on_timeout="deny",
            on_decision=trace.on_decision,
        ),
```

`RISKY_TOOLS` names `restart_service`, `rollback_deploy` and `scale_service`. The call stops at the gate. The reviewer sees the tool, the arguments and the agent's reason. A denial comes back to the model as `DENIED: <reason>`. No answer within 15 minutes counts as a denial. `TerminalReviewer` in `incident_agent/reviewers.py` asks at the keyboard. In production you swap in a `WebhookApprovalHandler` to your approval service.

## Step 4: Cap what one alert may do

```python file=incident_agent/config.py
        budget=BudgetConfig(
            enabled=True,
            # One production change per alert. A second one is refused before it reaches the reviewer.
            max_irreversible_per_run=1,
            tool_costs={name: ToolCostAnnotation(cost_weight=5.0, irreversible=True) for name in RISKY_TOOLS},
            # A runaway loop stops long before it hurts; a normal incident takes 12 to 20 calls.
            max_tool_calls_per_run=40,
            # An alert storm can't run up the model bill.
            max_runs_per_day=100,
            # Any of these crossed: the process pauses until a person resumes it.
            on_exceeded="pause",
        ),
```

An approval answers "is this change right?". The budget answers "how many changes may one alert make?". A denied call gives its irreversible action back, so a denial never uses up the alert's one change.

## Step 5: Write the instructions as code

The system prompt is assembled from `promptise.prompts.blocks` in `incident_agent/prompts.py`. Each block is a value you can test and diff:

```python file=incident_agent/prompts.py
def incident_prompt() -> PromptAssembler:
    return PromptAssembler(IDENTITY, SAFETY_RULES, RUNBOOK_CONTRACT, PHASES, FINAL_REPLY)
```

`SAFETY_RULES` holds the rules that keep production safe. `RUNBOOK_CONTRACT` says how to read a runbook. The agent does every Investigate step. It picks the first Mitigate condition its findings meet. It compares the Verify metric with its threshold. `tests/test_prompts.py` checks that the safety rules are in the rendered text.

The real runs shaped these rules. In the first run the model posted its findings only after the rollback. A rule now says to post before calling a fix. It also wrote a long final reply. A rule now caps it at three sentences.

## Step 6: Remember past incidents per service

`incident_agent/memory.py` keeps lessons in a `promptise.memory.InMemoryProvider` with `MemoryScope.PER_USER`. The scope key is the service name. A search for `checkout-service` can never return a `cart-service` incident. The agent reads it through a tool:

```python file=incident_agent/memory.py
        @tool
        async def recall_past_incidents(service: str, query: str = "") -> list[dict]:
            """Past incidents on one service, with their root cause and follow-ups.

            Args:
                service: The service name, for example "checkout-service".
                query: Optional words to rank by, for example "deploy timeout". Empty lists the latest.
            """
            return await memory.recall(service, query)
```

A `task_completed` runtime hook stores each finished incident summary. A copy goes to `memory.jsonl`, so the memory survives a restart. In a later real run the agent recalled the earlier INC-1042 with its root cause.

## Step 7: Record every step

```python file=incident_agent/config.py
        observe=ObservabilityConfig(
            level=ObserveLevel.FULL,
            session_name="incident-agent",
            transporters=[TransporterType.CALLBACK, TransporterType.HTML],
            on_event=trace.on_event,
            output_dir=str(state_dir / "traces"),
        ),
```

`IncidentTrace` in `incident_agent/trace.py` groups the timeline by run. It writes an NDJSON file and a readable timeline. The approval decisions come from `on_decision`. The incident summary merges the agent's findings with the incident tool's own record of status posts and changes. The timeline then never depends on the model's memory.

## Test it

`promptise run test` runs 35 offline tests against the real process, webhook, relay and MCP server. Only the model is scripted. This test from `tests/test_incident_flow.py` proves the budget:

```python file=tests/test_incident_flow.py
    # The second change never reached the reviewer or production.
    assert [r.tool_name for r in reviewer.requests] == ["rollback_deploy"]
    assert [a["action"] for a in run.world["actions"]] == ["rollback"]
    assert "max_irreversible_per_run" in run.tool_messages("restart_service")[0]
```

`promptise run test-live` runs one incident on your model key. It checks that the agent posted before it asked for the fix. It also checks that the rollback went through the gate.

## Pitfalls

**A model can skip a runbook step.** In one of three real runs with these instructions, the model skipped the INFO logs and missed the exact config value. Approval and the budget don't depend on the model following every step. Read the summary before you close the incident.

**Approving the same fix twice.** If your MCP servers also gate a tool with `requires_approval=True`, a matching agent policy asks the reviewer a second time. Pick one gate per tool.

**Repeated alerts.** Alertmanager resends a firing alert at every `repeat_interval`. Make `open_incident` idempotent on the alert fingerprint, as the project does.

**Custom reasoning graphs.** `build_agent()` accepts an `agent_pattern`. In Promptise 1.7.1, `ProcessConfig` has no such field. A runtime process runs the default tool loop. Put the investigation order in the instructions.

## FAQ

**Can the agent restart a service without a person?**
No. The approval policy holds the call. The ops server also refuses any key without the `remediator` role.

**What happens when nobody answers the approval request?**
After 15 minutes the request counts as a denial. The agent escalates.

**Does it work with PagerDuty or Grafana alerts?**
Yes. Change the `filter_expression` to match the sender's payload. Update the field names in `incident_agent/prompts.py`.

**Which model should I use?**
Any model with reliable tool calling. The project defaults to `openai:gpt-5-mini`. One incident took about 68,000 input and 11,000 output tokens.

**Where do I find what the agent did last night?**
In the state folder. `traces/` holds each run. `journal/` holds the trigger events. `ops-audit.jsonl` lists every call the ops server received, with the caller.
