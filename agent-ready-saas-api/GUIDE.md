---
title: "How to Build a Secure MCP Server for Your SaaS API in Python"
description: "Let customers' AI agents use your SaaS API safely: an MCP server with API keys, per-tool roles, human approval for deletes, audit logs and key revocation."
slug: agent-ready-saas-api
queries:
  - mcp server for saas api
  - secure mcp server python
  - let customers ai agents use my api
  - mcp server api key authentication
  - mcp human approval for destructive actions
  - openapi to mcp server
---

# How to Build a Secure MCP Server for Your SaaS API in Python

By the end of this guide, your customers' AI agents can work in your product through
an MCP server you control. Each agent signs in with its own key. Each tool says which
keys may call it. Deleting data waits for a person. Every call lands in a log that
shows any later edit. You can revoke a key while the server runs.

The example product is Taskboard, a small project-management SaaS with projects,
tasks and comments. The complete code runs offline in one command:
[agent-ready-saas-api](https://github.com/promptise-com/promptise-templates/tree/main/agent-ready-saas-api).

## The business problem

Customers now ask whether your product works with their AI assistant. Some use
Claude or Cursor. Some build their own agents. They all speak MCP, the Model
Context Protocol.

You already have a REST API with an OpenAPI spec. Exposing it to agents raises
questions your API never had to answer:

- An agent's key leaks. Can you stop it now, without a deploy?
- A reporting bot should read. Can it also delete?
- An agent misreads a request and deletes a project. Who said yes?
- A customer asks what their agent did last Tuesday. Can you prove the log is complete?
- One agent loops. Does everyone else slow down?

The MCP server in this guide answers each of them in code.

## How it works

The customer's agent connects over MCP with an `x-api-key` header. The MCP server
runs every call through five checks, in order:

1. **Audit.** The call is written to an HMAC-chained log, whatever the outcome.
2. **Auth.** The key becomes a client id, a workspace and a set of roles.
3. **Rate limit.** Each key has its own token bucket.
4. **Approval gate.** A deletion parks until a reviewer decides.
5. **Guard.** The tool's own `roles=[...]` check.

Then the tool calls the REST API with the token of the caller's workspace. The agent
never names the workspace. So an Acme key cannot reach Globex data, whatever
arguments the agent sends.

## Step 1: Draft the server from your OpenAPI spec

Start with MCPcast. It reads the spec and writes a working MCP server project.
With `--no-curate` it needs no model and no network:

```bash
promptise mcpcast openapi.json --no-curate --profile full --auth api-key --name taskboard --output mcpcast_draft --force
```

The draft has 14 tools, one per endpoint. Every write waits for approval. Each
workspace maps to its own API token. That is a good start.

Then read it against your product. In this project the review found six problems:

- Any valid key could call any tool.
- `add_comment` took an `author` argument, so an agent could sign as any person.
- A comment needed the same approval as deleting a project.
- Keys were read once at start-up, so revoking one needed a restart.
- There was no audit log and no rate limit.
- `health_check` and `delete_task` were exposed to agents.

The hand-tuned server in `mcp_server/` fixes each one. Keep the draft in the repo.
It shows reviewers what changed and why.

## Step 2: Sign agents in with API keys

A customer mints one key per agent in their workspace settings. The key carries the
workspace and an access level. The server stores only a SHA-256 hash of the secret.

`KeyStore.lookup()` is the hook the auth provider calls on every request:

```python
    def lookup(self, presented: str) -> dict[str, Any] | None:
        """APIKeyAuth(lookup=...) hook: the key's identity, or None to refuse it."""
        key_id, _, secret = presented.removeprefix(PREFIX).partition(".")
        if not presented.startswith(PREFIX) or not secret:
            return None
        record = self._load().get(key_id)
        if record is None or record.get("revoked_at"):
            return None
        if not secrets.compare_digest(_hash(secret), record["secret_sha256"]):
            return None
        return {
            "client_id": record["client_id"],
            "tenant_id": record["workspace"],
            "roles": record["roles"],
        }
```

(from `mcp_server/keys.py`)

The returned `tenant_id` becomes `ctx.client.tenant_id`. The roles feed the guards.
The server is built with `require_tenant=True`, so no tool runs without a key that
names a workspace. It also sets `hide_unauthorized_tools=True`. A reader key lists
four tools. It never sees the write tools.

## Step 3: Declare who may call each tool

Each tool states its role in the decorator. Reads need `reader`. Writes need
`editor`. Deleting a project needs `admin` plus a person's approval:

```python
    @server.tool(roles=["admin"], requires_approval=True, destructive_hint=True, rate_limit="5/min")
    async def delete_project(ctx: RequestContext, project_id: ProjectId) -> dict[str, Any]:
        """Permanently delete a project with all its tasks and comments.
        A person in the workspace must approve it first; the call waits for that decision.
        Use archive_project unless the user asked to delete."""
        project = await api.call(ws(ctx), "GET", f"/projects/{project_id}")
        await api.call(ws(ctx), "DELETE", f"/projects/{project_id}")
        return {"deleted": project_id, "name": project["name"]}
```

(from `mcp_server/server.py`)

A reader that calls a write tool gets `ACCESS_DENIED` with the reason:
`Requires any of roles [editor], but client has [reader]`. The handler never runs.

Fields that identify a person come from the key. They never come from the agent:

```python
        # The author comes from the verified key, never from the agent.
        comment = await api.call(
            ws(ctx), "POST", f"/tasks/{task_id}/comments", json={"body": body, "author": f"agent:{ctx.client_id}"}
        )
```

(from `mcp_server/server.py`)

## Step 4: Make deletions wait for a person

`requires_approval=True` only works with an `ApprovalGateMiddleware` installed. The
server refuses to build without one. Here the handler is a `PendingApprover`. A
gated call waits in its store until a reviewer decides. No decision before the
timeout means the call is denied.

The SDK's stock reviewer tools see every pending call on the server. On a
multi-tenant server that would show Acme's pending deletions to Globex's reviewers.
So this project registers its own two tools, scoped to the reviewer's workspace:

```python
        entry = next((p for p in approver.pending() if p["request_id"] == request_id), None)
        # Another workspace's request looks exactly like an unknown one.
        if entry is None or entry["tenant_id"] != ctx.client.tenant_id:
            raise ToolError(f"No pending request {request_id!r} in your workspace.", code="NOT_FOUND")
        if approve and entry["client_id"] == ctx.client_id:
            raise ToolError("You cannot approve a call you made yourself.", code="ACCESS_DENIED")
        resolved = approver.decide(request_id, approve, reviewer_id=ctx.client_id, reason=reason or None)
```

(from `mcp_server/approvals.py`)

The gate checks the tool's guards before it asks anyone. A reader asking to delete
is refused at once. Reviewers never see that request.

## Step 5: Log every call in a tamper-evident chain

The middleware order matters. Audit goes first, so refused calls are logged too:

```python
    # Audit goes first so refused calls (bad key, wrong role, rate limited,
    # approval denied) are in the log too.
    server.add_middleware(audit)
    server.add_middleware(AuthMiddleware(APIKeyAuth(lookup=keys.lookup)))
    server.add_middleware(
        RateLimitMiddleware(TokenBucketLimiter(rate_per_minute=settings.rate_per_minute, burst=settings.rate_burst))
    )
    server.add_middleware(ApprovalGateMiddleware(approver, timeout=settings.approval_timeout))
    register_approval_tools(server, approver)
```

(from `mcp_server/server.py`)

Each entry has the tool, the client id, the workspace, the roles, the arguments and
the outcome. Each entry's HMAC covers the one before it. The key comes from
`PROMPTISE_AUDIT_SECRET`. Check the file at any time:

```bash
promptise audit verify .data/audit.jsonl
```

Edit one field and the check fails at that line. Delete a line and it reports the gap.

## Step 6: Revoke a key while the server runs

`APIKeyAuth(lookup=...)` calls `lookup()` on every request. The key store re-reads
its file whenever the file changes. So a revocation from another process applies to
the next request:

```bash
promptise run keys revoke 1741aaf9
```

The running server answers that key with `401`. Other keys keep working. In
production, point `lookup()` at the table your admin UI writes.

## Step 7: Give each key its own rate limit

`RateLimitMiddleware` runs after `AuthMiddleware`, so each key gets its own bucket.
Write tools add a stricter per-tool limit, such as `rate_limit="30/min"`. An agent
over its limit gets `RATE_LIMIT_EXCEEDED` with a retry hint. Other agents of the
same customer are not affected.

## Step 8: Connect a customer's agent

This is the code a customer writes. It needs the server URL and an agent key:

```python
    return await build_agent(
        model=model,
        instructions=INSTRUCTIONS,
        # The key travels as the x-api-key header on every MCP request.
        servers={"taskboard": HTTPServerSpec(url=url, api_key=api_key)},
    )
```

(from `customer_agent/agent.py`)

Any other MCP client works the same way. With Claude Code:

```bash
claude mcp add taskboard --transport http http://127.0.0.1:8080/mcp --header "x-api-key: <agent key>"
```

## Test the rules offline

The tests use `TestClient`. It runs the full server pipeline in-process: auth, rate
limits, the approval gate, guards, audit and the handler. This test proves a
deletion waits:

```python
    call = asyncio.create_task(h.call("delete_project", {"project_id": "prj_103"}, as_="acme-ops-agent"))

    (pending,) = await h.wait_for_pending()
    assert pending["tool"] == "delete_project"
    assert pending["arguments"] == {"project_id": "prj_103"}
    assert pending["client_id"] == "acme-ops-agent"
    assert not call.done()
    assert "prj_103" in h.store.projects  # nothing deleted yet
```

(from `tests/test_approval.py`)

The suite has 34 tests and runs in about six seconds with no API key.

## Run it

```bash
pip install -r requirements.txt
promptise run demo
```

The demo starts the REST API and the MCP server on a local port. The customer's
agent connects over HTTP. Its model is a `ScriptedModel` from `promptise.testing`,
so it runs without a model key. The server, the API and the tool results are real.

For a real model, run `promptise run setup`, then `api`, `mcp`, `approvals` and
`agent` in four terminals. The README lists the commands.

## Adapt it to your product

- **Your API.** Change `mcp_server/upstream.py` and the tools in `mcp_server/server.py`.
- **Your key store.** Make `KeyStore.lookup()` query your database. Return `None` for revoked keys.
- **Your identity provider.** Swap `APIKeyAuth` for `JwksAuth`. Roles then come from the token.
- **Your review screen.** Call `approvals_list` and `approvals_decide` from your admin UI.

## Pitfalls

**Taking the tenant from a tool argument.** If the agent can pass a workspace id, it
can pass someone else's. Take it from the verified key.

**Gating everything.** Approval on every comment trains reviewers to click yes.
Gate what cannot be undone. Offer a reversible path, such as `archive_project`.

**Trusting identity fields in arguments.** An `author` or `user_id` argument lets an
agent act as anyone. Set those fields on the server.

**Adding audit after auth.** Then refused calls never reach the log. Add it first.

**Expecting the audit log to show every refused key.** With `require_tenant=True`, the
HTTP transport answers a missing or revoked key with `401` before the MCP layer. Those
requests are in your HTTP access log.

**Running several replicas.** Pending approvals and rate-limit buckets live in one
process. Use one replica, or an approval handler backed by a shared store.

## FAQ

**Do I have to start from MCPcast?**
No. It saves the first hour. It also labels each endpoint's risk. The server you
ship is plain Python you can write by hand.

**Can I use JWTs instead of API keys?**
Yes. Swap the provider for `JWTAuth`, `AsymmetricJWTAuth` or `JwksAuth`. They also
support revocation by token id or subject.

**What does the agent see while a deletion waits?**
The tool call stays open until the decision. Approved, it gets the result. Denied,
it gets `APPROVAL_DENIED` with the reviewer's reason.

**Can a workspace owner approve their own agent's deletion?**
Yes, when the agent has its own key. The check compares client ids. A call made with
the owner's personal key needs a second reviewer.

**How do I prove the audit log is complete?**
Run `promptise audit verify` on a schedule. Store the last hash outside the server.
Pass it as `--anchor` later to detect lines cut from the end.

**Does this work with clients that are not Promptise agents?**
Yes. The rules live in the server. Claude, Cursor or any MCP client gets the same
keys, roles, approvals and logs.
