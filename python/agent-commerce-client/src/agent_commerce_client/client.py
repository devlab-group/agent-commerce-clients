"""Discover gateway resources and pay with x402 within spending limits"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx
from x402 import x402Client
from x402.http.clients.httpx import PaymentError, x402_httpx_transport
from x402.http.utils import ResponseBodyTooLargeError
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.schemas import AbortResult, NoMatchingRequirementsError

_LOG = logging.getLogger("agent_commerce_client")

# Supported EVM networks and their USDC contracts; None allows any asset on local Anvil
_USDC: dict[str, str | None] = {
    "eip155:31337": None,  # local Anvil
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Base Sepolia
    "eip155:421614": "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d",  # Arbitrum Sepolia
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # Base
    "eip155:1": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # Ethereum
    "eip155:137": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # Polygon
    "eip155:42161": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # Arbitrum One
    "eip155:43114": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",  # Avalanche C-Chain
}
_TESTNETS = frozenset({"eip155:31337", "eip155:84532", "eip155:421614"})
_LIMIT = re.compile(r"[0-9]+(?:\.[0-9]{1,6})?")
_ATOMIC = re.compile(r"[0-9]+")
_GATEWAY_CODE = re.compile(r"[A-Z_]{1,64}")
_REASON = re.compile(r"[a-z0-9_]{1,64}")
_TX = re.compile(r"0x[0-9a-fA-F]{64}")
_UNITS = 1_000_000  # USDC has 6 decimals
_TIMEOUT = 60.0
_APPROVALS = ("paid", "always", "never")
_NO_OFFER = "The gateway offered no exact USDC payment on an allowed network"


class AgentCommerceError(Exception):
    """Client runtime failure with a short message for the model.

    The client uses LIMIT_EXCEEDED, NETWORK_NOT_ALLOWED, PAYMENT_FAILED,
    BACKEND_ERROR (with `status`) and GATEWAY_UNREACHABLE. Its error messages
    omit secrets and start with the code so frameworks can show the message alone.
    """

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        if not message.startswith(f"{code}: "):
            message = f"{code}: {message}"
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ResourceTool:
    """Gateway resource metadata for a framework tool"""

    name: str
    description: str
    input_schema: dict[str, Any]
    paid: bool
    needs_approval: bool


@dataclass(frozen=True)
class InvokeResult:
    """HTTP status, response body and decoded PAYMENT-RESPONSE, or None without a valid header"""

    status: int
    body: Any
    payment: dict[str, Any] | None


class SpendStore(Protocol):
    """Daily spending counter in micro-USDC; `add` must be atomic"""

    def add(self, day: str, amount: int) -> int:
        """Add `amount` to the UTC day `day` (YYYY-MM-DD) and return the new total.

        A negative amount rolls back a reservation.
        """
        ...


class MemorySpendStore:
    """Spend store held in process memory"""

    def __init__(self) -> None:
        self._totals: dict[str, int] = {}
        self._lock = threading.Lock()

    def add(self, day: str, amount: int) -> int:
        with self._lock:
            total = self._totals[day] = self._totals.get(day, 0) + amount
            return total


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _limit(name: str, value: Any) -> int:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a decimal string such as '0.05'")
    if not _LIMIT.fullmatch(value):
        raise ValueError(f"{name} must be a non-negative decimal with at most 6 decimal places")
    return int(Decimal(value) * _UNITS)


def _usdc(units: int) -> str:
    return format(Decimal(units) / _UNITS, "f")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _has(raw: dict[str, Any], key: str, item: str) -> bool:
    value = raw.get(key)
    return isinstance(value, list) and item in value


def _header(response: httpx.Response, name: str) -> dict[str, Any] | None:
    # x402 headers carry base64-encoded JSON
    try:
        value = json.loads(base64.b64decode(response.headers[name], validate=True))
    except (KeyError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _gateway_code(response: httpx.Response) -> str | None:
    try:
        code = response.json().get("code")
    except (ValueError, AttributeError):
        return None
    return code if isinstance(code, str) and _GATEWAY_CODE.fullmatch(code) else None


def _run(method: Any, *args: Any) -> Any:
    """Run an async method from sync code, including when an event loop is active"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(method(*args))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, method(*args)).result()


class _Payment:
    """Track one invocation's x402 client, payment attempts and signing state"""

    def __init__(self, owner: AgentCommerceClient) -> None:
        self._owner = owner
        self._attempts = 0
        self.signed = False
        self.error: AgentCommerceError | None = None
        client = x402Client()
        for network in owner._networks:
            client.register(network, owner._scheme)
        # Use the checks in `before` instead of x402's $1 cap and token allowlist
        # so higher max_per_call values and the local Anvil token remain usable
        client.set_spend_controls(False)
        # Filter before selection so an unacceptable offer cannot displace a USDC offer
        client.register_policy(lambda version, offers: [offer for offer in offers if owner._acceptable(offer)])
        client.on_before_payment_creation(self.before)
        client.on_after_payment_creation(self.after)
        self.client = client

    def _abort(self, code: str, message: str) -> AbortResult:
        self.error = AgentCommerceError(code, message)
        return AbortResult(reason=code)

    def before(self, ctx: Any) -> AbortResult | None:
        owner = self._owner
        offer = ctx.selected_requirements
        # Reject a second payment attempt within this invocation
        self._attempts += 1
        if self._attempts > 1:
            return self._abort("PAYMENT_FAILED", "A second payment is not allowed for one invocation")
        if not owner._acceptable(offer):
            return self._abort("NETWORK_NOT_ALLOWED", _NO_OFFER)
        raw = offer.get_amount()
        if not _ATOMIC.fullmatch(raw):
            raise ValueError("invalid payment amount")
        amount = int(raw)
        price = _usdc(amount)
        if amount > owner._max_per_call:
            return self._abort(
                "LIMIT_EXCEEDED",
                f"Price {price} USDC exceeds the per-call limit of {_usdc(owner._max_per_call)} USDC",
            )
        day = _today()
        if owner._store.add(day, amount) > owner._max_per_day:
            owner._store.add(day, -amount)
            return self._abort("LIMIT_EXCEEDED", f"Price {price} USDC exceeds the remaining daily allowance")
        return None

    def after(self, ctx: Any) -> None:
        # Keep the reservation after signing because funds may move
        self.signed = True

    def failure(self, cause: BaseException) -> AgentCommerceError:
        if self.error is not None:
            return self.error
        if isinstance(cause, NoMatchingRequirementsError):
            return AgentCommerceError("NETWORK_NOT_ALLOWED", _NO_OFFER)
        if isinstance(cause, httpx.HTTPError):
            return _unreachable(self.signed)
        return AgentCommerceError("PAYMENT_FAILED", "Payment could not be completed")


def _unreachable(signed: bool) -> AgentCommerceError:
    suffix = "; a payment may have been sent" if signed else ""
    return AgentCommerceError("GATEWAY_UNREACHABLE", f"Gateway could not be reached{suffix}")


class AgentCommerceClient:
    """Discover and invoke ACG resources, paying with x402 within spending limits.

    `list_tools`/`alist_tools` and `invoke`/`ainvoke` provide sync and async forms
    of the same operations. Invalid configuration raises ValueError or TypeError
    during construction; client runtime failures raise AgentCommerceError.

    Use `transport` to replace the HTTP transport in tests, for example with
    `httpx.MockTransport`. Each call closes the transport. Proxy environment
    variables are ignored.
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        signer: Any,
        networks: Sequence[str],
        max_per_call: str,
        max_per_day: str,
        resources: Sequence[str] | None = None,
        allow_mainnet: bool = False,
        approval: str = "paid",
        spend_store: SpendStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(gateway_url, str):
            raise TypeError("gateway_url must be a string")
        parts = urlsplit(gateway_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("gateway_url must be an http or https URL")
        if signer is None or isinstance(signer, (str, bytes, bytearray)):
            raise TypeError("signer must be a wallet object such as an eth_account account, not a raw private key")
        if not (hasattr(signer, "address") and hasattr(signer, "sign_typed_data")):
            raise TypeError("signer must expose `address` and `sign_typed_data`")
        if isinstance(networks, str) or not isinstance(networks, Sequence):
            raise TypeError("networks must be a list of CAIP-2 network ids")
        if not networks:
            raise ValueError("networks must not be empty")
        if not isinstance(allow_mainnet, bool):
            raise TypeError("allow_mainnet must be a bool")
        for network in networks:
            if network not in _USDC:
                raise ValueError(f"networks may only hold: {', '.join(_USDC)}")
            if network not in _TESTNETS and not allow_mainnet:
                raise ValueError(f"{network} is a mainnet; pass allow_mainnet=True to allow it")
        if resources is not None and (isinstance(resources, str) or not isinstance(resources, Sequence)):
            raise TypeError("resources must be a list of resource ids")
        if approval not in _APPROVALS:
            raise ValueError("approval must be 'paid', 'always' or 'never'")
        if spend_store is not None and not callable(getattr(spend_store, "add", None)):
            raise TypeError("spend_store must provide add(day, amount)")

        self._base = gateway_url.rstrip("/")
        self._networks = frozenset(networks)
        self._max_per_call = _limit("max_per_call", max_per_call)
        self._max_per_day = _limit("max_per_day", max_per_day)
        self._resources = None if resources is None else frozenset(resources)
        self._approval = approval
        self._store: SpendStore = spend_store if spend_store is not None else MemorySpendStore()
        self._transport = transport
        self._scheme = ExactEvmScheme(signer)

    def _acceptable(self, offer: Any) -> bool:
        # Accept exact EIP-3009 offers on configured networks, using their USDC asset
        # Permit2 requires a prior on-chain approval; ACG returns 412 without it
        if offer.scheme != "exact" or offer.network not in self._networks:
            return False
        if (offer.extra or {}).get("assetTransferMethod") not in (None, "eip3009"):
            return False
        usdc = _USDC[offer.network]
        return usdc is None or offer.asset.lower() == usdc.lower()

    # Discovery

    def list_tools(self) -> list[ResourceTool]:
        """Return eligible gateway resources as tool descriptions"""
        return _run(self.alist_tools)

    async def alist_tools(self) -> list[ResourceTool]:
        """Async form of `list_tools`"""
        response = await self._send("GET", "/api/resources")
        if not 200 <= response.status_code < 300:
            raise _backend_error(response)
        try:
            items = response.json()["resources"]
        except (ValueError, KeyError, TypeError):
            items = None
        if not isinstance(items, list):
            raise AgentCommerceError(
                "BACKEND_ERROR", "Gateway returned an unexpected resource list", response.status_code
            )
        tools = []
        for raw in items:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                _LOG.warning("Skipping a resource without a string id")
                continue
            reason = self._skip_reason(raw)
            if reason:
                _LOG.warning("Skipping resource %s: %s", json.dumps(raw["id"]), reason)
                continue
            tools.append(self._describe(raw))
        return tools

    def _skip_reason(self, raw: dict[str, Any]) -> str | None:
        if not _has(raw, "exposedVia", "http"):
            return "not exposed over http"
        if _pricing(raw).get("type") != "free" and not _has(raw, "paymentMethods", "x402"):
            return "not payable with x402"
        if self._resources is not None and raw["id"] not in self._resources:
            return "not in the resources allowlist"
        return None

    def _describe(self, raw: dict[str, Any]) -> ResourceTool:
        pricing = _pricing(raw)
        paid = pricing.get("type") != "free"
        summary = _text(raw.get("description")) or _text(raw.get("name")) or raw["id"]
        if not summary.endswith((".", "!", "?")):
            summary += "."
        if not paid:
            price = "Free."
        elif (
            pricing.get("type") == "fixed"
            and isinstance(pricing.get("amount"), str)
            and isinstance(pricing.get("currency"), str)
        ):
            price = f"Costs {pricing['amount']} {pricing['currency']} per call."
        else:
            price = "Paid."
        schema = raw.get("inputSchema")
        return ResourceTool(
            name=raw["id"],
            description=f"{summary} {price}",
            input_schema=schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
            paid=paid,
            needs_approval=(paid if self._approval == "paid" else self._approval == "always"),
        )

    # Invocation

    def invoke(self, resource: str, input: dict[str, Any] | None = None) -> InvokeResult:
        """Invoke a resource and pay with x402 if the gateway requires payment"""
        return _run(self.ainvoke, resource, input)

    async def ainvoke(self, resource: str, input: dict[str, Any] | None = None) -> InvokeResult:
        """Async form of `invoke`"""
        payment = _Payment(self)
        path = f"/api/resources/{quote(resource, safe='')}/invoke"
        response = await self._send("POST", path, {} if input is None else input, payment)
        status = response.status_code
        settled = _header(response, "PAYMENT-RESPONSE") or {}
        if status == 402 or settled.get("success") is False:
            # The x402 transport handles the initial challenge; a final 402 rejects the payment.
            # Failed settlement on another status (ACG uses 502) may still complete on chain.
            required = _header(response, "PAYMENT-REQUIRED") or {}
            reason = settled.get("errorReason") or required.get("error")
            reason = reason if isinstance(reason, str) and _REASON.fullmatch(reason) else None
            if status == 402:
                code = _gateway_code(response)
                detail = (f": {code}" if code else "") + (f" ({reason})" if reason else "")
                raise AgentCommerceError("PAYMENT_FAILED", f"The gateway refused the payment{detail}")
            transaction = settled.get("transaction")
            tx = f"tx {transaction}" if isinstance(transaction, str) and _TX.fullmatch(transaction) else None
            notes = ", ".join(note for note in (reason, tx) if note)
            detail = f" ({notes})" if notes else ""
            raise AgentCommerceError(
                "PAYMENT_FAILED", f"Settlement not confirmed{detail}; the payment may still complete on chain"
            )
        if 200 <= status < 300:
            body: Any = response.text
            if "json" in response.headers.get("content-type", ""):
                try:
                    body = response.json()
                except ValueError:
                    pass
            return InvokeResult(status, body, _header(response, "PAYMENT-RESPONSE"))
        raise _backend_error(response)

    async def _send(self, method: str, path: str, body: Any = None, payment: _Payment | None = None) -> httpx.Response:
        # Disable connection retries in the default transport to avoid replaying payments
        inner = self._transport or httpx.AsyncHTTPTransport(retries=0)
        transport = inner if payment is None else x402_httpx_transport(payment.client, inner)
        try:
            async with httpx.AsyncClient(transport=transport, timeout=_TIMEOUT) as http:
                return await http.request(method, self._base + path, json=body)
        except (PaymentError, ResponseBodyTooLargeError, httpx.HTTPError) as exc:
            # Unwrap PaymentError to classify transport and offer-selection failures
            cause = exc.__cause__ if isinstance(exc, PaymentError) and exc.__cause__ else exc
            raise (payment.failure(cause) if payment else _unreachable(False)) from None


def _pricing(raw: dict[str, Any]) -> dict[str, Any]:
    pricing = raw.get("pricing")
    return pricing if isinstance(pricing, dict) else {}


def _backend_error(response: httpx.Response) -> AgentCommerceError:
    code = _gateway_code(response)
    suffix = f" ({code})" if code else ""
    return AgentCommerceError(
        "BACKEND_ERROR", f"Gateway returned HTTP {response.status_code}{suffix}", response.status_code
    )
