"""Tests for LSP-funded inbound liquidity.

Two layers:

  - Pure-logic unit tests for the Zeus-preference rule, throttling,
    1-channel-per-wallet invariant, network mapping, dynamic reserve
    floor, quote persistence.

  - End-to-end tests using `MockLSPServer` — a stdlib http.server stub
    that impersonates Zeus and/or Megalithic on a free port. These
    exercise the full request flow including the HTTP roundtrip,
    quote persistence, order-state transitions, and the dispatch
    into electrum_pay_onchain (mocked to record).
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytest

import liquidityhelper
import lsp_providers
from node_database import LspPriceQuote, LspChannelOrder
from tests._fakes import FakeBitcartAPI
from tests._lsp_mock import MockLSPServer


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _set(monkeypatch, **kw):
    defaults = {
        "MANUAL_CHANNEL_CREATION_ENABLED": False,
        "LSP_CHANNEL_SIZE_SAT": 150_000,
        "LSP_CHANNEL_EXPIRY_BLOCKS": 52_596,
        "LSP_MIN_ONCHAIN_FOR_QUOTE_SAT": 1_000,
        "LSP_QUOTE_THROTTLE_HOURS": 24,
        "LSP_RESERVE_CAP_SAT": 50_000,
        "MIN_RESERVE_ONCHAIN": 10_000,
    }
    for name, value in {**defaults, **kw}.items():
        monkeypatch.setattr(liquidityhelper, name, value, raising=False)


def _make_quote(provider: str, fee_total_sat: int, order_id: str = "test"):
    """Build a minimal LspQuote stub with just the fields the picker uses."""
    return lsp_providers.LspQuote(
        provider=provider, network="mainnet",
        order_id=order_id,
        lsp_peer_pubkey="aa" * 33,
        lsp_peer_uri="aa" * 33 + "@1.2.3.4:9735",
        lsp_balance_sat=150_000,
        fee_total_sat=fee_total_sat,
        order_total_sat=fee_total_sat,
        channel_expiry_blocks=52_596,
        onchain_address="bc1qfakeforpicker",
        bolt11_invoice="lnbcfake",
    )


def _record_onchain_payment(monkeypatch):
    """Mock electrum_pay_onchain. Returns the call list."""
    calls: List[Dict[str, Any]] = []

    async def fake_pay(dest_addr, amount, label="", *, wallet, api=None):
        calls.append({
            "dest_addr": dest_addr,
            "amount": amount,
            "label": label,
            "wallet_id": wallet["id"],
        })
        return True
    monkeypatch.setattr(liquidityhelper, "electrum_pay_onchain", fake_pay)
    return calls


# ---------------------------------------------------------------------------
# _pick_with_zeus_preference  —  the ±10% Zeus-wins rule
# ---------------------------------------------------------------------------

class _StubProvider:
    def __init__(self, name): self.name = name


def test_picker_zeus_wins_when_zeus_cheaper():
    """Trivially Zeus when it's strictly cheaper."""
    quotes = {
        "zeus": (_StubProvider("zeus"), _make_quote("zeus", 100)),
        "megalithic": (_StubProvider("megalithic"), _make_quote("megalithic", 200)),
    }
    chosen = liquidityhelper._pick_with_zeus_preference(quotes)
    assert chosen[1].provider == "zeus"


def test_picker_zeus_wins_within_10pct_when_pricier():
    """Zeus is the pricier one, but within 10% of Megalithic.
    Zeus-preference rule: Zeus wins anyway."""
    quotes = {
        "zeus": (_StubProvider("zeus"), _make_quote("zeus", 109)),
        "megalithic": (_StubProvider("megalithic"), _make_quote("megalithic", 100)),
    }
    chosen = liquidityhelper._pick_with_zeus_preference(quotes)
    assert chosen[1].provider == "zeus"
    # 109/100 = 1.09 -> within 10%, Zeus wins


def test_picker_zeus_wins_exactly_at_10pct():
    """Boundary: Zeus is exactly 10% pricier. Tied = Zeus wins."""
    quotes = {
        "zeus": (_StubProvider("zeus"), _make_quote("zeus", 110)),
        "megalithic": (_StubProvider("megalithic"), _make_quote("megalithic", 100)),
    }
    chosen = liquidityhelper._pick_with_zeus_preference(quotes)
    assert chosen[1].provider == "zeus"


def test_picker_megalithic_wins_when_zeus_too_pricey():
    """Zeus is more than 10% pricier -> Megalithic wins (cheaper)."""
    quotes = {
        "zeus": (_StubProvider("zeus"), _make_quote("zeus", 200)),
        "megalithic": (_StubProvider("megalithic"), _make_quote("megalithic", 100)),
    }
    chosen = liquidityhelper._pick_with_zeus_preference(quotes)
    assert chosen[1].provider == "megalithic"


def test_picker_only_zeus_available():
    quotes = {"zeus": (_StubProvider("zeus"), _make_quote("zeus", 500))}
    chosen = liquidityhelper._pick_with_zeus_preference(quotes)
    assert chosen[1].provider == "zeus"


def test_picker_only_megalithic_available():
    quotes = {"megalithic": (_StubProvider("megalithic"), _make_quote("megalithic", 500))}
    chosen = liquidityhelper._pick_with_zeus_preference(quotes)
    assert chosen[1].provider == "megalithic"


def test_picker_empty_returns_none():
    assert liquidityhelper._pick_with_zeus_preference({}) is None


# ---------------------------------------------------------------------------
# Throttling — once per LSP per wallet per day
# ---------------------------------------------------------------------------

def test_throttle_allows_first_quote():
    """Empty DB -> can quote."""
    assert liquidityhelper._can_quote_lsp_for_wallet("zeus", "w1") is True


def test_throttle_blocks_within_window(monkeypatch):
    """A quote 1h ago should block a new quote (throttle=24h default)."""
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="o1", lsp_balance_sat=150_000, fee_total_sat=1000,
        order_total_sat=1000, channel_expiry_blocks=52596,
        fetched_at=datetime.datetime.now() - datetime.timedelta(hours=1),
    )
    assert liquidityhelper._can_quote_lsp_for_wallet("zeus", "w1") is False


def test_throttle_allows_after_window(monkeypatch):
    """A quote 25h ago is outside the 24h window -> allow."""
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="o1", lsp_balance_sat=150_000, fee_total_sat=1000,
        order_total_sat=1000, channel_expiry_blocks=52596,
        fetched_at=datetime.datetime.now() - datetime.timedelta(hours=25),
    )
    assert liquidityhelper._can_quote_lsp_for_wallet("zeus", "w1") is True


def test_throttle_is_per_wallet():
    """A quote on wallet A doesn't throttle wallet B."""
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w-A",
        order_id="o1", lsp_balance_sat=150_000, fee_total_sat=1000,
        order_total_sat=1000, channel_expiry_blocks=52596,
        fetched_at=datetime.datetime.now() - datetime.timedelta(hours=1),
    )
    assert liquidityhelper._can_quote_lsp_for_wallet("zeus", "w-B") is True


def test_throttle_is_per_provider():
    """A Zeus quote doesn't throttle a Megalithic quote on the same wallet."""
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="o1", lsp_balance_sat=150_000, fee_total_sat=1000,
        order_total_sat=1000, channel_expiry_blocks=52596,
        fetched_at=datetime.datetime.now() - datetime.timedelta(hours=1),
    )
    assert liquidityhelper._can_quote_lsp_for_wallet("megalithic", "w1") is True


# ---------------------------------------------------------------------------
# 1-channel-per-wallet invariant
# ---------------------------------------------------------------------------

def test_no_existing_order_means_can_request():
    assert liquidityhelper._wallet_has_open_lsp_order("w1") is False


def test_existing_paid_order_blocks_new_request():
    LspChannelOrder.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="o1", lsp_peer_pubkey="aa" * 33,
        lsp_balance_sat=150_000, fee_total_sat=1000,
        state="PAID",
    )
    assert liquidityhelper._wallet_has_open_lsp_order("w1") is True


def test_existing_ordered_state_blocks_new_request():
    LspChannelOrder.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="o1", lsp_peer_pubkey="aa" * 33,
        lsp_balance_sat=150_000, fee_total_sat=1000,
        state="ORDERED",
    )
    assert liquidityhelper._wallet_has_open_lsp_order("w1") is True


def test_terminal_state_does_not_block_new_request():
    """FAILED / EXPIRED / CLOSED orders don't count toward the
    one-channel-per-wallet limit (channel is gone)."""
    for state in ("FAILED", "EXPIRED", "CLOSED"):
        LspChannelOrder.create(
            provider="zeus", network="mainnet", wallet_id=f"w-{state}",
            order_id=f"o-{state}", lsp_peer_pubkey="aa" * 33,
            lsp_balance_sat=150_000, fee_total_sat=1000,
            state=state,
        )
        assert liquidityhelper._wallet_has_open_lsp_order(f"w-{state}") is False, state


def test_invariant_is_per_wallet():
    """Wallet A's existing channel doesn't block Wallet B."""
    LspChannelOrder.create(
        provider="zeus", network="mainnet", wallet_id="w-A",
        order_id="o-A", lsp_peer_pubkey="aa" * 33,
        lsp_balance_sat=150_000, fee_total_sat=1000,
        state="OPENED",
    )
    assert liquidityhelper._wallet_has_open_lsp_order("w-A") is True
    assert liquidityhelper._wallet_has_open_lsp_order("w-B") is False


# ---------------------------------------------------------------------------
# Dynamic on-chain reserve floor
# ---------------------------------------------------------------------------

def test_max_lsp_quote_6mo_empty_db():
    assert liquidityhelper.max_lsp_quote_in_last_6_months_sat() == 0


def test_max_lsp_quote_6mo_picks_largest(monkeypatch):
    _set(monkeypatch, LSP_MAX_FEE_PERCENT=1.0)   # disable percent cap for this test
    for fee in (500, 2000, 800):
        LspPriceQuote.create(
            provider="zeus", network="mainnet", wallet_id="w1",
            order_id=f"o-{fee}", lsp_balance_sat=150_000,
            fee_total_sat=fee, order_total_sat=fee, channel_expiry_blocks=52596,
        )
    assert liquidityhelper.max_lsp_quote_in_last_6_months_sat() == 2000


def test_max_lsp_quote_6mo_window():
    """A row 200d old is outside the window and ignored."""
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="old", lsp_balance_sat=150_000,
        fee_total_sat=10_000, order_total_sat=10_000,
        channel_expiry_blocks=52596,
        fetched_at=datetime.datetime.now() - datetime.timedelta(days=200),
    )
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="fresh", lsp_balance_sat=150_000,
        fee_total_sat=500, order_total_sat=500,
        channel_expiry_blocks=52596,
        fetched_at=datetime.datetime.now() - datetime.timedelta(days=5),
    )
    assert liquidityhelper.max_lsp_quote_in_last_6_months_sat() == 500


def test_max_lsp_quote_6mo_includes_all_providers():
    """User confirmed: max across BOTH Zeus and Megalithic."""
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="z", lsp_balance_sat=150_000, fee_total_sat=300,
        order_total_sat=300, channel_expiry_blocks=52596,
    )
    LspPriceQuote.create(
        provider="megalithic", network="mainnet", wallet_id="w1",
        order_id="m", lsp_balance_sat=150_000, fee_total_sat=900,
        order_total_sat=900, channel_expiry_blocks=52596,
    )
    assert liquidityhelper.max_lsp_quote_in_last_6_months_sat() == 900


def test_effective_min_reserve_uses_max_of_config_and_lsp(monkeypatch):
    """Isolating the max(config, lsp_history) behavior: disable the
    percent cap so the 15k quote isn't pre-filtered."""
    _set(monkeypatch, MIN_RESERVE_ONCHAIN=10_000, LSP_MAX_FEE_PERCENT=1.0)
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="o", lsp_balance_sat=150_000, fee_total_sat=15_000,
        order_total_sat=15_000, channel_expiry_blocks=52596,
    )
    assert liquidityhelper.effective_min_reserve_onchain() == 15_000


def test_effective_min_reserve_capped_at_LSP_RESERVE_CAP_SAT(monkeypatch):
    """User: 'never larger than 50,000 sats' — the LSP_RESERVE_CAP_SAT
    cap kicks in here. Disable the fee-percent cap for this test so the
    80k-sat quote isn't pre-filtered (we're isolating the
    LSP_RESERVE_CAP_SAT behavior)."""
    _set(monkeypatch, MIN_RESERVE_ONCHAIN=10_000, LSP_RESERVE_CAP_SAT=50_000,
         LSP_MAX_FEE_PERCENT=1.0)   # 100% — effectively disable percent cap
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="spike", lsp_balance_sat=150_000, fee_total_sat=80_000,
        order_total_sat=80_000, channel_expiry_blocks=52596,
    )
    assert liquidityhelper.effective_min_reserve_onchain() == 50_000


def test_effective_min_reserve_falls_back_to_config_when_no_quotes(monkeypatch):
    _set(monkeypatch, MIN_RESERVE_ONCHAIN=8_000, LSP_RESERVE_CAP_SAT=50_000)
    # No LspPriceQuote rows.
    assert liquidityhelper.effective_min_reserve_onchain() == 8_000


def test_effective_min_reserve_caps_high_config_too(monkeypatch):
    """If MIN_RESERVE_ONCHAIN itself is higher than the cap, the cap
    still applies."""
    _set(monkeypatch, MIN_RESERVE_ONCHAIN=100_000, LSP_RESERVE_CAP_SAT=50_000)
    assert liquidityhelper.effective_min_reserve_onchain() == 50_000


# ---------------------------------------------------------------------------
# Network mapping
# ---------------------------------------------------------------------------

@pytest.fixture
def api_with_lnd_info():
    """FakeBitcartAPI with get_lnd_info stubbed."""
    api = FakeBitcartAPI()

    network_per_wallet = {}

    async def get_lnd_info(wid):
        return {"network": network_per_wallet.get(wid)} if wid in network_per_wallet else None

    api.get_lnd_info = get_lnd_info
    api._set_network = lambda wid, net: network_per_wallet.update({wid: net})
    return api


def test_lsp_network_for_wallet_electrum_returns_none(api_with_lnd_info, event_loop):
    """Electrum wallets aren't LND, no LSP path for them."""
    api_with_lnd_info.add_wallet("w1", currency="btc")
    result = event_loop.run_until_complete(
        liquidityhelper.lsp_network_for_wallet(
            api_with_lnd_info.wallets["w1"], api_with_lnd_info,
        )
    )
    assert result is None


def test_lsp_network_for_wallet_normalizes_testnet3(api_with_lnd_info, event_loop):
    api_with_lnd_info.add_wallet("w1", currency="btclnd")
    api_with_lnd_info._set_network("w1", "testnet3")
    result = event_loop.run_until_complete(
        liquidityhelper.lsp_network_for_wallet(
            api_with_lnd_info.wallets["w1"], api_with_lnd_info,
        )
    )
    assert result == "testnet"


def test_lsp_network_for_wallet_returns_mainnet(api_with_lnd_info, event_loop):
    api_with_lnd_info.add_wallet("w1", currency="btclnd")
    api_with_lnd_info._set_network("w1", "mainnet")
    result = event_loop.run_until_complete(
        liquidityhelper.lsp_network_for_wallet(
            api_with_lnd_info.wallets["w1"], api_with_lnd_info,
        )
    )
    assert result == "mainnet"


def test_lsp_network_for_wallet_regtest_returns_none(api_with_lnd_info, event_loop):
    """Regtest is not served by any public LSP."""
    api_with_lnd_info.add_wallet("w1", currency="btclnd")
    api_with_lnd_info._set_network("w1", "regtest")
    result = event_loop.run_until_complete(
        liquidityhelper.lsp_network_for_wallet(
            api_with_lnd_info.wallets["w1"], api_with_lnd_info,
        )
    )
    assert result is None


def test_lsp_network_for_wallet_unknown_returns_none(api_with_lnd_info, event_loop):
    api_with_lnd_info.add_wallet("w1", currency="btclnd")
    api_with_lnd_info._set_network("w1", "wonderland")
    result = event_loop.run_until_complete(
        liquidityhelper.lsp_network_for_wallet(
            api_with_lnd_info.wallets["w1"], api_with_lnd_info,
        )
    )
    assert result is None


def test_lsp_network_for_wallet_normalizes_testnet4(api_with_lnd_info, event_loop):
    """Modern Bitcoin Core defaults to testnet4. We normalize to
    'testnet' for internal labeling — Zeus's testnet-lsps1.lnolymp.us
    is testnet3-specific, but the normalization keeps the routing
    consistent and a separate WARNING decision log surfaces the
    chain-flavor caveat."""
    api_with_lnd_info.add_wallet("w1", currency="btclnd")
    api_with_lnd_info._set_network("w1", "testnet4")
    result = event_loop.run_until_complete(
        liquidityhelper.lsp_network_for_wallet(
            api_with_lnd_info.wallets["w1"], api_with_lnd_info,
        )
    )
    assert result == "testnet"


def test_lsp_network_for_wallet_testnet4_emits_warning_decision(
    api_with_lnd_info, event_loop, caplog,
):
    """The testnet4 → testnet normalization comes with a WARNING-level
    decision log calling out Zeus's testnet3-only endpoint. Pin the
    severity so a future log-level downgrade doesn't quietly hide it.

    Uses a UNIQUE wallet_id ('w-testnet4-warn') so log_decision's
    in-memory dedup doesn't suppress this — the previous test in this
    file fires the same key+value on wallet 'w1'."""
    import logging as _logging
    api_with_lnd_info.add_wallet("w-testnet4-warn", currency="btclnd")
    api_with_lnd_info._set_network("w-testnet4-warn", "testnet4")

    decisions_logger = _logging.getLogger("liquidityhelper.decisions")
    decisions_logger.addHandler(caplog.handler)
    caplog.set_level(_logging.WARNING)
    try:
        event_loop.run_until_complete(
            liquidityhelper.lsp_network_for_wallet(
                api_with_lnd_info.wallets["w-testnet4-warn"], api_with_lnd_info,
            )
        )
    finally:
        decisions_logger.removeHandler(caplog.handler)
    warns = [r for r in caplog.records if r.levelno >= _logging.WARNING]
    assert any("testnet4" in r.getMessage() for r in warns), (
        f"expected a WARNING decision about testnet4; got {[r.getMessage() for r in warns]}"
    )
    assert any("testnet3-specific" in r.getMessage() for r in warns), (
        "warning must call out the testnet3-only nature of Zeus's endpoint"
    )


def test_lsp_network_for_wallet_testnet3_no_warning(
    api_with_lnd_info, event_loop, caplog,
):
    """testnet3 is the supported, expected testnet flavor — no warning."""
    import logging as _logging
    api_with_lnd_info.add_wallet("w-testnet3-quiet", currency="btclnd")
    api_with_lnd_info._set_network("w-testnet3-quiet", "testnet3")

    decisions_logger = _logging.getLogger("liquidityhelper.decisions")
    decisions_logger.addHandler(caplog.handler)
    caplog.set_level(_logging.WARNING)
    try:
        event_loop.run_until_complete(
            liquidityhelper.lsp_network_for_wallet(
                api_with_lnd_info.wallets["w-testnet3-quiet"], api_with_lnd_info,
            )
        )
    finally:
        decisions_logger.removeHandler(caplog.handler)
    warns = [r for r in caplog.records if r.levelno >= _logging.WARNING]
    assert not any("testnet" in r.getMessage() for r in warns), (
        "testnet3 must not emit a WARNING; only testnet4 does."
    )


# ---------------------------------------------------------------------------
# End-to-end with MockLSPServer
# ---------------------------------------------------------------------------

@pytest.fixture
def lsp_mocks():
    """Two MockLSPServer instances on free ports. Tests configure
    responses; the lsp_providers registry is swapped for stubbed
    providers pointing at the mocks."""
    zeus = MockLSPServer()
    meg = MockLSPServer()
    zeus.start()
    meg.start()
    try:
        yield zeus, meg
    finally:
        zeus.stop()
        meg.stop()
        lsp_providers.reset_lsp_providers()


def _install_mock_providers(monkeypatch, zeus_mock, meg_mock):
    """Build provider instances that point at the mock servers' base
    URLs, and register them as the active providers."""
    class _MockZeus(lsp_providers._RestLSPProvider):
        name = "zeus"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url=zeus_mock.base_url,
                lsp_peer_uri="aa" * 33 + "@zeus.test:9735",
            ),
            lsp_providers.NETWORK_TESTNET: lsp_providers._Endpoint(
                base_url=zeus_mock.base_url,
                lsp_peer_uri="aa" * 33 + "@zeus.test:9735",
            ),
        }

    class _MockMegalithic(lsp_providers._RestLSPProvider):
        name = "megalithic"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url=meg_mock.base_url,
                lsp_peer_uri="bb" * 33 + "@megalithic.test:9735",
            ),
        }

    lsp_providers.set_lsp_providers([_MockZeus(), _MockMegalithic()])
    return _MockZeus(), _MockMegalithic()


def test_e2e_request_picks_zeus_and_pays_onchain(lsp_mocks, monkeypatch, event_loop):
    """Happy path: both LSPs quote, Zeus wins (within 10%), on-chain
    payment fires to Zeus's address, LspChannelOrder is persisted in
    PAID state."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    # Both quote similar prices (within 10%): Zeus wins.
    zeus_mock.set_create_order_response(lambda req: {
        "order_id": "zeus-o-1",
        "lsp_balance_sat": req.get("lsp_balance_sat", "150000"),
        "client_balance_sat": "0",
        "channel_expiry_blocks": req.get("channel_expiry_blocks", 52596),
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "1050",
            "order_total_sat": "1050",
            "bolt11_invoice": "lnbczeus",
            "onchain_address": "bc1qzeuspay",
        },
    })
    meg_mock.set_create_order_response(lambda req: {
        "order_id": "meg-o-1",
        "lsp_balance_sat": req.get("lsp_balance_sat", "150000"),
        "client_balance_sat": "0",
        "channel_expiry_blocks": req.get("channel_expiry_blocks", 52596),
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "1000",   # cheaper, but within 10% of Zeus
            "order_total_sat": "1000",
            "bolt11_invoice": "lnbcmeg",
            "onchain_address": "bc1qmegpay",
        },
    })

    pay_calls = _record_onchain_payment(monkeypatch)

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)  # 1M sat
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )

    assert result is not None
    assert result.provider == "zeus"
    assert result.state == "PAID"
    # Zeus's onchain address is what we paid to.
    assert len(pay_calls) == 1
    assert pay_calls[0]["dest_addr"] == "bc1qzeuspay"
    # Persistence: both quotes saved, both order persisted in PAID state.
    quotes = list(LspPriceQuote.select())
    assert {q.provider for q in quotes} == {"zeus", "megalithic"}
    orders = list(LspChannelOrder.select())
    assert len(orders) == 1
    assert orders[0].provider == "zeus"


def test_e2e_picks_megalithic_when_much_cheaper(lsp_mocks, monkeypatch, event_loop):
    """Zeus quote > Megalithic + 10% -> Megalithic wins."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    zeus_mock.set_create_order_response(lambda req: {
        "order_id": "zeus-o-1", "lsp_balance_sat": "150000",
        "client_balance_sat": "0", "channel_expiry_blocks": 52596,
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "3000", "order_total_sat": "3000",
            "bolt11_invoice": "lnbcz", "onchain_address": "bc1qzeuspay",
        },
    })
    meg_mock.set_create_order_response(lambda req: {
        "order_id": "meg-o-1", "lsp_balance_sat": "150000",
        "client_balance_sat": "0", "channel_expiry_blocks": 52596,
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "1000", "order_total_sat": "1000",
            "bolt11_invoice": "lnbcm", "onchain_address": "bc1qmegpay",
        },
    })

    pay_calls = _record_onchain_payment(monkeypatch)

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result.provider == "megalithic"
    assert pay_calls[0]["dest_addr"] == "bc1qmegpay"


def test_e2e_skips_provider_when_throttled(lsp_mocks, monkeypatch, event_loop):
    """Zeus was quoted 1h ago; Megalithic is fresh. Only Megalithic
    is queried this run."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    # Pre-seed a recent Zeus quote -> throttle blocks Zeus.
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="prev", lsp_balance_sat=150_000, fee_total_sat=500,
        order_total_sat=500, channel_expiry_blocks=52596,
        fetched_at=datetime.datetime.now() - datetime.timedelta(hours=1),
    )

    _record_onchain_payment(monkeypatch)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is not None
    assert result.provider == "megalithic"
    # Zeus mock should NOT have been hit.
    assert all(r["path"] != "/api/v1/create_order"
               for r in zeus_mock.requests), zeus_mock.requests


def test_e2e_skips_when_below_min_onchain(lsp_mocks, monkeypatch, event_loop):
    """Wallet balance < LSP_MIN_ONCHAIN_FOR_QUOTE_SAT -> no quotes
    at all."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch, LSP_MIN_ONCHAIN_FOR_QUOTE_SAT=10_000)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.00001)  # 1k sat
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is None
    # Neither mock should have received a create_order.
    assert zeus_mock.requests == []
    assert meg_mock.requests == []


def test_e2e_skips_when_wallet_has_existing_lsp_channel(lsp_mocks, monkeypatch, event_loop):
    """An existing OPENED LspChannelOrder blocks new requests."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    LspChannelOrder.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="old", lsp_peer_pubkey="aa" * 33,
        lsp_balance_sat=150_000, fee_total_sat=1000, state="OPENED",
    )

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is None
    assert zeus_mock.requests == []
    assert meg_mock.requests == []


def test_e2e_skips_when_electrum_wallet(lsp_mocks, monkeypatch, event_loop):
    """LSPs are LND-only in this codebase. Electrum wallet -> skip."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=0.01)

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is None


def test_e2e_one_provider_errors_other_succeeds(lsp_mocks, monkeypatch, event_loop):
    """Zeus errors -> Megalithic is used. No exception bubbles out."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    zeus_mock.fail_create_order(status=500)
    meg_mock.set_create_order_response(lambda req: {
        "order_id": "m1", "lsp_balance_sat": "150000",
        "client_balance_sat": "0", "channel_expiry_blocks": 52596,
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "1500", "order_total_sat": "1500",
            "bolt11_invoice": "lnbcm", "onchain_address": "bc1qmegpay",
        },
    })

    pay_calls = _record_onchain_payment(monkeypatch)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is not None
    assert result.provider == "megalithic"
    assert pay_calls[0]["dest_addr"] == "bc1qmegpay"


def test_e2e_both_providers_fail_returns_none(lsp_mocks, monkeypatch, event_loop):
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    zeus_mock.fail_create_order(status=500)
    meg_mock.fail_create_order(status=500)

    pay_calls = _record_onchain_payment(monkeypatch)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is None
    assert pay_calls == []


def test_e2e_payment_failure_marks_order_failed(lsp_mocks, monkeypatch, event_loop):
    """Quote accepted but our on-chain pay fails -> order state FAILED."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    zeus_mock.set_create_order_response(lambda req: {
        "order_id": "z1", "lsp_balance_sat": "150000",
        "client_balance_sat": "0", "channel_expiry_blocks": 52596,
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "1000", "order_total_sat": "1000",
            "bolt11_invoice": "lnbcz", "onchain_address": "bc1qzeuspay",
        },
    })
    meg_mock.fail_create_order(status=503)

    async def fail_pay(*a, **kw):
        return False
    monkeypatch.setattr(liquidityhelper, "electrum_pay_onchain", fail_pay)

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is None
    orders = list(LspChannelOrder.select())
    assert len(orders) == 1
    assert orders[0].state == "FAILED"


def test_e2e_persists_correct_label_in_pay_call(lsp_mocks, monkeypatch, event_loop):
    """The on-chain payment label should encode the order_id so it's
    traceable in onchain history."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    zeus_mock.set_create_order_response(lambda req: {
        "order_id": "ord-traceable-42", "lsp_balance_sat": "150000",
        "client_balance_sat": "0", "channel_expiry_blocks": 52596,
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "1000", "order_total_sat": "1000",
            "bolt11_invoice": "lnbcz", "onchain_address": "bc1qzeuspay",
        },
    })
    meg_mock.fail_create_order(status=500)

    pay_calls = _record_onchain_payment(monkeypatch)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )

    event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert pay_calls[0]["label"] == "lsp_channel_order:ord-traceable-42"


# ---------------------------------------------------------------------------
# Daily cleanup
# ---------------------------------------------------------------------------

def test_cleanup_old_lsp_quotes_removes_old_rows(event_loop):
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="o-old", lsp_balance_sat=150_000, fee_total_sat=500,
        order_total_sat=500, channel_expiry_blocks=52596,
        fetched_at=datetime.datetime.now() - datetime.timedelta(days=200),
    )
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="o-new", lsp_balance_sat=150_000, fee_total_sat=600,
        order_total_sat=600, channel_expiry_blocks=52596,
        fetched_at=datetime.datetime.now() - datetime.timedelta(days=10),
    )
    deleted = event_loop.run_until_complete(
        liquidityhelper.cleanup_old_lsp_quotes()
    )
    assert deleted == 1
    remaining = list(LspPriceQuote.select())
    assert len(remaining) == 1
    assert remaining[0].order_id == "o-new"


# ---------------------------------------------------------------------------
# LSP_MAX_FEE_PERCENT — fee cap as fraction of channel size
# ---------------------------------------------------------------------------

def test_max_fee_percent_default_is_1pct():
    """Shipped default is 1%. Flag changes here are operator-facing,
    so pin the default."""
    import importlib
    import config
    importlib.reload(config)
    assert config.LSP_MAX_FEE_PERCENT == 0.01


def test_picker_rejects_quote_above_cap(lsp_mocks, monkeypatch, event_loop):
    """Zeus quotes 5% of channel (over the 1% cap); only Megalithic
    survives and wins by default."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch, LSP_MAX_FEE_PERCENT=0.01)   # 1%
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    # Zeus: 7500 sat fee on 150000 sat channel = 5% -> over the 1% cap
    zeus_mock.set_create_order_response(lambda req: {
        "order_id": "z-bad", "lsp_balance_sat": "150000",
        "client_balance_sat": "0", "channel_expiry_blocks": 52596,
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "7500", "order_total_sat": "7500",
            "bolt11_invoice": "lnbcz", "onchain_address": "bc1qzeuspay",
        },
    })
    # Megalithic: 1000 sat fee on 150000 = 0.66% -> under the cap
    meg_mock.set_create_order_response(lambda req: {
        "order_id": "m-ok", "lsp_balance_sat": "150000",
        "client_balance_sat": "0", "channel_expiry_blocks": 52596,
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "1000", "order_total_sat": "1000",
            "bolt11_invoice": "lnbcm", "onchain_address": "bc1qmegpay",
        },
    })

    pay_calls = _record_onchain_payment(monkeypatch)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is not None
    assert result.provider == "megalithic"
    assert pay_calls[0]["dest_addr"] == "bc1qmegpay"
    # The cap-exceeding quote is still PERSISTED for audit.
    quotes = list(LspPriceQuote.select().where(LspPriceQuote.order_id == "z-bad"))
    assert len(quotes) == 1
    assert quotes[0].fee_total_sat == 7500


def test_picker_rejects_both_quotes_when_both_exceed_cap(lsp_mocks, monkeypatch, event_loop):
    """Both Zeus and Megalithic above 1% -> request returns None."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch, LSP_MAX_FEE_PERCENT=0.01)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    for mock, oid, addr in (
        (zeus_mock, "z-bad", "bc1qzeuspay"),
        (meg_mock, "m-bad", "bc1qmegpay"),
    ):
        mock.set_create_order_response(lambda req, _oid=oid, _addr=addr: {
            "order_id": _oid, "lsp_balance_sat": "150000",
            "client_balance_sat": "0", "channel_expiry_blocks": 52596,
            "order_state": "CREATED",
            "payment": {
                "fee_total_sat": "10000",   # 6.67% — over cap
                "order_total_sat": "10000",
                "bolt11_invoice": "lnbc", "onchain_address": _addr,
            },
        })

    pay_calls = _record_onchain_payment(monkeypatch)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is None
    assert pay_calls == []
    # Both quotes still persisted for audit
    rows = list(LspPriceQuote.select())
    assert len(rows) == 2


def test_picker_accepts_exactly_at_cap(lsp_mocks, monkeypatch, event_loop):
    """Boundary: fee == cap * channel exactly. Not > cap -> accept."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch, LSP_MAX_FEE_PERCENT=0.01)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    # 1500 / 150000 = 0.01 exactly. Cap is `>`, not `>=`, so accept.
    zeus_mock.set_create_order_response(lambda req: {
        "order_id": "z-boundary", "lsp_balance_sat": "150000",
        "client_balance_sat": "0", "channel_expiry_blocks": 52596,
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "1500", "order_total_sat": "1500",
            "bolt11_invoice": "lnbcz", "onchain_address": "bc1qzeuspay",
        },
    })
    meg_mock.fail_create_order(status=500)

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )
    _record_onchain_payment(monkeypatch)

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is not None
    assert result.order_id == "z-boundary"


def test_picker_respects_custom_cap(lsp_mocks, monkeypatch, event_loop):
    """Operator bumps the cap to 5%. A 3% quote that would have been
    rejected at 1% now passes."""
    zeus_mock, meg_mock = lsp_mocks
    _set(monkeypatch, LSP_MAX_FEE_PERCENT=0.05)
    _install_mock_providers(monkeypatch, zeus_mock, meg_mock)

    zeus_mock.set_create_order_response(lambda req: {
        "order_id": "z3", "lsp_balance_sat": "150000",
        "client_balance_sat": "0", "channel_expiry_blocks": 52596,
        "order_state": "CREATED",
        "payment": {
            "fee_total_sat": "4500", "order_total_sat": "4500",  # 3%
            "bolt11_invoice": "lnbcz", "onchain_address": "bc1qzeuspay",
        },
    })
    meg_mock.fail_create_order(status=500)

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.01)
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})
    api.get_wallet_ln_node_id = lambda wid: _async_return(
        "cc" * 33 + "@client.test:9735"
    )
    pay_calls = _record_onchain_payment(monkeypatch)

    result = event_loop.run_until_complete(
        liquidityhelper.request_inbound_liquidity_from_lsp(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is not None
    assert pay_calls[0]["amount"] == liquidityhelper.sats_to_btc(4500)


def test_max_lsp_quote_6mo_filters_cap_exceeders(monkeypatch):
    """A persisted quote that violates the current cap should NOT pump
    the dynamic reserve floor. We never would have paid it."""
    _set(monkeypatch, LSP_MAX_FEE_PERCENT=0.01)
    # 5% of channel — would be rejected by the picker.
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="violator", lsp_balance_sat=150_000,
        fee_total_sat=7500, order_total_sat=7500,
        channel_expiry_blocks=52596,
    )
    # 0.5% — passes.
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="acceptable", lsp_balance_sat=150_000,
        fee_total_sat=750, order_total_sat=750,
        channel_expiry_blocks=52596,
    )
    # The violator must NOT win the max — we'd never pay it.
    assert liquidityhelper.max_lsp_quote_in_last_6_months_sat() == 750


def test_max_lsp_quote_6mo_handles_zero_balance_quote(monkeypatch):
    """Defensive: a malformed quote with lsp_balance_sat=0 (would cause
    division by zero) is silently dropped."""
    _set(monkeypatch, LSP_MAX_FEE_PERCENT=0.01)
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="weird", lsp_balance_sat=0,
        fee_total_sat=100, order_total_sat=100, channel_expiry_blocks=52596,
    )
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w1",
        order_id="normal", lsp_balance_sat=150_000,
        fee_total_sat=500, order_total_sat=500, channel_expiry_blocks=52596,
    )
    assert liquidityhelper.max_lsp_quote_in_last_6_months_sat() == 500


# ---------------------------------------------------------------------------
# Pay-decision logging — the comparison reasoning lands in decisions.log
# ---------------------------------------------------------------------------

def test_picker_logs_within_10pct_decision(monkeypatch):
    """When both providers quote and the prices are within ±10%, the
    log line should explicitly say 'within ±10%' so an operator can
    trace WHY Zeus won despite being pricier."""
    import logging as _logging
    captured: List[_logging.LogRecord] = []

    class _Capture(_logging.Handler):
        def emit(self, record): captured.append(record)

    cap = _Capture(level=_logging.DEBUG)
    liquidityhelper.decisions_logger.addHandler(cap)
    try:
        # Zeus 109 vs Megalithic 100 -> within 10%, Zeus wins.
        quotes = {
            "zeus": (_StubProvider("zeus"), _make_quote("zeus", 109)),
            "megalithic": (_StubProvider("megalithic"), _make_quote("megalithic", 100)),
        }
        liquidityhelper._pick_with_zeus_preference(quotes)
    finally:
        liquidityhelper.decisions_logger.removeHandler(cap)

    msgs = [r.getMessage() for r in captured]
    assert any("chose zeus" in m and "within ±10%" in m for m in msgs), msgs


def test_picker_logs_strictly_cheaper_decision(monkeypatch):
    """When Zeus is >10% cheaper, the log should say 'strictly cheaper'."""
    import logging as _logging
    captured: List[_logging.LogRecord] = []

    class _Capture(_logging.Handler):
        def emit(self, record): captured.append(record)

    cap = _Capture(level=_logging.DEBUG)
    liquidityhelper.decisions_logger.addHandler(cap)
    try:
        quotes = {
            "zeus": (_StubProvider("zeus"), _make_quote("zeus", 100)),
            "megalithic": (_StubProvider("megalithic"), _make_quote("megalithic", 200)),
        }
        liquidityhelper._pick_with_zeus_preference(quotes)
    finally:
        liquidityhelper.decisions_logger.removeHandler(cap)

    msgs = [r.getMessage() for r in captured]
    assert any("chose zeus" in m and "strictly cheaper" in m for m in msgs), msgs


def test_picker_logs_megalithic_win(monkeypatch):
    """Megalithic wins (>10% cheaper). Log should record the choice
    AND the reason."""
    import logging as _logging
    captured: List[_logging.LogRecord] = []

    class _Capture(_logging.Handler):
        def emit(self, record): captured.append(record)

    cap = _Capture(level=_logging.DEBUG)
    liquidityhelper.decisions_logger.addHandler(cap)
    try:
        quotes = {
            "zeus": (_StubProvider("zeus"), _make_quote("zeus", 200)),
            "megalithic": (_StubProvider("megalithic"), _make_quote("megalithic", 100)),
        }
        liquidityhelper._pick_with_zeus_preference(quotes)
    finally:
        liquidityhelper.decisions_logger.removeHandler(cap)

    msgs = [r.getMessage() for r in captured]
    assert any("chose megalithic" in m and ">10%" in m for m in msgs), msgs


def test_picker_logs_only_provider_decision(monkeypatch):
    """Only one provider returned a quote — log should say 'only
    provider available'."""
    import logging as _logging
    captured: List[_logging.LogRecord] = []

    class _Capture(_logging.Handler):
        def emit(self, record): captured.append(record)

    cap = _Capture(level=_logging.DEBUG)
    liquidityhelper.decisions_logger.addHandler(cap)
    try:
        quotes = {
            "megalithic": (_StubProvider("megalithic"), _make_quote("megalithic", 500)),
        }
        liquidityhelper._pick_with_zeus_preference(quotes)
    finally:
        liquidityhelper.decisions_logger.removeHandler(cap)

    msgs = [r.getMessage() for r in captured]
    assert any("chose megalithic" in m and "only provider available" in m
               for m in msgs), msgs


# ---------------------------------------------------------------------------
# ensure_lnd_wallets_peered_with_lsps — auto-peer at startup
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _prepopulated_lsp_peer_cache():
    """Auto-applied to every test in this module: pre-fill each
    provider's URI cache with `[hardcoded_fallback]`, so
    `get_all_peer_uris` doesn't try to hit the real Zeus/Megalithic
    endpoints during test runs. After the test, reset the registry so
    the next test starts clean.

    Tests that need to exercise the dynamic-URI flow (e.g. against
    MockLSPServer instances) can clear the cache themselves or use
    set_lsp_providers([...]) to inject providers without pre-populated
    caches."""
    for provider in lsp_providers.get_lsp_providers():
        for network in provider.supported_networks():
            try:
                fallback = provider.lsp_peer_uri(network=network)
            except Exception:
                continue
            if fallback and not fallback.startswith("UNKNOWN@"):
                provider._cached_uris[network] = [fallback]
            else:
                # Sentinel — represented as empty list in the cache so
                # callers know "no URIs available" without re-querying.
                provider._cached_uris[network] = []
    yield
    lsp_providers.reset_lsp_providers()


def _stub_lnd_rpc(behavior_per_pubkey: Dict[str, Any]):
    """behavior_per_pubkey: maps lsp pubkey hex -> 'ok' | Exception instance.
    Returns a function suitable for monkeypatching liquidityhelper.lnd_rpc.
    Also captures every call."""
    calls: List[Dict[str, Any]] = []

    async def fake_lnd_rpc(api, wallet_id, method, params=None, service="Lightning"):
        calls.append({
            "wallet_id": wallet_id, "method": method, "params": params,
        })
        if method != "ConnectPeer":
            return {}
        pubkey = (params or {}).get("addr", {}).get("pubkey", "").lower()
        behavior = behavior_per_pubkey.get(pubkey, "ok")
        if behavior == "ok":
            return {}
        if isinstance(behavior, Exception):
            raise behavior
        return {}
    return fake_lnd_rpc, calls


def test_auto_peer_skipped_when_config_disabled(monkeypatch, event_loop):
    """LSP_AUTO_PEER=False -> function returns immediately, no API calls."""
    _set(monkeypatch, LSP_AUTO_PEER=False)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    rpc, calls = _stub_lnd_rpc({})
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", rpc)

    event_loop.run_until_complete(
        liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
    )
    assert calls == []


def test_auto_peer_connects_lnd_wallet_to_both_lsps_on_mainnet(
    monkeypatch, event_loop,
):
    """Mainnet LND wallet -> ConnectPeer called for both Zeus and
    Megalithic peer pubkeys."""
    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    rpc, calls = _stub_lnd_rpc({})
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", rpc)

    event_loop.run_until_complete(
        liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
    )
    # Both providers have a mainnet endpoint; expect two ConnectPeer calls.
    connect_calls = [c for c in calls if c["method"] == "ConnectPeer"]
    assert len(connect_calls) == 2
    pubkeys = {c["params"]["addr"]["pubkey"] for c in connect_calls}
    # Zeus + Megalithic real pubkeys -> two distinct entries.
    assert len(pubkeys) == 2


def test_auto_peer_uses_perm_true_for_persistent_connection(
    monkeypatch, event_loop,
):
    """ConnectPeer must be called with perm=True so LND maintains the
    connection across disconnects."""
    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    rpc, calls = _stub_lnd_rpc({})
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", rpc)

    event_loop.run_until_complete(
        liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
    )
    for c in calls:
        if c["method"] == "ConnectPeer":
            assert c["params"]["perm"] is True


def test_auto_peer_skips_electrum_wallet(monkeypatch, event_loop):
    """Loop is LND-only; Electrum wallets get no ConnectPeer calls."""
    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    api.add_wallet("w-electrum", currency="btc")

    rpc, calls = _stub_lnd_rpc({})
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", rpc)

    event_loop.run_until_complete(
        liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
    )
    assert calls == []


def test_auto_peer_skips_unsupported_network(monkeypatch, event_loop):
    """Regtest LND wallet -> no LSP supports it, no ConnectPeer calls."""
    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "regtest"})

    rpc, calls = _stub_lnd_rpc({})
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", rpc)

    event_loop.run_until_complete(
        liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
    )
    assert calls == []


def test_auto_peer_classifies_lnd_starting_up_errors(monkeypatch, event_loop, caplog):
    """LND raises 'wallet not unlocked' / 'server is still in the
    process of starting' / connection-refused during its startup
    sequence. These are transient — function must not log them at
    WARNING (which would trip warning-level monitoring), but should
    record a 'starting_up' state-decision so the operator can see what
    happened."""
    import logging as _logging

    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    startup_error = RuntimeError("wallet not unlocked")

    async def stub(api, wid, method, params=None, service="Lightning"):
        if method == "ConnectPeer":
            raise startup_error
        return {}
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", stub)

    # Capture decisions logger to verify the 'starting_up' state lands.
    captured: List[_logging.LogRecord] = []

    class _Capture(_logging.Handler):
        def emit(self, record): captured.append(record)
    cap = _Capture(level=_logging.DEBUG)
    liquidityhelper.decisions_logger.addHandler(cap)
    try:
        with caplog.at_level(_logging.WARNING, logger="liquidityhelper"):
            event_loop.run_until_complete(
                liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
            )
    finally:
        liquidityhelper.decisions_logger.removeHandler(cap)

    # No WARNING records on the main logger (the noise we wanted to avoid).
    warning_records = [r for r in caplog.records
                       if r.levelno >= _logging.WARNING
                       and "ConnectPeer failed" in r.getMessage()]
    assert warning_records == [], (
        f"startup-race errors should NOT be logged at WARNING: {warning_records}"
    )
    # 'starting_up' state lands in decisions.log.
    msgs = [r.getMessage() for r in captured]
    assert any("LND still starting" in m for m in msgs), msgs


def test_auto_peer_unrelated_errors_still_warn(monkeypatch, event_loop, caplog):
    """A real ConnectPeer failure (e.g. authentication error) should
    still log at WARNING — only startup-race errors are downgraded."""
    import logging as _logging

    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    async def stub(api, wid, method, params=None, service="Lightning"):
        if method == "ConnectPeer":
            raise RuntimeError("permission denied: not authorized")
        return {}
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", stub)

    with caplog.at_level(_logging.WARNING, logger="liquidityhelper"):
        event_loop.run_until_complete(
            liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
        )

    warning_records = [r for r in caplog.records
                       if r.levelno >= _logging.WARNING]
    assert len(warning_records) >= 1
    # New message after the multi-URI refactor: "ALL URIs failed".
    assert any("URIs failed" in r.getMessage() for r in warning_records)


def test_lnd_not_ready_classifier():
    """Direct test for the pattern matcher. Captures the documented
    startup phrases we expect to see from LND mid-warmup."""
    cases = [
        ("wallet not unlocked", True),
        ("wallet is locked", True),
        ("server is still in the process of starting up", True),
        ("rpc service not active", True),
        ("connection refused (127.0.0.1:10009)", True),
        ("unavailable: name resolution failed", True),
        ("permission denied", False),
        ("invalid request", False),
        ("already connected to peer", False),  # not a startup case
        ("", False),
    ]
    for msg, expected in cases:
        assert liquidityhelper._looks_like_lnd_not_ready(msg) == expected, (
            f"_looks_like_lnd_not_ready({msg!r}) returned wrong value"
        )


def test_auto_peer_already_connected_does_not_raise(monkeypatch, event_loop):
    """LND raises 'already connected to peer' on repeat ConnectPeer.
    Function must handle this gracefully — no exception bubbling out."""
    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    # Both LSPs already connected.
    error = RuntimeError("already connected to peer: 0123abc")
    rpc, calls = _stub_lnd_rpc({
        # We don't know the exact lsp pubkeys without importing them, so
        # apply this error to any ConnectPeer call.
    })

    async def stub(api, wid, method, params=None, service="Lightning"):
        calls.append({
            "wallet_id": wid, "method": method, "params": params,
        })
        if method == "ConnectPeer":
            raise error
        return {}
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", stub)

    # Must not raise.
    event_loop.run_until_complete(
        liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
    )
    # Both providers still got the call attempt.
    assert len([c for c in calls if c["method"] == "ConnectPeer"]) == 2


def test_auto_peer_iterates_multiple_wallets(monkeypatch, event_loop):
    """3 LND wallets x 2 providers (mainnet) = 6 ConnectPeer calls."""
    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    for wid in ("w1", "w2", "w3"):
        api.add_wallet(wid, currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    rpc, calls = _stub_lnd_rpc({})
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", rpc)

    event_loop.run_until_complete(
        liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
    )
    connect_calls = [c for c in calls if c["method"] == "ConnectPeer"]
    assert len(connect_calls) == 6


def test_auto_peer_uses_correct_network_per_wallet(monkeypatch, event_loop):
    """Each wallet's network gates which LSPs are dialed. A testnet
    wallet only contacts LSPs that support testnet (Zeus does;
    Megalithic does not)."""
    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    api.add_wallet("w-testnet", currency="btclnd")

    network_per_wallet = {"w-testnet": "testnet"}

    async def get_lnd_info(wid):
        return {"network": network_per_wallet[wid]}
    api.get_lnd_info = get_lnd_info

    rpc, calls = _stub_lnd_rpc({})
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", rpc)

    event_loop.run_until_complete(
        liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
    )
    connect_calls = [c for c in calls if c["method"] == "ConnectPeer"]
    # Only Zeus supports testnet; Megalithic does not. So exactly one call.
    assert len(connect_calls) == 1


def test_auto_peer_does_not_fetch_get_wallets_twice(monkeypatch, event_loop):
    """Single API roundtrip for wallets, regardless of how many providers
    we iterate. Prevents accidental N+1."""
    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    call_counter = {"n": 0}
    orig_get_wallets = api.get_wallets

    async def counted_get_wallets(*a, **kw):
        call_counter["n"] += 1
        return await orig_get_wallets(*a, **kw)
    api.get_wallets = counted_get_wallets

    rpc, _ = _stub_lnd_rpc({})
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", rpc)

    event_loop.run_until_complete(
        liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
    )
    assert call_counter["n"] == 1


# ---------------------------------------------------------------------------
# Dynamic peer URI fetching — get_info-sourced URIs with hardcoded fallback
# ---------------------------------------------------------------------------

def test_get_active_peer_uri_sources_from_get_info(lsp_mocks, event_loop):
    """Mock LSP advertises a fresh peer URI in get_info.uris[0].
    get_active_peer_uri returns that, not the hardcoded fallback."""
    zeus_mock, _ = lsp_mocks
    zeus_mock.set_get_info_response({
        "supported_versions": [1],
        "uris": ["cd" * 33 + "@dynamic.peer.example:9735"],
        "options": {
            "min_channel_balance_sat": "100000",
            "max_channel_balance_sat": "10000000",
            "max_channel_expiry_blocks": 13000,
        },
    })

    class _MockZeus(lsp_providers._RestLSPProvider):
        name = "zeus"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url=zeus_mock.base_url + "/api/v1",
                lsp_peer_uri="ff" * 33 + "@hardcoded.fallback:9735",
            ),
        }

    provider = _MockZeus()
    # Cache empty -> should fetch from get_info
    assert provider._cached_uris == {}
    uris = event_loop.run_until_complete(
        provider.get_all_peer_uris(network="mainnet")
    )
    # Both the hardcoded fallback AND the dynamic URI should be included.
    assert "ff" * 33 + "@hardcoded.fallback:9735" in uris
    assert "cd" * 33 + "@dynamic.peer.example:9735" in uris
    # get_active_peer_uri returns one (the first); for this test it's the hardcoded.
    active = event_loop.run_until_complete(
        provider.get_active_peer_uri(network="mainnet")
    )
    assert active in uris
    # Subsequent calls hit the cache, no extra get_info request.
    initial_request_count = len(zeus_mock.requests)
    event_loop.run_until_complete(provider.get_all_peer_uris(network="mainnet"))
    assert len(zeus_mock.requests) == initial_request_count


def test_get_active_peer_uri_falls_back_when_get_info_fails(lsp_mocks, event_loop):
    """get_info returns 500 -> we fall back to the hardcoded URI."""
    zeus_mock, _ = lsp_mocks
    zeus_mock.fail_get_info(status=500)

    fallback_uri = "ff" * 33 + "@hardcoded.fallback:9735"

    class _MockZeus(lsp_providers._RestLSPProvider):
        name = "zeus"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url=zeus_mock.base_url + "/api/v1",
                lsp_peer_uri=fallback_uri,
            ),
        }

    provider = _MockZeus()
    uri = event_loop.run_until_complete(
        provider.get_active_peer_uri(network="mainnet")
    )
    assert uri == fallback_uri


def test_get_active_peer_uri_falls_back_when_no_uris_in_response(lsp_mocks, event_loop):
    """get_info succeeds but returns no `uris` field -> fall back."""
    zeus_mock, _ = lsp_mocks
    zeus_mock.set_get_info_response({
        "supported_versions": [1],
        # NO "uris" field
        "options": {
            "min_channel_balance_sat": "100000",
            "max_channel_balance_sat": "10000000",
            "max_channel_expiry_blocks": 13000,
        },
    })

    fallback_uri = "ee" * 33 + "@hardcoded.fallback:9735"

    class _MockZeus(lsp_providers._RestLSPProvider):
        name = "zeus"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url=zeus_mock.base_url + "/api/v1",
                lsp_peer_uri=fallback_uri,
            ),
        }

    provider = _MockZeus()
    uri = event_loop.run_until_complete(
        provider.get_active_peer_uri(network="mainnet")
    )
    assert uri == fallback_uri


def test_get_info_parses_uris_field(lsp_mocks, event_loop):
    """Sanity check: the parser exposes `uris` on LspInfo."""
    zeus_mock, _ = lsp_mocks
    zeus_mock.set_get_info_response({
        "supported_versions": [1],
        "uris": [
            "aa" * 33 + "@primary.peer:9735",
            "bb" * 33 + "@secondary.peer:9735",
        ],
        "options": {
            "min_channel_balance_sat": "100000",
            "max_channel_balance_sat": "10000000",
            "max_channel_expiry_blocks": 13000,
        },
    })

    class _MockZeus(lsp_providers._RestLSPProvider):
        name = "zeus"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url=zeus_mock.base_url + "/api/v1",
                lsp_peer_uri="ff" * 33 + "@fallback:9735",
            ),
        }

    info = event_loop.run_until_complete(
        _MockZeus().get_info(network="mainnet")
    )
    assert info.uris == [
        "aa" * 33 + "@primary.peer:9735",
        "bb" * 33 + "@secondary.peer:9735",
    ]


# ---------------------------------------------------------------------------
# Verified endpoint values from docs (regression-pins for the URLs)
# ---------------------------------------------------------------------------

def test_zeus_provider_endpoints_match_docs():
    """https://docs.zeusln.app/lsp/services/lsps1 lists three networks
    with these exact base URLs and peer pubkeys. If they ever rotate,
    update lsp_providers.ZeusProvider and this test together."""
    p = lsp_providers.ZeusProvider()
    assert p.network_endpoints["mainnet"].base_url == "https://lsps1.lnolymp.us/api/v1"
    assert p.network_endpoints["mainnet"].lsp_peer_uri.startswith(
        "031b301307574bbe9b9ac7b79cbe1700e31e544513eae0b5d7497483083f99e581"
    )
    assert p.network_endpoints["testnet"].base_url == "https://testnet-lsps1.lnolymp.us/api/v1"
    assert p.network_endpoints["signet"].base_url == "https://mutinynet-lsps1.lnolymp.us/api/v1"


def test_megalithic_provider_endpoints_match_docs():
    """https://docs.megalithic.me/lightning-services/lsp1-get-inbound-liquidity-for-mobile-clients/
    lists mainnet at /api/lsps1/v1/ (note: distinct prefix from Zeus)
    and Mutinynet at lsp1.mutiny.megalith-node.com."""
    p = lsp_providers.MegalithicProvider()
    assert p.network_endpoints["mainnet"].base_url == (
        "https://megalithic.me/api/lsps1/v1"
    )
    assert p.network_endpoints["signet"].base_url == (
        "https://lsp1.mutiny.megalith-node.com/api/lsps1/v1"
    )


def test_lsp_channel_expiry_within_provider_limits():
    """Both Zeus (13000) and Megalithic (13140) cap channel expiry at
    ~3 months. The default config must fit both."""
    import config as _config
    # Reload in case earlier test mutated module-level state.
    import importlib
    importlib.reload(_config)
    assert _config.LSP_CHANNEL_EXPIRY_BLOCKS <= 13_000


def test_megalithic_signet_uri_is_placeholder():
    """Megalithic doesn't publish a static Mutinynet peer pubkey. Our
    code marks the fallback with the sentinel 'UNKNOWN@...' string so
    ensure_lnd_wallets_peered_with_lsps can detect 'no usable URI' and
    avoid dialing garbage."""
    p = lsp_providers.MegalithicProvider()
    assert p.network_endpoints["signet"].lsp_peer_uri.startswith("UNKNOWN@")


# ---------------------------------------------------------------------------
# Multi-URI dialing: connect to BOTH the hardcoded fallback AND every URI
# from get_info, dedup, skip the UNKNOWN sentinel.
# ---------------------------------------------------------------------------

def test_get_all_peer_uris_includes_hardcoded_and_dynamic(lsp_mocks, event_loop):
    """get_info advertises a fresh URI; the hardcoded fallback is
    different. get_all_peer_uris should return BOTH."""
    zeus_mock, _ = lsp_mocks
    zeus_mock.set_get_info_response({
        "supported_versions": [1],
        "uris": ["cd" * 33 + "@dynamic.peer:9735"],
        "options": {
            "min_channel_balance_sat": "100000",
            "max_channel_balance_sat": "10000000",
            "max_channel_expiry_blocks": 13000,
        },
    })

    class _MockZeus(lsp_providers._RestLSPProvider):
        name = "zeus"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url=zeus_mock.base_url + "/api/v1",
                lsp_peer_uri="ff" * 33 + "@hardcoded:9735",
            ),
        }

    uris = event_loop.run_until_complete(
        _MockZeus().get_all_peer_uris(network="mainnet")
    )
    assert uris == [
        "ff" * 33 + "@hardcoded:9735",        # hardcoded first
        "cd" * 33 + "@dynamic.peer:9735",     # then dynamic
    ]


def test_get_all_peer_uris_dedupes(lsp_mocks, event_loop):
    """If get_info echoes the same URI as the hardcoded fallback, the
    result list should contain it once."""
    zeus_mock, _ = lsp_mocks
    shared_uri = "ee" * 33 + "@shared.peer:9735"
    zeus_mock.set_get_info_response({
        "supported_versions": [1],
        "uris": [shared_uri],
        "options": {
            "min_channel_balance_sat": "100000",
            "max_channel_balance_sat": "10000000",
            "max_channel_expiry_blocks": 13000,
        },
    })

    class _MockZeus(lsp_providers._RestLSPProvider):
        name = "zeus"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url=zeus_mock.base_url + "/api/v1",
                lsp_peer_uri=shared_uri,
            ),
        }

    uris = event_loop.run_until_complete(
        _MockZeus().get_all_peer_uris(network="mainnet")
    )
    assert uris == [shared_uri]


def test_get_all_peer_uris_filters_unknown_sentinel(lsp_mocks, event_loop):
    """The 'UNKNOWN@...' fallback (Megalithic Mutinynet) is excluded
    even when get_info doesn't return anything either."""
    zeus_mock, _ = lsp_mocks
    zeus_mock.set_get_info_response({
        "supported_versions": [1],
        "uris": [],   # nothing dynamic either
        "options": {
            "min_channel_balance_sat": "100000",
            "max_channel_balance_sat": "10000000",
            "max_channel_expiry_blocks": 13000,
        },
    })

    class _MockSignet(lsp_providers._RestLSPProvider):
        name = "test-lsp"
        network_endpoints = {
            lsp_providers.NETWORK_SIGNET: lsp_providers._Endpoint(
                base_url=zeus_mock.base_url + "/api/v1",
                lsp_peer_uri="UNKNOWN@example.test:9735",
            ),
        }

    uris = event_loop.run_until_complete(
        _MockSignet().get_all_peer_uris(network="signet")
    )
    assert uris == []


def test_get_all_peer_uris_multiple_from_get_info(lsp_mocks, event_loop):
    """LSPS1 spec permits get_info to return multiple URIs. All of them
    should be returned (after the hardcoded fallback)."""
    zeus_mock, _ = lsp_mocks
    zeus_mock.set_get_info_response({
        "supported_versions": [1],
        "uris": [
            "aa" * 33 + "@a.peer:9735",
            "bb" * 33 + "@b.peer:9735",
            "cc" * 33 + "@c.peer:9735",
        ],
        "options": {
            "min_channel_balance_sat": "100000",
            "max_channel_balance_sat": "10000000",
            "max_channel_expiry_blocks": 13000,
        },
    })

    class _MockZeus(lsp_providers._RestLSPProvider):
        name = "zeus"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url=zeus_mock.base_url + "/api/v1",
                lsp_peer_uri="ff" * 33 + "@hardcoded:9735",
            ),
        }

    uris = event_loop.run_until_complete(
        _MockZeus().get_all_peer_uris(network="mainnet")
    )
    assert len(uris) == 4
    assert uris[0] == "ff" * 33 + "@hardcoded:9735"


def test_auto_peer_dials_every_uri(monkeypatch, event_loop):
    """ensure_lnd_wallets_peered_with_lsps must dial EVERY URI from
    get_all_peer_uris, not just the first. Two hardcoded URIs for one
    provider -> two ConnectPeer calls for that provider."""
    _set(monkeypatch, LSP_AUTO_PEER=True)

    # Build a provider with TWO cached URIs for mainnet.
    class _MultiURIProvider(lsp_providers._RestLSPProvider):
        name = "multi"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url="http://unused.test",
                lsp_peer_uri="aa" * 33 + "@peer-a:9735",
            ),
        }

    p = _MultiURIProvider()
    p._cached_uris[lsp_providers.NETWORK_MAINNET] = [
        "aa" * 33 + "@peer-a:9735",
        "bb" * 33 + "@peer-b:9735",
    ]
    lsp_providers.set_lsp_providers([p])

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    rpc, calls = _stub_lnd_rpc({})
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", rpc)

    event_loop.run_until_complete(
        liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
    )

    connect_calls = [c for c in calls if c["method"] == "ConnectPeer"]
    assert len(connect_calls) == 2
    pubkeys = {c["params"]["addr"]["pubkey"] for c in connect_calls}
    assert pubkeys == {"aa" * 33, "bb" * 33}


def test_auto_peer_one_uri_succeeds_records_connected(monkeypatch, event_loop):
    """If one URI succeeds and another fails (e.g. stale pubkey), the
    overall provider state should record `connected`, not `failed`."""
    _set(monkeypatch, LSP_AUTO_PEER=True)

    class _MultiURIProvider(lsp_providers._RestLSPProvider):
        name = "mixed"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url="http://unused.test",
                lsp_peer_uri="aa" * 33 + "@peer-a:9735",
            ),
        }

    p = _MultiURIProvider()
    p._cached_uris[lsp_providers.NETWORK_MAINNET] = [
        "aa" * 33 + "@peer-a:9735",
        "bb" * 33 + "@peer-b:9735",
    ]
    lsp_providers.set_lsp_providers([p])

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    async def stub(api, wid, method, params=None, service="Lightning"):
        if method == "ConnectPeer":
            pubkey = (params or {}).get("addr", {}).get("pubkey")
            if pubkey == "bb" * 33:
                raise RuntimeError("peer not found")
            # peer-a succeeds
            return {}
        return {}
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", stub)

    import logging as _logging
    captured = []

    class _Capture(_logging.Handler):
        def emit(self, record): captured.append(record)
    cap = _Capture(level=_logging.DEBUG)
    liquidityhelper.decisions_logger.addHandler(cap)
    try:
        event_loop.run_until_complete(
            liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
        )
    finally:
        liquidityhelper.decisions_logger.removeHandler(cap)

    msgs = [r.getMessage() for r in captured]
    # Overall provider state should be `connected`, not `all_failed`.
    assert any("LSP peer connected" in m for m in msgs), msgs
    assert not any("connect FAILED on all" in m for m in msgs), msgs


def test_auto_peer_all_uris_fail_records_all_failed(monkeypatch, event_loop, caplog):
    """If every URI for a provider fails for a non-transient reason,
    the decision-log should show `all_failed` and the operational log
    should WARN."""
    import logging as _logging

    _set(monkeypatch, LSP_AUTO_PEER=True)

    class _MultiURIProvider(lsp_providers._RestLSPProvider):
        name = "mixed"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url="http://unused.test",
                lsp_peer_uri="aa" * 33 + "@peer-a:9735",
            ),
        }

    p = _MultiURIProvider()
    p._cached_uris[lsp_providers.NETWORK_MAINNET] = [
        "aa" * 33 + "@peer-a:9735",
        "bb" * 33 + "@peer-b:9735",
    ]
    lsp_providers.set_lsp_providers([p])

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    async def stub(api, wid, method, params=None, service="Lightning"):
        if method == "ConnectPeer":
            raise RuntimeError("permission denied")
        return {}
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", stub)

    with caplog.at_level(_logging.WARNING, logger="liquidityhelper"):
        event_loop.run_until_complete(
            liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
        )
    warning_records = [r for r in caplog.records if r.levelno >= _logging.WARNING]
    assert any("ALL URIs failed" in r.getMessage() for r in warning_records)


def test_auto_peer_already_connected_counts_as_success(monkeypatch, event_loop):
    """'already connected to peer' on every URI should still record the
    provider as connected — not failed."""
    _set(monkeypatch, LSP_AUTO_PEER=True)

    class _MultiURIProvider(lsp_providers._RestLSPProvider):
        name = "warm"
        network_endpoints = {
            lsp_providers.NETWORK_MAINNET: lsp_providers._Endpoint(
                base_url="http://unused.test",
                lsp_peer_uri="aa" * 33 + "@peer-a:9735",
            ),
        }

    p = _MultiURIProvider()
    p._cached_uris[lsp_providers.NETWORK_MAINNET] = [
        "aa" * 33 + "@peer-a:9735",
        "bb" * 33 + "@peer-b:9735",
    ]
    lsp_providers.set_lsp_providers([p])

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "mainnet"})

    async def stub(api, wid, method, params=None, service="Lightning"):
        if method == "ConnectPeer":
            raise RuntimeError("already connected to peer")
        return {}
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", stub)

    import logging as _logging
    captured = []

    class _Capture(_logging.Handler):
        def emit(self, record): captured.append(record)
    cap = _Capture(level=_logging.DEBUG)
    liquidityhelper.decisions_logger.addHandler(cap)
    try:
        event_loop.run_until_complete(
            liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
        )
    finally:
        liquidityhelper.decisions_logger.removeHandler(cap)

    msgs = [r.getMessage() for r in captured]
    assert any("LSP peer connected" in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# Auto-peering network-skip decision log (introduced by the LSP network
# audit fix). Pin that we emit a structured log when a provider is
# skipped due to unsupported-network — the old code silently `continue`d
# and operators had no visibility into why an LSP wasn't being dialed.
# ---------------------------------------------------------------------------

def test_auto_peer_logs_decision_when_lsp_does_not_support_network(
    monkeypatch, event_loop, caplog,
):
    """Wallet on testnet → Megalithic (mainnet+signet only) is filtered;
    the skip emits a `lsp_peer_skip_unsupported_network` decision log."""
    import logging as _logging
    _set(monkeypatch, LSP_AUTO_PEER=True)
    api = FakeBitcartAPI()
    # Unique wallet_id avoids log_decision dedup collision.
    api.add_wallet("w-meg-tn-skip", currency="btclnd")
    api.get_lnd_info = lambda wid: _async_return({"network": "testnet"})

    rpc, _calls = _stub_lnd_rpc({})
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", rpc)

    decisions_logger = _logging.getLogger("liquidityhelper.decisions")
    decisions_logger.addHandler(caplog.handler)
    caplog.set_level(_logging.INFO)
    try:
        event_loop.run_until_complete(
            liquidityhelper.ensure_lnd_wallets_peered_with_lsps(api)
        )
    finally:
        decisions_logger.removeHandler(caplog.handler)

    skip_msgs = [
        r.getMessage() for r in caplog.records
        if "Skipping LSP" in r.getMessage()
        and "does not support network" in r.getMessage()
    ]
    assert any("megalithic" in m for m in skip_msgs), (
        f"expected a decision log for megalithic + testnet skip; got "
        f"{skip_msgs}"
    )
    assert any("testnet" in m for m in skip_msgs)


# ---------------------------------------------------------------------------
# pick_best_lsp_for_inbound diagnostic on "no quote" return.
# When ALL providers skip for network reasons, the caller previously
# saw a generic "no quote" log. Now it should see a structured
# explanation including the supported-network map.
# ---------------------------------------------------------------------------

def test_pick_best_lsp_no_quote_when_all_filtered_for_network(
    monkeypatch, event_loop, caplog,
):
    """No LSP supports the wallet's network → the no-quote log
    explicitly says NO LSPs serve this network AND prints the
    supported-network map of every configured provider."""
    import logging as _logging

    # Custom providers that explicitly don't support the wallet's net.
    class _OnlyMainnetA(lsp_providers.LSPProvider):
        name = "only-mainnet-a"
        def supported_networks(self):
            return ["mainnet"]
        def lsp_peer_uri(self, *, network):
            raise NotImplementedError
        async def get_info(self, *, network):
            raise NotImplementedError
        async def create_order(self, **kw):
            raise NotImplementedError
        async def get_order(self, **kw):
            raise NotImplementedError

    class _OnlyMainnetB(_OnlyMainnetA):
        name = "only-mainnet-b"

    lsp_providers.set_lsp_providers([_OnlyMainnetA(), _OnlyMainnetB()])

    api = FakeBitcartAPI()
    api.add_wallet("w-tn-noquote", currency="btclnd")
    api.get_wallet_ln_node_id = lambda wid: _async_return("aa" * 33 + "@x:9735")

    decisions_logger = _logging.getLogger("liquidityhelper.decisions")
    decisions_logger.addHandler(caplog.handler)
    caplog.set_level(_logging.INFO)
    try:
        result = event_loop.run_until_complete(
            liquidityhelper.pick_best_lsp_for_inbound(
                wallet=api.wallets["w-tn-noquote"], api=api, network="testnet",
            )
        )
    finally:
        decisions_logger.removeHandler(caplog.handler)
        lsp_providers.reset_lsp_providers()

    assert result is None
    no_quote_msgs = [
        r.getMessage() for r in caplog.records
        if "NO LSPs support" in r.getMessage()
    ]
    assert no_quote_msgs, (
        f"expected a 'NO LSPs support network=X' log when all "
        f"providers filtered for network; got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert "testnet" in no_quote_msgs[0]
    # The supported-network map must list both providers and their nets.
    assert "only-mainnet-a" in no_quote_msgs[0]
    assert "only-mainnet-b" in no_quote_msgs[0]


def test_pick_best_lsp_no_quote_when_all_errored(
    monkeypatch, event_loop, caplog,
):
    """When providers ARE compatible but all raise on create_order,
    the no-quote log lists per-provider reasons (not the generic
    'NO LSPs support' message)."""
    import logging as _logging

    class _ErroringProvider(lsp_providers.LSPProvider):
        name = "errprov"
        def supported_networks(self):
            return ["mainnet"]
        def lsp_peer_uri(self, *, network):
            raise NotImplementedError
        async def get_info(self, *, network):
            raise NotImplementedError
        async def create_order(self, **kw):
            raise RuntimeError("HTTP 500 from LSP backend")
        async def get_order(self, **kw):
            raise NotImplementedError

    lsp_providers.set_lsp_providers([_ErroringProvider()])

    api = FakeBitcartAPI()
    api.add_wallet("w-err-noquote", currency="btclnd")
    api.get_wallet_ln_node_id = lambda wid: _async_return("aa" * 33 + "@x:9735")

    decisions_logger = _logging.getLogger("liquidityhelper.decisions")
    decisions_logger.addHandler(caplog.handler)
    caplog.set_level(_logging.INFO)
    try:
        result = event_loop.run_until_complete(
            liquidityhelper.pick_best_lsp_for_inbound(
                wallet=api.wallets["w-err-noquote"], api=api, network="mainnet",
            )
        )
    finally:
        decisions_logger.removeHandler(caplog.handler)
        lsp_providers.reset_lsp_providers()

    assert result is None
    # Expect the breakdown log, NOT the all-network one.
    breakdown_msgs = [
        r.getMessage() for r in caplog.records
        if "Per-provider reasons" in r.getMessage()
    ]
    assert breakdown_msgs, (
        f"expected per-provider reasons in no-quote log; got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert "errprov" in breakdown_msgs[0]
    assert "create_order_error" in breakdown_msgs[0]


# ---------------------------------------------------------------------------
# audit_lsp_network_compatibility — the daily pre-flight summary.
# ---------------------------------------------------------------------------

def test_lsp_compatibility_audit_emits_per_wallet_per_provider_log(
    monkeypatch, event_loop, caplog,
):
    """For two wallets (mainnet + testnet) × two real providers
    (Zeus + Megalithic) the audit emits 4 decision logs labelled
    `lsp_compatibility` PLUS one top-level matrix line."""
    import logging as _logging

    api = FakeBitcartAPI()
    api.add_wallet("w-audit-main", currency="btclnd")
    api.add_wallet("w-audit-tn", currency="btclnd")
    networks = {"w-audit-main": "mainnet", "w-audit-tn": "testnet"}
    async def get_lnd_info(wid):
        return {"network": networks.get(wid)}
    api.get_lnd_info = get_lnd_info

    lsp_providers.reset_lsp_providers()    # use real providers

    decisions_logger = _logging.getLogger("liquidityhelper.decisions")
    decisions_logger.addHandler(caplog.handler)
    caplog.set_level(_logging.INFO)
    try:
        event_loop.run_until_complete(
            liquidityhelper.audit_lsp_network_compatibility(api)
        )
    finally:
        decisions_logger.removeHandler(caplog.handler)

    compat_msgs = [
        r.getMessage() for r in caplog.records
        if "LSP compatibility:" in r.getMessage()
    ]
    # 2 wallets × 2 providers = 4 per-cell decisions.
    assert len(compat_msgs) >= 4, (
        f"expected 4 per-(wallet, provider) compatibility logs; got "
        f"{len(compat_msgs)}: {compat_msgs}"
    )
    # Megalithic does NOT serve testnet — that exact cell must be
    # reported as unsupported.
    meg_tn = [
        m for m in compat_msgs
        if "w-audit-tn" in m and "megalithic" in m and "unsupported" in m
    ]
    assert meg_tn, (
        f"expected Megalithic × w-audit-tn to be reported unsupported; "
        f"got {compat_msgs}"
    )
    # Zeus DOES serve testnet.
    zeus_tn = [
        m for m in compat_msgs
        if "w-audit-tn" in m and "zeus" in m and "→ supported" in m
    ]
    assert zeus_tn, (
        f"expected Zeus × w-audit-tn to be reported supported; got "
        f"{compat_msgs}"
    )
    # Top-line matrix log
    matrix_msgs = [
        r.getMessage() for r in caplog.records
        if "LSP compatibility audit:" in r.getMessage()
    ]
    assert matrix_msgs, "expected a top-line matrix summary"


def test_lsp_compatibility_audit_no_lnd_wallets_skips_quietly(
    monkeypatch, event_loop, caplog,
):
    """No LND wallets at all → no per-cell logs and a zero-count
    matrix line. Pin against a regression where we'd spam decisions
    for non-LND wallets."""
    import logging as _logging

    api = FakeBitcartAPI()
    api.add_wallet("w-elec", currency="btc")    # Electrum, NOT btclnd
    lsp_providers.reset_lsp_providers()

    decisions_logger = _logging.getLogger("liquidityhelper.decisions")
    decisions_logger.addHandler(caplog.handler)
    caplog.set_level(_logging.INFO)
    try:
        event_loop.run_until_complete(
            liquidityhelper.audit_lsp_network_compatibility(api)
        )
    finally:
        decisions_logger.removeHandler(caplog.handler)

    compat_msgs = [
        r for r in caplog.records
        if "LSP compatibility:" in r.getMessage()
    ]
    assert not compat_msgs, (
        f"non-LND wallets must not generate per-cell logs; got "
        f"{[r.getMessage() for r in compat_msgs]}"
    )


# ---------------------------------------------------------------------------
# small async helper
# ---------------------------------------------------------------------------

def _async_return(v):
    async def _coro():
        return v
    return _coro()
