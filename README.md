# Promptise templates

Complete AI agent projects for real business problems, built with
[Promptise Foundry](https://github.com/promptise-com/Foundry). Clone one, run the demo, then connect your own
systems.

Every project runs offline first. The demo uses a scripted model from `promptise.testing`, so you need no API
key to see it work. The outside systems (billing, ticketing, logs, your API) are small local fakes with
realistic data. Each project has tests that prove its business rules.

| Use case | What it does | Guide |
|---|---|---|
| [Invoice reconciliation](invoice-reconciliation/) | Reconciles invoices every night and follows up on failed payments. Refunds are capped in code and large ones wait for a person. | [Read the guide](https://www.promptise.com/use-cases/invoice-reconciliation) |
| [Multi-tenant support agent](multi-tenant-support/) | One support agent serves many customer companies. Each one keeps its own data, memory, permissions and rate limit. | [Read the guide](https://www.promptise.com/use-cases/multi-tenant-support) |
| [Agent-ready SaaS API](agent-ready-saas-api/) | Puts an MCP server in front of a REST API so customers' AI agents can use it. Agents sign in, tools have roles, deletes wait for approval and every call is audited. | [Read the guide](https://www.promptise.com/use-cases/agent-ready-saas-api) |
| [On-call incident agent](incident-response-agent/) | An alert wakes the agent. It investigates, follows the runbook and asks before any risky fix. Every step is traced. | [Read the guide](https://www.promptise.com/use-cases/incident-response-agent) |

## Run one

```bash
git clone https://github.com/promptise-com/promptise-templates
cd promptise-templates/agent-ready-saas-api
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
promptise run demo
```

Each folder's README covers the real-model run, the tests and how to adapt the project to your systems.

## Requirements

Python 3.10 or newer. Each project pins the Promptise version it was built and tested with.

## License

Apache 2.0. See [LICENSE](LICENSE).
