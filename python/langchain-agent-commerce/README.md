# langchain-agent-commerce

Framework tools for discovering and paying for API endpoints behind a self-hosted
[Agent Commerce Gateway](https://acg.devlab.group), using x402 within spending limits you set.

`AgentCommerceToolkit` turns the resources of a gateway into LangChain tools.
[`agent-commerce-client`](../agent-commerce-client) handles payments, limits and filtering; this package maps its
resource descriptions to `StructuredTool`s and builds the human-approval middleware. The behavior contract is in
[docs/buyer-core.md](https://github.com/devlab-group/agent-commerce-clients/blob/master/docs/buyer-core.md).

Documentation: <https://acg.devlab.group/docs/integrations/langchain>.

## Install

```bash
pip install langchain-agent-commerce
```

Requires Python 3.11 or later and LangChain 1.x.

## Quick start

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

Choose a chat model with LangChain tool-calling support. This example runs paid tools without human approval; see
[Human approval](#human-approval). The toolkit accepts the keyword arguments of `AgentCommerceClient`, including
`gateway_url`, `signer`, `networks`, `max_per_call`, `max_per_day`, `resources`, `allow_mainnet`, `approval` and
`spend_store`. See the [core README](../agent-commerce-client/README.md) for configuration details.

Each tool is a `StructuredTool` named after the resource id. Its description comes from the gateway and ends with the
price (`Costs 0.01 USDC per call.`, `Free.` or `Paid.`), and its argument schema is the resource's JSON Schema. A tool
returns `{"status": ..., "body": ..., "payment": ...}`, where `payment` is the decoded `PAYMENT-RESPONSE` (network,
transaction hash, payer) or `None` for a free call.

## Human approval

Add the approval middleware to require a human decision before a paid call. Without it, the agent calls paid tools on
its own within `max_per_call` and `max_per_day`, as in the quick start.

By default, tools for paid resources need approval (`approval="paid"`; `"always"` and `"never"` change that).
`toolkit.approval_middleware()` builds LangChain's `HumanInTheLoopMiddleware` (`langchain.agents.middleware`) with
`interrupt_on` set for exactly those tools. The reviewer sees the price in the request description and can approve, edit
or reject the call. The middleware needs a checkpointer and a `thread_id`:

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

tools = toolkit.get_tools()
agent = create_agent(
    model="openai:gpt-5-mini",
    tools=tools,
    middleware=[toolkit.approval_middleware()],
    checkpointer=InMemorySaver(),
)
config = {"configurable": {"thread_id": "1"}}
result = agent.invoke({"messages": [("user", "Buy the market report")]}, config=config, version="v2")
# result.interrupts[0].value["action_requests"] lists the paid calls awaiting a decision;
# no invocation has been sent to the gateway yet
result = agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config, version="v2")
```

A rejection looks like `{"type": "reject", "message": "too expensive"}`; the model then receives the reason as the tool
result. `approval_middleware()` uses the tools of the last `get_tools()` call.

## Errors

An `AgentCommerceError` reaches the model as tool output text, for example
`LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC`, and the tool message gets the error status.
It does not crash the agent. Codes: `LIMIT_EXCEEDED`, `NETWORK_NOT_ALLOWED`, `PAYMENT_FAILED`, `BACKEND_ERROR`,
`GATEWAY_UNREACHABLE`. Configuration errors raise `ValueError` or `TypeError` when the toolkit is created.

## Tests

```bash
uv run pytest langchain-agent-commerce   # from the python/ directory of the repository
```

The suite checks the tool mapping, the approval middleware, an agent run with a scripted model, and the standard
`langchain-tests` `ToolsUnitTests` suite.

The end-to-end test makes a real payment through a running gateway and chain, without a language model. Enable it with
`ACG_E2E=1`; configuration and environment variables are listed in [`../e2e/acg_e2e.py`](../e2e/acg_e2e.py):

```bash
ACG_E2E=1 uv run pytest langchain-agent-commerce/tests/test_e2e_langchain.py -s
```

## License

Apache-2.0
