# Microsoft Agent Framework

`agent-framework-agent-commerce` lets Microsoft Agent Framework agents discover and pay for API endpoints behind a
self-hosted Agent Commerce Gateway, using x402 within spending limits you set. The Python [buyer core](buyer-core.md)
handles payments and limits; this package maps gateway resources to `FunctionTool`s and sets their approval mode.

Source and package README: [`python/agent-framework-agent-commerce`](../python/agent-framework-agent-commerce).

## Install

```bash
pip install agent-framework-agent-commerce
```

Requires Python 3.11 or later and `agent-framework-core` 1.x. Install a chat client package such as
`agent-framework-openai` separately when using that provider.

## Example

```python
import asyncio
import os

from agent_framework import Agent, Message
from agent_framework.openai import OpenAIChatClient
from agent_framework_agent_commerce import agent_commerce_tools
from eth_account import Account


async def main() -> None:
    tools = await agent_commerce_tools(
        gateway_url="https://merchant.example.com",
        signer=Account.from_key(os.environ["BUYER_PRIVATE_KEY"]),
        networks=["eip155:84532"],
        max_per_call="0.05",
        max_per_day="1.00",
    )
    agent = Agent(client=OpenAIChatClient(model="gpt-5-mini"), instructions="Use the tools to answer.", tools=tools)
    session = agent.create_session()

    response = await agent.run("Buy the market report and summarize it.", session=session)
    while response.user_input_requests:
        # Show request.function_call (name and arguments) to a person, then send the decision back
        approval = response.user_input_requests[0].to_function_approval_response(True)
        response = await agent.run(Message(role="user", contents=[approval]), session=session)
    print(response.text)


asyncio.run(main())
```

`agent_commerce_tools()` takes the configuration of the [buyer core](buyer-core.md#configuration). Await it to discover
resources over the network. Each eligible resource becomes a `FunctionTool` named after its id, with a description that
includes pricing information and its JSON Schema as input (`FunctionTool(..., input_model=<schema>)`).

## Approval

Tools that need approval (paid ones by default; the core's `approval` setting changes that) get
`approval_mode="always_require"`, all others `"never_require"`. The agent then returns a `function_approval_request` in
`response.user_input_requests` before the tool runs; answer it with `request.to_function_approval_response(True)` or
`False`, as in the example. For tools requiring approval, an invocation reaches the gateway only after approval.
Resource discovery happens when the tools are created.

## Errors

An `AgentCommerceError` is returned to the model as text prefixed with `Error:` and the error code, for example
`Error: LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC`. Agent Framework hides the text of a
raised exception from the model unless `include_detailed_errors` is set, which is why the tool returns the message
instead of raising. The codes are listed in the [buyer core errors](buyer-core.md#errors).

## End-to-end evidence

The end-to-end test calls a paid tool against a running gateway and chain, without a language model. It checks that a
price within the limit returns a result and a `PAYMENT-RESPONSE` with a transaction hash, and reduces the buyer's
balance by the price. An over-limit call must leave the balance and receipt list unchanged.

Local demo stack (gateway `http://127.0.0.1:8080`, Anvil `eip155:31337`, MockUSDC, `market_report` at 0.01 USDC):

| Case                   | Result                                                                                                                                                |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Price within the limit | Tool result returned; tx `0x4d59bdade92f893f31d1c7b72b581f68c9730899709aa751abeee1f2b03b1447`; buyer balance 99880000 to 99870000 base units (-10000) |
| `max_per_call="0.005"` | `Error: LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC`; balance unchanged; receipt list unchanged                          |

Base Sepolia (`eip155:84532`), 2026-11-13: the demo merchant from ACG's `deploy/testnet-demo` run locally (gateway 2.7.1
in a locally built image, before the demo moved to the published 2.7.2 image; public facilitator
`https://x402.org/facilitator`, Circle USDC, `report` at 0.01 USDC, a temporary merchant wallet as `payTo`).

| Case                   | Result                                                                                                                                                                                                                                                      |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Price within the limit | Tool result returned; tx [`0xce31b71f39e00cd1de00237e4642961ca976cf97b7677da0eec7d6377f612c2e`](https://sepolia.basescan.org/tx/0xce31b71f39e00cd1de00237e4642961ca976cf97b7677da0eec7d6377f612c2e); buyer balance 19890000 to 19880000 base units (-10000) |
| `max_per_call="0.005"` | `Error: LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC`; balance unchanged; receipt list unchanged                                                                                                                                |

Run it yourself with `ACG_E2E=1 uv run pytest agent-framework-agent-commerce/tests/test_e2e_agent_framework.py -s` from
the `python/` directory; the gateway URL, network, RPC, asset, resource, admin token and key source are environment
variables listed in [`python/e2e/acg_e2e.py`](../python/e2e/acg_e2e.py).
