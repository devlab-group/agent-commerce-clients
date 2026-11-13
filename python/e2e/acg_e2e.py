"""Helpers for the opt-in end-to-end tests against a running Agent Commerce Gateway stack.

Every setting can be overridden with an environment variable, for example to rerun
the tests against a Base Sepolia merchant:

    ACG_E2E=1                    enable the tests
    ACG_E2E_GATEWAY_URL          gateway base URL (default http://127.0.0.1:8080)
    ACG_E2E_NETWORK              CAIP-2 network (default eip155:31337)
    ACG_E2E_RPC_URL              JSON-RPC endpoint of that network (default http://127.0.0.1:8545)
    ACG_E2E_ASSET                token contract (default: `asset` of the deploy file)
    ACG_E2E_RESOURCE             paid resource id (default market_report)
    ACG_E2E_PRICE                its price in USDC (default 0.01)
    ACG_E2E_ADMIN_TOKEN          operator token for GET /api/receipts
    ACG_E2E_BUYER_PRIVATE_KEY    buyer key; otherwise read from the deploy file
    ACG_E2E_DEPLOY_FILE          JSON file with `asset` and `buyer.privateKey` (default: the
                                 local demo stack's .deploy/local.json next to this repo)
"""

import json
import os
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from eth_account import Account

DEFAULT_DEPLOY_FILE = Path(__file__).resolve().parents[2].parent / "agent-commerce" / ".deploy" / "local.json"
# The local demo stack ships this well-known token; it protects nothing real
DEFAULT_ADMIN_TOKEN = "local-demo-admin-token"

enabled = pytest.mark.skipif(os.environ.get("ACG_E2E") != "1", reason="set ACG_E2E=1 to run against a live gateway")


def usdc(units: int) -> str:
    return format(Decimal(units) / 1_000_000, "f")


class Stack:
    def __init__(self) -> None:
        env = os.environ.get
        self.gateway_url = env("ACG_E2E_GATEWAY_URL", "http://127.0.0.1:8080")
        self.network = env("ACG_E2E_NETWORK", "eip155:31337")
        self.rpc_url = env("ACG_E2E_RPC_URL", "http://127.0.0.1:8545")
        self.resource = env("ACG_E2E_RESOURCE", "market_report")
        self.price = int(Decimal(env("ACG_E2E_PRICE", "0.01")) * 1_000_000)
        self.admin_token = env("ACG_E2E_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)
        deploy = None
        if not (env("ACG_E2E_ASSET") and env("ACG_E2E_BUYER_PRIVATE_KEY")):
            deploy = json.loads(Path(env("ACG_E2E_DEPLOY_FILE", str(DEFAULT_DEPLOY_FILE))).read_text())
        self.asset = env("ACG_E2E_ASSET") or deploy["asset"]
        self.buyer = Account.from_key(env("ACG_E2E_BUYER_PRIVATE_KEY") or deploy["buyer"]["privateKey"])

    def client_kwargs(self, max_per_call: str = "0.05", max_per_day: str = "1.00") -> dict:
        return {
            "gateway_url": self.gateway_url,
            "signer": self.buyer,
            "networks": [self.network],
            "max_per_call": max_per_call,
            "max_per_day": max_per_day,
        }

    def rpc(self, method: str, *params):
        response = httpx.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)})
        return response.json()["result"]

    def balance(self) -> int:
        # ERC-20 balanceOf(address)
        data = "0x70a08231" + self.buyer.address[2:].lower().rjust(64, "0")
        return int(self.rpc("eth_call", {"to": self.asset, "data": data}, "latest"), 16)

    def receipt_ids(self) -> set[str]:
        response = httpx.get(
            f"{self.gateway_url}/api/receipts?limit=50", headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        response.raise_for_status()
        return {receipt["id"] for receipt in response.json()["receipts"]}

    def wait_for_balance(self, expected: int, seconds: float = 20) -> int:
        # A public RPC node may lag behind the settlement the gateway has confirmed
        deadline = time.monotonic() + seconds
        while (balance := self.balance()) != expected and time.monotonic() < deadline:
            time.sleep(1)
        return balance

    def transaction_succeeded(self, tx_hash: str, seconds: float = 20) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            receipt = self.rpc("eth_getTransactionReceipt", tx_hash)
            if receipt:
                return receipt["status"] == "0x1"
            time.sleep(1)
        return False
