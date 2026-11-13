"""End-to-end: the toolkit's tools against a live gateway and chain, no language model."""

import re

import pytest
from acg_e2e import Stack, enabled, usdc
from langchain_agent_commerce import AgentCommerceToolkit

pytestmark = [enabled, pytest.mark.e2e]


@pytest.fixture(scope="module")
def stack():
    return Stack()


def tool_of(stack, **limits):
    tools = {t.name: t for t in AgentCommerceToolkit(**stack.client_kwargs(**limits)).get_tools()}
    return tools[stack.resource]


def test_price_within_limit_pays_once(stack):
    tool = tool_of(stack)
    before = stack.balance()
    result = tool.invoke({})
    assert isinstance(result, dict), result
    assert result["status"] == 200
    payment = result["payment"]
    assert payment["success"] is True
    assert re.fullmatch(r"0x[0-9a-fA-F]{64}", payment["transaction"])
    assert stack.wait_for_balance(before - stack.price) == before - stack.price
    assert stack.transaction_succeeded(payment["transaction"])
    print(f"\nE2E langchain tx {payment['transaction']} balance {before} -> {before - stack.price}")


def test_price_above_limit_pays_nothing(stack):
    tool = tool_of(stack, max_per_call=usdc(stack.price // 2))
    balance, receipts = stack.balance(), stack.receipt_ids()
    output = tool.invoke({})
    # The error comes back as tool output text, not as an exception
    assert isinstance(output, str) and output.startswith("LIMIT_EXCEEDED")
    assert stack.balance() == balance
    assert stack.receipt_ids() == receipts
    print(f"\nE2E langchain over-limit: {output}")
