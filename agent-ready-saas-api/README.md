# Agent-ready SaaS API: an MCP server your customers' agents can use safely

Your customers want their AI agents to work in your product. Taskboard, a small
project-management SaaS, already has a REST API for projects, tasks and comments.
This project puts an MCP server in front of that API, so any MCP client (Claude,
Cursor, a Promptise agent, a customer's own code) can use it with an agent key.

The server is built to be handed to customers:

- **Agents sign in.** Every call carries an agent API key from the workspace settings.
  The key decides the workspace and the access level.
- **Each tool declares who may call it.** Reader keys read. Editor keys create and
  comment. Only admin keys can ask to delete, and a reader never even sees the
  write tools.
- **Deleting waits for a person.** `delete_project` parks until a reviewer in the same
  workspace approves it. Nobody can approve their own call. No decision in time means no.
- **Every call is in a tamper-evident log.** Allowed and refused calls go into an
  HMAC-chained audit log. Editing or removing a line is detected.
- **A key can be revoked while the server runs.** The next request with that key is
  refused. No restart.
- **Each key has its own rate limit.** One runaway agent cannot slow the others down.

It starts from what most SaaS teams have: an OpenAPI spec. `promptise mcpcast`
generates a first server from it in seconds. The hand-tuned server in `mcp_server/`
is what ships. See [From the MCPcast draft to the server that ships](#from-the-mcpcast-draft-to-the-server-that-ships).

## Architecture

```text
 customer's agent ──MCP over HTTP, x-api-key──▶  Taskboard MCP server  ──HTTPS, workspace token──▶  Taskboard REST API
 (customer_agent/)                               (mcp_server/)                                      (taskboard_api/)
                                                   1. AuditMiddleware        HMAC-chained log
 reviewer's console ──approvals_list/decide──▶     2. AuthMiddleware         key -> client, workspace, roles
 (scripts/approvals.py)                            3. RateLimitMiddleware    one bucket per key
                                                   4. ApprovalGateMiddleware deletions wait for a person
                                                   5. roles=[...] guard      on every tool
```

The agent never chooses the workspace. The server takes it from the verified key and
calls the API with that workspace's token, so a key for Acme cannot reach Globex data.

## Quickstart

Requires Python 3.10 or newer.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 1. Offline demo (no API key)

```bash
promptise run demo        # or: python demo.py
```

It starts the REST API in-process and the MCP server on a real HTTP port on
127.0.0.1. Then the customer's agent connects with its key. The agent's model is a
`ScriptedModel` from `promptise.testing`, so its tool calls are fixed in advance.
The MCP server, the API and the tool results are real. Abridged output:

```text
== 1. A call without a key is refused ================================
POST /mcp with no x-api-key -> HTTP 401
== 2. A read-only key sees reads only, and cannot write ==============
tools: list_projects, find_tasks, get_task, list_members
create_task -> Requires any of roles [editor], but client has [reader]
== 3. The customer's agent does the weekly overdue sweep =============
  find_tasks -> [{"id": "tsk_1001", "project_id": "prj_101", "title": "Write homepage copy", ...
  add_comment -> {"id": "cmt_5000", "author": "agent:acme-ops-agent", "body": "@maya this is past due. ...
  create_task -> {"id": "tsk_5002", "project_id": "prj_101", "title": "Review answers on overdue tasks", ...
== 4. Deleting a project waits for a person ==========================
waiting: delete_project({'project_id': 'prj_103'}) asked by acme-ops-agent
dana@acme approves -> {"request_id": "...", "approved": true, "resolved": true}
  delete_project -> {"deleted": "prj_103", "name": "2024 trade show"}
== 5. Revoke the agent's key while the server runs ===================
$ python -m mcp_server.keys revoke 1741aaf9 -> revoked 1741aaf9
next call with the revoked key -> MCPConnectionRejectedError: HTTP 401 Unauthorized
== 6. The audit log is complete and tamper-evident ===================
verify: ok=True entries=8
after editing one line: ok=False -> bad_signature: the entry's signature does not match: it was modified or forged
```

### 2. Real run with a model

Four terminals, all from this folder:

```bash
promptise run setup        # writes .env: fresh dev tokens, an audit key, an agent key, a reviewer key
export OPENAI_API_KEY=...  # or add it to .env; any provider string works via TASKBOARD_AGENT_MODEL

promptise run api          # terminal 1: the REST API on :8000
promptise run mcp          # terminal 2: the MCP server on :8080/mcp
promptise run approvals    # terminal 3: the reviewer's console (dana@acme)
promptise run agent        # terminal 4: the agent does the weekly sweep
promptise run agent "Delete the 2024 trade show project"   # watch terminal 3 ask you
promptise run chat         # or talk to the agent
```

Then try the operator side:

```bash
promptise run keys list
promptise run keys create --workspace acme --name acme-report-bot --access reader
promptise run keys revoke <key_id>       # the running server refuses the key at once
promptise run audit                      # verifies .data/audit.jsonl with PROMPTISE_AUDIT_SECRET
```

`TASKBOARD_AGENT_MODEL` takes any Promptise model string, for example
`anthropic:claude-sonnet-4-5`. Set the matching provider key.

### Connect another MCP client

The server speaks plain MCP over streamable HTTP. For example, with Claude Code:

```bash
claude mcp add taskboard --transport http http://127.0.0.1:8080/mcp --header "x-api-key: <agent key>"
```

## From the MCPcast draft to the server that ships

`mcpcast_draft/` was generated from `openapi.json` with no model and no network:

```bash
promptise run spec       # export openapi.json from the FastAPI app
promptise run mcpcast    # promptise mcpcast openapi.json --no-curate --profile full --auth api-key ...
```

The draft is a working server with its own tests (`cd mcpcast_draft && PYTHONPATH=. pytest`).
It gates every write behind approval and maps each workspace to its token. Reading it
against the product showed what had to change before customers get it:

| The draft | The shipped server |
|---|---|
| 14 tools, one per endpoint, including `health_check` and `delete_task` | 10 agent tools. `get_task` returns the comments too. No health check, no task deletion |
| Any valid key may call any tool | Each tool declares a role: `reader`, `editor` or `admin` |
| Every write waits for approval, even a comment | Reversible writes run at once. Deleting a project waits for a person |
| `add_comment` takes an `author` argument, so an agent could sign as anyone | The author is set from the verified key: `agent:acme-ops-agent` |
| Client keys are read once at start-up. Revoking one needs a restart | Keys are hashed in a store that is re-read when it changes. Revocation is live |
| No audit log, no rate limit | HMAC-chained audit log, one rate-limit bucket per key |
| Examples with invented ids (`task_id: '123'`) | Descriptions that name real id formats (`prj_101`, `tsk_1001`) |

With a model key you can let MCPcast curate the surface instead of `--no-curate`, and
score it with `--eval`. Review the plan either way: the plan is the part you own.

## Adapt it to your systems

| To connect | Change |
|---|---|
| Your own REST API | `mcp_server/upstream.py` (base URL, auth header, error mapping) and the tools in `mcp_server/server.py` |
| Your key store (database, admin UI) | `mcp_server/keys.py`: make `KeyStore.lookup()` query your table. It must return `None` for revoked keys |
| An identity provider instead of API keys | Swap `APIKeyAuth` for `JwksAuth` in `mcp_server/server.py`. Roles come from the token's `roles` claim |
| Your approval UI | `mcp_server/approvals.py` holds the two reviewer tools. `scripts/approvals.py` is the minimal console |
| Your log pipeline | `AuditMiddleware(log_path=...)` in `mcp_server/server.py`. Run `promptise audit verify` in CI or a cron job |
| Limits per plan tier | Pass a `key_func` and your own limiter to `RateLimitMiddleware` |

Run one MCP server replica for now: the pending approvals and the rate-limit buckets
live in the process. Use an approval handler backed by a shared store before you scale out.

## Project layout

```text
agent-ready-saas-api/
├── promptise.toml          # scripts: demo, setup, api, mcp, agent, chat, approvals, keys, audit, mcpcast, test
├── demo.py                 # the offline end-to-end demo
├── openapi.json            # exported from taskboard_api (promptise run spec)
├── taskboard_api/          # the existing REST API (FastAPI) and its seed data
├── mcpcast_draft/          # the server MCPcast generated from openapi.json, kept for comparison
├── mcp_server/             # the hand-tuned MCP server that ships
│   ├── server.py           # middleware pipeline and every tool
│   ├── keys.py             # hashed agent keys, live revocation, CLI
│   ├── approvals.py        # reviewer tools scoped to one workspace
│   ├── upstream.py         # HTTP client for the REST API
│   └── config.py           # settings from the environment
├── customer_agent/         # what a customer writes: an agent that connects with its key
├── scripts/                # setup, run the API and server, the reviewer console, key CLI
└── tests/                  # offline pytest suite
```

## Tests

```bash
promptise run test    # or: pytest
```

34 tests, offline, in about 6 seconds. They use the MCP server's `TestClient` (the full
pipeline, in-process) and one subprocess run of the demo. They cover: a call without a
key is refused; a read-only key cannot create, change or delete; a delete waits for
approval, runs when approved and is denied on a no or a timeout; nobody approves their
own call; reviewers only see their own workspace; the audit log records allowed and
refused calls and detects an edited or deleted line; a revoked key stops working without
a restart; rate limits are per key; one workspace never reaches another.

One thing to know: with `require_tenant=True` the HTTP transport refuses a missing or
revoked key with `401` before the MCP layer. Those requests are in your HTTP access
log, not in the audit log. Calls refused inside the MCP layer (wrong role, rate limit,
denied approval) are in the audit log.
