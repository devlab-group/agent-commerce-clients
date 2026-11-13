"""Core behavior beyond the conformance suite: validation, limits under concurrency, error hygiene."""

import asyncio
import base64
import json
import logging
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from agent_commerce_client import AgentCommerceClient, AgentCommerceError, MemorySpendStore
from agent_commerce_client import client as core
from eth_account import Account
from helpers import DAY, SUITE, b64, fixture

KEY = SUITE["testSignerPrivateKey"]


def make(handler=None, **overrides):
    config = {
        "gateway_url": "https://merchant.example.com",
        "signer": Account.from_key(KEY),
        "networks": ["eip155:84532"],
        "max_per_call": "0.05",
        "max_per_day": "1.00",
        "transport": httpx.MockTransport(handler or (lambda request: httpx.Response(200, json={"ok": True}))),
        **overrides,
    }
    return AgentCommerceClient(**config)


def paying_gateway(requests=None):
    """402 for an unpaid request, 200 with a PAYMENT-RESPONSE once a signature arrives."""

    def handler(request):
        if requests is not None:
            requests.append(request)
        if "payment-signature" not in request.headers:
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(fixture("payment-required.json"))})
        return httpx.Response(
            200, json={"ok": True}, headers={"PAYMENT-RESPONSE": b64(fixture("payment-response.json"))}
        )

    return handler


def priced_gateway(amount, **offer_fields):
    """Like `paying_gateway`, with the offer's amount (base units) and fields replaced."""
    challenge = fixture("payment-required.json")
    challenge["accepts"][0] = {**challenge["accepts"][0], "amount": amount, **offer_fields}

    def handler(request):
        if "payment-signature" not in request.headers:
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(challenge)})
        return httpx.Response(200, json={"ok": True})

    return handler


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch):
    monkeypatch.setattr(core, "_today", lambda: DAY)


# Configuration


@pytest.mark.parametrize("signer", [KEY, bytes.fromhex(KEY[2:]), None, object(), SimpleNamespace(address="0x1")])
def test_signer_must_be_a_wallet_object(signer):
    with pytest.raises(TypeError):
        make(signer=signer)


@pytest.mark.parametrize("limit", [0.05, 1, None])
def test_limits_must_be_decimal_strings(limit):
    with pytest.raises(TypeError):
        make(max_per_call=limit)


@pytest.mark.parametrize("limit", ["1e3", " 1", ".5", "1.", "0x10", "٣", "1,5", "NaN", "inf", ""])
def test_limits_reject_anything_but_plain_decimals(limit):
    with pytest.raises(ValueError):
        make(max_per_day=limit)


@pytest.mark.parametrize("override", [{"networks": "eip155:84532"}, {"resources": "report"}, {"allow_mainnet": "yes"}])
def test_bare_strings_and_loose_flags_are_type_errors(override):
    with pytest.raises(TypeError):
        make(**override)


@pytest.mark.parametrize(
    "url", ["merchant.example.com", "ftp://merchant.example.com", "https://", "file:///etc/passwd"]
)
def test_gateway_url_must_be_http_or_https(url):
    with pytest.raises(ValueError):
        make(gateway_url=url)


@pytest.mark.parametrize("url", [None, 8080, b"https://merchant.example.com"])
def test_gateway_url_must_be_a_string(url):
    with pytest.raises(TypeError):
        make(gateway_url=url)


@pytest.mark.parametrize("store", [object(), {}, "memory"])
def test_spend_store_must_provide_add(store):
    with pytest.raises(TypeError):
        make(spend_store=store)


def test_zero_limit_is_valid_and_blocks_every_payment():
    client = make(paying_gateway(), max_per_call="0")
    with pytest.raises(AgentCommerceError) as raised:
        client.invoke("report")
    assert raised.value.code == "LIMIT_EXCEEDED"


# Limits are exact to the base unit (6 decimals) and inclusive


@pytest.mark.parametrize("limit_name", ["max_per_call", "max_per_day"])
@pytest.mark.parametrize(
    ("limit", "amount", "pays"),
    [
        ("0.01", "10000", True),
        ("0.01", "10001", False),
        ("0.000001", "1", True),
        ("0.000001", "2", False),
        ("0.000003", "3", True),
        ("1", "1000000", True),
        ("1.000000", "1000001", False),
    ],
)
def test_limits_are_inclusive_and_exact_to_one_base_unit(limit_name, limit, amount, pays):
    # The other limit stays generous
    other = "max_per_day" if limit_name == "max_per_call" else "max_per_call"
    client = make(priced_gateway(amount), **{limit_name: limit, other: "100"})
    if pays:
        assert client.invoke("report").status == 200
    else:
        with pytest.raises(AgentCommerceError) as raised:
            client.invoke("report")
        assert raised.value.code == "LIMIT_EXCEEDED"


def test_a_price_above_one_dollar_is_paid_when_the_limit_allows_it():
    # x402's own default cap is $1 per payment; only max_per_call decides here
    store = MemorySpendStore()
    client = make(priced_gateway("2000000"), max_per_call="5", max_per_day="10", spend_store=store)
    assert client.invoke("report").status == 200
    assert store.add(DAY, 0) == 2_000_000


def test_a_token_x402_does_not_know_is_paid_on_an_allowed_network():
    # The local Anvil demo chain uses a mock USDC at a custom address
    offer = {
        "network": "eip155:31337",
        "asset": "0x5fbdb2315678afecb367f032d93f642f64180aa3",
        "extra": {"name": "MockUSDC", "version": "2", "assetTransferMethod": "eip3009"},
    }
    assert make(priced_gateway("10000", **offer), networks=["eip155:31337"]).invoke("report").status == 200


# The limit hook


BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


def offer(network="eip155:84532", amount="10000", scheme="exact", asset=BASE_SEPOLIA_USDC, extra=None):
    return SimpleNamespace(
        selected_requirements=SimpleNamespace(
            network=network, scheme=scheme, asset=asset, extra=extra, get_amount=lambda: amount
        )
    )


def hook_state(**overrides):
    store = MemorySpendStore()
    client = make(spend_store=store, **overrides)
    return core._Payment(client), store


@pytest.mark.parametrize(
    "bad",
    [
        {"network": "eip155:1"},
        {"scheme": "upto"},
        {"asset": "0x2222222222222222222222222222222222222222"},
        {"asset": ""},
        {"extra": {"assetTransferMethod": "permit2"}},
        {"extra": {"assetTransferMethod": "erc7710"}},
        {"extra": {"assetTransferMethod": ""}},
    ],
)
def test_hook_refuses_an_offer_that_is_not_exact_usdc_on_an_allowed_network(bad):
    payment, store = hook_state()
    result = payment.before(offer(**bad))
    assert result.reason == "NETWORK_NOT_ALLOWED"
    assert payment.error.code == "NETWORK_NOT_ALLOWED"
    assert store.add(DAY, 0) == 0


def test_hook_rechecks_the_network_before_anything_is_reserved():
    payment, store = hook_state()
    result = payment.before(offer(network="eip155:1"))
    assert result.reason == "NETWORK_NOT_ALLOWED"
    assert payment.error.code == "NETWORK_NOT_ALLOWED"
    assert store.add(DAY, 0) == 0


@pytest.mark.parametrize("amount", ["-10000", "1_000", " 5", "0.01", "", "1e4", "٣"])
def test_hook_refuses_amounts_that_are_not_plain_atomic_integers(amount):
    payment, store = hook_state()
    with pytest.raises(ValueError):
        payment.before(offer(amount=amount))
    assert store.add(DAY, 0) == 0


def test_hook_reserves_then_rolls_back_only_an_overshoot():
    payment, store = hook_state(max_per_day="0.015")
    assert payment.before(offer()) is None
    assert store.add(DAY, 0) == 10_000
    # A second invocation has its own payment state
    assert core._Payment(payment._owner).before(offer()).reason == "LIMIT_EXCEEDED"
    # The failed second reservation was rolled back; the first one stays
    assert store.add(DAY, 0) == 10_000


def test_a_reservation_stays_counted_after_the_gateway_refuses_the_signed_payment():
    store = MemorySpendStore()
    client = make(
        lambda r: httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(fixture("payment-required-rejected.json"))}),
        spend_store=store,
    )
    with pytest.raises(AgentCommerceError):
        client.invoke("report")
    assert store.add(DAY, 0) == 10_000


def test_hook_allows_only_one_payment_per_invocation():
    payment, store = hook_state()
    assert payment.before(offer()) is None
    assert payment.before(offer()).reason == "PAYMENT_FAILED"
    assert payment.error.code == "PAYMENT_FAILED"
    assert store.add(DAY, 0) == 10_000


# Networks and the token paid on each


def test_the_asset_table_matches_the_known_usdc_of_the_x402_library():
    from x402.mechanisms.evm.default_assets import DEFAULT_ASSETS

    pinned = {n: usdc for n, usdc in core._USDC.items() if usdc is not None}
    assert len(pinned) == 7
    for network, usdc in pinned.items():
        assert DEFAULT_ASSETS[network][0]["asset"].lower() == usdc.lower()
    assert core._USDC["eip155:31337"] is None


@pytest.mark.parametrize("network", ["eip155:31337", "eip155:84532", "eip155:421614"])
def test_testnets_need_no_flag(network):
    make(networks=[network])


@pytest.mark.parametrize("network", ["eip155:1", "eip155:8453", "eip155:137", "eip155:42161", "eip155:43114"])
def test_mainnets_need_allow_mainnet(network):
    with pytest.raises(ValueError):
        make(networks=[network])
    make(networks=[network], allow_mainnet=True)


@pytest.mark.parametrize(
    "network",
    [
        "eip155:11155111",
        "eip155:11155420",
        "eip155:80002",
        "eip155:43113",
        "eip155:10",
        "eip155:0",
        "eip155:084532",
        "base",
        84532,
    ],
)
def test_networks_outside_the_acg_table_are_refused_even_with_allow_mainnet(network):
    with pytest.raises(ValueError):
        make(networks=[network], allow_mainnet=True)


NETWORK_USDC = [(n, u) for n, u in core._USDC.items() if u is not None]


@pytest.mark.parametrize(("network", "usdc"), NETWORK_USDC)
@pytest.mark.parametrize("casing", [str, str.lower, lambda a: "0x" + a[2:].upper()])
def test_the_networks_usdc_is_paid_whatever_the_address_casing(network, usdc, casing):
    store = MemorySpendStore()
    client = make(
        priced_gateway("10000", network=network, asset=casing(usdc)),
        networks=[network],
        allow_mainnet=True,
        spend_store=store,
    )
    assert client.invoke("report").status == 200
    assert store.add(DAY, 0) == 10_000


@pytest.mark.parametrize(("network", "usdc"), NETWORK_USDC)
def test_another_token_on_an_allowed_network_is_never_signed(network, usdc):
    requests = []
    store = MemorySpendStore()

    def handler(request):
        requests.append(request)
        return priced_gateway("10000", network=network, asset="0x2222222222222222222222222222222222222222")(request)

    client = make(handler, networks=[network], allow_mainnet=True, spend_store=store)
    with pytest.raises(AgentCommerceError) as raised:
        client.invoke("report")
    assert raised.value.code == "NETWORK_NOT_ALLOWED"
    assert len(requests) == 1 and store.add(DAY, 0) == 0


def test_the_local_chain_accepts_any_token_and_pins_nothing_else():
    other = "0x2222222222222222222222222222222222222222"
    client = make(priced_gateway("10000", network="eip155:31337", asset=other), networks=["eip155:31337"])
    assert client.invoke("report").status == 200


@pytest.mark.parametrize("method", ["permit2", "erc7710", "EIP3009", ""])
def test_an_offer_with_another_transfer_method_is_never_signed(method):
    requests = []
    store = MemorySpendStore()

    def handler(request):
        requests.append(request)
        return priced_gateway("10000", extra={"name": "USDC", "version": "2", "assetTransferMethod": method})(request)

    with pytest.raises(AgentCommerceError) as raised:
        make(handler, spend_store=store).invoke("report")
    assert raised.value.code == "NETWORK_NOT_ALLOWED"
    assert len(requests) == 1 and store.add(DAY, 0) == 0


def test_an_offer_without_a_transfer_method_or_with_eip3009_is_paid():
    for extra in ({"name": "USDC", "version": "2"}, {"name": "USDC", "version": "2", "assetTransferMethod": "eip3009"}):
        assert make(priced_gateway("10000", extra=extra)).invoke("report").status == 200


def test_a_non_exact_offer_on_an_allowed_network_is_never_signed():
    requests = []

    def handler(request):
        requests.append(request)
        return priced_gateway("10000", scheme="upto")(request)

    with pytest.raises(AgentCommerceError) as raised:
        make(handler).invoke("report")
    assert raised.value.code == "NETWORK_NOT_ALLOWED"
    assert len(requests) == 1


def test_an_offer_list_pays_with_the_one_acceptable_offer_wherever_it_stands():
    usdc = {"asset": BASE_SEPOLIA_USDC}
    wrong = {"asset": "0x2222222222222222222222222222222222222222"}
    permit2 = {"asset": BASE_SEPOLIA_USDC, "extra": {"name": "USDC", "version": "2", "assetTransferMethod": "permit2"}}
    for order in (
        [wrong, usdc],
        [usdc, wrong],
        [wrong, {**usdc, "scheme": "upto"}, usdc],
        [permit2, usdc],
        [usdc, permit2],
    ):
        challenge = fixture("payment-required.json")
        base = challenge["accepts"][0]
        challenge["accepts"] = [{**base, **o} for o in order]
        signed = []

        def handler(request, challenge=challenge, signed=signed):
            if "payment-signature" not in request.headers:
                return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(challenge)})
            signed.append(json.loads(base64.b64decode(request.headers["payment-signature"]))["accepted"])
            return httpx.Response(200, json={})

        make(handler).invoke("report")
        assert [(a["scheme"], a["asset"]) for a in signed] == [("exact", BASE_SEPOLIA_USDC)]


# Spending is atomic and per UTC day


def test_daily_limit_holds_under_concurrent_invocations():
    requests = []
    store = MemorySpendStore()
    client = make(paying_gateway(requests), max_per_day="0.03", spend_store=store)
    outcomes = []

    def worker():
        try:
            outcomes.append(client.invoke("report").status)
        except AgentCommerceError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes, key=str) == [200] * 3 + ["LIMIT_EXCEEDED"] * 9
    assert len([r for r in requests if "payment-signature" in r.headers]) == 3
    assert store.add(DAY, 0) == 30_000


def test_a_new_utc_day_starts_with_a_fresh_allowance(monkeypatch):
    day = {"value": "2026-01-01"}
    monkeypatch.setattr(core, "_today", lambda: day["value"])
    client = make(paying_gateway(), max_per_day="0.01")
    assert client.invoke("report").status == 200
    with pytest.raises(AgentCommerceError):
        client.invoke("report")
    day["value"] = "2026-01-02"
    assert client.invoke("report").status == 200


def test_a_custom_store_sees_one_reservation_and_one_rollback():
    calls = []

    class Store:
        total = 0

        def add(self, day, amount):
            calls.append((day, amount))
            self.total += amount
            return self.total

    client = make(paying_gateway(), max_per_day="0.015", spend_store=Store())
    client.invoke("report")
    with pytest.raises(AgentCommerceError):
        client.invoke("report")
    assert calls == [(DAY, 10_000), (DAY, 10_000), (DAY, -10_000)]


def test_memory_store_adds_atomically_across_threads():
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        store = MemorySpendStore()
        threads = [threading.Thread(target=lambda: [store.add(DAY, 1) for _ in range(20_000)]) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous)
    assert store.add(DAY, 0) == 160_000
    assert store.add(DAY, -60_000) == 100_000
    assert store.add("2026-01-02", 5) == 5


@pytest.mark.parametrize("zone", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])
def test_the_day_key_is_the_utc_date_whatever_the_local_zone(zone, monkeypatch):
    # At least one of UTC+14 and UTC-11 has a different date from UTC
    # Remove the frozen-day fixture to exercise the real clock
    monkeypatch.undo()
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        before = time.strftime("%Y-%m-%d", time.gmtime())
        key = core._today()
        after = time.strftime("%Y-%m-%d", time.gmtime())
    finally:
        monkeypatch.undo()
        time.tzset()
    assert key in {before, after}


# Transport behavior


def test_sync_calls_work_inside_a_running_event_loop():
    client = make(paying_gateway())

    async def main():
        return client.invoke("report")

    assert asyncio.run(main()).status == 200


def test_async_calls_run_concurrently_on_one_client():
    client = make(paying_gateway(), max_per_day="1.00")

    async def main():
        return await asyncio.gather(*(client.ainvoke("report") for _ in range(5)))

    assert [r.status for r in asyncio.run(main())] == [200] * 5
    assert client._store.add(DAY, 0) == 50_000


def test_discovery_never_pays_even_when_the_gateway_answers_402():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(fixture("payment-required.json"))})

    store = MemorySpendStore()
    client = make(handler, spend_store=store)
    with pytest.raises(AgentCommerceError) as raised:
        client.list_tools()
    assert (raised.value.code, raised.value.status) == ("BACKEND_ERROR", 402)
    assert len(requests) == 1 and "payment-signature" not in requests[0].headers
    assert store.add(DAY, 0) == 0


def test_redirects_are_not_followed():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://elsewhere.example.com/"})

    with pytest.raises(AgentCommerceError) as raised:
        make(handler).invoke("report")
    assert (raised.value.code, raised.value.status) == ("BACKEND_ERROR", 307)
    assert len(requests) == 1


def test_resource_id_cannot_change_the_request_path():
    requests = []
    make(lambda r: requests.append(r) or httpx.Response(200, json={})).invoke("../admin?x=1")
    assert requests[0].url.raw_path == b"/api/resources/..%2Fadmin%3Fx%3D1/invoke"


def test_missing_input_is_sent_as_an_empty_object():
    requests = []
    make(lambda r: requests.append(r) or httpx.Response(200, json={})).invoke("weather_basic")
    assert json.loads(requests[0].content) == {}


def test_gateway_url_with_a_path_prefix_and_trailing_slash():
    requests = []
    make(
        lambda r: requests.append(r) or httpx.Response(200, json={}), gateway_url="https://host.example.com/acg/"
    ).invoke("x")
    assert requests[0].url.path == "/acg/api/resources/x/invoke"


def test_non_json_bodies_come_back_as_text_and_bad_payment_headers_are_ignored():
    def handler(request):
        return httpx.Response(200, text="plain words", headers={"PAYMENT-RESPONSE": "%%%not base64%%%"})

    result = make(handler).invoke("report")
    assert (result.status, result.body, result.payment) == (200, "plain words", None)


# Error hygiene


def error_of(handler, **overrides):
    with pytest.raises(AgentCommerceError) as raised:
        make(handler, **overrides).invoke("report")
    return raised.value


def test_backend_bodies_are_never_echoed():
    body = {"code": "INPUT_INVALID", "message": "secret-token-123 failed", "details": {"password": "hunter2"}}
    error = error_of(lambda r: httpx.Response(400, json=body))
    assert str(error) == "BACKEND_ERROR: Gateway returned HTTP 400 (INPUT_INVALID)"
    assert (error.code, error.status) == ("BACKEND_ERROR", 400)


@pytest.mark.parametrize("code", ["lowercase", "HAS SPACE", "X" * 65, "<b>X</b>", 12, None, ["A"]])
def test_gateway_codes_that_do_not_match_the_token_pattern_are_dropped(code):
    error = error_of(lambda r: httpx.Response(500, json={"code": code, "message": "secret"}))
    assert str(error) == "BACKEND_ERROR: Gateway returned HTTP 500"


def test_refused_payment_message_carries_only_token_shaped_details():
    def refused(reason):
        return error_of(
            lambda r: httpx.Response(
                402,
                json={"code": "PAYMENT_INVALID", "message": "secret-token-123"},
                headers={"PAYMENT-REQUIRED": b64({**fixture("payment-required.json"), "error": reason})},
            )
        )

    assert (
        str(refused("invalid_payload"))
        == "PAYMENT_FAILED: The gateway refused the payment: PAYMENT_INVALID (invalid_payload)"
    )
    assert (
        str(refused("Bad reason! secret-token-123"))
        == "PAYMENT_FAILED: The gateway refused the payment: PAYMENT_INVALID"
    )


def test_unreachable_after_signing_says_a_payment_may_have_been_sent():
    def handler(request):
        if "payment-signature" in request.headers:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(fixture("payment-required.json"))})

    assert "a payment may have been sent" in str(error_of(handler))
    assert "payment may" not in str(error_of(lambda r: (_ for _ in ()).throw(httpx.ConnectError("no", request=r))))


def test_a_signer_that_fails_aborts_the_payment_with_a_generic_message():
    class Signer:
        address = "0x0000000000000000000000000000000000000001"

        def sign_typed_data(self, *args, **kwargs):
            raise RuntimeError(f"hardware wallet exploded {KEY}")

    requests = []
    error = error_of(paying_gateway(requests), signer=Signer())
    assert (error.code, error.message) == ("PAYMENT_FAILED", "PAYMENT_FAILED: Payment could not be completed")
    assert KEY not in str(error) and not [r for r in requests if "payment-signature" in r.headers]


def test_malformed_resource_lists_are_backend_errors():
    for body in ({"resources": "nope"}, {"other": []}, [1, 2], "text"):
        with pytest.raises(AgentCommerceError) as raised:
            make(lambda r, body=body: httpx.Response(200, json=body)).list_tools()
        assert raised.value.code == "BACKEND_ERROR"


def test_resources_without_an_id_are_skipped_with_a_warning(caplog):
    body = {"resources": [{"name": "x", "exposedVia": ["http"]}, "junk", fixture("resources.json")["resources"][1]]}
    with caplog.at_level(logging.WARNING, logger="agent_commerce_client"):
        tools = make(lambda r: httpx.Response(200, json=body)).list_tools()
    assert [t.name for t in tools] == ["weather_basic"]
    assert len(caplog.records) == 2


def test_free_status_survives_odd_pricing_shapes():
    body = {
        "resources": [
            {"id": "a", "exposedVia": ["http"], "paymentMethods": ["x402"], "pricing": "free"},
            {"id": "b", "exposedVia": "http", "pricing": {"type": "free"}},
        ]
    }
    tools = make(lambda r: httpx.Response(200, json=body)).list_tools()
    # "a" is paid with an unknown price; "b" is skipped because exposedVia is not a list
    assert [(t.name, t.paid, t.description) for t in tools] == [("a", True, "a. Paid.")]


def test_error_string_form_includes_the_code():
    assert str(AgentCommerceError("LIMIT_EXCEEDED", "too much")) == "LIMIT_EXCEEDED: too much"


# Descriptions and the allowlist


def described(*resources, **overrides):
    body = {"resources": [{"exposedVia": ["http"], "paymentMethods": ["x402"], **r} for r in resources]}
    return make(lambda request: httpx.Response(200, json=body), **overrides).list_tools()


@pytest.mark.parametrize(
    ("resource", "description"),
    [
        ({"id": "a", "name": "N", "description": "Done!", "pricing": {"type": "free"}}, "Done! Free."),
        ({"id": "a", "name": "N", "description": "Done?", "pricing": {"type": "free"}}, "Done? Free."),
        ({"id": "a", "name": "N", "description": "Done.", "pricing": {"type": "free"}}, "Done. Free."),
        ({"id": "a", "name": "N", "description": "Done", "pricing": {"type": "free"}}, "Done. Free."),
        ({"id": "a", "name": "N", "description": "  Done  ", "pricing": {"type": "free"}}, "Done. Free."),
        ({"id": "a", "name": "N", "description": "", "pricing": {"type": "free"}}, "N. Free."),
        ({"id": "a", "name": "N", "pricing": {"type": "free"}}, "N. Free."),
        ({"id": "a", "pricing": {"type": "free"}}, "a. Free."),
        (
            {"id": "a", "description": "D.", "pricing": {"type": "fixed", "amount": "0.01", "currency": "USDC"}},
            "D. Costs 0.01 USDC per call.",
        ),
        ({"id": "a", "description": "D.", "pricing": {"type": "fixed"}}, "D. Paid."),
        ({"id": "a", "description": "D.", "pricing": {"type": "fixed", "amount": "0.01"}}, "D. Paid."),
        (
            {"id": "a", "description": "D.", "pricing": {"type": "metered", "amount": "0.01", "currency": "USDC"}},
            "D. Paid.",
        ),
        ({"id": "a", "description": "D."}, "D. Paid."),
    ],
)
def test_description_text(resource, description):
    assert [t.description for t in described(resource)] == [description]


def test_an_empty_allowlist_keeps_nothing_and_none_keeps_everything(caplog):
    body = [{"id": "a", "pricing": {"type": "free"}}, {"id": "b", "pricing": {"type": "free"}}]
    with caplog.at_level(logging.WARNING, logger="agent_commerce_client"):
        assert described(*body, resources=[]) == []
        assert [t.name for t in described(*body, resources=None)] == ["a", "b"]
        assert [t.name for t in described(*body, resources=("b",))] == ["b"]
    assert [r.getMessage().split(":")[0] for r in caplog.records] == [
        'Skipping resource "a"',
        'Skipping resource "b"',
        'Skipping resource "a"',
    ]


def test_a_skipped_id_is_logged_as_one_escaped_double_quoted_token(caplog):
    with caplog.at_level(logging.WARNING, logger="agent_commerce_client"):
        described({"id": 'weird "id"\nnext', "exposedVia": ["mcp"]})
    (record,) = caplog.records
    assert '"weird \\"id\\"\\nnext"' in record.getMessage() and "\n" not in record.getMessage()


def test_error_messages_start_with_the_code_exactly_once():
    error = AgentCommerceError("LIMIT_EXCEEDED", "too much", 429)
    assert (error.message, str(error), error.args) == ("LIMIT_EXCEEDED: too much",) * 2 + (
        ("LIMIT_EXCEEDED: too much",),
    )
    assert error.status == 429
    assert AgentCommerceError("LIMIT_EXCEEDED", "LIMIT_EXCEEDED: too much").message == "LIMIT_EXCEEDED: too much"
    assert (
        AgentCommerceError("BACKEND_ERROR", "LIMIT_EXCEEDED: nested").message == "BACKEND_ERROR: LIMIT_EXCEEDED: nested"
    )
