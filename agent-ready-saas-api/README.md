# Agent-ready SaaS API: an MCP server your customers' agents can use safely

> **Reference application.** This shows how this use case can look with Promptise.
> It is not a finished product. Taskboard is a small sample API with sample data,
> standing in for your real product. Point the MCP server at your own API before you
> use it with real data.

Your customers want their AI agents to work inside your product. Taskboard, a small
project-management SaaS, has a REST API for projects, tasks and comments. This project
puts an MCP server in front of it. Any MCP client can then use the product with an
agent key: Claude, Cursor, or an agent the customer builds with Promptise.

The server is built to be handed to customers:

- **Agents sign in.** Every call carries an agent API key. The key decides the workspace and the access level.
- **Each tool declares who may call it.** Reader keys read. Editor keys write. Only admin keys can ask to delete.
- **Deleting waits for a person.** A reviewer in the same workspace approves it. Nobody approves their own call.
- **Every call is in a tamper-evident log.** Editing or removing a line is detected.
- **A key can be revoked while the server runs.** The next request with that key is refused.
- **Each key has its own rate limit.** One runaway agent cannot slow the others down.
- **Health probes.** `/health/ready` answers `503` while the Taskboard API is down.

## Quickstart

Requires Python 3.10 or newer and a model key. The default model is `openai:gpt-5-mini`.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
promptise run setup              # writes .env: dev API tokens, an audit key and three agent keys
export OPENAI_API_KEY=...        # or put it in .env
```

Then start the product and run the agent, each in its own terminal:

```bash
promptise run api                # 1. the Taskboard REST API on :8000
promptise run mcp                # 2. the MCP server on :8080/mcp
promptise run approvals          # 3. the reviewer's console, as dana@acme
promptise run agent              # 4. the customer's agent does the weekly overdue sweep
```

Next, try the rules:

```bash
promptise run agent "Delete the 2024 trade show project."    # waits until you answer y in terminal 3
promptise run agent-reader "Mark the homepage copy task as done."   # a read-only key
promptise run keys list
promptise run keys revoke <key_id>   # the running server refuses that key from the next request
promptise run audit                  # verifies the HMAC chain of .data/audit.jsonl
promptise run chat                   # talk to the agent
```

Any provider works: set `TASKBOARD_AGENT_MODEL` (for example `anthropic:claude-sonnet-4-5`)
and that provider's key.

## A real run

Output from `openai:gpt-5-mini` against the running API and MCP server. Lines are
shortened. The agent's own wording varies from run to run.

```text
$ promptise run agent
> Do the weekly overdue sweep for the Website relaunch project. Comment on each overdue task asking the assignee for a new due date. Then create one task for Maya, due this Friday, to review the answers.
  -> list_projects({'status': 'active'})
  -> find_tasks({'project_id': 'prj_101', 'overdue_only': True})
  <- find_tasks: [{"id": "tsk_1001", "project_id": "prj_101", "title": "Write homepage copy", "assignee": "maya", ...
  -> list_members({})
  -> add_comment({'task_id': 'tsk_1001', 'body': 'Hi Maya, this task is overdue. Could you please update the due date so we can plan next steps? Thanks.'})
  <- add_comment: {"id": "cmt_5000", "author": "agent:acme-ops-agent", ...
  -> add_comment({'task_id': 'tsk_1002', 'body': 'Hi Leo, this task is overdue. Could you please update the due date so we can plan next steps? Thanks.'})
  -> create_task({'project_id': 'prj_101', 'title': 'Review overdue task responses', 'assignee': 'maya', 'due_date': '2026-10-23', ...})
  <- create_task: {"id": "tsk_5002", "project_id": "prj_101", "title": "Review overdue task responses", ...
...
Summary of changes: comments cmt_5000, cmt_5001; created task tsk_5002 (project prj_101).

[7 model calls, 6 tool calls, 16018 tokens. Trace: .data/agent-trace.jsonl]

$ promptise run agent "Delete the 2024 trade show project."
  -> list_projects({'status': 'all'})
  -> delete_project({'project_id': 'prj_103'})          # waits here until the reviewer answers
  <- delete_project: {"deleted": "prj_103", "name": "2024 trade show"}
...
Summary of change: deleted project prj_103.

$ promptise run approvals                                 # terminal 3
acme-ops-agent wants to run delete_project with {"project_id": "prj_103"}
Approve? [y/N] y
approved: {"request_id": "dae73b79...", "approved": true, "resolved": true}

$ promptise run agent-reader "Mark the homepage copy task as done and comment that it shipped."
  -> find_tasks({'status': 'in_progress', 'limit': 100})
  -> list_members({})
I cannot mark tasks done or add comments because this agent's API key does not allow updating tasks; a workspace admin can grant the needed permission.

$ promptise run keys revoke 2794e3c1 && promptise run agent-reader "List the active projects."
revoked 2794e3c1
The Taskboard MCP server refused this agent key (HTTP 401). It is unknown or was revoked.

$ promptise run audit
OK 21 entries: intact.
```

The read-only agent never sees the write tools, so it cannot try them. A client that
calls one anyway gets `ACCESS_DENIED` (see `tests/test_permissions.py`).

## Connect any MCP client

The server speaks MCP over streamable HTTP at `http://127.0.0.1:8080/mcp`. Each client
sends its agent key in the `x-api-key` header. Mint one key per client:

```bash
promptise run keys create --workspace acme --name cursor-maya --access editor
```

**Cursor** (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "taskboard": {
      "url": "http://127.0.0.1:8080/mcp",
      "headers": { "x-api-key": "<agent key>" }
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add taskboard --transport http http://127.0.0.1:8080/mcp --header "x-api-key: <agent key>"
```

**Claude Desktop** has no header field for HTTP servers in `claude_desktop_config.json`.
Use the `mcp-remote` bridge, which runs locally and adds the header:

```json
{
  "mcpServers": {
    "taskboard": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8080/mcp", "--header", "x-api-key:${TASKBOARD_KEY}"],
      "env": { "TASKBOARD_KEY": "<agent key>" }
    }
  }
}
```

**A Promptise agent** (what `customer_agent/agent.py` does):

```python
servers = {"taskboard": HTTPServerSpec(url="http://127.0.0.1:8080/mcp", api_key=key)}
```

In production, serve the MCP server over HTTPS behind your own domain.

## How it is built

| Part | Promptise feature | Where |
|---|---|---|
| MCP server | `MCPServer(require_tenant=True, hide_unauthorized_tools=True)` | `mcp_server/server.py` |
| Agent sign-in | `APIKeyAuth(lookup=...)` with hashed keys, re-read on change | `mcp_server/keys.py` |
| Per-tool permissions | `@server.tool(roles=[...])` guards | `mcp_server/server.py` |
| Human approval | `requires_approval=True`, `ApprovalGateMiddleware`, `PendingApprover` | `mcp_server/server.py`, `mcp_server/approvals.py` |
| Audit log | `AuditMiddleware` (HMAC chain), `promptise audit verify` | `mcp_server/server.py` |
| Rate limits | `RateLimitMiddleware(TokenBucketLimiter)` plus per-tool `rate_limit=` | `mcp_server/server.py` |
| Health probes | `HealthCheck` with a check on the Taskboard API | `mcp_server/server.py` |
| The customer's agent | `build_agent` with `HTTPServerSpec(api_key=...)`, the ReAct pattern | `customer_agent/agent.py` |
| Instructions as code | `@prompt` with `Identity`, `Rules` and a `Section` that states today's date | `customer_agent/prompts.py` |
| Conversation history | `SQLiteConversationStore` and `agent.chat(session_id=...)` | `customer_agent/agent.py` |
| Traces | `ObservabilityConfig`: HTML report, JSON trace log, live tool-call lines | `customer_agent/agent.py`, `customer_agent/main.py` |
| Project and scripts | `promptise.toml` and `promptise run` | `promptise.toml` |
| Tests without a model | `TestClient`, `ScriptedModel` | `tests/`, `demo.py` |

```text
 customer's agent ──MCP over HTTP, x-api-key──▶  Taskboard MCP server  ──HTTP, workspace token──▶  Taskboard REST API
 (customer_agent/)                               (mcp_server/)                                     (taskboard_api/)
                                                   1. AuditMiddleware        HMAC-chained log
 reviewer's console ──approvals_list/decide──▶     2. AuthMiddleware         key -> client, workspace, roles
 (scripts/approvals.py)                            3. RateLimitMiddleware    one bucket per key
                                                   4. ApprovalGateMiddleware deletions wait for a person
                                                   5. roles=[...] guard      on every tool
```

The agent never chooses the workspace. The server takes it from the verified key. It
calls the API with that workspace's token, so an Acme key cannot reach Globex data.

## Adapt it to your systems

| To connect | Change |
|---|---|
| Your own REST API | `mcp_server/upstream.py` (base URL, auth header, error mapping) and the tools in `mcp_server/server.py` |
| Your key store | `mcp_server/keys.py`: make `KeyStore.lookup()` query your database. Return `None` for a revoked key |
| An identity provider instead of API keys | Swap `APIKeyAuth` for `JwksAuth` in `mcp_server/server.py`. Roles come from the token's `roles` claim |
| Your approval screen | Call `approvals_list` and `approvals_decide` from your admin UI. `scripts/approvals.py` is the minimal version |
| Your log pipeline | `AuditMiddleware(log_path=...)`. Run `promptise audit verify` on a schedule |
| Your agent's job | `customer_agent/prompts.py` for the instructions, `customer_agent/main.py` for the task |

Run one MCP server replica for now. Pending approvals and rate-limit buckets live in
the process. Use an approval handler backed by a shared store before you scale out.

## Project layout

```text
agent-ready-saas-api/
├── promptise.toml          # scripts: setup, api, mcp, approvals, agent, agent-reader, chat, keys, audit, test, test-live, demo-offline
├── taskboard_api/          # the sample REST API (FastAPI) with sample data; serves /openapi.json
├── mcp_server/             # the MCP server customers connect to
│   ├── server.py           # middleware pipeline, health check and every tool
│   ├── keys.py             # hashed agent keys, live revocation, CLI
│   ├── approvals.py        # reviewer tools scoped to one workspace
│   ├── upstream.py         # HTTP client for the REST API
│   └── config.py           # settings from the environment
├── customer_agent/         # what a customer writes: an agent that connects with its key
│   ├── agent.py            # build_agent, conversation store, traces
│   ├── prompts.py          # the instructions as a Prompt
│   └── main.py             # one task from the command line
├── scripts/                # setup, run the API and server, the reviewer console, key CLI
├── local_stack.py          # the whole product in one process, for demo-offline and the live test
├── demo.py                 # demo-offline: the same server with a scripted model
└── tests/                  # offline pytest suite and one live test
```

## Tests

```bash
promptise run test          # 31 offline tests, about 5 seconds, no key needed
promptise run test-live     # 1 real end-to-end run, about 25k tokens, needs the model key
```

The offline tests use the MCP server's `TestClient`, which runs the full pipeline
in-process. They cover:

- a call without a key is refused;
- a read-only key cannot create, change or delete;
- a deletion waits for approval, runs when approved and is denied on a no or a timeout;
- nobody approves their own call, and reviewers only see their own workspace;
- the audit log records allowed and refused calls and detects an edited or deleted line;
- a revoked key stops working without a restart;
- rate limits are per key;
- one workspace never reaches another.

The live test starts the product, lets the real model do the sweep and a deletion, and
approves the deletion through the reviewer tools.

With `require_tenant=True`, the HTTP transport refuses a missing or revoked key with
`401` before the MCP layer. Those requests are in your HTTP access log, not in the
audit log.

## No key yet?

```bash
promptise run demo-offline
```

It runs the same API and MCP server on your machine. The agent's model is a
`ScriptedModel` from `promptise.testing`, so its tool calls are fixed in advance. The
server, the API and every tool result are real.
