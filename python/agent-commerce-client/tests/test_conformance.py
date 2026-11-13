"""Conformance runner: every config, discovery and invocation scenario, sync and async"""

import asyncio
import base64
import json
import logging
from decimal import Decimal

import httpx
import pytest
from agent_commerce_client import AgentCommerceClient, AgentCommerceError, MemorySpendStore
from agent_commerce_client import client as core
from helpers import DAY, SUITE, Gateway, build_config, fixture

PATHS = ["sync", "async"]
# The expectation keys this runner checks; a scenario with another key must not pass unchecked
DISCOVERY_KEYS = {"tools", "toolNames", "needsApproval", "skipped", "error"}
INVOCATION_KEYS = {"requests", "signatures", "signedOffer", "result", "error", "spentAfter"}


def make_client(scenario, gateway, monkeypatch):
    monkeypatch.setattr(core, "_today", lambda: DAY)
    store = MemorySpendStore()
    client = AgentCommerceClient(
        **build_config(scenario["config"], spend_store=store, transport=httpx.MockTransport(gateway))
    )
    return client, store


def call(path, client, method, *args):
    # Run the sync form, or the async form on a fresh event loop
    if path == "sync":
        return getattr(client, method)(*args)
    return asyncio.run(getattr(client, "a" + method)(*args))


def public(tool):
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
        "paid": tool.paid,
        "needsApproval": tool.needs_approval,
    }


def micro(decimal_string):
    return int(Decimal(decimal_string) * 1_000_000)


@pytest.mark.parametrize("scenario", SUITE["config"], ids=lambda s: s["name"])
def test_config(scenario):
    config = build_config(scenario["config"])
    if scenario["expect"] == "ok":
        AgentCommerceClient(**config)
    else:
        with pytest.raises((ValueError, TypeError)):
            AgentCommerceClient(**config)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("scenario", SUITE["discovery"], ids=lambda s: s["name"])
def test_discovery(scenario, path, monkeypatch, caplog):
    if "resources" in scenario:
        responses = [{"status": 200, "body": fixture(scenario["resources"])}]
    else:
        responses = [scenario["response"]]
    gateway = Gateway(responses)
    client, _ = make_client(scenario, gateway, monkeypatch)
    expect = scenario["expect"]
    assert set(expect) <= DISCOVERY_KEYS
    assert set(scenario) <= {"name", "config", "resources", "response", "expect"}

    with caplog.at_level(logging.WARNING, logger="agent_commerce_client"):
        if "error" in expect:
            with pytest.raises(AgentCommerceError) as raised:
                call(path, client, "list_tools")
            assert raised.value.code == expect["error"]["code"]
            assert raised.value.status == expect["error"].get("status")
            assert raised.value.message.startswith(f"{raised.value.code}: ")
            return
        tools = call(path, client, "list_tools")

    assert len(gateway.requests) == 1
    assert gateway.requests[0].method == "GET"
    assert str(gateway.requests[0].url) == SUITE["defaults"]["gatewayUrl"] + "/api/resources"
    if "tools" in expect:
        assert [public(t) for t in tools] == expect["tools"]
    if "toolNames" in expect:
        assert [t.name for t in tools] == expect["toolNames"]
    if "needsApproval" in expect:
        assert {t.name: t.needs_approval for t in tools} == expect["needsApproval"]
    if "skipped" in expect:
        warned = [
            r.getMessage() for r in caplog.records if r.name == "agent_commerce_client" and r.levelno == logging.WARNING
        ]
        # One warning per skipped id, in gateway order, each naming its id in double quotes
        assert len(warned) == len(expect["skipped"])
        for message, skipped in zip(warned, expect["skipped"]):
            assert f'"{skipped}"' in message


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("scenario", SUITE["invocations"], ids=lambda s: s["name"])
def test_invocation(scenario, path, monkeypatch):
    gateway = Gateway(scenario["responses"])
    client, store = make_client(scenario, gateway, monkeypatch)
    if "spentToday" in scenario:
        store.add(DAY, micro(scenario["spentToday"]))
    expect = scenario["expect"]
    invoked = scenario["call"]
    assert set(expect) <= INVOCATION_KEYS
    assert set(scenario) <= {"name", "config", "spentToday", "call", "responses", "expect"}

    if "error" in expect:
        with pytest.raises(AgentCommerceError) as raised:
            call(path, client, "invoke", invoked["resource"], invoked["input"])
        assert raised.value.code == expect["error"]["code"]
        assert raised.value.status == expect["error"].get("status")
        # The message starts with the code, stays short and does not echo the backend body or any key
        assert raised.value.message.startswith(f"{raised.value.code}: ")
        assert str(raised.value) == raised.value.message
        assert len(raised.value.message) < 200
        assert SUITE["testSignerPrivateKey"][2:] not in str(raised.value)
        for part in expect["error"].get("messageContains", []):
            assert part in raised.value.message
        for part in expect["error"].get("messageExcludes", []):
            assert part not in raised.value.message
    else:
        result = call(path, client, "invoke", invoked["resource"], invoked["input"])
        assert result.status == expect["result"]["status"]
        assert result.body == expect["result"]["body"]
        payment = expect["result"]["payment"]
        assert result.payment == (None if payment is None else fixture(payment))

    # No request past the scripted responses, and the counts the scenario expects
    assert len(gateway.requests) == expect["requests"]
    assert len(gateway.signatures) == expect["signatures"]
    assert all(
        r.method == "POST" and r.url.path == f"/api/resources/{invoked['resource']}/invoke" for r in gateway.requests
    )
    assert all(json.loads(r.content) == invoked["input"] for r in gateway.requests)
    if "signedOffer" in expect:
        signed = json.loads(base64.b64decode(gateway.signatures[0].headers["payment-signature"]))
        offer = signed["accepted"]
        assert {k: offer[k] for k in expect["signedOffer"]} == expect["signedOffer"]
    assert store.add(DAY, 0) == micro(expect["spentAfter"])
