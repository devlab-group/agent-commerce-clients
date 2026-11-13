"""Mapping tests: tool type, name, description, schema and approval. No payment logic lives here."""

import base64
import json
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_agent_commerce import AgentCommerceToolkit
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

CONFORMANCE = Path(__file__).resolve().parents[3] / "conformance"
SUITE = json.loads((CONFORMANCE / "scenarios.json").read_text())
RESOURCES = json.loads((CONFORMANCE / "fixtures" / "resources.json").read_text())
PAYMENT_REQUIRED = (CONFORMANCE / "fixtures" / "payment-required.json").read_bytes()
SIGNER = Account.from_key(SUITE["testSignerPrivateKey"])


class Gateway:
    """Discovery from the shared fixture; invocations answer 402, which the price limit then refuses."""

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


def make_toolkit(gateway, **overrides):
    return AgentCommerceToolkit(
        **{
            "gateway_url": "https://merchant.example.com",
            "signer": SIGNER,
            "networks": ["eip155:84532"],
            "max_per_call": "0.005",
            "max_per_day": "1.00",
            "transport": httpx.MockTransport(gateway),
            **overrides,
        }
    )


def test_tools_map_the_core_descriptions():
    toolkit = make_toolkit(Gateway())
    tools = toolkit.get_tools()
    described = toolkit.client.list_tools()
    assert (
        [t.name for t in tools]
        == [d.name for d in described]
        == ["report", "weather_basic", "no_description", "no_schema"]
    )
    for tool, expected in zip(tools, described):
        assert isinstance(tool, StructuredTool)
        assert tool.description == expected.description
        assert tool.args_schema == expected.input_schema
        assert tool.metadata == {"needs_approval": expected.needs_approval}
        assert tool.func is not None and tool.coroutine is not None
        assert tool.handle_tool_error is True


@pytest.mark.parametrize(
    ("approval", "gated"),
    [
        ("paid", {"report", "no_description"}),
        ("always", {"report", "weather_basic", "no_description", "no_schema"}),
        ("never", set()),
    ],
)
def test_approval_middleware_gates_the_tools_needing_approval(approval, gated):
    toolkit = make_toolkit(Gateway(), approval=approval)
    tools = toolkit.get_tools()
    middleware = toolkit.approval_middleware()
    assert isinstance(middleware, HumanInTheLoopMiddleware)
    assert set(middleware.interrupt_on) == gated == {t.name for t in tools if t.metadata["needs_approval"]}
    for name, config in middleware.interrupt_on.items():
        assert config["allowed_decisions"] == ["approve", "edit", "reject"]
        assert next(t for t in tools if t.name == name).description in config["description"]


def test_approval_middleware_discovers_when_get_tools_was_not_called():
    gateway = Gateway()
    toolkit = make_toolkit(gateway)
    assert set(toolkit.approval_middleware().interrupt_on) == {"report", "no_description"}
    assert [r.method for r in gateway.requests] == ["GET"]


def test_construction_errors_surface_from_the_core():
    with pytest.raises(TypeError):
        AgentCommerceToolkit(
            gateway_url="https://x", signer="0xabc", networks=["eip155:84532"], max_per_call="1", max_per_day="1"
        )


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(name, call_id="call_1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}])


def agent_of(toolkit, calls, **kwargs):
    model = ScriptedModel(messages=iter([*calls, AIMessage(content="done")]))
    return create_agent(model=model, tools=toolkit.get_tools(), **kwargs)


def test_tool_error_reaches_the_model_as_text():
    # The README example: create_agent(model=..., tools=toolkit.get_tools())
    gateway = Gateway()
    agent = agent_of(make_toolkit(gateway), [tool_call("report")])
    result = agent.invoke({"messages": [("user", "report please")]})
    message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert message.status == "error"
    assert message.content.startswith("LIMIT_EXCEEDED")
    assert result["messages"][-1].content == "done"
    assert not [r for r in gateway.requests if "payment-signature" in r.headers]


async def test_agent_runs_async_tools():
    agent = agent_of(make_toolkit(Gateway()), [tool_call("weather_basic")])
    result = await agent.ainvoke({"messages": [("user", "weather")]})
    message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert json.loads(message.content) == {"status": 200, "body": {"ok": True}, "payment": None}


@pytest.mark.parametrize(
    ("decision", "invoked", "tool_output"),
    [
        ({"type": "reject", "message": "too expensive"}, ["weather_basic"], "too expensive"),
        # Approved: the paid tool runs and the gateway is asked (the 0.005 limit then refuses the price)
        ({"type": "approve"}, ["weather_basic", "report"], "LIMIT_EXCEEDED"),
    ],
)
def test_a_turn_with_a_paid_tool_call_pauses_until_a_human_decides(decision, invoked, tool_output):
    gateway = Gateway()
    toolkit = make_toolkit(gateway)
    config = {"configurable": {"thread_id": "t1"}}
    calls = [{"name": "weather_basic", "args": {}, "id": "c1"}, {"name": "report", "args": {}, "id": "c2"}]
    agent = agent_of(
        toolkit,
        [AIMessage(content="", tool_calls=calls)],
        middleware=[toolkit.approval_middleware()],
        checkpointer=InMemorySaver(),
    )
    paused = agent.invoke({"messages": [("user", "go")]}, config, version="v2")
    assert [a["name"] for a in paused.interrupts[0].value["action_requests"]] == ["report"]
    # The turn is paused as a whole: nothing was invoked yet
    assert gateway.invocations() == []

    done = agent.invoke(Command(resume={"decisions": [decision]}), config=config, version="v2")
    contents = [m.content for m in done.value["messages"] if isinstance(m, ToolMessage)]
    assert any(tool_output in c for c in contents)
    # LangChain runs the calls of one turn in parallel, so their order is not fixed
    assert sorted(r.url.path.split("/")[3] for r in gateway.invocations()) == sorted(invoked)


def tools_of(gateway, **overrides):
    return {t.name: t for t in make_toolkit(gateway, **overrides).get_tools()}


def test_arguments_reach_the_gateway_as_the_json_body_on_the_sync_path():
    gateway = Gateway()
    result = tools_of(gateway)["weather_basic"].invoke({"city": "Berlin"})
    assert result == {"status": 200, "body": {"ok": True}, "payment": None}
    assert [json.loads(r.content) for r in gateway.invocations()] == [{"city": "Berlin"}]


async def test_arguments_reach_the_gateway_as_the_json_body_on_the_async_path():
    gateway = Gateway()
    result = await tools_of(gateway)["weather_basic"].ainvoke({"city": "Berlin"})
    assert result == {"status": 200, "body": {"ok": True}, "payment": None}
    assert [json.loads(r.content) for r in gateway.invocations()] == [{"city": "Berlin"}]


async def test_a_failing_async_tool_returns_text_instead_of_raising():
    output = await tools_of(Gateway())["report"].ainvoke({})
    assert output == "LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC"


def test_a_failing_sync_tool_returns_text_instead_of_raising():
    output = tools_of(Gateway())["report"].invoke({})
    assert output == "LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC"


def test_a_turn_with_only_free_tool_calls_runs_without_a_pause():
    gateway = Gateway()
    toolkit = make_toolkit(gateway)
    agent = agent_of(
        toolkit, [tool_call("weather_basic")], middleware=[toolkit.approval_middleware()], checkpointer=InMemorySaver()
    )
    done = agent.invoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "t2"}}, version="v2")
    assert done.interrupts == ()
    assert [r.url.path.split("/")[3] for r in gateway.invocations()] == ["weather_basic"]
