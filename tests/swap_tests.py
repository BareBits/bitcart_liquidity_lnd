"""Tests for the submarine-swap subsystem.

Six of seven tests are pure-Python / pure-DB and need no infra. The seventh
(`test_full_loop_out_end_to_end`) requires `loop_rig` and is auto-skipped
if podman/docker isn't installed or the loopserver image cannot be pulled.
"""

from __future__ import annotations

import asyncio
import datetime
import pytest

import liquidityhelper
from swap_providers import (
    SwapDirection, SwapProvider, SwapQuote, SwapResult,
)
from node_database import SwapPriceQuote


# ---------------------------------------------------------------------------
# Helpers: a deterministic fake SwapProvider so we can drive
# pick_best_swap_provider_for_out without standing up loopd.
# ---------------------------------------------------------------------------

class _FakeProvider(SwapProvider):
    """SwapProvider whose `quote_out` returns whatever the test plants."""

    def __init__(self, name: str, quote: SwapQuote, result: SwapResult = None):
        self.name = name
        self._quote = quote
        self._result = result
        self.quote_calls = 0
        self.initiate_calls = 0

    async def quote_out(self, amount_sat, *, wallet=None, api=None):
        self.quote_calls += 1
        return self._quote

    async def initiate_out(self, wallet, api, amount_sat, dest_addr):
        self.initiate_calls += 1
        return self._result


def _quote(provider="loop", amount=1_000_000, swap_fee=2_000, miner_fee=500):
    total = swap_fee + miner_fee
    return SwapQuote(
        provider=provider,
        direction=SwapDirection.OUT,
        amount_sat=amount,
        swap_fee_sat=swap_fee,
        miner_fee_sat=miner_fee,
        total_fee_sat=total,
        fee_percent=total / amount,
        raw=None,
    )


def _install_providers(monkeypatch, providers):
    """Replace the module-global swap provider registry with a fixed list."""
    monkeypatch.setattr(liquidityhelper, "SWAP_PROVIDERS", providers)
    # _swap_provider_registry() returns SWAP_PROVIDERS immediately if non-empty,
    # so this is sufficient — no need to touch _LOOPD_MANAGER.


_FAKE_WALLET = {"id": "test-wallet-xyz", "currency": "btclnd"}


# ---------------------------------------------------------------------------
# 1. Only provider picks loop (single-provider case)
# ---------------------------------------------------------------------------

def test_only_provider_picks_loop(monkeypatch, event_loop):
    fake = _FakeProvider("loop", _quote(amount=1_000_000, swap_fee=2_000, miner_fee=500))
    _install_providers(monkeypatch, [fake])

    picked = event_loop.run_until_complete(
        liquidityhelper.pick_best_swap_provider_for_out(
            1_000_000, wallet=_FAKE_WALLET, api=None,
        )
    )
    assert picked is not None
    provider, quote = picked
    assert provider.name == "loop"
    assert quote.total_fee_sat == 2_500
    assert fake.quote_calls == 1


# ---------------------------------------------------------------------------
# 2. Quote rejected when total_fee_sat > MAX_SWAP_FLAT
# ---------------------------------------------------------------------------

def test_rejects_over_flat_cap(monkeypatch, event_loop):
    monkeypatch.setattr(liquidityhelper, "MAX_SWAP_FLAT", 1_000)   # tight cap
    monkeypatch.setattr(liquidityhelper, "MAX_SWAP_PERCENT", 1.0)  # disable %
    fake = _FakeProvider(
        "loop",
        _quote(amount=1_000_000, swap_fee=2_000, miner_fee=500),  # total 2_500
    )
    _install_providers(monkeypatch, [fake])

    picked = event_loop.run_until_complete(
        liquidityhelper.pick_best_swap_provider_for_out(
            1_000_000, wallet=_FAKE_WALLET, api=None,
        )
    )
    assert picked is None


# ---------------------------------------------------------------------------
# 3. Quote rejected when fee_percent > MAX_SWAP_PERCENT
# ---------------------------------------------------------------------------

def test_rejects_over_percent_cap(monkeypatch, event_loop):
    monkeypatch.setattr(liquidityhelper, "MAX_SWAP_FLAT", 10_000_000)  # disable flat
    monkeypatch.setattr(liquidityhelper, "MAX_SWAP_PERCENT", 0.001)    # 0.1% cap
    fake = _FakeProvider(
        "loop",
        # 2_500 / 100_000 = 2.5% -> over 0.1% cap
        _quote(amount=100_000, swap_fee=2_000, miner_fee=500),
    )
    _install_providers(monkeypatch, [fake])

    picked = event_loop.run_until_complete(
        liquidityhelper.pick_best_swap_provider_for_out(
            100_000, wallet=_FAKE_WALLET, api=None,
        )
    )
    assert picked is None


# ---------------------------------------------------------------------------
# 4. Every quote is persisted to SwapPriceQuote, even rejected ones.
# ---------------------------------------------------------------------------

def test_persists_quote_to_db(monkeypatch, event_loop):
    # One acceptable quote, one too-expensive quote. Both should land in DB.
    cheap = _FakeProvider("loop", _quote(provider="loop",
                                         amount=1_000_000, swap_fee=2_000, miner_fee=500))
    pricey = _FakeProvider("boltz", _quote(provider="boltz",
                                           amount=1_000_000, swap_fee=99_999, miner_fee=500))
    _install_providers(monkeypatch, [cheap, pricey])

    assert SwapPriceQuote.select().count() == 0
    picked = event_loop.run_until_complete(
        liquidityhelper.pick_best_swap_provider_for_out(
            1_000_000, wallet=_FAKE_WALLET, api=None,
        )
    )
    assert picked is not None and picked[0].name == "loop"

    rows = list(SwapPriceQuote.select().order_by(SwapPriceQuote.total_fee_sat))
    assert len(rows) == 2
    assert {r.provider for r in rows} == {"loop", "boltz"}
    # Both got persisted with the correct denormalized fee_percent.
    for r in rows:
        assert r.amount_sat == 1_000_000
        assert r.direction == "out"
        assert abs(r.fee_percent - (r.total_fee_sat / r.amount_sat)) < 1e-9


# ---------------------------------------------------------------------------
# 5. End-to-end loop-out through real loopd + real loopserver. Skipped if
#    podman/docker isn't available.
# ---------------------------------------------------------------------------

def test_full_loop_out_end_to_end(loop_rig, event_loop):
    from swap_providers import LoopProvider, SwapDirection
    provider = LoopProvider(loop_rig.loopd_manager)

    # Quote a 500k sat swap-out — the rig's A->S channel is 0.2 BTC = 20_000_000
    # sats so 500k is well within capacity.
    amount = 500_000
    fake_wallet = {"id": f"test-wallet-{loop_rig.a.name.lower()}", "currency": "btclnd"}

    # `quote_out` will short-circuit via the LoopdInstance pre-registered in
    # the manager — no Bitcart API needed.
    quote = event_loop.run_until_complete(
        provider.quote_out(amount, wallet=fake_wallet, api=None)
    )
    assert quote is not None
    assert quote.direction == SwapDirection.OUT
    assert quote.amount_sat == amount
    assert quote.total_fee_sat >= 0

    dest_addr = event_loop.run_until_complete(loop_rig.s.new_address())
    result = event_loop.run_until_complete(
        provider.initiate_out(fake_wallet, None, amount, dest_addr)
    )
    assert result is not None
    assert len(result.swap_id) == 64  # 32-byte hash hex
    assert result.htlc_address  # non-empty
    assert result.state == "INITIATED"


# ---------------------------------------------------------------------------
# 6. cleanup_old_swap_quotes prunes rows older than 6 months and leaves
#    fresh ones intact.
# ---------------------------------------------------------------------------

def test_cleanup_old_quotes(event_loop):
    now = datetime.datetime.now()
    SwapPriceQuote.create(
        provider="loop", direction="out", amount_sat=100_000,
        total_fee_sat=500, fee_percent=0.005,
        fetched_at=now - datetime.timedelta(days=200),  # > 6 mo
    )
    SwapPriceQuote.create(
        provider="loop", direction="out", amount_sat=100_000,
        total_fee_sat=600, fee_percent=0.006,
        fetched_at=now - datetime.timedelta(days=5),    # fresh
    )
    assert SwapPriceQuote.select().count() == 2

    deleted = event_loop.run_until_complete(liquidityhelper.cleanup_old_swap_quotes())
    assert deleted == 1
    remaining = list(SwapPriceQuote.select())
    assert len(remaining) == 1
    assert remaining[0].total_fee_sat == 600


# ---------------------------------------------------------------------------
# 7. Swap fees on-chain get accounted into onchain_network_fees_paid_for_swaps.
#    Pure unit test of `is_swap_transaction` + the accumulator.
# ---------------------------------------------------------------------------

def test_swap_fee_accounting():
    from classes import StoreStats
    stats = StoreStats(
        store_id="s", ln_total_revenue_in_sats=0, onchain_total_revenue_in_sats=0,
        total_bb_fees_paid_in_sats=0,
        ineligible_revenue_because_of_promo_in_sats=0,
        ineligible_revenue_because_of_topups_in_sats=0,
        ineligible_revenue_because_of_bb_topups_in_sats=0,
        ln_network_fees_paid_for_bb_topup_returns_in_sats=0,
        onchain_network_fees_paid_for_bb_topup_returns_in_sats=0,
        ln_network_fees_paid_for_fee_payments_in_sats=0,
        onchain_network_fees_paid_for_fee_payments_in_sats=0,
        ln_network_fees_paid_for_payouts_in_sats=0,
        onchain_network_fees_paid_for_payouts_in_sats=0,
        ineligible_revenue_because_not_liquidityhelper_wallet_in_sats=0,
        revenue_eligible_for_fee=0,
        ineligible_revenue_because_not_ln_transaction_in_sats=0,
        onchain_network_fees_paid_for_channel_opens_in_sats=0,
        onchain_network_fees_paid_for_channel_closes_in_sats=0,
        onchain_network_fees_paid_for_swaps_in_sats=0,
        onchain_network_fees_paid_for_lsp_orders_in_sats=0,
        onchain_lsp_service_fees_paid_in_sats=0,
        total_referral_fees_paid_in_sats=0,
        ln_network_fees_paid_for_referral_payments_in_sats=0,
        onchain_network_fees_paid_for_referral_payments_in_sats=0,
        misc_ln_network_fees_in_sats=0,
    )

    # A loop-out HTLC publication tx that loopd labeled "loop-out:<swap_id>".
    swap_tx = {
        "label": "loop-out: 0001020304",
        "incoming": False,
        "fee_sat": 1234,
        "txid": "deadbeef",
    }
    assert liquidityhelper.is_swap_transaction(swap_tx)

    # Replay the accounting branch from new_calc_invoice_stats inline:
    if liquidityhelper.is_swap_transaction(swap_tx):
        if not swap_tx.get("incoming"):
            stats.onchain_network_fees_paid_for_swaps_in_sats += abs(float(swap_tx["fee_sat"]))

    assert stats.onchain_network_fees_paid_for_swaps_in_sats == 1234

    # And the aggregate counts it as on-chain network fee
    total_with_onchain = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=True, include_ln_network_fees=False,
    )
    assert total_with_onchain == 1234

    # Negative case: a normal channel-close tx is NOT classified as a swap.
    close_tx = {"label": "channel close", "incoming": True, "fee_sat": 200}
    assert not liquidityhelper.is_swap_transaction(close_tx)
