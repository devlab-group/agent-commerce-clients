# Buyer core

The buyer core lets an agent discover and pay for resources behind an Agent Commerce Gateway (ACG). The Python and
TypeScript implementations use the same conformance suite in [`conformance/`](../conformance):

| Language   | Package                                     |
| ---------- | ------------------------------------------- |
| Python     | `agent-commerce-client` (PyPI)              |
| TypeScript | `@devlab.group/agent-commerce-client` (npm) |

Framework integrations ([LangChain](langchain.md), [Microsoft Agent Framework](microsoft-agent-framework.md),
[Vercel AI SDK](ai-sdk.md)) map core resources to framework tools and approval settings. The core handles payments.

The core talks to the gateway only through its public HTTP contract: `GET /api/resources`,
`POST /api/resources/{id}/invoke` and x402 v2 headers (`PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, `PAYMENT-RESPONSE`).
Merchants need no gateway changes to use these clients.

## Configuration

| Python          | TypeScript          | Required | Meaning                                                                   |
| --------------- | ------------------- | -------- | ------------------------------------------------------------------------- |
| `gateway_url`   | `gatewayUrl`        | Yes      | Gateway base URL, `http` or `https`                                       |
| `signer`        | `wallet`            | Yes      | Wallet object (`eth_account` account, viem account). Raw keys are refused |
| `networks`      | `networks`          | Yes      | Allowed CAIP-2 EVM networks, for example `eip155:84532` (Base Sepolia)    |
| `max_per_call`  | `limits.maxPerCall` | Yes      | Per-invocation limit in USDC, a decimal string such as `"0.05"`           |
| `max_per_day`   | `limits.maxPerDay`  | Yes      | Limit per UTC day in USDC                                                 |
| `resources`     | `resources`         | No       | Allowlist of resource ids; every eligible resource when omitted           |
| `allow_mainnet` | `allowMainnet`      | No       | Must be `true` before any mainnet network is accepted                     |
| `approval`      | `approval`          | No       | `paid` (default), `always` or `never`: which tools need human approval    |
| `spend_store`   | `spendStore`        | No       | Daily spending counter; process memory by default                         |
| -               | `fetch`             | No       | Fetch implementation; `globalThis.fetch` by default (TypeScript)          |
| `transport`     | -                   | No       | HTTP transport override for tests (Python); closed after each call        |

Limits must be non-negative decimal strings with up to 6 decimal places. `networks` accepts the EVM networks below.
Payments use each network's USDC, with any asset allowed on local Anvil:

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

Mainnet networks require `allow_mainnet=True` in Python or `allowMainnet: true` in TypeScript. Unknown network ids are
rejected even with that setting. Invalid configuration fails during client construction. An empty `resources` list
excludes every resource; omit it to include all eligible resources.

## Discovery

The core reads `GET /api/resources` and keeps resources that meet all three conditions:

- `exposedVia` includes `http`;
- `pricing.type` is `free`, or `paymentMethods` includes `x402`;
- the `resources` allowlist is absent or lists its `id`.

Each resource excluded by these filters produces one warning. Eligible resources become tool descriptions:

- `name`: the resource `id`;
- `description`: `description`, falling back to `name`, then `id`; a full stop is added unless the text ends with `.`,
  `!` or `?`, followed by a space and `Costs <amount> <currency> per call.`, `Free.`, or `Paid.` when the price has no
  fixed amount;
- `inputSchema`: `inputSchema`, or `{"type": "object", "properties": {}}`;
- `paid`: `true` unless `pricing.type` is `free`;
- `needsApproval`: `paid` under `approval: paid`, always `true` under `always`, always `false` under `never`.

## Invocation and spending limits

An invocation sends the tool input as the JSON body of `POST /api/resources/{id}/invoke`. A free resource returns its
result directly. A paid resource returns `402` with an x402 challenge. The core pays through the official x402 client
(`@x402/fetch` and `@x402/evm`, or the Python `x402` package):

1. An offer is acceptable only with the `exact` scheme, a network listed in `networks`, that network's USDC and the
   EIP-3009 transfer method (`extra.assetTransferMethod` absent or `eip3009`; Permit2 would need a prior on-chain
   approval). Unacceptable offers are removed before the x402 client selects one. The exact EVM scheme is registered for
   the listed networks. If no acceptable offer remains, the core signs nothing.
2. Before the payload is signed, the x402 before-payment hook checks the selected offer's network, scheme, asset and
   transfer method again. Its amount, in USDC base units (6 decimals), must fit both the per-call limit and the
   remaining daily allowance. The hook atomically reserves that amount in the spend store before signing. A rejected
   offer is never signed.
3. The core sends the signed request once and does not retry it, regardless of the outcome. The default HTTP transport
   has retries disabled, and invocation requests do not follow redirects. Each invocation can produce at most one
   payment.
4. After signing, the reserved amount remains in the daily total even if the gateway rejects the payment, because the
   core cannot always tell whether funds moved.

The model calling the tool cannot change any limit.

A successful `2xx` response returns `{ status, body, payment }`: the HTTP status, the body and the decoded
`PAYMENT-RESPONSE` header. The body is parsed as JSON when its `Content-Type` contains `json` and parsing succeeds;
otherwise it remains text. `payment` carries the network, transaction hash and payer, or is `null` when the response has
no valid payment header, as with a free call.

## Errors

Runtime failures raise `AgentCommerceError`. Its short message starts with the code and omits secrets, so frameworks can
show it to the model. For example: `LIMIT_EXCEEDED: Price is above the per-call limit`.

| Code                  | When                                                                                                                                                                                                                                                                                                                                         |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LIMIT_EXCEEDED`      | The price is above the per-call limit or the remaining daily allowance                                                                                                                                                                                                                                                                       |
| `NETWORK_NOT_ALLOWED` | The gateway offered no acceptable payment: `exact`, EIP-3009, USDC, allowed network                                                                                                                                                                                                                                                          |
| `PAYMENT_FAILED`      | The gateway rejected the signed payment (HTTP 402), or returned a failed `PAYMENT-RESPONSE` on another status (ACG uses 502). A rejection settles nothing. An unconfirmed settlement may still complete on chain; the message includes a valid transaction hash when available. The reserved amount remains in the daily total in both cases |
| `BACKEND_ERROR`       | Any other non-2xx response, including invocation redirects; carries the HTTP status. ACG settles nothing for a backend failure under the default `authorization` payment flow                                                                                                                                                                |
| `GATEWAY_UNREACHABLE` | The gateway could not be reached                                                                                                                                                                                                                                                                                                             |

## Not supported

MPP, Solana and NEAR rails (only x402 `exact` on EVM), the MCP transport, resources that require an AP2 mandate or other
authorization, and wallet key storage.
