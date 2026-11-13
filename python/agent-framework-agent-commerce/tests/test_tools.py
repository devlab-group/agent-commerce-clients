"""Mapping tests: tool type, name, description, schema and approval. No payment logic lives here."""

import base64
import json
from pathlib import Path

import httpx
import pytest
from agent_framework import (
    Agent,
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    FunctionTool,
    Message,
)
from agent_framework_agent_commerce import agent_commerce_tools
from eth_account import Account

CONFORMANCE = Path(__file__).resolve().parents[3] / "conformance"
SUITE = json.loads((CONFORMANCE / "scenarios.json").read_text())
RESOURCES = json.loads((CONFORMANCE / "fixtures" / "resources.json").read_text())
PAYMENT_REQUIRED = (CONFORMANCE / "fixtures" / "payment-required.json").read_bytes()


class Gateway:
    """Discovery from the shared fixture; paid invocations answer 402, which the price limit then refuses."""

    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=RESOURCES)
        if request.url.path.endswith("/weather_basic/invoke"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": base64.b64encode(PAYMENT_REQUIRED).decode()})

    def invocations(self):
        return [r for r in self.requests if r.method == "POST"]


async def make_tools(gateway, **overrides):
    return await agent_commerce_tools(
        **{
            "gateway_url": "https://merchant.example.com",
            "signer": Account.from_key(SUITE["testSignerPrivateKey"]),
            "networks": ["eip155:84532"],
            "max_per_call": "0.005",
            "max_per_day": "1.00",
            "transport": httpx.MockTransport(gateway),
            **overrides,
        }
    )


async def test_tools_map_the_core_descriptions():
    tools = await make_tools(Gateway())
    expected = {t["name"]: t for t in SUITE["discovery"][0]["expect"]["tools"]}
    assert [t.name for t in tools] == list(expected)
    for tool in tools:
        assert isinstance(tool, FunctionTool)
        assert tool.description == expected[tool.name]["description"]
        assert tool.parameters() == expected[tool.name]["inputSchema"]
        assert tool.approval_mode == ("always_require" if expected[tool.name]["needsApproval"] else "never_require")


@pytest.mark.parametrize(
    ("approval", "mode"),
    [("always", "always_require"), ("never", "never_require")],
)
async def test_approval_setting_reaches_every_tool(approval, mode):
    tools = await make_tools(Gateway(), approval=approval)
    assert {t.approval_mode for t in tools} == {mode}


async def test_construction_errors_surface_from_the_core():
    with pytest.raises(TypeError):
        await agent_commerce_tools(
            gateway_url="https://x", signer="0xabc", networks=["eip155:84532"], max_per_call="1", max_per_day="1"
        )


async def test_free_tool_returns_the_result_and_a_failing_tool_returns_text():
    tools = {t.name: t for t in await make_tools(Gateway())}
    contents = await tools["weather_basic"].invoke(arguments={"city": "Berlin"})
    assert json.loads(contents[0].text) == {"status": 200, "body": {"ok": True}, "payment": None}
    # An exception text would be hidden from the model by default; this one is returned
    contents = await tools["report"].invoke(arguments={})
    assert contents[0].text.startswith("Error: LIMIT_EXCEEDED")


class ScriptedClient(FunctionInvocationLayer, BaseChatClient):
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        reply = self.replies.pop(0)
        return ChatResponse(messages=[Message(role="assistant", contents=[reply])])


async def test_paid_tool_waits_for_approval_in_an_agent():
    gateway = Gateway()
    client = ScriptedClient([Content.from_function_call(call_id="c1", name="report", arguments={}), "done"])
    agent = Agent(client=client, instructions="Use the tools.", tools=await make_tools(gateway))
    session = agent.create_session()

    paused = await agent.run("report please", session=session)
    assert [r.function_call.name for r in paused.user_input_requests] == ["report"]
    # Nothing was invoked yet: nobody has approved
    assert gateway.invocations() == []

    approval = paused.user_input_requests[0].to_function_approval_response(True)
    done = await agent.run(Message(role="user", contents=[approval]), session=session)
    results = [c.result for m in done.messages for c in m.contents if c.type == "function_result"]
    assert results and results[0].startswith("Error: LIMIT_EXCEEDED")
    assert len(gateway.invocations()) == 1


async def test_arguments_reach_the_gateway_as_the_json_body():
    gateway = Gateway()
    tools = {t.name: t for t in await make_tools(gateway)}
    await tools["weather_basic"].invoke(arguments={"city": "Berlin"})
    assert [json.loads(r.content) for r in gateway.invocations()] == [{"city": "Berlin"}]


async def test_invalid_arguments_never_reach_the_gateway():
    gateway = Gateway()
    tools = {t.name: t for t in await make_tools(gateway)}
    with pytest.raises(Exception, match="city"):
        await tools["weather_basic"].invoke(arguments={})
    assert gateway.invocations() == []
