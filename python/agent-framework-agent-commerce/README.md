# agent-framework-agent-commerce

Framework tools for discovering and paying for API endpoints behind a self-hosted
[Agent Commerce Gateway](https://acg.devlab.group), using x402 within spending limits you set.

`agent_commerce_tools()` turns the resources of a gateway into Microsoft Agent Framework `FunctionTool`s.
[`agent-commerce-client`](../agent-commerce-client) handles payments, limits and filtering; this package maps its
resource descriptions to tools and sets their approval mode. The behavior contract is in
[docs/buyer-core.md](https://github.com/devlab-group/agent-commerce-clients/blob/main/docs/buyer-core.md).

Documentation: <https://acg.devlab.group/docs/integrations/microsoft-agent-framework>.

## Install

```bash
pip install agent-framework-agent-commerce
```

Requires Python 3.11 or later and `agent-framework-core` 1.x. Install a chat client package such as
`agent-framework-openai` separately when using that provider.

## Quick start

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

`agent_commerce_tools()` accepts the keyword arguments of `AgentCommerceClient`, including `gateway_url`, `signer`,
`networks`, `max_per_call`, `max_per_day`, `resources`, `allow_mainnet`, `approval` and `spend_store`. See the
[core README](../agent-commerce-client/README.md) for configuration details. Await it to discover resources over the
network.

Each tool is a `FunctionTool` named after the resource id. The package passes the gateway's JSON Schema to `input_model`
when constructing the tool. Its description includes the resource description and pricing information
(`Costs 0.01 USDC per call.`, `Free.` or `Paid.`). Its input schema is the resource's JSON Schema. A tool returns
`{"status": ..., "body": ..., "payment": ...}`, where `payment` is the decoded `PAYMENT-RESPONSE` (network, transaction
hash, payer) or `None` for a free call.

## Human approval

By default, tools for paid resources have `approval_mode="always_require"` and all others `"never_require"`
(`approval="paid"`; `"always"` and `"never"` change that). The agent then pauses with a `function_approval_request` in
`response.user_input_requests` before the tool runs, as in the example above. For tools requiring approval, invocation
waits for approval. Resource discovery happens when the tools are created.

## Errors

An `AgentCommerceError` is returned to the model as text, for example
`Error: LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC`. Agent Framework hides the text of a
raised exception from the model unless `include_detailed_errors` is set, which is why the tool returns the message
instead of raising. Codes: `LIMIT_EXCEEDED`, `NETWORK_NOT_ALLOWED`, `PAYMENT_FAILED`, `BACKEND_ERROR`,
`GATEWAY_UNREACHABLE`. Configuration errors raise `ValueError` or `TypeError` from `agent_commerce_tools()`.

## Tests

```bash
uv run pytest agent-framework-agent-commerce   # from the python/ directory of the repository
```

The suite checks the tool mapping and runs an agent with a scripted chat client to confirm that a paid tool waits for
approval.

The end-to-end test makes a real payment through a running gateway and chain, without a language model. Enable it with
`ACG_E2E=1`; configuration and environment variables are listed in [`../e2e/acg_e2e.py`](../e2e/acg_e2e.py):

```bash
ACG_E2E=1 uv run pytest agent-framework-agent-commerce/tests/test_e2e_agent_framework.py -s
```

## License

Apache-2.0
