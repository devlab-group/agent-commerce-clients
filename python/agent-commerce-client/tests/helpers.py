"""Shared test helpers: the conformance fixtures and a scripted mock gateway"""

import base64
import json
from pathlib import Path

import httpx
from eth_account import Account

CONFORMANCE = Path(__file__).resolve().parents[3] / "conformance"
SUITE = json.loads((CONFORMANCE / "scenarios.json").read_text())
DAY = "2026-01-01"
RESPONSE_KEYS = {
    "status",
    "body",
    "bodyFixture",
    "text",
    "headers",
    "paymentRequired",
    "paymentResponse",
    "networkError",
}
KEYS = {
    "gatewayUrl": "gateway_url",
    "maxPerCall": "max_per_call",
    "maxPerDay": "max_per_day",
    "allowMainnet": "allow_mainnet",
}


def fixture(name):
    return json.loads((CONFORMANCE / "fixtures" / name).read_text())


def b64(document):
    return base64.b64encode(json.dumps(document).encode()).decode()


def build_config(scenario_config, **extra):
    merged = {**SUITE["defaults"], **scenario_config}
    kwargs = {KEYS.get(k, k): v for k, v in merged.items() if v is not None}
    signer = Account.from_key(SUITE["testSignerPrivateKey"])
    return {"signer": signer, **kwargs, **extra}


class Gateway:
    """httpx mock transport that plays scripted responses and records what it receives."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if len(self.requests) > len(self.responses):
            return httpx.Response(599, json={"error": "unexpected request"})
        spec = self.responses[len(self.requests) - 1]
        if spec.get("networkError"):
            raise httpx.ConnectError("connection refused", request=request)
        assert set(spec) <= RESPONSE_KEYS, f"response keys the runner does not handle: {set(spec) - RESPONSE_KEYS}"
        headers = dict(spec.get("headers", {}))
        if "paymentRequired" in spec:
            headers["PAYMENT-REQUIRED"] = b64(fixture(spec["paymentRequired"]))
        if "paymentResponse" in spec:
            headers["PAYMENT-RESPONSE"] = b64(fixture(spec["paymentResponse"]))
        if "text" in spec:
            return httpx.Response(spec["status"], text=spec["text"], headers=headers)
        body = fixture(spec["bodyFixture"]) if "bodyFixture" in spec else spec.get("body")
        return httpx.Response(spec["status"], json=body, headers=headers)

    @property
    def signatures(self):
        return [r for r in self.requests if "payment-signature" in r.headers]
