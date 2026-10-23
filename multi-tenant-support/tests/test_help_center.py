"""Each tenant searches only its own help articles."""

from __future__ import annotations

import asyncio

from support_desk.help_center import HelpCenters


def search(tenant_id: str, query: str):
    async def scenario():
        centers = HelpCenters()
        await centers.index()
        return await centers.search(tenant_id, query)

    return asyncio.run(scenario())


def test_the_same_question_finds_each_tenants_own_policy():
    acme = search("acme", "how long can I return a product for a refund")
    globex = search("globex", "how long can I return a product for a refund")
    assert acme[0]["source"] == "returns-and-refunds.md" and "30 days" in acme[0]["text"]
    assert globex[0]["source"] == "returns.md" and "14 days" in globex[0]["text"]


def test_results_never_cross_tenants():
    acme_sources = {h["source"] for h in search("acme", "warranty error code battery wifi schedule")}
    globex_sources = {h["source"] for h in search("globex", "warranty error code battery wifi schedule")}
    assert acme_sources <= {
        "returns-and-refunds.md",
        "base-station-reset.md",
        "battery-life.md",
        "warranty.md",
    }
    assert globex_sources <= {"returns.md", "error-codes.md", "schedules.md", "warranty.md"}


def test_unknown_tenant_gets_nothing():
    assert search("initech", "returns") == []
