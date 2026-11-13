"""End-to-end: the Agent Framework tools against a live gateway and chain, no language model."""

import json
import re

import pytest
from acg_e2e import Stack, enabled, usdc
from agent_framework_agent_commerce import agent_commerce_tools

pytestmark = [enabled, pytest.mark.e2e]


@pytest.fixture(scope="module")
def stack():
    return Stack()


async def tool_of(stack, **limits):
    tools = {t.name: t for t in await agent_commerce_tools(**stack.client_kwargs(**limits))}
    return tools[stack.resource]


async def test_price_within_limit_pays_once(stack):
    tool = await tool_of(stack)
    before = stack.balance()
    contents = await tool.invoke(arguments={})
    result = json.loads(contents[0].text)
    assert result["status"] == 200
    payment = result["payment"]
    assert payment["success"] is True
    assert re.fullmatch(r"0x[0-9a-fA-F]{64}", payment["transaction"])
    assert stack.wait_for_balance(before - stack.price) == before - stack.price
    assert stack.transaction_succeeded(payment["transaction"])
    print(f"\nE2E agent-framework tx {payment['transaction']} balance {before} -> {before - stack.price}")


async def test_price_above_limit_pays_nothing(stack):
    tool = await tool_of(stack, max_per_call=usdc(stack.price // 2))
    balance, receipts = stack.balance(), stack.receipt_ids()
    contents = await tool.invoke(arguments={})
    output = contents[0].text
    # The error comes back as text for the model, not as an exception
    assert output.startswith("Error: LIMIT_EXCEEDED")
    assert stack.balance() == balance
    assert stack.receipt_ids() == receipts
    print(f"\nE2E agent-framework over-limit: {output}")
