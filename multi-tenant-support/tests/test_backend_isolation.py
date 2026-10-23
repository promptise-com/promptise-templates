"""The backend serves each tenant its own data, decided by the token alone."""

from __future__ import annotations

import asyncio

from promptise.mcp.server import JWTAuth, TestClient

from conftest import as_user, call
from support_desk.identity import AUDIENCE, ISSUER, signing_secret


def test_acme_user_gets_only_her_acme_tickets(server, login):
    async def scenario():
        alice = as_user(server, login.token_for("acme", "alice"))
        return await call(alice, "list_tickets")

    tickets = asyncio.run(scenario())
    assert {t["ticket_id"] for t in tickets} == {"ACME-1041", "ACME-1042", "ACME-1039"}
    # Ben's ticket is in the same tenant but not hers.
    assert all(t["requester"] == "alice" for t in tickets)


def test_same_user_id_in_two_tenants_sees_two_different_accounts(server, login):
    async def scenario():
        acme_alice = as_user(server, login.token_for("acme", "alice"))
        globex_alice = as_user(server, login.token_for("globex", "alice"))
        return await call(acme_alice, "list_tickets"), await call(globex_alice, "list_tickets")

    acme, globex = asyncio.run(scenario())
    assert [t["ticket_id"] for t in globex] == ["GLX-2207"]
    assert not {t["ticket_id"] for t in acme} & {t["ticket_id"] for t in globex}


def test_globex_cannot_read_an_acme_ticket_by_id(server, login):
    async def scenario():
        gina = as_user(server, login.token_for("globex", "gina"))
        return await call(gina, "get_ticket", {"ticket_id": "ACME-1041"}), await call(
            gina, "get_ticket", {"ticket_id": "GLX-9999"}
        )

    other_tenant, nonexistent = asyncio.run(scenario())
    # The reply for another tenant's ticket looks exactly like a ticket that does not exist.
    assert other_tenant["error"]["code"] == nonexistent["error"]["code"] == "TOOL_ERROR"
    assert other_tenant["error"]["message"] == "Ticket ACME-1041 was not found for this customer."
    assert "stops after 10 minutes" not in str(other_tenant)


def test_a_tenant_argument_cannot_switch_tenants(server, login):
    async def scenario():
        gina = as_user(server, login.token_for("globex", "gina"))
        return await call(gina, "list_tickets", {"tenant": "acme", "tenant_id": "acme"})

    tickets = asyncio.run(scenario())
    assert {t["ticket_id"] for t in tickets} == {"GLX-2201", "GLX-2204"}


def test_a_new_acme_ticket_is_invisible_to_globex(server, login):
    async def scenario():
        alice = as_user(server, login.token_for("acme", "alice"))
        ticket = await call(
            alice,
            "open_ticket",
            {"subject": "Dock light blinks orange", "description": "The base station blinks orange all day."},
        )
        gina = as_user(server, login.token_for("globex", "gina"))
        return ticket, await call(gina, "get_ticket", {"ticket_id": ticket["ticket_id"]})

    ticket, seen_by_globex = asyncio.run(scenario())
    assert ticket["ticket_id"] == "ACME-1044"
    assert seen_by_globex["error"]["code"] == "TOOL_ERROR"


def test_tool_permissions_follow_the_tenants_plan(server, login):
    async def scenario():
        acme = as_user(server, login.token_for("acme", "alice"))
        globex = as_user(server, login.token_for("globex", "gina"))
        acme_tools = {t.name for t in await acme.list_tools()}
        globex_tools = {t.name for t in await globex.list_tools()}
        return acme_tools, globex_tools, await call(globex, "get_order", {"order_id": "GX-7003"})

    acme_tools, globex_tools, denied = asyncio.run(scenario())
    assert "get_order" in acme_tools
    # Globex's trial plan has no orders:read scope: the tool is hidden and refused.
    assert "get_order" not in globex_tools
    assert denied["error"]["code"] == "ACCESS_DENIED"
    assert "acme" not in denied["error"]["message"].lower()


def test_forged_and_tenantless_tokens_are_refused(server):
    forged = JWTAuth(secret="not-the-real-secret", audience=AUDIENCE, issuer=ISSUER).create_token(
        {"sub": "mallory", "org": "acme", "scope": "tickets:read"}
    )
    no_tenant = JWTAuth(secret=signing_secret(), audience=AUDIENCE, issuer=ISSUER).create_token(
        {"sub": "mallory", "scope": "tickets:read"}
    )

    async def scenario():
        return (
            await call(as_user(server, forged), "list_tickets"),
            await call(as_user(server, no_tenant), "list_tickets"),
            await call(TestClient(server), "list_tickets"),
        )

    forged_reply, tenantless_reply, anonymous_reply = asyncio.run(scenario())
    assert forged_reply["error"]["code"] == "AUTHENTICATION_ERROR"
    # require_tenant=True puts a RequireTenant guard on every tool.
    assert tenantless_reply["error"]["code"] == "ACCESS_DENIED"
    assert tenantless_reply["error"]["details"]["guard"] == "RequireTenant"
    assert anonymous_reply["error"]["code"] == "AUTHENTICATION_ERROR"


def test_the_agents_own_credential_reads_no_customer_data(server, login):
    async def scenario():
        service = as_user(server, login.service_token())
        return {t.name for t in await service.list_tools()}, await call(service, "list_tickets")

    tools, reply = asyncio.run(scenario())
    assert tools == {"list_tickets", "get_ticket", "open_ticket", "get_order"}
    assert reply["error"]["message"] == "This organisation has no support workspace."
