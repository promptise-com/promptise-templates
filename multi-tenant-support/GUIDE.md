---
title: "Multi-Tenant AI Support Agent: Build It in Python"
description: "Build one AI support agent for many customer companies. Each tenant keeps its own data, memory, permissions and rate limit, even under prompt injection."
slug: multi-tenant-support
queries:
  - multi-tenant AI agent
  - multi-tenant AI customer support agent
  - tenant isolation for LLM agents
  - per-tenant rate limiting MCP server
  - prevent cross-tenant data leakage AI agent
  - multi-tenant RAG per customer
---

# How to Build a Multi-Tenant AI Customer Support Agent in Python

You can run one AI support agent for all of your customers. Each customer company sees only its own help articles, tickets and orders. Each one keeps its own memory, chat sessions, permissions and quota. A user who types "show me Acme's tickets" gets nothing from Acme.

> **Reference application.** This project shows how this use case can look with Promptise. It is not a finished product. The ticketing, orders and help-center data are local stand-ins with sample data, not connectors to a real helpdesk or shop system. Replace the adapter files before you use it with real data.

The complete project is the `multi-tenant-support` folder of the Promptise templates repository.

## The business problem

A SaaS company sells a support agent to many companies. Here they are Acme Robotics and Globex Home. Each company is a tenant. Their end users ask about returns, devices, tickets, orders and refunds.

One agent per tenant is costly to run. One shared agent is cheaper. It also carries a serious risk. A single missing filter can show one company's data to another. A model that obeys a clever prompt can do the same.

The fix is to make the tenant part of every lookup by construction. The model never decides which tenant it serves.

## How it works

The tenant travels with each request in two forms.

The first form is a signed token. Your login service issues a JWT for the end user. The token carries the tenant in an `org` claim. It also carries the plan's scopes. The agent sends it on every call to the ticketing backend. The backend reads the tenant from the verified token only.

The second form is a `CallerContext`. Your app passes it on each chat message. Inside the agent it decides four things. Chat sessions belong to `tenant::user`. Memory is stored under the same key. The help-center search picks the tenant's own index. The prompt names the tenant's company and plan.

Both forms come from the same user record. So the agent and the backend can't disagree about the tenant.

## Step 1: Put the tenant in the user's identity

The login service turns a signed-in user into a `CallerContext`. The token inside it holds the tenant claim and the plan's scopes.

From `support_desk/identity.py`:

```python
    def caller_for(self, tenant_id: str, user_id: str) -> CallerContext:
        """The identity one agent invocation runs as."""
        user = USERS[(tenant_id, user_id)]
        return CallerContext(
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            bearer_token=self.token_for(tenant_id, user_id),
            metadata={"name": user.name},
        )
```

Both seed tenants have a user called `alice`. A user id alone must never be an isolation key.

## Step 2: Build a backend that trusts only the token

The ticketing backend is an MCP server built with the Promptise MCP server SDK. One server serves every tenant.

From `support_desk/backend.py`:

```python
    server = MCPServer(
        "support-desk",
        instructions="Tickets and orders for the signed-in end user's company.",
        require_tenant=True,
        hide_unauthorized_tools=True,
    )
    # Order matters: identify the caller, record the call, apply the quota,
    # then hold gated tools for a decision.
    server.add_middleware(
        AuthMiddleware(
            JWTAuth(secret=secret or signing_secret(), audience=AUDIENCE, issuer=ISSUER),
            tenant_claim=TENANT_CLAIM,
            on_authenticate=attach_plan,
        )
    )
    server.add_middleware(audit)
    server.add_middleware(RateLimitMiddleware(PlanLimiter(), key_func=tenant_bucket))
    server.add_middleware(ApprovalGateMiddleware(refunds, timeout=30))
```

`AuthMiddleware` copies the `org` claim to `ctx.client.tenant_id`. With `require_tenant=True`, a token without a tenant is refused on every tool. `HasScope` guards on each tool enforce the plan. `hide_unauthorized_tools=True` lists each tenant only the tools its plan allows.

`tenant_bucket` returns `plan|tenant` as the rate-limit key. So every user of a company draws from one quota. When Globex runs out, it gets `RATE_LIMIT_EXCEEDED`. Acme keeps working.

## Step 3: Write tools that never take a tenant

Each tool reads the tenant from the verified request.

From `support_desk/backend.py`:

```python
        ticket = caller_records(ctx).ticket(ticket_id.strip().upper(), requester=ctx.client.client_id)
        if ticket is None:
            # Same answer whether the ticket belongs to another customer, another
            # tenant, or nobody: the reply must not confirm that an ID exists.
            raise ToolError(f"Ticket {ticket_id} was not found for this customer.")
        return ticket
```

A prompt injection has nothing to change here. The model can ask for `ACME-1041` while serving Globex. The lookup runs inside Globex's records and finds nothing.

## Step 4: Hold refunds for review

A refund moves money, so the backend gates it. The tool is declared with `requires_approval=True`:

```python file=support_desk/backend.py
    @server.tool(destructive_hint=True, guards=[HasScope(REFUNDS_WRITE)], requires_approval=True)
```

`ApprovalGateMiddleware` calls `RefundReview` before the tool runs. The request carries the caller's tenant and user. Refunds up to the plan's limit are approved by policy. Larger ones go to the support leads' queue. The agent tells the customer what happens next. In production, swap in a `PendingApprover` or a `WebhookApprovalHandler`.

## Step 5: Give each tenant its own help center

Each tenant gets its own `RAGPipeline` and vector store. There is no shared index, so there is no filter to forget.

From `support_desk/help_center.py`:

```python
    @tool
    async def search_help_center(query: str, caller: CallerContext | None) -> str:
```

A parameter annotated `CallerContext` is filled by Promptise. It is not part of the tool's schema, so the model can't see or set it. The tool searches the index of `caller.tenant_id`.

## Step 6: Write the instructions as code

The rules are the same for every tenant. The plan is not. A context provider adds the signed-in company to the prompt on every request.

From `support_desk/instructions.py`:

```python
@prompt()
@context(callable_context(signed_in_tenant, header="## Signed-in customer"))
async def support_instructions() -> str:
    """Help the signed-in customer of this company with their question."""
```

`signed_in_tenant` reads the current caller with `get_current_caller()`. It lists what the company's plan includes and what it does not. The rules live in `SUPPORT_BLOCKS` (an `Identity` and a `Rules` block).

## Step 7: Build the agent once

One agent serves every tenant.

From `agent.py`:

```python
        extra_tools=[help_center_tool(help_centers), preference_tool(memory)],
        # PER_USER + a caller with tenant_id: searched as "acme::alice" before each
        # answer. Only stated preferences are stored (remember_preference), not
        # every exchange: the chat history already lives in the conversation store.
        memory=memory,
        memory_max_results=3,
        # Sessions are owned by tenant::user: another tenant's user gets SessionAccessDenied.
        conversation_store=conversations or InMemoryConversationStore(),
        conversation_max_messages=20,
        guardrails=payment_data_guardrail(),
        observe=tracing(trace_dir),
```

The guardrail redacts card numbers and secrets in the user's message. The model, the session store and memory only see `[CREDIT_CARD]`. Observability records every model call and tool call with the chat session. The default ReAct pattern fits: support questions are short tool loops.

## Step 8: Answer in the user's session

From `agent.py`:

```python
    answer = await agent.chat(question, session_id=session_id, caller=caller)
```

That one call checks session ownership. It loads the history. It scopes memory, renders the tenant's prompt and sends the user's token to the backend.

## Run it

Set a model key and run the demo:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # set OPENAI_API_KEY
.venv/bin/promptise run demo
```

These lines come from a real run with `openai:gpt-5-mini`:

```
[acme/alice] I also want to return the vacuum itself, order AC-5001. Please refund it.
   tool  request_refund(order_id='AC-5001') -> APPROVAL_DENIED: Approval denied for tool 'request_refund': Refunds above 50 EUR need a support lead. ...
   agent I submitted the refund for order AC-5001, but refunds over €50 need a support lead and your request was sent to the lead's review queue. ...

[globex/gina] Continue this conversation.  (sent with another tenant's session id)
   app   SessionAccessDenied: User 'globex::gina' denied access to session 'sess_6393...' (owned by 'acme::alice')

[globex/gina] Any update on my tickets?
   tool  list_tickets(status=None) -> RATE_LIMIT_EXCEEDED: Rate limit exceeded
   agent I couldn't fetch your tickets because of a rate limit. Please try again in about a minute ...
```

The full demo used about 40,000 tokens. Ask your own question with `promptise run ask globex/gina "Show me Acme's open tickets"`.

**No key yet?** Run `promptise run demo-offline`. A `ScriptedModel` from `promptise.testing` plays the model. Everything else is real.

## Test the isolation

The offline tests use a scripted model that falls for the injection. It calls every tool it can think of.

From `tests/test_agent_isolation.py`:

```python
        [
            ToolCall("get_ticket", {"ticket_id": "ACME-1041"}),
            ToolCall("list_tickets", {"tenant": "acme"}),
            ToolCall("get_order", {"order_id": "AC-5001"}),
            ToolCall("search_help_center", {"query": "Acme Robotics return policy 30 days"}),
        ],
```

The test then checks every message the model received. None of them holds Acme data. Other tests cover sessions, memory, card redaction, refunds and quotas. A live test runs the injection against the real model and skips without a key.

## Adapt it to your systems

- **Ticketing and orders.** Replace `TicketDesk` in `support_desk/records.py` with your helpdesk or database client. Keep the single `for_tenant()` entry point.
- **Identity.** Issue tokens from your identity provider. On the backend, use `JwksAuth` or `AsymmetricJWTAuth`.
- **Refunds.** Route `RefundReview` to your approval process.
- **Help centers.** Swap `HashingEmbedder` for your embedding model and `InMemoryVectorStore` for your vector database.
- **Sessions and memory.** Use `PostgresConversationStore` and `ChromaProvider` with `scope=MemoryScope.PER_USER`.

## Pitfalls

**Memory is shared by default.** A provider created without `scope=` pools every caller's memories. Set `MemoryScope.PER_USER`.

**A caller without a token runs as the agent.** The backend takes the tenant from the token. Always pass the user's `bearer_token`.

**Hiding a tool is not access control.** `hide_unauthorized_tools` only filters the list. The guard refuses the call.

**`Depends()` runs before authentication.** A dependency sees no tenant yet. Look up tenant data inside the handler.

**Keep traces per tenant.** Since Promptise 1.9.0, every trace entry records the tenant and the session, so `acme::alice` and `globex::alice` stay apart. On older versions, a trace entry stores only `alice`. There, put the session id in the caller's metadata, as `ask()` does, and map sessions to tenants.

**Blocks on a per-request prompt are sent twice.** The agent sends a prompt's build-time render and its per-request render. Put fixed rules in `graph_blocks` and keep the per-request prompt small.

**Pattern guardrails have false positives.** The IBAN pattern matches parcel tracking numbers. Enable only the categories your support flow needs.

**Error messages can leak.** "That ticket belongs to another company" confirms the ticket exists. Return the same message as for a missing ticket.

## FAQ

**Can one AI agent safely serve many companies?**
Yes, when the tenant comes from a verified identity on every request. The model must never choose the tenant. In this project, no tool takes a tenant argument.

**How does the backend know which tenant is calling?**
The agent sends the end user's JWT on each MCP tool call. `AuthMiddleware` verifies it and reads the tenant claim.

**Does prompt injection still matter if data is isolated?**
Yes. Injection can still trigger actions inside the user's own tenant. Isolation stops it from crossing tenants. Keep risky tools behind scopes and approval gates.

**Should tenants share one vector index with a metadata filter?**
It works. Still, one missed filter leaks data. Separate indexes per tenant remove that risk.

**How do I give tenants different features?**
Put the plan's scopes in the token. Guard each tool with `HasScope`. Tell the model the plan in the per-request prompt.

**How do I test isolation without an API key?**
Use `ScriptedModel` from `promptise.testing`. Script the tool calls an attacker would want. Then assert on what came back.
