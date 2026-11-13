// End-to-end run against a real gateway, opt-in: ACG_E2E=1 npm run test:e2e
//
// Defaults target the local demo stack (Anvil, MockUSDC, gateway on :8080). The variables are the
// ones python/e2e/acg_e2e.py reads, plus two the Python suite does not use. Override with:
//   ACG_E2E_GATEWAY_URL        gateway base URL
//   ACG_E2E_NETWORK            CAIP-2 network the buyer pays on
//   ACG_E2E_RPC_URL            JSON-RPC endpoint of that network, for balance and receipt checks
//   ACG_E2E_DEPLOY_FILE        JSON file with `asset` and `buyer.privateKey` (local demo stack only)
//   ACG_E2E_ASSET              ERC-20 (USDC) address; required unless ACG_E2E_DEPLOY_FILE is set
//   ACG_E2E_BUYER_PRIVATE_KEY  buyer key; required unless ACG_E2E_DEPLOY_FILE is set
//   ACG_E2E_RESOURCE           paid resource id
//   ACG_E2E_RESOURCE_INPUT     JSON input for that resource (TypeScript only)
//   ACG_E2E_PRICE              price of the resource in USDC (default 0.01)
//   ACG_E2E_LOW_LIMIT          maxPerCall below the price, for the refusal scenario (TypeScript only)
//   ACG_E2E_ADMIN_TOKEN        bearer token of GET /api/receipts
import { readFileSync } from 'node:fs';
import { createPublicClient, erc20Abi, http, parseUnits } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { beforeAll, describe, expect, it } from 'vitest';
import { AgentCommerceError } from '@devlab.group/agent-commerce-client';
import type { InvokeResult } from '@devlab.group/agent-commerce-client';
import { agentCommerceTools } from '../src/index';

const env = process.env;
const GATEWAY = env.ACG_E2E_GATEWAY_URL ?? 'http://127.0.0.1:8080';
const NETWORK = env.ACG_E2E_NETWORK ?? 'eip155:31337';
const RPC = env.ACG_E2E_RPC_URL ?? 'http://127.0.0.1:8545';
const RESOURCE = env.ACG_E2E_RESOURCE ?? 'market_report';
const INPUT = JSON.parse(env.ACG_E2E_RESOURCE_INPUT ?? '{}') as Record<string, unknown>;
const PRICE = parseUnits(env.ACG_E2E_PRICE ?? '0.01', 6);
const LOW_LIMIT = env.ACG_E2E_LOW_LIMIT ?? '0.005';
const ADMIN_TOKEN = env.ACG_E2E_ADMIN_TOKEN ?? 'local-demo-admin-token';
const DEPLOY_FILE = env.ACG_E2E_DEPLOY_FILE;

// Same switch as the Python suite: only the exact value 1 enables it, ACG_E2E=0 keeps it off
describe.skipIf(env.ACG_E2E !== '1')('e2e: AI SDK tool pays a real gateway', () => {
  const chain = createPublicClient({ transport: http(RPC) });
  let wallet: ReturnType<typeof privateKeyToAccount>;
  let asset: `0x${string}`;

  beforeAll(() => {
    const deploy = () => JSON.parse(readFileSync(DEPLOY_FILE ?? '', 'utf8'));
    wallet = privateKeyToAccount((env.ACG_E2E_BUYER_PRIVATE_KEY ?? deploy().buyer.privateKey) as `0x${string}`);
    asset = (env.ACG_E2E_ASSET ?? deploy().asset) as `0x${string}`;
  });

  const balance = () => chain.readContract({ address: asset, abi: erc20Abi, functionName: 'balanceOf', args: [wallet.address] });

  async function receipts() {
    const response = await fetch(`${GATEWAY}/api/receipts?limit=50`, { headers: { authorization: `Bearer ${ADMIN_TOKEN}` } });
    expect(response.status).toBe(200);
    return ((await response.json()) as { receipts: { id: string; payment?: { externalReference?: string } }[] }).receipts;
  }

  async function runTool(maxPerCall: string) {
    const tools = await agentCommerceTools({
      gatewayUrl: GATEWAY,
      wallet,
      networks: [NETWORK],
      limits: { maxPerCall, maxPerDay: '1.00' },
      resources: [RESOURCE],
    });
    const resource = tools[RESOURCE];
    expect(resource, `gateway lists ${RESOURCE}`).toBeDefined();
    expect(resource!.needsApproval).toBe(true);
    // The human approval step belongs to generateText; calling execute is what an approved call does
    const execute = resource!.execute as (input: unknown, options: unknown) => Promise<InvokeResult>;
    return execute(INPUT, { toolCallId: 'e2e', messages: [] });
  }

  it('pays a price within the limit: result, tx hash, balance drops by the price', async () => {
    const [balanceBefore, receiptsBefore] = [await balance(), await receipts()];

    const result = await runTool('0.05');

    expect(result.status).toBe(200);
    expect(result.payment?.success).toBe(true);
    expect(result.payment?.network).toBe(NETWORK);
    expect(result.payment?.payer?.toLowerCase()).toBe(wallet.address.toLowerCase());
    const tx = result.payment?.transaction as `0x${string}`;
    expect(tx).toMatch(/^0x[0-9a-fA-F]{64}$/);
    console.log(`e2e paid call: tx ${tx}, status ${result.status}, body ${JSON.stringify(result.body)}`);

    expect((await chain.waitForTransactionReceipt({ hash: tx })).status).toBe('success');
    const balanceAfter = await balance();
    console.log(`e2e balance: ${balanceBefore} -> ${balanceAfter} base units`);
    expect(balanceAfter).toBe(balanceBefore - PRICE);

    const after = await receipts();
    const known = new Set(receiptsBefore.map((r) => r.id));
    const added = after.filter((r) => !known.has(r.id));
    expect(added.map((r) => r.payment?.externalReference?.toLowerCase())).toContain(tx.toLowerCase());
  }, 120_000);

  it('refuses a price above the limit: nothing is signed, charged or logged', async () => {
    const [balanceBefore, receiptsBefore] = [await balance(), await receipts()];

    const error = await runTool(LOW_LIMIT).then(
      () => undefined,
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(AgentCommerceError);
    expect((error as AgentCommerceError).code).toBe('LIMIT_EXCEEDED');
    console.log(`e2e refused call: ${(error as Error).message}`);
    const [balanceAfter, receiptsAfter] = [await balance(), await receipts()];
    console.log(`e2e balance: ${balanceBefore} -> ${balanceAfter} base units, receipts ${receiptsBefore.length} -> ${receiptsAfter.length}`);
    expect(balanceAfter).toBe(balanceBefore);
    expect(receiptsAfter.map((r) => r.id)).toEqual(receiptsBefore.map((r) => r.id));
  }, 60_000);
});
