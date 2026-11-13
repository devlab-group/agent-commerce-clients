import { ExactEvmScheme } from '@x402/evm/exact/client';
import type { ClientEvmSigner } from '@x402/evm';
import { decodePaymentResponseHeader, x402Client, x402HTTPClient } from '@x402/fetch';
import type { PaymentRequired } from '@x402/fetch';

/** Client failure categories; see {@link AgentCommerceError} */
export type ErrorCode =
  | 'LIMIT_EXCEEDED'
  | 'NETWORK_NOT_ALLOWED'
  | 'PAYMENT_FAILED'
  | 'BACKEND_ERROR'
  | 'GATEWAY_UNREACHABLE';

/**
 * Client runtime failure with a short message for the model.
 * Client-generated messages omit secrets and start with the error code.
 */
export class AgentCommerceError extends Error {
  readonly code: ErrorCode;
  /** HTTP status, set for `BACKEND_ERROR` */
  readonly status?: number;

  constructor(code: ErrorCode, message: string, status?: number) {
    super(`${code}: ${message}`);
    this.name = 'AgentCommerceError';
    this.code = code;
    if (status !== undefined) this.status = status;
  }
}

/** Daily spending counter in USDC base units (6 decimals) */
export interface SpendStore {
  /**
   * Atomically adds `amount` (negative to roll back) to the total of the UTC
   * day `day` (`YYYY-MM-DD`) and returns the new total
   */
  add(day: string, amount: bigint): bigint | Promise<bigint>;
}

export interface AgentCommerceOptions {
  /** Gateway base URL, `http` or `https` */
  gatewayUrl: string;
  /** Wallet object such as a viem account; raw private keys are refused */
  wallet: ClientEvmSigner;
  /** Allowed EVM networks from the supported set, for example `eip155:84532` */
  networks: string[];
  /** Non-negative USDC limits as decimal strings with up to 6 decimal places */
  limits: { maxPerCall: string; maxPerDay: string };
  /** Allowlist of resource ids; every eligible resource when omitted */
  resources?: string[];
  /** Must be `true` before any mainnet network is accepted */
  allowMainnet?: boolean;
  /** Approval requirement recorded on tools, default `paid` */
  approval?: 'paid' | 'always' | 'never';
  /** Daily spending counter, process memory by default */
  spendStore?: SpendStore;
  /** Fetch implementation, `globalThis.fetch` by default */
  fetch?: typeof fetch;
}

export interface ResourceTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  paid: boolean;
  needsApproval: boolean;
}

/** Decoded `PAYMENT-RESPONSE` header: network, transaction hash, payer */
export type Payment = ReturnType<typeof decodePaymentResponseHeader>;

export interface InvokeResult {
  status: number;
  body: unknown;
  payment: Payment | null;
}

export interface AgentCommerceClient {
  listTools(): Promise<ResourceTool[]>;
  invoke(resource: string, input?: Record<string, unknown>): Promise<InvokeResult>;
}

/** Create a spend store in process memory */
export function memorySpendStore(): SpendStore {
  const totals = new Map<string, bigint>();
  return {
    add(day, amount) {
      const total = (totals.get(day) ?? 0n) + amount;
      totals.set(day, total);
      return total;
    },
  };
}

// Supported EVM networks and their USDC contracts; local Anvil allows any asset
const NETWORKS = new Map<string, { mainnet: boolean; usdc?: string }>([
  ['eip155:31337', { mainnet: false }],
  ['eip155:84532', { mainnet: false, usdc: '0x036CbD53842c5426634e7929541eC2318f3dCF7e' }],
  ['eip155:421614', { mainnet: false, usdc: '0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d' }],
  ['eip155:8453', { mainnet: true, usdc: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913' }],
  ['eip155:1', { mainnet: true, usdc: '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48' }],
  ['eip155:137', { mainnet: true, usdc: '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359' }],
  ['eip155:42161', { mainnet: true, usdc: '0xaf88d065e77c8cC2239327C5EDb3A432268e5831' }],
  ['eip155:43114', { mainnet: true, usdc: '0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E' }],
]);
const TIMEOUT_MS = 60_000;

// Accept bare gateway codes and x402 reason tokens for error details
const token = (value: unknown, pattern: RegExp) =>
  typeof value === 'string' && pattern.test(value) ? value : undefined;
const gatewayToken = (value: unknown) => token(value, /^[A-Z_]{1,64}$/);
const reasonToken = (value: unknown) => token(value, /^[a-z0-9_]{1,64}$/);
const detail = (...tokens: (string | undefined)[]) => {
  const text = tokens.filter(Boolean).join(', ');
  return text ? ` (${text})` : '';
};

function toMicro(name: string, value: unknown): bigint {
  if (typeof value !== 'string' || !/^\d+(\.\d{1,6})?$/.test(value)) {
    throw new TypeError(`${name} must be a non-negative decimal string with at most 6 decimals`);
  }
  const [whole, fraction = ''] = value.split('.');
  return BigInt(whole + fraction.padEnd(6, '0'));
}

const has = (list: unknown, item: string) => Array.isArray(list) && list.includes(item);

interface Reply {
  status: number;
  headers: Headers;
  text: string;
}

interface Resource {
  id: string;
  name?: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
  pricing?: { type?: string; amount?: string; currency?: string };
  exposedVia?: string[];
  paymentMethods?: string[];
}

/** Create a client, throwing if configuration is invalid */
export function createAgentCommerceClient(options: AgentCommerceOptions): AgentCommerceClient {
  if (typeof options !== 'object' || options === null) throw new TypeError('options are required');
  const { gatewayUrl, wallet, networks, resources: allowlist, allowMainnet, approval = 'paid' } = options;

  if (typeof gatewayUrl !== 'string') throw new TypeError('gatewayUrl is required');
  if (!/^https?:$/.test(new URL(gatewayUrl).protocol)) throw new TypeError('gatewayUrl must be http or https');
  const base = gatewayUrl.replace(/\/+$/, '');

  if (typeof wallet !== 'object' || wallet === null || typeof wallet.signTypedData !== 'function') {
    throw new TypeError('wallet must be an account object such as a viem account, not a raw private key');
  }

  if (!Array.isArray(networks) || networks.length === 0) throw new TypeError('networks must not be empty');
  for (const network of networks) {
    const known = NETWORKS.get(network);
    if (!known) throw new TypeError('networks may only list the EVM networks ACG supports, such as eip155:84532');
    if (known.mainnet && allowMainnet !== true) {
      throw new TypeError(`${network} is a mainnet; set allowMainnet to true to accept it`);
    }
  }

  const maxPerCall = toMicro('limits.maxPerCall', options.limits?.maxPerCall);
  const maxPerDay = toMicro('limits.maxPerDay', options.limits?.maxPerDay);

  if (!['paid', 'always', 'never'].includes(approval)) throw new TypeError('approval must be paid, always or never');
  if (allowlist !== undefined && (!Array.isArray(allowlist) || allowlist.some((id) => typeof id !== 'string'))) {
    throw new TypeError('resources must be an array of resource ids');
  }
  const store = options.spendStore ?? memorySpendStore();
  if (typeof store.add !== 'function') throw new TypeError('spendStore must have an add method');
  const fetchImpl = options.fetch ?? globalThis.fetch;

  // Accept exact EIP-3009 offers on configured networks, using their USDC asset
  // This client does not set up Permit2 approvals or ERC-7710 delegations
  const acceptable = (offer: PaymentRequired['accepts'][number]) => {
    const usdc = NETWORKS.get(offer.network)?.usdc;
    return (
      offer.scheme === 'exact' &&
      networks.includes(offer.network) &&
      (offer.extra?.assetTransferMethod ?? 'eip3009') === 'eip3009' &&
      (usdc === undefined || (typeof offer.asset === 'string' && offer.asset.toLowerCase() === usdc.toLowerCase()))
    );
  };

  // Register the exact scheme for each configured network
  const x402 = new x402Client();
  for (const network of networks) x402.register(network as `${string}:${string}`, new ExactEvmScheme(wallet));
  // Use the hook's limits instead of x402's $1 cap and token allowlist
  x402.setSpendControls(false);
  x402.onBeforePaymentCreation(async ({ selectedRequirements: offer }) => {
    if (!acceptable(offer)) {
      throw new AgentCommerceError('NETWORK_NOT_ALLOWED', 'Gateway offered a payment that is not acceptable');
    }
    if (!/^\d+$/.test(offer.amount)) throw new AgentCommerceError('PAYMENT_FAILED', 'Gateway sent an invalid price');
    const amount = BigInt(offer.amount);
    if (amount > maxPerCall) throw new AgentCommerceError('LIMIT_EXCEEDED', 'Price is above the per-call limit');
    // Roll back an over-limit reservation; retain it after signing
    const day = new Date().toISOString().slice(0, 10);
    if ((await store.add(day, amount)) > maxPerDay) {
      await store.add(day, -amount);
      throw new AgentCommerceError('LIMIT_EXCEEDED', 'Price is above the remaining daily limit');
    }
  });
  const http = new x402HTTPClient(x402);

  async function send(url: string, init: RequestInit): Promise<Reply> {
    try {
      const response = await fetchImpl(url, { ...init, signal: AbortSignal.timeout(TIMEOUT_MS) });
      return { status: response.status, headers: response.headers, text: await response.text() };
    } catch {
      throw new AgentCommerceError('GATEWAY_UNREACHABLE', 'Gateway could not be reached');
    }
  }

  const ok = (reply: Reply) => reply.status >= 200 && reply.status < 300;

  function parse(reply: Reply): unknown {
    if (!(reply.headers.get('content-type') ?? '').includes('json')) return reply.text;
    try {
      return JSON.parse(reply.text);
    } catch {
      return reply.text;
    }
  }

  const gatewayCode = (reply: Reply) => gatewayToken((parse(reply) as { code?: unknown } | null)?.code);

  function backendError(reply: Reply) {
    return new AgentCommerceError(
      'BACKEND_ERROR',
      `Gateway answered HTTP ${reply.status}${detail(gatewayCode(reply))}`,
      reply.status,
    );
  }

  function settlement(reply: Reply): Payment | null {
    const header = reply.headers.get('PAYMENT-RESPONSE');
    if (!header) return null;
    try {
      return decodePaymentResponseHeader(header);
    } catch {
      return null;
    }
  }

  function challenge(reply: Reply): PaymentRequired | null {
    try {
      const required = http.getPaymentRequiredResponse((name) => reply.headers.get(name), undefined);
      return Array.isArray(required.accepts) ? required : null;
    } catch {
      return null;
    }
  }

  async function sign(required: PaymentRequired) {
    try {
      // Filter unacceptable offers before x402 selects one
      const accepts = required.accepts.filter(acceptable);
      if (accepts.length === 0) {
        throw new AgentCommerceError('NETWORK_NOT_ALLOWED', 'Gateway offered no exact USDC payment on an allowed network');
      }
      return await x402.createPaymentPayload({ ...required, accepts });
    } catch (error) {
      if (error instanceof AgentCommerceError) throw error;
      throw new AgentCommerceError('PAYMENT_FAILED', 'Payment could not be created');
    }
  }

  function finish(reply: Reply, paid: boolean): InvokeResult {
    const payment = settlement(reply);
    if (payment?.success === false || (paid && reply.status === 402)) {
      const reason = reasonToken(payment?.errorReason ?? challenge(reply)?.error);
      if (reply.status === 402) {
        throw new AgentCommerceError('PAYMENT_FAILED', `Gateway did not accept the payment${detail(gatewayCode(reply), reason)}`);
      }
      // Failed settlement on another status (ACG uses 502) may still complete on chain
      const transaction = token(payment?.transaction, /^0x[0-9a-fA-F]{64}$/);
      throw new AgentCommerceError(
        'PAYMENT_FAILED',
        `Settlement not confirmed${detail(reason, transaction && `tx ${transaction}`)}; the payment may still complete on chain`,
      );
    }
    if (!ok(reply)) throw backendError(reply);
    return { status: reply.status, body: parse(reply), payment };
  }

  return {
    async listTools() {
      const reply = await send(`${base}/api/resources`, { headers: { accept: 'application/json' } });
      if (!ok(reply)) throw backendError(reply);
      const list = (parse(reply) as { resources?: unknown } | null)?.resources;
      if (!Array.isArray(list)) {
        throw new AgentCommerceError('BACKEND_ERROR', 'Gateway sent an unexpected resource list', reply.status);
      }

      const tools: ResourceTool[] = [];
      for (const resource of list as Resource[]) {
        if (typeof resource?.id !== 'string') continue;
        const paid = resource.pricing?.type !== 'free';
        const skipped = !has(resource.exposedVia, 'http')
          ? 'not exposed over http'
          : paid && !has(resource.paymentMethods, 'x402')
            ? 'not payable with x402'
            : allowlist && !allowlist.includes(resource.id)
              ? 'not in the resources allowlist'
              : undefined;
        if (skipped) {
          console.warn(`agent-commerce: skipped resource "${resource.id}": ${skipped}`);
          continue;
        }
        const text = resource.description || resource.name || resource.id;
        const { amount, currency } = resource.pricing ?? {};
        tools.push({
          name: resource.id,
          description: `${/[.!?]$/.test(text) ? text : `${text}.`} ${
            !paid ? 'Free.' : amount && currency ? `Costs ${amount} ${currency} per call.` : 'Paid.'
          }`,
          inputSchema: resource.inputSchema ?? { type: 'object', properties: {} },
          paid,
          needsApproval: approval === 'always' || (approval === 'paid' && paid),
        });
      }
      return tools;
    },

    async invoke(resource, input = {}) {
      const url = `${base}/api/resources/${encodeURIComponent(resource)}/invoke`;
      // Keep signed payments at the configured gateway by refusing redirects
      const post = (headers: Record<string, string> = {}) =>
        send(url, {
          method: 'POST',
          redirect: 'manual',
          headers: { 'content-type': 'application/json', accept: 'application/json', ...headers },
          body: JSON.stringify(input),
        });

      const first = await post();
      if (first.status !== 402) return finish(first, false);

      const required = challenge(first);
      if (!required) throw backendError(first);
      const payload = await sign(required);
      // Send the signed payment once; do not retry it
      return finish(await post(http.encodePaymentSignatureHeader(payload)), true);
    },
  };
}
