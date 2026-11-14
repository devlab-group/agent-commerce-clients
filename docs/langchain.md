# LangChain

`langchain-agent-commerce` lets LangChain agents discover and pay for API endpoints behind a self-hosted Agent Commerce
Gateway, using x402 within spending limits you set. The Python [buyer core](buyer-core.md) handles payments and limits;
this package maps gateway resources to `StructuredTool`s and builds the human-approval middleware.

Source and package README: [`python/langchain-agent-commerce`](../python/langchain-agent-commerce).

## Install

```bash
pip install langchain-agent-commerce
```

Requires Python 3.11 or later and LangChain 1.x.

## Example

```python
import os

from eth_account import Account
from langchain.agents import create_agent
from langchain_agent_commerce import AgentCommerceToolkit

toolkit = AgentCommerceToolkit(
    gateway_url="https://merchant.example.com",
    signer=Account.from_key(os.environ["BUYER_PRIVATE_KEY"]),
    networks=["eip155:84532"],
    max_per_call="0.05",
    max_per_day="1.00",
)
agent = create_agent(model="openai:gpt-5-mini", tools=toolkit.get_tools())
```

The toolkit accepts the [buyer core configuration](buyer-core.md#configuration). Each eligible resource becomes a tool
named after its id, with a description that includes pricing information and arguments defined by its JSON Schema.

This example lets the agent call paid tools within `max_per_call` and `max_per_day` without human approval. Add the
middleware below to require a decision before a paid call.

## Approval

`toolkit.approval_middleware()` builds LangChain's `HumanInTheLoopMiddleware` for the tools that need approval (paid
ones by default; the core's `approval` setting changes that). The middleware needs a checkpointer and a `thread_id`:

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

agent = create_agent(
    model="openai:gpt-5-mini",
    tools=toolkit.get_tools(),
    middleware=[toolkit.approval_middleware()],
    checkpointer=InMemorySaver(),
)
config = {"configurable": {"thread_id": "1"}}
result = agent.invoke({"messages": [("user", "Buy the market report")]}, config=config, version="v2")
# result.interrupts[0].value["action_requests"] holds the paid calls waiting for a decision
result = agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config, version="v2")
```

## Errors

`AgentCommerceError` failures reach the model as tool output text prefixed with the error code, for example
`LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC`. These failures do not stop the agent. See
the [buyer core errors](buyer-core.md#errors) for the codes.

## End-to-end evidence

The end-to-end test calls a paid tool against a running gateway and chain, without a language model. It checks that a
price within the limit returns a result and a `PAYMENT-RESPONSE` with a transaction hash, and reduces the buyer's
balance by the price. An over-limit call must leave the balance and receipt list unchanged.

Local demo stack (gateway `http://127.0.0.1:8080`, Anvil `eip155:31337`, MockUSDC, `market_report` at 0.01 USDC):

| Case                   | Result                                                                                                                                                |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Price within the limit | Tool result returned; tx `0x583c0255eee89b80c848550d651d7cd73a3d67b3a2008906e33b2cdb6d314b98`; buyer balance 99890000 to 99880000 base units (-10000) |
| `max_per_call="0.005"` | `LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC`; balance unchanged; receipt list unchanged                                 |

Base Sepolia (`eip155:84532`), 2026-11-13: the demo merchant from ACG's `deploy/testnet-demo` run locally (gateway 2.7.1
in a locally built image, before the demo moved to the published 2.7.2 image; public facilitator
`https://x402.org/facilitator`, Circle USDC, `report` at 0.01 USDC, a temporary merchant wallet as `payTo`).

| Case                   | Result                                                                                                                                                                                                                                                      |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Price within the limit | Tool result returned; tx [`0x9cfda7c3a70fd0456d2d8b30af1c29cccdf49f548e22f4bd735e47e9212fed69`](https://sepolia.basescan.org/tx/0x9cfda7c3a70fd0456d2d8b30af1c29cccdf49f548e22f4bd735e47e9212fed69); buyer balance 19880000 to 19870000 base units (-10000) |
| `max_per_call="0.005"` | `LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC`; balance unchanged; receipt list unchanged                                                                                                                                       |

The first attempt failed at the public facilitator (`invalid_exact_evm_transaction_failed`) without a transaction or
charge. The gateway returned 502 `PAYMENT_SETTLEMENT_FAILED`; the rerun recorded above passed.

Run it yourself with `ACG_E2E=1 uv run pytest langchain-agent-commerce/tests/test_e2e_langchain.py -s` from the
`python/` directory; the gateway URL, network, RPC, asset, resource, admin token and key source are environment
variables listed in [`python/e2e/acg_e2e.py`](../python/e2e/acg_e2e.py).
