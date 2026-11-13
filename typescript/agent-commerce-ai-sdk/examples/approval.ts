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
