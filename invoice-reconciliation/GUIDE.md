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

The example business is Lumen Metrics, a made-up subscription SaaS. The complete project
runs offline in one command:
[invoice-reconciliation](https://github.com/promptise-com/promptise-templates/tree/main/invoice-reconciliation).

## The business problem

A subscription business takes thousands of card payments a month. Most match their
invoice. Some do not. A retry charges a customer twice. A coupon is applied at checkout
but not on the invoice. Someone marks an invoice paid by hand. A bank transfer arrives
with no reference.

Finance usually finds these at month end, in a spreadsheet. By then the double-charged
customer has written to support. The failed payment has turned into churn.

Matching invoices to payments is arithmetic. The work around it is judgement and
writing. Someone has to decide what each mismatch means. Someone has to tell the
customer. Someone has to leave a clear note for the person who closes the books. That
part fits an agent.

## How it works

The agent runs as one process in the Promptise Agent Runtime. Two triggers wake it.

A cron trigger fires at 04:30 Berlin time. The agent calls `reconcile_day` for the day
before. It opens a finance note for every mismatch that is not a double charge. Then it
refunds the duplicate payments and emails each customer.

A webhook trigger takes Stripe's `invoice.payment_failed` events. The agent checks the
invoice and the customer in the billing system. It emails the customer a payment link.
It opens a note for finance.

The agent talks to two MCP servers. The billing server holds invoices, payments, refunds
and notes. The email server sends templated mail. In the repo both are local fakes with
seed data. In production each one fronts a real service.

## Step 1: Keep the matching in plain code

Finding a mismatch should give the same answer every night. So `reconcile_day` is plain
Python, not a prompt. It compares each invoice with its payments and refunds. A refunded
payment no longer counts.

From `fake_systems/reconcile.py`:

```python file=fake_systems/reconcile.py
        full_charges = [p for p in live if net[p["id"]] == expected]
        if received > expected and len(full_charges) >= 2 and received % expected == 0:
            duplicates = full_charges[1:]
            mismatches.append(
                Mismatch(
                    kind="double_charge",
                    detail=f"Charged {len(full_charges)} times for one invoice.",
                    duplicate_payment_ids=[p["id"] for p in duplicates],
                    refundable_cents=sum(net[p["id"]] for p in duplicates),
                    **base,
                )
            )
```

The report names the exact payments to refund. The agent never has to work out which
charge was the extra one. Amounts are integer cents, as in Stripe.

The seed data plants nine mismatches for one day. There are five double charges, an
underpayment and an invoice marked paid with no payment. There is also a failed card
payment and a bank transfer that matches no invoice. The tests check that the matcher
finds exactly these.

## Step 2: Wake the agent at night and on Stripe events

Both triggers go in one `ProcessConfig`. From `finance_agent/process.py`:

```python file=finance_agent/process.py
        triggers=[
            TriggerConfig(
                type="cron",
                cron_expression=settings.cron_expression,
                cron_timezone=settings.cron_timezone,
            ),
            TriggerConfig(
                type="webhook",
                webhook_path="/stripe",
                webhook_port=settings.webhook_port,
                hmac_secret=settings.webhook_secret,
                signature_scheme="stripe",  # checks Stripe-Signature, rejects replays
                # Other Stripe events are answered 200 "ignored" and cost nothing.
                filter_expression="payload['type'] == 'invoice.payment_failed'",
            ),
        ],
```

`signature_scheme="stripe"` checks the `Stripe-Signature` header against your endpoint
secret. A request with a wrong signature gets `401`. A replayed request gets `401` too.
The filter drops every other event type before the model runs.

The schedule uses 04:30 on purpose. Jobs between 01:00 and 03:00 local time run twice or
shift on daylight saving nights.

The runtime hands the webhook body to the agent as untrusted data inside a delimited
block. The instructions tell the agent to check the invoice and the customer with its
tools first. The next step makes that check hold even if the model skips it.

## Step 3: Put the guard rails in the tools

Some rules belong to the system of record. They should hold for any caller.

The billing server refuses a refund that would leave the invoice underpaid. That is what
makes a "duplicate" refund safe. From `fake_systems/billing_server.py`:

```python file=fake_systems/billing_server.py
        if received - amount_cents < invoice["amount_cents"]:
            raise ToolError(
                f"Refusing: {invoice['id']} would be underpaid after this refund. "
                "This is not a duplicate charge; open a finance note instead.",
                code="NOT_A_DUPLICATE",
            )
```

The email server takes a customer ID and an invoice ID, never an address. It looks up the
address on file. It refuses an invoice that belongs to someone else. From
`fake_systems/email_server.py`:

```python file=fake_systems/email_server.py
        if invoice["customer_id"] != customer_id:
            raise ToolError(
                f"Invoice {invoice_id} does not belong to customer {customer_id}.",
                code="CUSTOMER_MISMATCH",
            )
```

A forged payload that names another customer gets no email. The email server also sends
each template once per invoice, because Stripe retries webhooks.

## Step 4: Cap refunds with an autonomy budget

The budget counts what the agent does in each run. Marking the refund tool irreversible
makes it count toward `max_irreversible_per_run`. From `finance_agent/rules.py`:

```python file=finance_agent/rules.py
def autonomy_budget() -> BudgetConfig:
    """Limits per run and per day.  Only refunds count as irreversible."""
    return BudgetConfig(
        enabled=True,
        max_irreversible_per_run=MAX_REFUNDS_PER_RUN,
        max_tool_calls_per_run=40,
        max_runs_per_day=200,  # a webhook storm stops here, not at the model bill
        tool_costs={
            REFUND_TOOL: ToolCostAnnotation(cost_weight=5.0, irreversible=True),
            "send_customer_email": ToolCostAnnotation(cost_weight=2.0),
        },
        # The run that hits a limit finishes, then the process pauses until
        # someone calls resume().  Webhook events queue up meanwhile.
        on_exceeded="pause",
    )
```

The runtime checks the limit before each tool call. The fourth refund never reaches the
billing server. The agent gets an error as the tool result instead. Calls in the same
model turn are checked one by one. Four parallel refunds still give three.

After that run the process pauses. Four double charges in one night means something
upstream broke. A person should look before the agent goes on.

## Step 5: Send large refunds to a person

The approval gate sits in front of the refund tool. A rule approves small refunds. Every
other refund goes to the reviewer. From `finance_agent/rules.py`:

```python file=finance_agent/rules.py
async def refund_within_auto_limit(request: ApprovalRequest) -> bool:
    """True when a refund is small enough to skip the human reviewer.

    Anything unexpected (a missing or non-integer amount) returns False, so
    the call goes to a person instead of through.
    """
    amount = request.arguments.get("amount_cents")
    return isinstance(amount, int) and 0 < amount <= REFUND_APPROVAL_THRESHOLD_CENTS
```

The rule goes into an `AutoApprovalClassifier`. The classifier's `fallback` is the human
reviewer. The policy in `finance_agent/rules.py` denies the refund when nobody answers in time:

```python file=finance_agent/rules.py
    return ApprovalPolicy(
        tools=[REFUND_TOOL],
        handler=classifier,
        timeout=timeout,
        on_timeout="deny",  # nobody answered: no money moves
        on_decision=decided,
    )
```

While the request waits, the run is parked. No money moves. In the demo the reviewer is
code in the same process reading a `QueueApprovalHandler`. In production you pass a
`WebhookApprovalHandler` and answer from Slack or your back office.

## Step 6: Write the follow-up in code

When the budget refuses a call, the agent is told to stop calling tools. So the agent
cannot be trusted to report what it left undone. A runtime hook writes that note
instead. From `finance_agent/hooks.py`:

```python file=finance_agent/hooks.py
    async def on_permission_denied(ctx: HookContext) -> None:
        data = ctx.data
        if data.get("kind") == "budget" and data.get("tool_name") == REFUND_TOOL:
            note_refund_not_made(
                db_path,
                data.get("arguments") or {},
                f"the per-run limit ({data['limit_name']} = {data['limit_value']}) was reached. "
                "The agent is paused until someone resumes it, and the next reconciliation "
                "lists every double charge still open",
            )
```

A second callback does the same for refunds a person denied. It runs as the approval
policy's `on_decision`.

## Step 7: Trace every step

Three records answer three questions. The trace shows what the agent did. The approval
log shows who decided each refund. The journal shows what was refused before it ran.

The trace is one `ObservabilityConfig` on the process. From `finance_agent/process.py`:

```python file=finance_agent/process.py
        observe=ObservabilityConfig(
            level=ObserveLevel.STANDARD,
            session_name=PROCESS_NAME,
            transporters=[TransporterType.STRUCTURED_LOG],
            log_file=str(settings.trace_dir / "finance-ops.jsonl"),
        ),
```

Each line is one event: a run, an LLM turn, a tool call or a tool result. Tool arguments
are in it. Email addresses and keys are redacted before they are written.

## Test the rules offline

`ScriptedModel` from `promptise.testing` takes the model's place. You write its tool calls
in advance. The process, the MCP servers, the budget and the gate are the real ones. From
`tests/test_refund_limits.py`:

```python file=tests/test_refund_limits.py
def test_the_fourth_refund_in_a_run_is_blocked(make_settings, db):
    model = ScriptedModel([*(refund(p, c) for p, c in SMALL_DUPLICATES), "Done."])
    settings = make_settings(model)
```

The script asks for four refunds. The test then reads the ledger. It finds three refunds,
a paused process and a high-priority note for the fourth. Other tests post signed Stripe
events to the real webhook port. They check that the right customer got the email.

## Run it

The offline demo plays one business day with no API key:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/promptise run demo
```

It prints the webhook answer, the approval request and the budget pause. Then it lists
the refunds, the outbox and the finance notes.

For a real run, set `OPENAI_API_KEY` and `STRIPE_WEBHOOK_SECRET` in `.env`. Then start
it with `promptise run start`. `scripts/send_test_webhook.py` posts a signed sample event
to the running process.

## Adapt it to your systems

Keep the tool names and arguments. Change what is behind them.

- **Billing.** Replace the SQLite queries in `fake_systems/billing_server.py` with calls
  to Stripe or your ledger. Keep the underpaid check.
- **Matching.** Teach `fake_systems/reconcile.py` your rules for partial payments,
  credit notes and currencies.
- **Email.** Swap the outbox insert in `fake_systems/email_server.py` for your email
  provider.
- **Approvals.** Set `APPROVAL_WEBHOOK_URL` to use your approval service.
- **Limits.** Change the threshold and the refunds per run in `finance_agent/rules.py`.

## Pitfalls

- **Rules in the prompt only.** A model can be talked out of an instruction. Put each
  limit where the runtime or the tool enforces it.
- **Letting the model type email addresses.** Pass IDs and look up the address in the
  service that owns it.
- **Reporting through the model after a limit.** The budget tells the model to stop. Write
  the follow-up in a hook.
- **Approvals that block the queue.** The process runs one event at a time. A long
  approval timeout at night delays the next webhook by as long.
- **Two replicas.** Every copy with the cron trigger reconciles. Run the scheduled process
  in one place.
- **Floats for money.** Use integer cents from the payment provider to the tool arguments.

## FAQ

### Can the agent refund more than three charges in one night?

No. The fourth refund is refused before it runs. The process then pauses until someone
resumes it. The next night's reconciliation still lists every double charge left open.

### What happens if nobody answers an approval request?

The refund is denied when the timeout ends. No money moves. The agent's code opens a
high-priority note so finance can refund by hand.

### Does the agent trust the Stripe payload?

No. The webhook checks the Stripe signature first. The agent then reads the invoice and
the customer from the billing system. The email server refuses an invoice that belongs
to another customer.

### Can I use a model other than OpenAI?

Yes. Set `FINANCE_AGENT_MODEL` to another provider string, such as
`anthropic:claude-sonnet-4-5`. Set that provider's API key.

### How do I test it without an API key?

Run `promptise run test`. The tests use `ScriptedModel` and need no network.
