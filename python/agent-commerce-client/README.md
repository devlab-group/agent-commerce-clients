# agent-commerce-client

Python buyer client for a self-hosted [Agent Commerce Gateway](https://acg.devlab.group) (ACG). It discovers gateway
resources and pays for them with x402, within spending limits you set in code.

The framework packages build on this client and add no payment logic of their own:
[`langchain-agent-commerce`](../langchain-agent-commerce) and
[`agent-framework-agent-commerce`](../agent-framework-agent-commerce) (Microsoft Agent Framework). The full behavior
contract, shared with the TypeScript client, is in
[docs/buyer-core.md](https://github.com/devlab-group/agent-commerce-clients/blob/main/docs/buyer-core.md).

## Install

```bash
pip install agent-commerce-client
```

Python 3.10 or newer. Payments use the official [`x402`](https://pypi.org/project/x402/) package (`exact` scheme on EVM
networks).

## Quick start

```python
import os

from agent_commerce_client import AgentCommerceClient, AgentCommerceError
from eth_account import Account

client = AgentCommerceClient(
    gateway_url="https://merchant.example.com",
    signer=Account.from_key(os.environ["BUYER_PRIVATE_KEY"]),
    networks=["eip155:84532"],  # Base Sepolia
    max_per_call="0.05",  # USDC
    max_per_day="1.00",  # USDC per UTC day
)

for tool in client.list_tools():
    print(tool.name, "-", tool.description)

try:
    result = client.invoke("report", {"topic": "x"})
    print(result.status, result.body, result.payment)  # payment: network, transaction hash, payer
except AgentCommerceError as error:
    print(error.code, error.message, error.status)
```

Use `await client.alist_tools()` and `await client.ainvoke(...)` for async calls. The sync methods call the same
implementation and can also run while an event loop is active.

## Configuration

| Argument        | Required | Meaning                                                                          |
| --------------- | -------- | -------------------------------------------------------------------------------- |
| `gateway_url`   | Yes      | Gateway base URL, `http` or `https`                                              |
| `signer`        | Yes      | Wallet object, for example an `eth_account` account. A raw key string is refused |
| `networks`      | Yes      | Allowed networks from the table below, for example `["eip155:84532"]`            |
| `max_per_call`  | Yes      | Limit per invocation in USDC, a decimal string with up to 6 decimals             |
| `max_per_day`   | Yes      | Limit per UTC day in USDC                                                        |
| `resources`     | No       | Allowlist of resource ids; every eligible resource when omitted                  |
| `allow_mainnet` | No       | Must be `True` before any mainnet network is accepted                            |
| `approval`      | No       | `"paid"` (default), `"always"` or `"never"`: which tools carry `needs_approval`  |
| `spend_store`   | No       | Daily spending counter; process memory by default                                |
| `transport`     | No       | HTTP transport override for tests; closed after each call                        |

Invalid configuration raises `ValueError` or `TypeError` during client construction. `resources=[]` excludes every
resource; leave it as `None` to include all eligible resources.

`networks` accepts the EVM networks below. Payments use each network's USDC, with any asset allowed on local Anvil:

| Network           | Id              | Kind    | USDC                                         |
| ----------------- | --------------- | ------- | -------------------------------------------- |
| Local Anvil       | `eip155:31337`  | testnet | any asset (local development chain)          |
| Base Sepolia      | `eip155:84532`  | testnet | `0x036CbD53842c5426634e7929541eC2318f3dCF7e` |
| Arbitrum Sepolia  | `eip155:421614` | testnet | `0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d` |
| Base              | `eip155:8453`   | mainnet | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Ethereum          | `eip155:1`      | mainnet | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` |
| Polygon           | `eip155:137`    | mainnet | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` |
| Arbitrum One      | `eip155:42161`  | mainnet | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` |
| Avalanche C-Chain | `eip155:43114`  | mainnet | `0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E` |

Mainnet networks require `allow_mainnet=True`. Unknown network ids raise `ValueError` even with that setting.

## Spending limits

The client enforces limits before signing a payment. When the gateway answers `402`, an offer is acceptable only with
the `exact` scheme, a network in `networks`, that network's USDC and an EIP-3009 transfer (`extra.assetTransferMethod`
absent or `eip3009`; `permit2` needs an on-chain approval first, and ACG answers 412 without it). Unacceptable offers
are removed before the x402 client selects one, so an earlier non-USDC offer cannot displace an acceptable USDC offer.
If none remains, the call fails with `NETWORK_NOT_ALLOWED`. The x402 before-payment hook then checks the selected offer
again, and the amount (USDC base units, 6 decimals) must not exceed `max_per_call`; it is reserved in the spend store
against `max_per_day`. A refusal raises `AgentCommerceError` before a signature exists. The model calling a tool cannot
change any limit.

Each invocation sends at most one request with `PAYMENT-SIGNATURE` and does not retry it. After signing, the reserved
amount remains in the daily total even if the gateway rejects the payment, because the client cannot always tell whether
funds moved.

To share the daily counter between processes, pass a `spend_store` with an atomic `add` method:

```python
class SpendStore:
    def add(self, day: str, amount: int) -> int:
        """Add `amount` micro-USDC (negative to roll back) to `day` (UTC, YYYY-MM-DD); return the new total."""
```

## Resources and tools

`list_tools()` keeps a resource when it is exposed over `http`, is free or accepts x402, and is in the `resources`
allowlist (if given). Each resource excluded by these filters produces one warning on the `agent_commerce_client`
logger. A tool is a `ResourceTool` with `name` (the resource id), `description` (ends with `Costs 0.01 USDC per call.`,
`Free.` or `Paid.`), `input_schema`, `paid` and `needs_approval`. Framework wrappers use `needs_approval` to configure
human approval.

Tool names and descriptions come from the gateway and reach the model. Use `resources=[...]` to expose only resources
you trust.

## Errors

`AgentCommerceError` has `code`, `message` and `status`. The message starts with the code, for example
`LIMIT_EXCEEDED: Price 0.01 USDC exceeds the per-call limit of 0.005 USDC`, because frameworks often show a model only
the message. Client-generated messages omit keys, headers and backend bodies. They may include a gateway error code such
as `INPUT_INVALID`, an x402 reason such as `invalid_payload`, or a valid transaction hash for an unconfirmed settlement.

| Code                  | When                                                                                                                                   |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `LIMIT_EXCEEDED`      | The price is above the per-call limit or the remaining daily allowance                                                                 |
| `NETWORK_NOT_ALLOWED` | The gateway offered no `exact` USDC payment on an allowed network                                                                      |
| `PAYMENT_FAILED`      | The gateway refused the signed payment, or could not confirm settlement; the message then says the payment may still complete on chain |
| `BACKEND_ERROR`       | Any other non-2xx answer, redirects included; `status` holds the status                                                                |
| `GATEWAY_UNREACHABLE` | The gateway could not be reached (60 second request timeout)                                                                           |

## Not supported

MPP, Solana and NEAR rails, the MCP transport, resources that need an AP2 mandate or other authorization, wallet key
storage and proxy environment variables. Requests do not follow redirects.

## x402 library defaults

The Python `x402` package defaults to a $1 payment cap and an allowlist of known tokens, checked before policies and
hooks. This client disables those controls with `set_spend_controls(False)` and applies its network, USDC and spending
checks instead. That allows a `max_per_call` above 1 USDC and the custom token used on local Anvil.

## Tests

```bash
uv run pytest agent-commerce-client   # from the python/ directory of the repository
```

The suite runs every scenario of the shared conformance suite in `conformance/` on both the sync and async paths.

## License

Apache-2.0
