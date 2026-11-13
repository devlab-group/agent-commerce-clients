import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentCommerceError, createAgentCommerceClient, memorySpendStore } from '../src/index';
import type { AgentCommerceOptions, SpendStore } from '../src/index';
import { GATEWAY, account, fixture, micro, scripted, signed, toResponse, utcDay } from './support';
import type { MockResponse, Recorded } from './support';

const PAY: MockResponse[] = [
  { status: 402, paymentRequired: 'payment-required.json', bodyFixture: 'payment-required-body.json' },
  { status: 200, body: { ok: true }, paymentResponse: 'payment-response.json' },
];

function client(extra: Partial<AgentCommerceOptions>, limits = { maxPerCall: '0.05', maxPerDay: '1.00' }) {
  return createAgentCommerceClient({
    gatewayUrl: GATEWAY,
    wallet: account,
    networks: ['eip155:84532'],
    limits,
    ...extra,
  });
}

async function failure(promise: Promise<unknown>): Promise<AgentCommerceError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(AgentCommerceError);
    return error as AgentCommerceError;
  }
  throw new Error('expected the call to fail');
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-11-13T12:00:00Z'));
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('configuration', () => {
  it('refuses a raw private key as wallet without echoing it', () => {
    const key = '0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6';
    const create = () => client({ wallet: key as never });
    expect(create).toThrow(TypeError);
    expect(create).not.toThrow(key);
  });

  it('refuses an unusable spend store', () => {
    expect(() => client({ spendStore: {} as SpendStore })).toThrow(TypeError);
  });

  it.each([{}, { address: account.address }, null, 'wallet'])('refuses %j as wallet', (wallet) => {
    expect(() => client({ wallet: wallet as never })).toThrow(TypeError);
  });

  it.each(['ftp://merchant.example.com', 'file:///etc/passwd', 'merchant.example.com', ''])(
    'refuses %j as gatewayUrl',
    (gatewayUrl) => {
      expect(() => client({ gatewayUrl })).toThrow(TypeError);
    },
  );

  // Independent copy of the network table in docs/buyer-core.md
  const TABLE = [
    ['eip155:31337', false],
    ['eip155:84532', false],
    ['eip155:421614', false],
    ['eip155:8453', true],
    ['eip155:1', true],
    ['eip155:137', true],
    ['eip155:42161', true],
    ['eip155:43114', true],
  ] as const;

  it.each(TABLE)('network %s (mainnet: %s) is accepted, and without allowMainnet only a testnet is', (network, mainnet) => {
    expect(() => client({ networks: [network], allowMainnet: true })).not.toThrow();
    const without = () => client({ networks: [network] });
    if (mainnet) expect(without).toThrow(TypeError);
    else expect(without).not.toThrow();
  });

  // Ids outside the table are refused even with allowMainnet, including testnets that ACG does not support
  it.each([
    'eip155:11155111',
    'eip155:11155420',
    'eip155:80002',
    'eip155:43113',
    'eip155:10',
    'eip155:abc',
    'eip155:',
    'eip155:*',
    'eip155:84532 ',
    'constructor',
    '__proto__',
    84532,
  ])('refuses %j as a network even with allowMainnet', (network) => {
    expect(() => client({ networks: [network as string], allowMainnet: true })).toThrow(TypeError);
  });

  it('refuses a list that mixes an allowed network with an unknown one', () => {
    expect(() => client({ networks: ['eip155:84532', 'eip155:999999'], allowMainnet: true })).toThrow(TypeError);
  });

  it.each([true, 'true', 1])('treats allowMainnet %j as set only when it is boolean true', (allowMainnet) => {
    const create = () => client({ networks: ['eip155:8453'], allowMainnet: allowMainnet as never });
    if (allowMainnet === true) expect(create).not.toThrow();
    else expect(create).toThrow(TypeError);
  });

  it.each([['a', 1], 'a'])('refuses %j as the resources allowlist', (allowlist) => {
    expect(() => client({ resources: allowlist as never })).toThrow(TypeError);
  });
});

describe('spend limits', () => {
  it('allows a price exactly equal to maxPerCall', async () => {
    const mock = scripted(PAY);
    const result = await client({ fetch: mock.fetch }, { maxPerCall: '0.01', maxPerDay: '1' }).invoke('report', {});
    expect(result.status).toBe(200);
  });

  it('accumulates across calls and refuses once the day is full', async () => {
    const store = memorySpendStore();
    const limits = { maxPerCall: '0.05', maxPerDay: '0.02' };
    const mock = scripted([...PAY, ...PAY, PAY[0]!]);
    const c = client({ fetch: mock.fetch, spendStore: store }, limits);
    await c.invoke('report', {});
    await c.invoke('report', {});
    expect((await failure(c.invoke('report', {}))).code).toBe('LIMIT_EXCEEDED');
    expect(await store.add(utcDay(), 0n)).toBe(micro('0.02'));
    expect(signed(mock.requests)).toHaveLength(2);
  });

  it('keys the counter by UTC day and starts fresh after midnight', async () => {
    const days: string[] = [];
    const totals = new Map<string, bigint>();
    const store: SpendStore = {
      add(day, amount) {
        days.push(day);
        totals.set(day, (totals.get(day) ?? 0n) + amount);
        return totals.get(day) as bigint;
      },
    };
    const limits = { maxPerCall: '0.05', maxPerDay: '0.01' };
    const mock = scripted([...PAY, ...PAY]);
    const c = client({ fetch: mock.fetch, spendStore: store }, limits);
    vi.setSystemTime(new Date('2026-11-13T23:59:59Z'));
    await c.invoke('report', {});
    vi.setSystemTime(new Date('2026-11-14T00:00:01Z'));
    await c.invoke('report', {});
    expect(days).toEqual(['2026-11-13', '2026-11-14']);
  });

  it('reserves atomically: parallel calls cannot overspend the day', async () => {
    const totals = new Map<string, bigint>();
    const store: SpendStore = {
      async add(day, amount) {
        await new Promise((resolve) => setTimeout(resolve, 5));
        totals.set(day, (totals.get(day) ?? 0n) + amount);
        return totals.get(day) as bigint;
      },
    };
    const requests: Recorded[] = [];
    const fetch = async (url: string | URL | Request, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      requests.push({ url: String(url), method: 'POST', headers, body: undefined, redirect: undefined, signal: undefined });
      return toResponse(PAY[headers.has('payment-signature') ? 1 : 0]!);
    };
    const c = client({ fetch: fetch as typeof globalThis.fetch, spendStore: store }, { maxPerCall: '0.05', maxPerDay: '0.01' });
    const outcomes = await Promise.allSettled([c.invoke('report', {}), c.invoke('report', {})]);
    expect(outcomes.map((o) => o.status).sort()).toEqual(['fulfilled', 'rejected']);
    expect(signed(requests)).toHaveLength(1);
    expect(totals.get(utcDay())).toBe(micro('0.01'));
  });

  it('does not send a payment when the spend store fails', async () => {
    const store: SpendStore = {
      add() {
        throw new Error('disk full: secret-path');
      },
    };
    const mock = scripted([PAY[0]!]);
    const error = await failure(client({ fetch: mock.fetch, spendStore: store }).invoke('report', {}));
    expect(error.code).toBe('PAYMENT_FAILED');
    expect(error.message).not.toContain('secret-path');
    expect(signed(mock.requests)).toHaveLength(0);
  });
});

describe('payment offers', () => {
  const offer = (change: Record<string, unknown>, network = 'eip155:84532') => {
    const base = fixture('payment-required.json');
    return { ...base, accepts: [{ ...base.accepts[0], network, ...change }] };
  };
  const limits = { maxPerCall: '10', maxPerDay: '10' };

  it('pays above the x402 default cap of 1 USD when the configured limit allows it', async () => {
    const mock = scripted([{ status: 402, challenge: offer({ amount: '5000000' }) }, PAY[1]!]);
    const result = await client({ fetch: mock.fetch }, limits).invoke('report', {});
    expect(result.status).toBe(200);
    expect(signed(mock.requests)).toHaveLength(1);
  });

  it('signs any asset on local Anvil, where the test token is not a real USDC', async () => {
    const challenge = offer({ asset: '0x5FbDB2315678afecb367f032d93F642f64180aa3' }, 'eip155:31337');
    const mock = scripted([{ status: 402, challenge }, PAY[1]!]);
    const result = await client({ fetch: mock.fetch, networks: ['eip155:31337'] }, limits).invoke('report', {});
    expect(result.status).toBe(200);
    expect(signed(mock.requests)).toHaveLength(1);
  });

  // Independent copy of the USDC addresses in docs/buyer-core.md
  const USDC = {
    'eip155:84532': '0x036CbD53842c5426634e7929541eC2318f3dCF7e',
    'eip155:421614': '0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d',
    'eip155:8453': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
    'eip155:1': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
    'eip155:137': '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359',
    'eip155:42161': '0xaf88d065e77c8cC2239327C5EDb3A432268e5831',
    'eip155:43114': '0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E',
  } as const;

  it.each(Object.entries(USDC))('pays in the USDC of %s, compared case-insensitively', async (network, usdc) => {
    for (const asset of [usdc, usdc.toLowerCase(), usdc.toUpperCase().replace('0X', '0x')]) {
      const mock = scripted([{ status: 402, challenge: offer({ asset }, network) }, PAY[1]!]);
      const c = client({ fetch: mock.fetch, networks: [network], allowMainnet: true }, limits);
      expect((await c.invoke('report', {})).status).toBe(200);
      expect(signed(mock.requests)).toHaveLength(1);
    }
  });

  it.each(Object.entries(USDC))('refuses another network\'s USDC on %s', async (network, usdc) => {
    const foreign = Object.values(USDC).find((address) => address !== usdc)!;
    const mock = scripted([{ status: 402, challenge: offer({ asset: foreign }, network) }]);
    const c = client({ fetch: mock.fetch, networks: [network], allowMainnet: true }, limits);
    expect((await failure(c.invoke('report', {}))).code).toBe('NETWORK_NOT_ALLOWED');
    expect(signed(mock.requests)).toHaveLength(0);
  });

  it.each(['permit2', 'erc7710', 'something-new'])(
    'refuses an offer with assetTransferMethod %s before signing',
    async (assetTransferMethod) => {
      const store = memorySpendStore();
      const base = fixture('payment-required.json');
      const extra = { ...base.accepts[0].extra, assetTransferMethod };
      const mock = scripted([{ status: 402, challenge: offer({ extra }) }]);
      const error = await failure(client({ fetch: mock.fetch, spendStore: store }, limits).invoke('report', {}));
      expect(error.code).toBe('NETWORK_NOT_ALLOWED');
      expect(signed(mock.requests)).toHaveLength(0);
      expect(await store.add(utcDay(), 0n)).toBe(0n);
    },
  );

  it('accepts an offer whose extra does not name a transfer method', async () => {
    const { assetTransferMethod: _dropped, ...extra } = fixture('payment-required.json').accepts[0].extra;
    const mock = scripted([{ status: 402, challenge: offer({ extra }) }, PAY[1]!]);
    expect((await client({ fetch: mock.fetch }, limits).invoke('report', {})).status).toBe(200);
    expect(signed(mock.requests)).toHaveLength(1);
  });

  it('pays with the eip3009 offer when a permit2 offer comes first', async () => {
    const base = fixture('payment-required.json');
    const permit2 = { ...base.accepts[0], extra: { ...base.accepts[0].extra, assetTransferMethod: 'permit2' } };
    const mock = scripted([{ status: 402, challenge: { ...base, accepts: [permit2, base.accepts[0]] } }, PAY[1]!]);
    await client({ fetch: mock.fetch }, limits).invoke('report', {});
    const header = signed(mock.requests)[0]?.headers.get('payment-signature') ?? '';
    expect(JSON.parse(Buffer.from(header, 'base64').toString('utf8')).accepted).toEqual(base.accepts[0]);
  });

  it('refuses a non-string asset', async () => {
    const mock = scripted([{ status: 402, challenge: offer({ asset: 12345 }) }]);
    const error = await failure(client({ fetch: mock.fetch }, limits).invoke('report', {}));
    expect(error.code).toBe('NETWORK_NOT_ALLOWED');
  });

  it('refuses every offer when none is acceptable, without touching the daily total', async () => {
    const store = memorySpendStore();
    const base = fixture('payment-required.json');
    const challenge = { ...base, accepts: [{ ...base.accepts[0], scheme: 'upto' }, { ...base.accepts[0], asset: '0x01' }] };
    const mock = scripted([{ status: 402, challenge }]);
    const error = await failure(client({ fetch: mock.fetch, spendStore: store }, limits).invoke('report', {}));
    expect(error.message).toBe('NETWORK_NOT_ALLOWED: Gateway offered no exact USDC payment on an allowed network');
    expect(await store.add(utcDay(), 0n)).toBe(0n);
  });

  it('picks the acceptable offer from several, whatever its position', async () => {
    const base = fixture('payment-required.json');
    const good = base.accepts[0];
    const bad = [{ ...good, asset: '0x02' }, { ...good, scheme: 'upto' }, { ...good, network: 'eip155:1' }];
    const mock = scripted([{ status: 402, challenge: { ...base, accepts: [...bad, good, ...bad] } }, PAY[1]!]);
    await client({ fetch: mock.fetch }, limits).invoke('report', {});
    const header = signed(mock.requests)[0]?.headers.get('payment-signature') ?? '';
    expect(JSON.parse(Buffer.from(header, 'base64').toString('utf8')).accepted).toEqual(good);
  });

  it.each(['-10000', '1.5', '1e4', '0x10', '', ' 10', 'ten'])(
    'refuses the malformed price %j before signing and leaves the daily total alone',
    async (amount) => {
      const store = memorySpendStore();
      const mock = scripted([{ status: 402, challenge: offer({ amount }) }]);
      const error = await failure(client({ fetch: mock.fetch, spendStore: store }, limits).invoke('report', {}));
      expect(error.code).toBe('PAYMENT_FAILED');
      expect(signed(mock.requests)).toHaveLength(0);
      expect(await store.add(utcDay(), 0n)).toBe(0n);
    },
  );

  it('reports a failed settlement as PAYMENT_FAILED whatever the HTTP status', async () => {
    const mock = scripted([PAY[0]!, { status: 502, body: {}, paymentResponse: 'payment-response-failed.json' }]);
    const error = await failure(client({ fetch: mock.fetch }).invoke('report', {}));
    expect(error.message).toBe('PAYMENT_FAILED: Settlement not confirmed (insufficient_funds); the payment may still complete on chain');
    expect(error.status).toBeUndefined();
  });
});

describe('requests', () => {
  it('encodes the resource id and does not follow redirects', async () => {
    const mock = scripted([{ status: 200, body: {} }]);
    await client({ fetch: mock.fetch }).invoke('a/b?c', {});
    expect(mock.requests[0]?.url).toBe(`${GATEWAY}/api/resources/a%2Fb%3Fc/invoke`);
  });

  it('sends the invocation without redirects and with a 60 second timeout', async () => {
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    const mock = scripted([...PAY]);
    await client({ fetch: mock.fetch }).invoke('report', {});
    expect(mock.requests.map((r) => r.redirect)).toEqual(['manual', 'manual']);
    expect(mock.requests.every((r) => r.signal instanceof AbortSignal)).toBe(true);
    expect(timeout).toHaveBeenCalledTimes(2);
    expect(timeout).toHaveBeenCalledWith(60_000);
  });

  it('sends an empty JSON object when no input is given', async () => {
    const mock = scripted([{ status: 200, body: {} }]);
    await client({ fetch: mock.fetch }).invoke('report');
    expect(mock.requests[0]?.body).toBe('{}');
  });

  it('keeps a gateway path prefix and drops trailing slashes', async () => {
    const mock = scripted([{ status: 200, body: { resources: [] } }]);
    await client({ fetch: mock.fetch, gatewayUrl: `${GATEWAY}/acg//` }).listTools();
    expect(mock.requests[0]?.url).toBe(`${GATEWAY}/acg/api/resources`);
  });

  it('returns a non-JSON body as text', async () => {
    const fetch = async () => new Response('hello', { status: 200, headers: { 'content-type': 'text/plain' } });
    const result = await client({ fetch: fetch as typeof globalThis.fetch }).invoke('report', {});
    expect(result).toEqual({ status: 200, body: 'hello', payment: null });
  });

  it('returns a body that claims to be JSON but is not as text', async () => {
    const fetch = async () => new Response('{broken', { status: 200, headers: { 'content-type': 'application/json' } });
    const result = await client({ fetch: fetch as typeof globalThis.fetch }).invoke('report', {});
    expect(result).toEqual({ status: 200, body: '{broken', payment: null });
  });

  it('returns the result with no payment when PAYMENT-RESPONSE cannot be decoded', async () => {
    const fetch = async () =>
      new Response('{}', { status: 200, headers: { 'content-type': 'application/json', 'PAYMENT-RESPONSE': '!!not base64!!' } });
    const result = await client({ fetch: fetch as typeof globalThis.fetch }).invoke('report', {});
    expect(result).toEqual({ status: 200, body: {}, payment: null });
  });

  it('turns a 402 without a payment challenge into a backend error, unpaid', async () => {
    const mock = scripted([{ status: 402, body: { status: 'error' } }]);
    const error = await failure(client({ fetch: mock.fetch }).invoke('report', {}));
    expect(error).toMatchObject({ code: 'BACKEND_ERROR', status: 402 });
    expect(mock.requests).toHaveLength(1);
  });

  it('turns a redirect into a backend error instead of following it', async () => {
    const mock = scripted([{ status: 302, body: {} }]);
    const error = await failure(client({ fetch: mock.fetch }).invoke('report', {}));
    expect(error).toMatchObject({ code: 'BACKEND_ERROR', status: 302 });
  });
});

describe('discovery of untrusted gateway data', () => {
  const list = (resources: unknown[]) => scripted([{ status: 200, body: { resources } }]);

  it.each([{}, [], 'text', { resources: 'report' }, null])(
    'rejects the resource list body %j as a backend error',
    async (body) => {
      const mock = scripted([{ status: 200, body }]);
      const error = await failure(client({ fetch: mock.fetch }).listTools());
      expect(error).toMatchObject({ code: 'BACKEND_ERROR', status: 200 });
    },
  );

  it('ignores entries without an id and skips resources whose exposedVia is not a list', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const free = { pricing: { type: 'free' }, exposedVia: ['http'] };
    const mock = list([
      null,
      'report',
      { ...free, name: 'no id' },
      { ...free, id: 'string_via', exposedVia: 'http' },
      { ...free, id: 'ok' },
    ]);
    const tools = await client({ fetch: mock.fetch }).listTools();
    expect(tools.map((t) => t.name)).toEqual(['ok']);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0]?.[0])).toContain('"string_via"');
  });

  it.each([
    ['Done.', 'Done. Free.'],
    ['Done!', 'Done! Free.'],
    ['Done?', 'Done? Free.'],
    ['Done', 'Done. Free.'],
    ['Done..', 'Done.. Free.'],
  ])('ends the description %j with a full stop only when it has no terminator: %j', async (description, expected) => {
    const free = { id: 'f', description, pricing: { type: 'free' }, exposedVia: ['http'] };
    const [tool] = await client({ fetch: list([free]).fetch }).listTools();
    expect(tool?.description).toBe(expected);
  });

  it('describes a paid resource without a usable price as Paid, never as undefined', async () => {
    const paid = { id: 'p', name: 'Odd', pricing: { type: 'fixed' }, exposedVia: ['http'], paymentMethods: ['x402'] };
    const [tool] = await client({ fetch: list([paid]).fetch }).listTools();
    expect(tool).toMatchObject({ name: 'p', description: 'Odd. Paid.', paid: true, needsApproval: true });
  });

  // An empty allowlist excludes every resource
  it('keeps nothing when the allowlist is empty', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const mock = scripted([{ status: 200, body: fixture('resources.json') }]);
    expect(await client({ fetch: mock.fetch, resources: [] }).listTools()).toEqual([]);
  });
});

describe('error messages', () => {
  it('never echo backend bodies and only echo well-formed tokens', async () => {
    const hostile = { status: 500, body: { code: 'sk-live_secret', message: 'sk-live_secret', details: 'sk-live_secret' } };
    const error = await failure(client({ fetch: scripted([hostile]).fetch }).invoke('report', {}));
    expect(error.message).toBe('BACKEND_ERROR: Gateway answered HTTP 500');
    expect(JSON.stringify(error)).not.toContain('sk-live_secret');

    const known = await failure(
      client({ fetch: scripted([{ status: 400, bodyFixture: 'error-input-invalid.json' }]).fetch }).invoke('x', {}),
    );
    expect(known.message).toBe('BACKEND_ERROR: Gateway answered HTTP 400 (INPUT_INVALID)');
  });

  it('echoes the gateway code and x402 reason of a refused payment, and nothing else', async () => {
    const mock = scripted([PAY[0]!, { status: 402, paymentRequired: 'payment-required-rejected.json', bodyFixture: 'payment-rejected-body.json' }]);
    const error = await failure(client({ fetch: mock.fetch }).invoke('report', {}));
    expect(error.message).toBe('PAYMENT_FAILED: Gateway did not accept the payment (PAYMENT_INVALID, invalid_payload)');

    const badReason = { ...fixture('payment-response-failed.json'), errorReason: 'Insufficient Funds: 0xdeadbeef' };
    const fetch = async (_url: unknown, init?: RequestInit) => {
      if (!new Headers(init?.headers).has('payment-signature')) return toResponse(PAY[0]!);
      const header = Buffer.from(JSON.stringify(badReason)).toString('base64');
      return new Response('{}', { status: 402, headers: { 'content-type': 'application/json', 'PAYMENT-RESPONSE': header } });
    };
    const other = await failure(client({ fetch: fetch as typeof globalThis.fetch }).invoke('report', {}));
    expect(other.message).toBe('PAYMENT_FAILED: Gateway did not accept the payment');
  });
});

it('README shows the verified quickstart', () => {
  const root = new URL('../', import.meta.url);
  const readme = readFileSync(new URL('README.md', root), 'utf8');
  expect(readme).toContain(readFileSync(new URL('examples/quickstart.ts', root), 'utf8').trim());
});
