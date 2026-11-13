# Buyer core conformance suite

Both buyer cores must pass these shared fixtures and scenarios. Their runners in `python/agent-commerce-client` and
`typescript/agent-commerce-client` read `scenarios.json` and run every scenario against a mock gateway. Skipping a
scenario in either runner is a bug.

## Fixtures

`fixtures/` holds gateway responses recorded from `@devlab.group/agent-commerce` 2.7.1 configured for Base Sepolia
(remote facilitator, Circle USDC), plus derived and synthetic entries:

| File                                | Source                                                                          |
| ----------------------------------- | ------------------------------------------------------------------------------- |
| `resources.json`                    | Recorded `GET /api/resources`, plus synthetic `mpp_only` and `no_schema`        |
| `payment-required.json`             | Recorded `PAYMENT-REQUIRED` header, base64-decoded                              |
| `payment-required-body.json`        | Recorded 402 body                                                               |
| `payment-required-rejected.json`    | Recorded `PAYMENT-REQUIRED` after an invalid `PAYMENT-SIGNATURE`                |
| `payment-rejected-body.json`        | Recorded body of that refusal                                                   |
| `payment-required-sepolia.json`     | Derived: offer moved to Ethereum Sepolia (`eip155:11155111`)                    |
| `payment-required-multi.json`       | Derived: a Base mainnet offer placed before the Base Sepolia one                |
| `payment-required-wrong-asset.json` | Derived: the Base Sepolia offer in a token other than USDC                      |
| `payment-required-mixed-asset.json` | Derived: a non-USDC offer placed before the USDC one, same network              |
| `payment-required-upto.json`        | Derived: the offer with scheme `upto` instead of `exact`                        |
| `payment-required-permit2.json`     | Derived: the offer with `assetTransferMethod: permit2`                          |
| `payment-response.json`             | Built with ACG's `settlementDocument` shape (needs a real settlement to record) |
| `payment-response-failed.json`      | Built with ACG's `settlementFailure` shape                                      |
| `payment-response-pending.json`     | Same shape, unconfirmed settlement with a hash (`settlement_pending`)           |
| `payment-response-uncertain.json`   | Same shape, unconfirmed settlement without a hash (`unexpected_settle_error`)   |
| `payment-response-untrusted.json`   | Derived: the pending one with text instead of a hash in `transaction`           |
| `error-input-invalid.json`          | Recorded 400 `INPUT_INVALID`                                                    |
| `error-not-found.json`              | Recorded 404 `RESOURCE_NOT_FOUND`                                               |

## Scenario format

Configuration keys use camelCase; the Python runner maps them to snake_case. Each scenario's `config` overrides
`defaults`, and `null` removes a key. The signer is built from `testSignerPrivateKey`, a well-known Anvil development
key that signs offline and must never be funded.

- `config[]`: build a client. `expect` is `"ok"` or `"error"` (a configuration error raised at construction, not one of
  the runtime codes).
- `discovery[]`: the mocked `GET {gatewayUrl}/api/resources` answers with `resources` (a fixture file) or `response`.
  `expect.tools` is the exact ordered list of tool descriptions; `expect.toolNames` lists only the names;
  `expect.needsApproval` maps a name to its flag; `expect.skipped` lists the ids that must each produce one warning
  naming the id; `expect.error` specifies an error code and status.
- `invocations[]`: `call` invokes `resource` with `input`. The mocked `POST {gatewayUrl}/api/resources/{id}/invoke`
  answers with `responses` in order, one per request; a request past the end fails the test. A response has `status`, a
  JSON `body`, a `bodyFixture` or a raw `text` body (sent with the `content-type` from `headers`), optional raw
  `headers`, and optional `paymentRequired` and `paymentResponse` fixtures, which the mock base64-encodes into the
  `PAYMENT-REQUIRED` and `PAYMENT-RESPONSE` headers. `networkError: true` simulates a transport failure.
  - `spentToday`: USDC already counted for the UTC day of the call, before it runs.
  - `expect.requests`: total invoke requests sent.
  - `expect.signatures`: requests carrying `PAYMENT-SIGNATURE`.
  - `expect.signedOffer`: fields of the decoded `PAYMENT-SIGNATURE` payload's `accepted` offer.
  - `expect.result`: `{ status, body, payment }`; `payment` is a fixture name whose JSON must equal the decoded
    `PAYMENT-RESPONSE`, or `null`.
  - `expect.error`: `{ code, status?, messageContains?, messageExcludes? }`. The error message must start with
    `<code>: `, contain each string of `messageContains` and none of `messageExcludes`.
  - `expect.spentAfter`: USDC counted for the day after the call, compared as a decimal value (`"1"` equals `"1.00"`).

## Checklist

1. Resource filtering: `http`, `x402`, `free`, `resources` allowlist - `discovery`.
2. Tool description: name, price in the description, schema - `discovery`.
3. Rejection when `maxPerCall` is exceeded - `invocations`.
4. Rejection when the daily limit is exceeded - `invocations`.
5. Network not in `networks`; mainnet without `allowMainnet`; asset other than the network's USDC; scheme other than
   `exact`; transfer method other than `eip3009` - `invocations`, `config`.
6. No retry after a payment has been submitted; no redirect followed - `invocations`.
7. Error handling and error codes - `invocations`, `discovery`.
