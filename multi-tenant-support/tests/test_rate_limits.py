"""Each tenant has its own quota, sized by its plan."""

from __future__ import annotations

import asyncio

from conftest import as_user, call
from support_desk.tenants import PLANS


def test_tenant_over_its_quota_is_refused_while_the_other_keeps_working(server, login):
    trial_burst = PLANS["trial"].burst

    async def scenario():
        gina = as_user(server, login.token_for("globex", "gina"))
        alice = as_user(server, login.token_for("acme", "alice"))
        globex_replies = [await call(gina, "list_tickets") for _ in range(trial_burst + 1)]
        acme_replies = [await call(alice, "list_tickets") for _ in range(trial_burst + 1)]
        return globex_replies, acme_replies

    globex, acme = asyncio.run(scenario())
    assert all(isinstance(r, list) for r in globex[:trial_burst])
    refused = globex[trial_burst]
    assert refused["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert refused["error"]["retryable"] is True
    # Acme's business plan quota is separate and much larger.
    assert all(isinstance(r, list) for r in acme)


def test_the_quota_is_shared_by_every_user_of_a_tenant(server, login):
    async def scenario():
        gina = as_user(server, login.token_for("globex", "gina"))
        for _ in range(PLANS["trial"].burst):
            await call(gina, "list_tickets")
        # A different Globex user draws from the same, now empty, bucket.
        globex_alice = as_user(server, login.token_for("globex", "alice"))
        acme_alice = as_user(server, login.token_for("acme", "alice"))
        return await call(globex_alice, "list_tickets"), await call(acme_alice, "list_tickets")

    globex_alice, acme_alice = asyncio.run(scenario())
    assert globex_alice["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    # Same user_id "alice", other tenant: unaffected.
    assert isinstance(acme_alice, list)
