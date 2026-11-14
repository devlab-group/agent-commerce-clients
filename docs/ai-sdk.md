# Vercel AI SDK

`@devlab.group/agent-commerce-ai-sdk` lets [Vercel AI SDK](https://ai-sdk.dev) agents use resources behind an Agent
Commerce Gateway (ACG). `agentCommerceTools()` discovers eligible resources and returns an AI SDK tool for each. The
agent calls free tools directly and pays for paid ones with [x402](https://www.x402.org), within spending limits that
the model cannot change.

The package maps resources to tools. The core client handles discovery, payments and limits; see the
[buyer core](https://acg.devlab.group/docs/integrations/buyer-core) contract.

## Install

```bash
npm install @devlab.group/agent-commerce-ai-sdk ai viem
```

Requires Node.js 22 or later and AI SDK 7 (`ai` is a peer dependency). `viem` provides the wallet account.

## Example

```ts
import { generateText, isStepCount } from 'ai';
import { privateKeyToAccount } from 'viem/accounts';
import { agentCommerceToolApproval, agentCommerceTools } from '@devlab.group/agent-commerce-ai-sdk';

const tools = await agentCommerceTools({
  gatewayUrl: 'https://merchant.example.com',
  wallet: privateKeyToAccount(process.env.BUYER_PRIVATE_KEY as `0x${string}`),
  networks: ['eip155:84532'],
  limits: { maxPerCall: '0.05', maxPerDay: '1.00' },
});

const { text, content } = await generateText({
  model: 'openai/gpt-5-mini',
  prompt: 'Get me the market report on solar panels.',
  tools,
  // Paid tools stop for human approval; free tools run directly
  toolApproval: agentCommerceToolApproval(tools),
  stopWhen: isStepCount(5),
});

for (const part of content) {
  if (part.type === 'tool-approval-request') console.log('Needs approval:', part.toolCall.toolName, part.toolCall.input);
}
console.log(text);
```

For a paid market report, generation pauses with an approval request, which the loop prints. See [Approval](#approval)
to respond and continue.

Use a dedicated wallet that holds little more than the daily limit, on a network listed in `networks`. Payments use that
network's USDC, except on local Anvil, which allows any asset. Testnets such as Base Sepolia (`eip155:84532`) need no
extra opt-in; mainnet networks require `allowMainnet: true`. Supported networks are listed on the
[buyer core page](https://acg.devlab.group/docs/integrations/buyer-core#configuration).

| Option                                  | Meaning                                                                                             |
| --------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `gatewayUrl`                            | Gateway base URL                                                                                    |
| `wallet`                                | viem account; raw private keys are refused                                                          |
| `networks`                              | Allowed CAIP-2 EVM networks                                                                         |
| `limits.maxPerCall`, `limits.maxPerDay` | USDC limits per call and per UTC day, as decimal strings                                            |
| `resources`                             | Optional allowlist of resource ids                                                                  |
| `approval`                              | Optional: `paid` (default), `always` or `never`                                                     |
| `allowMainnet`, `spendStore`, `fetch`   | Optional, see the [buyer core](https://acg.devlab.group/docs/integrations/buyer-core#configuration) |

## Approval

In AI SDK 7, `generateText`, `streamText` and `ToolLoopAgent` take their approval rules from the `toolApproval` option.
`agentCommerceToolApproval(tools)` returns `'user-approval'` for each tool whose `needsApproval` flag is `true`. The
default `approval: 'paid'` requires approval for paid tools and lets free tools run directly; `'always'` requires
approval for every tool, and `'never'` for none. A tool that needs approval pauses generation with a
`tool-approval-request`. Your application responds with a `tool-approval-response` to continue:

```ts
import { createInterface } from 'node:readline/promises';
import { generateText, isStepCount } from 'ai';
import type { ModelMessage } from 'ai';
import { privateKeyToAccount } from 'viem/accounts';
import { agentCommerceToolApproval, agentCommerceTools } from '@devlab.group/agent-commerce-ai-sdk';

const tools = await agentCommerceTools({
  gatewayUrl: 'https://merchant.example.com',
  wallet: privateKeyToAccount(process.env.BUYER_PRIVATE_KEY as `0x${string}`),
  networks: ['eip155:84532'],
  limits: { maxPerCall: '0.05', maxPerDay: '1.00' },
  approval: 'paid', // the default: paid resources wait for a human, free ones run directly
});

async function confirm(question: string) {
  const terminal = createInterface({ input: process.stdin, output: process.stdout });
  const answer = await terminal.question(question);
  terminal.close();
  return answer.trim().toLowerCase() === 'y';
}

const messages: ModelMessage[] = [{ role: 'user', content: 'Get me the market report on solar panels.' }];
const options = {
  model: 'openai/gpt-5-mini',
  tools,
  toolApproval: agentCommerceToolApproval(tools),
  stopWhen: isStepCount(5),
} as const;

let result = await generateText({ ...options, messages });
messages.push(...result.response.messages);

// A tool that needs approval stops the loop with a request instead of running
const requests = result.content.filter((part) => part.type === 'tool-approval-request');
if (requests.length > 0) {
  const responses = [];
  for (const request of requests) {
    const approved = await confirm(`Run ${request.toolCall.toolName} ${JSON.stringify(request.toolCall.input)}? [y/N] `);
    responses.push({ type: 'tool-approval-response' as const, approvalId: request.approvalId, approved });
  }
  messages.push({ role: 'tool', content: responses });
  result = await generateText({ ...options, messages });
}
console.log(result.text);
```

Entries in `toolApproval` override a tool's `needsApproval` flag. Each generated tool carries this flag, which applies
when `toolApproval` has no entry for it. To change one tool, spread the helper's result first:
`{ ...agentCommerceToolApproval(tools), report: 'approved' }`. A `toolApproval` function replaces the per-tool flags
altogether.

## Errors

A limit or payment failure throws an `AgentCommerceError`; the AI SDK passes its text to the model as a tool error and
the generation continues. The codes are `LIMIT_EXCEEDED`, `NETWORK_NOT_ALLOWED`, `PAYMENT_FAILED`, `BACKEND_ERROR` and
`GATEWAY_UNREACHABLE`; see [Errors](https://acg.devlab.group/docs/integrations/buyer-core#errors).

## More

- [Buyer core contract](https://acg.devlab.group/docs/integrations/buyer-core): configuration, discovery, spending
  limits, errors
- [Package README](https://github.com/devlab-group/agent-commerce-clients/tree/master/typescript/agent-commerce-ai-sdk#readme)

## End-to-end evidence

The runs below called the tool's `execute` function through `test/e2e.test.ts`, without a language model.

Local demo stack (Anvil chain `eip155:31337`, MockUSDC, resource `market_report` at 0.01 USDC). These transactions exist
only on the local chain.

| Scenario                                                          | Result                                                                                                                                                                                        |
| ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Price within limit (`maxPerCall: '0.05'`)                         | Status 200, `PAYMENT-RESPONSE` present, tx `0xda69fd586106f1666259616abf664d85450ed96f1ea0b3b89615d06e94508478`                                                                               |
| Same, second run                                                  | Status 200, tx `0x202045c392da03d445568cc0e432a072270635ab4a4a71be0018b5cbf6ceae96`; buyer balance 99960000 to 99950000 base units (10000 = 0.01 USDC); a gateway receipt carries the tx hash |
| Same, third run (after the USDC and transfer-method offer checks) | Status 200, tx `0x0109c19bbd56493b7f66ec9f7ab17144d7f9bb3901d4cbc3ad826428f26b7878`; buyer balance 99870000 to 99860000 base units                                                            |
| Price above limit (`maxPerCall: '0.005'`)                         | `LIMIT_EXCEEDED` before signing; buyer balance unchanged (99860000 in the latest run); gateway receipt list unchanged (17 before, 17 after in the latest run)                                 |

Base Sepolia (`eip155:84532`), 2026-11-13: the demo merchant from ACG's `deploy/testnet-demo` run locally (gateway
2.7.1, public facilitator `https://x402.org/facilitator`, Circle USDC, `report` at 0.01 USDC, a temporary merchant
wallet as `payTo`).

| Case                                      | Result                                                                                                                                                                                                                                                               |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Price within limit (`maxPerCall: '0.05'`) | Status 200, `PAYMENT-RESPONSE` present, tx [`0x9af6317a2b4c4805d7f9d1083191c3373164effd897f9577b5871fd8ac16e546`](https://sepolia.basescan.org/tx/0x9af6317a2b4c4805d7f9d1083191c3373164effd897f9577b5871fd8ac16e546); buyer balance 19870000 to 19860000 base units |
| Price above limit (`maxPerCall: '0.005'`) | `LIMIT_EXCEEDED` before signing; buyer balance unchanged; gateway receipt list unchanged                                                                                                                                                                             |

The first attempt failed at the public facilitator (`invalid_exact_evm_transaction_failed`) without a transaction or
charge. The gateway returned 502 `PAYMENT_SETTLEMENT_FAILED`; the rerun recorded above passed.
