"""The standard langchain-tests unit suite for tools, run on a tool the toolkit builds."""

import json
from pathlib import Path

import httpx
from eth_account import Account
from langchain_agent_commerce import AgentCommerceToolkit
from langchain_tests.unit_tests import ToolsUnitTests

CONFORMANCE = Path(__file__).resolve().parents[3] / "conformance"
SUITE = json.loads((CONFORMANCE / "scenarios.json").read_text())
RESOURCES = json.loads((CONFORMANCE / "fixtures" / "resources.json").read_text())


class TestAgentCommerceToolStandard(ToolsUnitTests):
    @property
    def tool_constructor(self):
        toolkit = AgentCommerceToolkit(
            gateway_url="https://merchant.example.com",
            signer=Account.from_key(SUITE["testSignerPrivateKey"]),
            networks=["eip155:84532"],
            max_per_call="0.05",
            max_per_day="1.00",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=RESOURCES)),
        )
        return next(tool for tool in toolkit.get_tools() if tool.name == "weather_basic")

    @property
    def tool_invoke_params_example(self):
        return {"city": "Berlin"}
