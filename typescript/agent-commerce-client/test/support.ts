import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { privateKeyToAccount } from 'viem/accounts';

const root = fileURLToPath(new URL('../../../conformance/', import.meta.url));

export const scenarios = JSON.parse(readFileSync(`${root}scenarios.json`, 'utf8'));
export const fixture = (name: string) => JSON.parse(readFileSync(`${root}fixtures/${name}`, 'utf8'));
export const account = privateKeyToAccount(scenarios.testSignerPrivateKey);
export const GATEWAY = scenarios.defaults.gatewayUrl as string;
export const b64 = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64');

export interface MockResponse {
  status?: number;
  body?: unknown;
  bodyFixture?: string;
  paymentRequired?: string;
  // An inline payment challenge instead of a fixture file
  challenge?: unknown;
  paymentResponse?: string;
  // Raw response headers
  headers?: Record<string, string>;
  // Raw body sent as is, with the content-type from `headers`
  text?: string;
  networkError?: boolean;
}

export function toResponse(r: MockResponse): Response {
  if (r.networkError) throw new TypeError('fetch failed');
  const body = r.bodyFixture ? fixture(r.bodyFixture) : r.body;
  const headers: Record<string, string> = { 'content-type': 'application/json', ...r.headers };
  if (r.paymentRequired) headers['PAYMENT-REQUIRED'] = b64(fixture(r.paymentRequired));
  if (r.challenge) headers['PAYMENT-REQUIRED'] = b64(r.challenge);
  if (r.paymentResponse) headers['PAYMENT-RESPONSE'] = b64(fixture(r.paymentResponse));
  const raw = r.text ?? (body === undefined ? null : JSON.stringify(body));
  return new Response(raw, { status: r.status ?? 200, headers });
}

export interface Recorded {
  url: string;
  method: string;
  headers: Headers;
  body: string | undefined;
  redirect: RequestInit['redirect'];
  signal: AbortSignal | null | undefined;
}

// Answers the n-th request with the n-th response; a request past the end is flagged and fails as a network error
export function scripted(responses: MockResponse[]) {
  const requests: Recorded[] = [];
  const state = { overrun: false };
  const fetch = async (url: string | URL | Request, init?: RequestInit) => {
    requests.push({
      url: String(url),
      method: init?.method ?? 'GET',
      headers: new Headers(init?.headers),
      body: init?.body as string | undefined,
      redirect: init?.redirect,
      signal: init?.signal,
    });
    const next = responses[requests.length - 1];
    if (!next) {
      state.overrun = true;
      throw new TypeError('unexpected request');
    }
    return toResponse(next);
  };
  return { fetch: fetch as typeof globalThis.fetch, requests, state };
}

export const signed = (requests: Recorded[]) => requests.filter((r) => r.headers.has('payment-signature'));
export const utcDay = () => new Date().toISOString().slice(0, 10);
export const micro = (value: string) => {
  const [whole, fraction = ''] = value.split('.');
  return BigInt(whole + fraction.padEnd(6, '0'));
};
