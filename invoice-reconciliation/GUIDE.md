---
title: "How to Build an Invoice Reconciliation AI Agent in Python"
description: "Build an agent that reconciles invoices against payments every night, follows up failed Stripe payments and refunds double charges within hard limits."
slug: invoice-reconciliation
queries:
  - invoice reconciliation agent
  - automate payment reconciliation python
  - stripe invoice.payment_failed webhook automation
  - ai agent refund approval
  - limit ai agent refunds
  - ai agent for finance operations
---

# How to Build an Invoice Reconciliation AI Agent in Python

By the end of this guide, an agent checks yesterday's invoices against payments every
night. It opens a note for each mismatch a person must look at. It refunds clear double
charges by itself. When a card payment fails, it emails the customer a link to pay.

The limits live in code. The agent makes at most three refunds per run. A refund above
100 EUR waits for a person. Every step lands in a trace.

> **Reference application.** This is not a finished product. It shows how this use case
> can look with Promptise. The billing system and the email service are local stand-ins
> with sample data, not connectors to Stripe or an email provider. Replace the adapter
> files before you use it with real data.

The complete project is on GitHub:
[invoice-reconciliation](https://github.com/promptise-com/promptise-templates/tree/main/invoice-reconciliation).

## The business problem

A subscription business takes thousands of card payments a month. Most match their
invoice. Some do not. A retry charges a customer twice. A coupon is applied at checkout
but not on the invoice. Someone marks an invoice paid by hand.

Finance usually finds these at month end, in a spreadsheet. By then the double-charged
customer has written to support. The failed payment has turned into churn.

Matching invoices to payments is arithmetic. The work around it is judgement and
writing. Someone decides what each mismatch means. Someone tells the customer. Someone
leaves a clear note for the person who closes the books. That part fits an agent.

## How it works

The agent runs as one process in the Promptise Agent Runtime. Two triggers wake it. A
cron trigger fires at 04:30 Berlin time. A webhook trigger takes Stripe's
`invoice.payment_failed` events.

The agent talks to two MCP servers built with the Promptise MCP server SDK. The billing
server holds invoices, payments, refunds and notes. The email server sends templated
mail. Both check an API key and a role on every call. Both write a signed audit log.

## Step 1: Keep the matching in plain code

Finding a mismatch should give the same answer every night. So `reconcile_day` is plain
Python, not a prompt. It names the exact payments to refund. From
`fake_systems/reconcile.py`:

```python file=fake_systems/reconcile.py
        full_charges = [p for p in live if net[p["id"]] == expected]
        if received > expected and len(full_charges) >= 2 and received % expected == 0:
            duplicates = full_charges[1:]
```

The sample data plants nine mismatches for one day. Five are double charges. The others
are an underpayment, an invoice marked paid with no payment, a failed card payment and a
bank transfer that matches no invoice.

## Step 2: Wake the agent at night and on Stripe events

Both triggers go in one `ProcessConfig`. From `finance_agent/process.py`:

```python file=finance_agent/process.py
            TriggerConfig(
                type="webhook",
                webhook_path="/stripe",
                webhook_port=settings.webhook_port,
                hmac_secret=settings.webhook_secret,
                signature_scheme="stripe",  # checks Stripe-Signature, rejects replays
                # Other Stripe events are answered 200 "ignored" and cost nothing.
                filter_expression="payload['type'] == 'invoice.payment_failed'",
            ),
```

A wrong signature gets `401`. A replayed request gets `401` too. The filter drops every
other event type before the model runs. The cron trigger next to it uses 04:30, because
jobs between 01:00 and 03:00 run twice or shift on daylight saving nights.

## Step 3: Lock down the tools with auth, roles and an audit log

The billing server refuses any call without a valid key. Each tool names the role it
needs. From `fake_systems/billing_server.py`:

```python file=fake_systems/billing_server.py
    server.tool(roles=[READ], read_only_hint=True)(get_customer)
    server.tool(roles=[REFUNDS], destructive_hint=True, idempotent_hint=False)(
        refund_duplicate_charge
    )
```

The agent's key has the refund role. A read-only dashboard key does not. The audit
middleware chains each entry to the one before with an HMAC. `verify_audit_log` finds any
entry edited or deleted later.

The tools also guard the business rules. The billing server refuses a refund that would
leave the invoice underpaid. From `fake_systems/billing_server.py`:

```python file=fake_systems/billing_server.py
        if received - amount_cents < invoice["amount_cents"]:
            raise ToolError(
                f"Refusing: {invoice['id']} would be underpaid after this refund. "
                "This is not a duplicate charge; open a finance note instead.",
                code="NOT_A_DUPLICATE",
            )
```

The email server takes a customer ID and an invoice ID, never an address. It refuses an
invoice that belongs to another customer.

## Step 4: Cap refunds with an autonomy budget

Marking the refund tool irreversible makes it count toward `max_irreversible_per_run`.
From `finance_agent/rules.py`:

```python file=finance_agent/rules.py
        max_irreversible_per_run=MAX_REFUNDS_PER_RUN,
        max_tool_calls_per_run=40,
        max_runs_per_day=200,  # a webhook storm stops here, not at the model bill
        tool_costs={
            REFUND_TOOL: ToolCostAnnotation(cost_weight=5.0, irreversible=True),
```

The runtime checks the limit before each tool call. The fourth refund never reaches the
billing server. Calls in one model turn are checked one by one. After that run the
process pauses until finance resumes it.

## Step 5: Send large refunds to a person

A rule approves small refunds. Every other refund goes to the reviewer. From
`finance_agent/rules.py`:

```python file=finance_agent/rules.py
    amount = request.arguments.get("amount_cents")
    return isinstance(amount, int) and 0 < amount <= REFUND_APPROVAL_THRESHOLD_CENTS
```

The rule sits in an `AutoApprovalClassifier`. Its fallback is the person. In the repo the
person answers at the terminal. In production you pass a `WebhookApprovalHandler`. When
nobody answers in time, the refund is denied.

## Step 6: Write the instructions as code

The prompt is built from blocks. The threshold comes from the same constant the gate
uses. From `finance_agent/prompt.py`:

```python file=finance_agent/prompt.py
                f"Refunds above {threshold:.2f} EUR wait for a person to approve them. "
                "Call the refund tool for them anyway.",
```

The refund count per run is not in the prompt, on purpose. In the first real run, a model
told "three" stopped at three. It never reported the fourth double charge. Now the model
asks for every refund. The runtime refuses the extra ones. Code writes the note.

## Step 7: Give the agent a memory of follow-ups

The agent keeps a short list of the emails it sent. From `finance_agent/process.py`:

```python file=finance_agent/process.py
            initial_state={"followups": []},
            writable_keys=["followups"],
            state_tool=True,
```

The runtime shows the list at the start of every run. The journal keeps it across
restarts. The agent may write only this one key. In the real run, the night run saw that the
customer had already been emailed. Its note said so.

## Step 8: Write the follow-up in code

When the budget refuses a call, the agent is told to stop calling tools. So a runtime
hook writes the note instead. From `finance_agent/hooks.py`:

```python file=finance_agent/hooks.py
        if data.get("kind") == "budget" and data.get("tool_name") == REFUND_TOOL:
```

The approval policy's `on_decision` does the same for refunds a person denied.

Three records answer three questions. The trace shows what the agent did. The approval
log shows who decided each refund. The journal shows each trigger and what was refused.

## Run it

Set a model key in `.env`, for example `OPENAI_API_KEY`. Set `STRIPE_WEBHOOK_SECRET` and
`PROMPTISE_AUDIT_SECRET` too. Then start the application:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/promptise run reconcile
```

Send the sample Stripe event from a second terminal with `promptise run webhook`. Type
`y` when the terminal asks about the 490 EUR refund. This is part of a real run with
`openai:gpt-5-mini`:

```text
[approval needed] finance-ops wants to call refund_duplicate_charge: refund 490.00 EUR on payment ch_2041b (Duplicate charge refund for in_2041)
  ✓ refund_duplicate_charge 230ms  → {"refund_id": "re_0004", "payment_id": "ch_2041b", "invoice_id": "in_2041", "customer_id": "cus_PzR02mt", "amount": "490.00 EUR", "status": "refunded"}
◂ finance-ops done · 67.86s · 9 llm call(s) · 13 tool call(s) · 36957 tok
Budget violation: max_irreversible_per_run (limit=3, current=4), action=pause
audit log billing: 12 entries, intact
```

The model asked for five refunds. Three went through. Two were refused and noted. The day
cost about 0.03 USD. No key yet? `promptise run demo-offline` plays the same day with a
scripted model.

## Adapt it to your systems

Keep the tool names and arguments. Change what is behind them.

- **Billing.** Replace the SQLite queries in `fake_systems/billing_server.py` with calls
  to Stripe or your ledger. Keep the underpaid check.
- **Email.** Swap the outbox insert in `fake_systems/email_server.py` for your provider.
- **Identity.** Swap `APIKeyAuth` in `fake_systems/security.py` for `JwksAuth`.
- **Approvals.** Set `APPROVAL_WEBHOOK_URL` to use your approval service.

## Pitfalls

- **Rules in the prompt only.** A model can be talked out of an instruction. Put each
  limit where the runtime or the tool enforces it.
- **Limits stated in the prompt.** A model told the cap stops early and stays silent about
  the rest. Let the code refuse and report.
- **Letting the model type email addresses.** Pass IDs and look up the address in the
  service that owns it.
- **Default reasoning effort.** The first real day cost 0.11 USD at the default effort.
  Low effort did the same work for 0.03 USD.
- **Two replicas.** Every copy with the cron trigger reconciles. Run one.
- **Floats for money.** Use integer cents from the payment provider to the tool arguments.

## FAQ

### Can the agent refund more than three charges in one night?

No. The fourth refund is refused before it runs. The process pauses until someone resumes
it. The next reconciliation still lists every double charge left open.

### What happens if nobody answers an approval request?

The refund is denied when the timeout ends. No money moves. Code opens a high-priority
note so finance can refund by hand.

### Does the agent trust the Stripe payload?

No. The webhook checks the Stripe signature first. The agent then reads the invoice and
the customer from the billing system. The email server refuses another customer's invoice.

### Can I use a model other than OpenAI?

Yes. Set `FINANCE_AGENT_MODEL` to another provider string, such as
`anthropic:claude-sonnet-4-5`. Set that provider's API key.

### How do I test it?

`promptise run test` runs 36 offline tests with a scripted model. `promptise run
test-live` runs one real day with your key.
