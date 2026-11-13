import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentCommerceError, createAgentCommerceClient, memorySpendStore } from '../src/index';
import { GATEWAY, account, fixture, micro, scenarios, scripted, signed, toResponse, utcDay } from './support';
import type { MockResponse } from './support';

const KNOWN_EXPECT_KEYS = new Set([
  'tools',
  'toolNames',
  'needsApproval',
  'skipped',
  'error',
  'requests',
  'signatures',
  'signedOffer',
  'result',
  'spentAfter',
]);

function options(config: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  const merged: Record<string, unknown> = { ...scenarios.defaults, ...config };
  for (const key of Object.keys(merged)) if (merged[key] === null) delete merged[key];
  const { maxPerCall, maxPerDay, ...rest } = merged;
  return { ...rest, limits: { maxPerCall, maxPerDay }, wallet: account, ...extra } as never;
}

function expectError(
  error: unknown,
  expected: { code: string; status?: number; messageContains?: string[]; messageExcludes?: string[] },
) {
  expect(error).toBeInstanceOf(AgentCommerceError);
  expect((error as AgentCommerceError).code).toBe(expected.code);
  if (expected.status !== undefined) expect((error as AgentCommerceError).status).toBe(expected.status);
  // The message starts with the code because frameworks often show the model only the message
  expect((error as Error).message.startsWith(`${expected.code}: `)).toBe(true);
  expect((error as Error).message).not.toContain(scenarios.testSignerPrivateKey);
  for (const part of expected.messageContains ?? []) expect((error as Error).message).toContain(part);
  for (const part of expected.messageExcludes ?? []) expect((error as Error).message).not.toContain(part);
}

async function failure(promise: Promise<unknown>) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error('expected the call to fail');
}

beforeEach(() => {
  // Pin the UTC day to keep spentToday and the day key from straddling midnight
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-11-13T12:00:00Z'));
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it('loads a non-trivial suite', () => {
  expect(scenarios.version).toBe(1);
  expect(scenarios.config.length).toBeGreaterThan(0);
  expect(scenarios.discovery.length).toBeGreaterThan(0);
  expect(scenarios.invocations.length).toBeGreaterThan(0);
});

describe('conformance: config', () => {
  for (const scenario of scenarios.config) {
    it(scenario.name, () => {
      const create = () => createAgentCommerceClient(options(scenario.config, { fetch: scripted([]).fetch }));
      if (scenario.expect === 'ok') {
        expect(create).not.toThrow();
      } else {
        expect(scenario.expect).toBe('error');
        expect(create).toThrow();
        // A configuration error is a plain Error, not one of the runtime codes
        expect(create).not.toThrow(AgentCommerceError);
      }
    });
  }
});

describe('conformance: discovery', () => {
  for (const scenario of scenarios.discovery) {
    it(scenario.name, async () => {
      for (const key of Object.keys(scenario.expect)) expect(KNOWN_EXPECT_KEYS.has(key)).toBe(true);
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      const response: MockResponse = scenario.resources
        ? { status: 200, body: fixture(scenario.resources) }
        : scenario.response;
      const mock = scripted([response]);
      const client = createAgentCommerceClient(options(scenario.config, { fetch: mock.fetch }));

      if (scenario.expect.error) {
        expectError(await failure(client.listTools()), scenario.expect.error);
        return;
      }
      const tools = await client.listTools();
      expect(mock.requests).toHaveLength(1);
      expect(mock.requests[0]).toMatchObject({ url: `${GATEWAY}/api/resources`, method: 'GET' });

      if (scenario.expect.tools) expect(tools).toEqual(scenario.expect.tools);
      if (scenario.expect.toolNames) expect(tools.map((t) => t.name)).toEqual(scenario.expect.toolNames);
      if (scenario.expect.needsApproval) {
        expect(Object.fromEntries(tools.map((t) => [t.name, t.needsApproval]))).toEqual(scenario.expect.needsApproval);
      }
      if (scenario.expect.skipped) {
        const messages = warn.mock.calls.map((call) => String(call[0]));
        expect(messages).toHaveLength(scenario.expect.skipped.length);
        for (const id of scenario.expect.skipped) {
          expect(messages.filter((m) => m.includes(`"${id}"`))).toHaveLength(1);
        }
      }
    });
  }
});

describe('conformance: invocations', () => {
  for (const scenario of scenarios.invocations) {
    it(scenario.name, async () => {
      for (const key of Object.keys(scenario.expect)) expect(KNOWN_EXPECT_KEYS.has(key)).toBe(true);
      const store = memorySpendStore();
      if (scenario.spentToday) await store.add(utcDay(), micro(scenario.spentToday));
      const mock = scripted(scenario.responses);
      const client = createAgentCommerceClient(options(scenario.config, { fetch: mock.fetch, spendStore: store }));

      const { resource, input } = scenario.call;
      const outcome = client.invoke(resource, input);
      if (scenario.expect.error) {
        expectError(await failure(outcome), scenario.expect.error);
      } else {
        const result = await outcome;
        const { status, body, payment } = scenario.expect.result;
        expect(result).toEqual({ status, body, payment: payment === null ? null : fixture(payment) });
      }

      expect(mock.state.overrun).toBe(false);
      expect(mock.requests.every((request) => request.redirect === 'manual')).toBe(true);
      expect(mock.requests).toHaveLength(scenario.expect.requests);
      expect(signed(mock.requests)).toHaveLength(scenario.expect.signatures);
      expect(mock.requests[0]).toMatchObject({
        url: `${GATEWAY}/api/resources/${resource}/invoke`,
        method: 'POST',
        body: JSON.stringify(input),
      });
      if (scenario.expect.signedOffer) {
        const header = signed(mock.requests)[0]?.headers.get('payment-signature') ?? '';
        const payload = JSON.parse(Buffer.from(header, 'base64').toString('utf8'));
        expect(payload.accepted).toMatchObject(scenario.expect.signedOffer);
      }
      expect(await store.add(utcDay(), 0n)).toBe(micro(scenario.expect.spentAfter));
    });
  }
});

it('toResponse base64-encodes fixtures the way the gateway does', () => {
  const response = toResponse({ status: 402, paymentRequired: 'payment-required.json' });
  const header = response.headers.get('PAYMENT-REQUIRED') ?? '';
  expect(JSON.parse(Buffer.from(header, 'base64').toString('utf8'))).toEqual(fixture('payment-required.json'));
});
