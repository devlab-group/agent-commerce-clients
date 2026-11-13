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
