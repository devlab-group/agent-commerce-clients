# @devlab.group/agent-commerce-client

Buyer-side client for [Agent Commerce Gateway](https://acg.devlab.group) (ACG). It discovers and invokes gateway
resources, paying with [x402](https://www.x402.org) (`exact` scheme on EVM chains) within spending limits that the
calling model cannot change.

Framework wrappers use this client for payments. For the Vercel AI SDK, see
[`@devlab.group/agent-commerce-ai-sdk`](https://www.npmjs.com/package/@devlab.group/agent-commerce-ai-sdk). The behavior
contract shared with the Python client is the [buyer core page](https://acg.devlab.group/docs/integrations/buyer-core).

## Install

```bash
npm install @devlab.group/agent-commerce-client viem
```

Node.js 20 or later. `viem` provides the wallet account.

## Quick start

```ts
import { privateKeyToAccount } from 'viem/accounts';
import { AgentCommerceError, createAgentCommerceClient } from '@devlab.group/agent-commerce-client';

const client = createAgentCommerceClient({
  gatewayUrl: 'https://merchant.example.com',
  wallet: privateKeyToAccount(process.env.BUYER_PRIVATE_KEY as `0x${string}`),
  networks: ['eip155:84532'],
  limits: { maxPerCall: '0.05', maxPerDay: '1.00' },
});

for (const tool of await client.listTools()) {
  console.log(`${tool.name}: ${tool.description}`);
}

try {
  const { status, body, payment } = await client.invoke('report', { topic: 'solar panels' });
  console.log(status, body, payment?.transaction);
} catch (error) {
  if (!(error instanceof AgentCommerceError)) throw error;
  console.error(error.code, error.status, error.message);
}
```

`BUYER_PRIVATE_KEY` is read by your application. Pass the resulting account object to the client; raw private keys are
rejected. Use a dedicated wallet that holds little more than the daily limit.

`invoke` returns `{ status, body, payment }`: the HTTP status, response body and decoded `PAYMENT-RESPONSE` header
(network, transaction hash, payer). The body is parsed when its `Content-Type` contains `json` and parsing succeeds;
otherwise it remains text. `payment` is `null` when the response has no valid payment header, as with a free call.

## Options

| Option              | Required | Meaning                                                                           |
| ------------------- | -------- | --------------------------------------------------------------------------------- |
| `gatewayUrl`        | Yes      | Gateway base URL, `http` or `https`                                               |
| `wallet`            | Yes      | Account object with `address` and `signTypedData`, for example a viem account     |
| `networks`          | Yes      | Allowed networks, from the table below, for example `eip155:84532` (Base Sepolia) |
| `limits.maxPerCall` | Yes      | Per-invocation limit in USDC, a decimal string with up to 6 decimals              |
| `limits.maxPerDay`  | Yes      | Limit per UTC day in USDC                                                         |
| `resources`         | No       | Allowlist of resource ids; every eligible resource when omitted                   |
| `allowMainnet`      | No       | Must be `true` before any mainnet network is accepted                             |
| `approval`          | No       | `paid` (default), `always` or `never`; sets `needsApproval` on each tool          |
| `spendStore`        | No       | Daily spending counter; process memory by default                                 |
| `fetch`             | No       | Fetch implementation, `globalThis.fetch` by default                               |

`networks` accepts the EVM networks below. Payments use each network's USDC, with any asset allowed on local Anvil:

| Kind    | Networks                                                                                                                               |
| ------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Testnet | `eip155:84532` (Base Sepolia), `eip155:421614` (Arbitrum Sepolia), `eip155:31337` (local Anvil, any asset)                             |
| Mainnet | `eip155:8453` (Base), `eip155:1` (Ethereum), `eip155:137` (Polygon), `eip155:42161` (Arbitrum One), `eip155:43114` (Avalanche C-Chain) |

Mainnet networks require `allowMainnet: true`. Unknown network ids are rejected even with that setting. The USDC address
of each network is listed on the [buyer core page](https://acg.devlab.group/docs/integrations/buyer-core#configuration).

Invalid configuration throws during client construction. Requests time out after 60 seconds.

## Spending limits

The x402 client registers the `exact` scheme for the configured `networks`. When a paid resource returns `402`, the
client filters offers by scheme, network, USDC asset and EIP-3009 transfer method (`extra.assetTransferMethod` absent or
`eip3009`). Permit2 approvals and ERC-7710 delegations are not supported. The x402 before-payment hook then checks the
selected offer before signing:

1. its scheme, network, asset and transfer method must meet those conditions;
2. its amount, in USDC base units (6 decimals), must not exceed `limits.maxPerCall`;
3. the amount is added to the day's total in the spend store, and the total must not exceed `limits.maxPerDay`.

An unacceptable or over-limit offer throws `NETWORK_NOT_ALLOWED` or `LIMIT_EXCEEDED` before signing. A reservation is
rolled back only if it exceeds the daily limit. The client sends the signed request once and does not retry it,
regardless of the outcome, so each invocation produces at most one payment. After signing, the reserved amount remains
in the daily total even if the gateway rejects the payment, because the client cannot always tell whether funds moved.

Each client has its own spend store in process memory by default; restarting the process resets the daily total. For a
limit that survives restarts or spans processes, pass a `spendStore`: any object with an `add(day, amount)` method that
atomically adds a `bigint` number of USDC base units (negative to roll back) to the total of the UTC day `day`
(`YYYY-MM-DD`) and returns the new total. The `SpendStore` type describes it, and `memorySpendStore()` returns the
default.

## Approval

The client records approval requirements but does not prompt the user. `listTools()` sets `needsApproval` to `true` for
paid resources under `approval: 'paid'`, for every tool under `'always'`, for none under `'never'`. The framework
wrapper uses this flag to configure its approval mechanism.

## Errors

Failures throw `AgentCommerceError` with a `code`, an optional HTTP `status`, and a short message that starts with the
code and contains no secrets, backend bodies or headers. A gateway error code or x402 failure reason appears in the
message only when it is a bare token such as `INPUT_INVALID` or `invalid_payload`.

| Code                  | When                                                                                                                                                                   |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LIMIT_EXCEEDED`      | The price is above the per-call limit or the remaining daily allowance                                                                                                 |
| `NETWORK_NOT_ALLOWED` | The gateway offered no `exact` USDC payment on an allowed network                                                                                                      |
| `PAYMENT_FAILED`      | The gateway refused the signed payment, or could not confirm settlement; the message then says the payment may still complete on chain                                 |
| `BACKEND_ERROR`       | Any other non-2xx response, including invocation redirects; carries `status`. ACG settles nothing for a backend failure under the default `authorization` payment flow |
| `GATEWAY_UNREACHABLE` | The gateway could not be reached, or did not answer within 60 seconds                                                                                                  |

Resources that the client cannot use (not exposed over HTTP, not payable with x402, or outside `resources`) are excluded
from `listTools()` with one `console.warn` per resource.

## Not supported

MPP, Solana and NEAR rails (only x402 `exact` on EVM), the MCP transport, resources that require an AP2 mandate or other
authorization, and wallet key storage.

## License

Apache-2.0
