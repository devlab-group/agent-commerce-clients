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
