# @devlab.group/agent-commerce-ai-sdk

[Vercel AI SDK](https://ai-sdk.dev) tools for resources behind an [Agent Commerce Gateway](https://acg.devlab.group)
(ACG). `agentCommerceTools()` turns eligible gateway resources into AI SDK tools. The model calls them like other tools.
Paid calls use [x402](https://www.x402.org) within spending limits that the model cannot change, and require human
approval by default.

The package maps resources to tools. Discovery, payment and limits live in
[`@devlab.group/agent-commerce-client`](https://www.npmjs.com/package/@devlab.group/agent-commerce-client), documented
on the [buyer core page](https://acg.devlab.group/docs/integrations/buyer-core).

## Install

```bash
npm install @devlab.group/agent-commerce-ai-sdk ai viem
```

Requires Node.js 22 or later and AI SDK 7 (`ai` is a peer dependency). `viem` provides the wallet account.

## Quick start

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

Options are those of the core client: `gatewayUrl`, `wallet` (a viem account), `networks`, `limits` (`maxPerCall`,
`maxPerDay`, USDC decimal strings), and the optional `resources`, `allowMainnet`, `approval`, `spendStore` and `fetch`.
See the [configuration table](https://acg.devlab.group/docs/integrations/buyer-core#configuration). Invalid
configuration rejects the returned promise.

Each tool uses the resource id as its name and the resource's JSON input schema for arguments. Its description includes
the price (`Costs 0.01 USDC per call.`, `Free.`, or `Paid.` when no fixed price is available).

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

A limit, payment or gateway failure throws `AgentCommerceError`. The AI SDK passes the error text to the model as a tool
error, for example `AgentCommerceError: LIMIT_EXCEEDED: Price is above the per-call limit`, so the model can explain the
failure or try another action. The error carries a `code` (`LIMIT_EXCEEDED`, `NETWORK_NOT_ALLOWED`, `PAYMENT_FAILED`,
`BACKEND_ERROR`, `GATEWAY_UNREACHABLE`) and no secrets. See the
[error table](https://acg.devlab.group/docs/integrations/buyer-core#errors).

## Tests

From the `typescript/` directory of the repository:

```bash
npm install
npm test                    # unit and conformance tests, type-check of the examples
ACG_E2E=1 ACG_E2E_DEPLOY_FILE=path/to/local.json npm run test:e2e  # real gateway, see test/e2e.test.ts for the variables
```

## License

Apache-2.0
