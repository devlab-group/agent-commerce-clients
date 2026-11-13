import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { asSchema, generateText, isStepCount } from 'ai';
import type { ModelMessage, ToolSet } from 'ai';
import { MockLanguageModelV4 } from 'ai/test';
import { privateKeyToAccount } from 'viem/accounts';
import { describe, expect, it, vi } from 'vitest';
import { agentCommerceToolApproval, agentCommerceTools } from '../src/index';
import type { AgentCommerceOptions } from '../src/index';

const conformance = fileURLToPath(new URL('../../../conformance/', import.meta.url));
const fixture = (name: string) => JSON.parse(readFileSync(`${conformance}fixtures/${name}`, 'utf8'));
const b64 = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64');
const wallet = privateKeyToAccount(JSON.parse(readFileSync(`${conformance}scenarios.json`, 'utf8')).testSignerPrivateKey);
const json = { 'content-type': 'application/json' };

// Gateway double: discovery, then 402 for an unpaid invoke and 200 once a payment signature arrives
function gateway(invoked: string[] = []) {
  return (async (url: string | URL | Request, init?: RequestInit) => {
    const target = String(url);
    if (target.endsWith('/api/resources')) {
      return new Response(JSON.stringify(fixture('resources.json')), { status: 200, headers: json });
    }
    invoked.push(target);
    if (target.includes('/weather_basic/')) {
      return new Response(JSON.stringify({ status: 'error', code: 'RESOURCE_NOT_FOUND' }), { status: 404, headers: json });
    }
    if (!new Headers(init?.headers).has('payment-signature')) {
      const headers = { ...json, 'PAYMENT-REQUIRED': b64(fixture('payment-required.json')) };
      return new Response(JSON.stringify(fixture('payment-required-body.json')), { status: 402, headers });
    }
    const headers = { ...json, 'PAYMENT-RESPONSE': b64(fixture('payment-response.json')) };
    return new Response(JSON.stringify({ ok: true, url: '/api/report' }), { status: 200, headers });
  }) as typeof globalThis.fetch;
}

const options = (extra: Partial<AgentCommerceOptions> = {}): AgentCommerceOptions => ({
  gatewayUrl: 'https://merchant.example.com',
  wallet,
  networks: ['eip155:84532'],
  limits: { maxPerCall: '0.05', maxPerDay: '1.00' },
  fetch: gateway(),
  ...extra,
});

const usage = {
  inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 1, text: 1, reasoning: 0 },
};

// Mock model: first turn calls `toolName`, the next turn answers
function callsThenAnswers(toolName: string, input: unknown) {
  let turn = 0;
  return new MockLanguageModelV4({
    doGenerate: async () => {
      turn += 1;
      return turn === 1
        ? {
            content: [{ type: 'tool-call', toolCallId: 'call-1', toolName, input: JSON.stringify(input) }],
            finishReason: { unified: 'tool-calls', raw: undefined },
            usage,
            warnings: [],
          }
        : {
            content: [{ type: 'text', text: 'done' }],
            finishReason: { unified: 'stop', raw: undefined },
            usage,
            warnings: [],
          };
    },
  });
}

describe('tool mapping', () => {
  it('maps each eligible resource to a function tool with description, schema and approval', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const tools = await agentCommerceTools(options());
    expect(Object.keys(tools)).toEqual(['report', 'weather_basic', 'no_description', 'no_schema']);

    const report = tools.report!;
    expect(report.type).toBeUndefined(); // a plain function tool, not a dynamic or provider tool
    expect(report.description).toBe('Returns a small demo report. Costs 0.01 USDC per call.');
    expect(asSchema(report.inputSchema).jsonSchema).toEqual(
      fixture('resources.json').resources[0].inputSchema,
    );
    expect(report.needsApproval).toBe(true);
    expect(typeof report.execute).toBe('function');

    expect(tools.weather_basic!.description).toBe('Current basic weather for a city. Free.');
    expect(tools.weather_basic!.needsApproval).toBe(false);
    expect(asSchema(tools.no_schema!.inputSchema).jsonSchema).toEqual({ type: 'object', properties: {} });
  });

  it('follows the approval mode', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const needs = (tools: ToolSet) => Object.values(tools).map((t) => t.needsApproval);
    expect(needs(await agentCommerceTools(options({ approval: 'always' })))).toEqual([true, true, true, true]);
    expect(needs(await agentCommerceTools(options({ approval: 'never' })))).toEqual([false, false, false, false]);
  });

  it('passes configuration errors through', async () => {
    await expect(agentCommerceTools(options({ networks: ['eip155:8453'] }))).rejects.toThrow(TypeError);
  });
});

describe('with generateText', () => {
  it('stops a paid tool at an approval request and pays nothing', async () => {
    const invoked: string[] = [];
    const tools = await agentCommerceTools(options({ fetch: gateway(invoked) }));
    const model = callsThenAnswers('report', { topic: 'x' });
    const result = await generateText({ model, prompt: 'go', tools, stopWhen: isStepCount(3) });
    expect(result.content.map((part) => part.type)).toEqual(['tool-call', 'tool-approval-request']);
    expect(invoked).toEqual([]);
  });

  it('pays once the human approves, and returns the transaction hash to the model', async () => {
    const invoked: string[] = [];
    const tools = await agentCommerceTools(options({ fetch: gateway(invoked) }));
    const model = callsThenAnswers('report', { topic: 'x' });
    const messages: ModelMessage[] = [{ role: 'user', content: 'go' }];
    const first = await generateText({ model, messages, tools, stopWhen: isStepCount(3) });
    messages.push(...first.response.messages);
    const request = first.content.find((part) => part.type === 'tool-approval-request');
    expect(request).toBeDefined();
    messages.push({
      role: 'tool',
      content: [{ type: 'tool-approval-response', approvalId: request!.approvalId, approved: true }],
    });

    const second = await generateText({ model, messages, tools, stopWhen: isStepCount(3) });
    expect(invoked).toHaveLength(2); // the 402 and the signed retry
    // The approved call runs before the model is asked again; its result is in that prompt
    const toolMessage = model.doGenerateCalls[1]?.prompt.find((message) => message.role === 'tool');
    expect(JSON.stringify(toolMessage)).toContain(fixture('payment-response.json').transaction);
    expect(second.text).toBe('done');
  });

  it('hands a core error to the model as a tool error instead of crashing', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const tools = await agentCommerceTools(options());
    const model = callsThenAnswers('weather_basic', { city: 'Berlin' });
    const result = await generateText({ model, prompt: 'go', tools, stopWhen: isStepCount(3) });
    expect(result.text).toBe('done');
    const toolError = result.content.find((part) => part.type === 'tool-error');
    expect(toolError?.error).toMatchObject({ code: 'BACKEND_ERROR', status: 404 });
    expect(JSON.stringify(model.doGenerateCalls[1]?.prompt)).toContain(
      'BACKEND_ERROR: Gateway answered HTTP 404 (RESOURCE_NOT_FOUND)',
    );
  });
});

describe('toolApproval', () => {
  const run = (tools: ToolSet, toolApproval: Parameters<typeof generateText>[0]['toolApproval'], name: string, input: unknown) =>
    generateText({
      model: callsThenAnswers(name, input),
      prompt: 'go',
      tools,
      toolApproval,
      stopWhen: isStepCount(3),
    });
  const withoutFlags = (tools: ToolSet): ToolSet =>
    Object.fromEntries(Object.entries(tools).map(([name, t]) => [name, { ...t, needsApproval: undefined }]));

  it('lists the tools that need approval, following the approval option', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(agentCommerceToolApproval(await agentCommerceTools(options()))).toEqual({
      report: 'user-approval',
      no_description: 'user-approval',
    });
    expect(agentCommerceToolApproval(await agentCommerceTools(options({ approval: 'always' })))).toEqual({
      report: 'user-approval',
      weather_basic: 'user-approval',
      no_description: 'user-approval',
      no_schema: 'user-approval',
    });
    expect(agentCommerceToolApproval(await agentCommerceTools(options({ approval: 'never' })))).toEqual({});
  });

  it('stops a paid tool at an approval request by itself, without the tool-level flag', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const invoked: string[] = [];
    const tools = await agentCommerceTools(options({ fetch: gateway(invoked) }));
    const result = await run(withoutFlags(tools), agentCommerceToolApproval(tools), 'report', { topic: 'x' });
    expect(result.content.map((part) => part.type)).toEqual(['tool-call', 'tool-approval-request']);
    expect(invoked).toEqual([]);
  });

  it('lets a free tool run when the paid ones are marked', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const invoked: string[] = [];
    const tools = await agentCommerceTools(options({ fetch: gateway(invoked) }));
    const result = await run(withoutFlags(tools), agentCommerceToolApproval(tools), 'weather_basic', { city: 'Berlin' });
    expect(result.content.map((part) => part.type)).toContain('tool-error'); // the double answers 404; it did run
    expect(invoked).toHaveLength(1);
  });

  it('lets an entry override the tool-level flag, as the README says', async () => {
    const invoked: string[] = [];
    const tools = await agentCommerceTools(options({ fetch: gateway(invoked) }));
    const result = await run(tools, { ...agentCommerceToolApproval(tools), report: 'approved' }, 'report', { topic: 'x' });
    expect(invoked).toHaveLength(2); // the 402 and the signed retry
    // 'approved' still records the request, answers it by itself, and the tool runs
    expect(result.content.map((part) => part.type)).toEqual([
      'tool-call',
      'tool-approval-request',
      'tool-approval-response',
      'tool-result',
      'text',
    ]);
  });

  it('falls back to the tool-level flag for a tool the toolApproval object does not list', async () => {
    const invoked: string[] = [];
    const tools = await agentCommerceTools(options({ fetch: gateway(invoked) }));
    const result = await run(tools, {}, 'report', { topic: 'x' });
    expect(result.content.map((part) => part.type)).toEqual(['tool-call', 'tool-approval-request']);
    expect(invoked).toEqual([]);
  });
});

describe('documentation shows the verified examples', () => {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const text = (path: string) => readFileSync(path, 'utf8');
  const quickstart = text(`${root}examples/quickstart.ts`).trim();
  const approval = text(`${root}examples/approval.ts`).trim();

  it.each([`${root}README.md`, `${root}../../docs/ai-sdk.md`])('%s holds both examples', (path) => {
    expect(text(path)).toContain(quickstart);
    expect(text(path)).toContain(approval);
  });
});
