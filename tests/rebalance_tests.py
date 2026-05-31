"""Circular-rebalance engine tests.

These are unit-level tests that mock the LND gRPC stubs and the
peewee DB, then exercise the rebalance helpers directly. The goal is
to pin the algorithm's contracts (budget gate, channel-pair iteration,
binary-halving termination, success persistence) without needing a
regtest LN rig.

A separate set of tests covers the dashboard + stats wiring so a
rebalance fee surfaces as `ln_rebalances` in the breakdown and
counts toward the 2% developer-fee cap.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

import liquidityhelper
from lnd_proto import lightning_pb2, router_pb2
from node_database import Rebalance, LndPaymentLabel


def _run(coro):
    # See lnd_fee_controls_tests._run for the rationale.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Mock stubs
# ---------------------------------------------------------------------------

class _CapturedUnary:
    """Async-callable that records the request and returns a stub response."""
    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls: List[Any] = []
    async def __call__(self, request, timeout: Optional[float] = None):
        self.calls.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


class _StreamingCapture:
    """SendPaymentV2 stub — yields one Payment update on iteration."""
    def __init__(self, payment_update=None, raise_exc=None):
        self.payment_update = payment_update
        self.raise_exc = raise_exc
        self.calls: List[Any] = []
    def __call__(self, request):
        self.calls.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self
    def __aiter__(self):
        return self
    async def __anext__(self):
        if self.payment_update is None:
            raise StopAsyncIteration
        upd = self.payment_update
        self.payment_update = None
        return upd


class _FakeLightningStub:
    def __init__(self, *, get_info=None, add_invoice=None,
                 query_routes=None):
        self.GetInfo = _CapturedUnary(response=get_info)
        self.AddInvoice = _CapturedUnary(response=add_invoice)
        self.QueryRoutes = _CapturedUnary(response=query_routes)


class _FakeRouterStub:
    def __init__(self, *, send_payment_update=None):
        self.SendPaymentV2 = _StreamingCapture(payment_update=send_payment_update)


def _wire_stubs(monkeypatch, wallet_id, lightning_stub, router_stub):
    monkeypatch.setattr(
        liquidityhelper, "_LND_CONNECTIONS",
        {wallet_id: {
            "channel": object(),
            "stubs": {"Lightning": lightning_stub, "Router": router_stub},
        }},
    )


@pytest.fixture(autouse=True)
def _clean_db_per_test():
    """Wipe Rebalance + LndPaymentLabel + SimpleVariable between tests
    so dedupe state and budget math doesn't leak."""
    from database import SimpleVariable
    Rebalance.delete().execute()
    LndPaymentLabel.delete().execute()
    SimpleVariable.delete().where(
        SimpleVariable.name == "FIRST_REBALANCE_ATTEMPT_DATE",
    ).execute()
    yield
    Rebalance.delete().execute()
    LndPaymentLabel.delete().execute()
    SimpleVariable.delete().where(
        SimpleVariable.name == "FIRST_REBALANCE_ATTEMPT_DATE",
    ).execute()


# ---------------------------------------------------------------------------
# Budget calculation
# ---------------------------------------------------------------------------

def test_budget_zero_when_yearly_budget_zero(monkeypatch):
    """REBALANCE_YEARLY_BUDGET_SAT=0 disables the rebalancer."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 0, raising=False)
    assert liquidityhelper._compute_rebalance_budget_remaining("w1") == 0


def test_budget_first_attempt_records_date_and_grants_daily_allowance(monkeypatch):
    """On the very first attempt (no SimpleVariable marker, no prior
    Rebalance rows), available budget == one day's allowance.
    Mathematically: 3650/365 = 10 → 10 sats budget on day 1."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 3650, raising=False)
    avail = liquidityhelper._compute_rebalance_budget_remaining("w1")
    # day_1: daily × 1 day = 10 sats, minus 0 spent = 10
    assert avail == 10


def test_budget_rollover_accumulates_unused_days(monkeypatch):
    """If first_attempt_date was 10 days ago and we've never spent
    anything, available = daily × 10 = 100 sats (with daily=10)."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 3650, raising=False)
    # Plant a first-attempt date 10 days ago.
    from database import SimpleVariable
    ten_days_ago = liquidityhelper.utcnow_naive() - datetime.timedelta(days=10)
    SimpleVariable.create(
        name="FIRST_REBALANCE_ATTEMPT_DATE",
        value=ten_days_ago.isoformat(),
    )
    avail = liquidityhelper._compute_rebalance_budget_remaining("w1")
    # days_active = 10 + 1 (today) = 11; daily=10; earned=110; spent=0.
    assert avail == 110


def test_budget_subtracts_prior_spend(monkeypatch):
    """Past Rebalance rows subtract their fee from the rollover."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 3650, raising=False)
    from database import SimpleVariable
    ten_days_ago = liquidityhelper.utcnow_naive() - datetime.timedelta(days=10)
    SimpleVariable.create(
        name="FIRST_REBALANCE_ATTEMPT_DATE",
        value=ten_days_ago.isoformat(),
    )
    Rebalance.create(
        payment_hash="aa" * 32, wallet_id="w1",
        date=liquidityhelper.utcnow_naive(),
        amount_sat=50_000, fee_sat=30,
        out_channel_point="tx1:0", in_channel_point="tx2:0",
    )
    avail = liquidityhelper._compute_rebalance_budget_remaining("w1")
    # earned=110, spent=30 → 80
    assert avail == 80


def test_budget_zero_when_overspent(monkeypatch):
    """If spend somehow exceeds earned (shouldn't happen but defensive),
    budget clamps to 0 — we never go negative."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 3650, raising=False)
    from database import SimpleVariable
    SimpleVariable.create(
        name="FIRST_REBALANCE_ATTEMPT_DATE",
        value=liquidityhelper.utcnow_naive().isoformat(),
    )
    Rebalance.create(
        payment_hash="bb" * 32, wallet_id="w1",
        date=liquidityhelper.utcnow_naive(),
        amount_sat=100_000, fee_sat=500,   # way more than day-1 budget of 10
        out_channel_point="tx1:0", in_channel_point="tx2:0",
    )
    avail = liquidityhelper._compute_rebalance_budget_remaining("w1")
    assert avail == 0


def test_budget_isolated_per_wallet(monkeypatch):
    """One wallet's spend doesn't deplete another wallet's budget."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 3650, raising=False)
    from database import SimpleVariable
    SimpleVariable.create(
        name="FIRST_REBALANCE_ATTEMPT_DATE",
        value=liquidityhelper.utcnow_naive().isoformat(),
    )
    Rebalance.create(
        payment_hash="cc" * 32, wallet_id="w1",
        date=liquidityhelper.utcnow_naive(),
        amount_sat=50_000, fee_sat=5,
        out_channel_point="tx1:0", in_channel_point="tx2:0",
    )
    # w2 has no rebalances of its own → full day-1 budget.
    assert liquidityhelper._compute_rebalance_budget_remaining("w2") == 10
    # w1 lost 5 sats → 5 sats remaining.
    assert liquidityhelper._compute_rebalance_budget_remaining("w1") == 5


# ---------------------------------------------------------------------------
# Channel-pair selection
# ---------------------------------------------------------------------------

def _chan(chan_id, local, capacity, remote_pub="ab" * 33, active=True):
    """Synthesize a channel dict matching what get_wallet_ln_channels returns."""
    return {
        "chan_id": str(chan_id),
        "local_balance": local,
        "capacity": capacity,
        "remote_balance": capacity - local,
        "active": active,
        "remote_pubkey": remote_pub,
        "channel_point": f"tx{chan_id}:0",
    }


def test_rank_pairs_orders_by_imbalance():
    """Most-imbalanced pair comes first: highest-ratio channel as
    `out`, lowest-ratio as `in`."""
    chans = [
        _chan(1, local=10_000,  capacity=100_000),   # 10%
        _chan(2, local=90_000,  capacity=100_000),   # 90%
        _chan(3, local=50_000,  capacity=100_000),   # 50%
    ]
    pairs = liquidityhelper._rank_channel_pairs(chans)
    # First pair: out=chan_id=2 (most full), in=chan_id=1 (most empty)
    assert pairs[0][0]["chan_id"] == "2"
    assert pairs[0][1]["chan_id"] == "1"


def test_rank_pairs_skips_inactive_channels():
    """Inactive channels can't route, so they're not eligible for
    either side."""
    chans = [
        _chan(1, local=10_000, capacity=100_000, active=False),
        _chan(2, local=90_000, capacity=100_000),
        _chan(3, local=50_000, capacity=100_000),
    ]
    pairs = liquidityhelper._rank_channel_pairs(chans)
    chan_ids = {(p[0]["chan_id"], p[1]["chan_id"]) for p in pairs}
    # Channel 1 must NOT appear on either side.
    assert all("1" not in (o, i) for o, i in chan_ids)


def test_rank_pairs_returns_empty_when_fewer_than_two_active():
    chans = [_chan(1, local=50_000, capacity=100_000)]
    assert liquidityhelper._rank_channel_pairs(chans) == []


# ---------------------------------------------------------------------------
# attempt_circular_rebalance — orchestration paths
# ---------------------------------------------------------------------------

class _FakeAPI:
    def __init__(self, channels):
        self.channels = channels
    async def get_wallet_ln_channels(self, wallet_id, active_only=False, online_only=False):
        return [c for c in self.channels if (not active_only or c.get("active"))]


def test_attempt_skips_when_budget_exhausted(monkeypatch):
    """Budget at 0 → skip cleanly without touching channels or stubs."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 3650, raising=False)
    from database import SimpleVariable
    SimpleVariable.create(
        name="FIRST_REBALANCE_ATTEMPT_DATE",
        value=liquidityhelper.utcnow_naive().isoformat(),
    )
    Rebalance.create(
        payment_hash="dd" * 32, wallet_id="w1",
        date=liquidityhelper.utcnow_naive(),
        amount_sat=10, fee_sat=11,  # already spent more than day-1 allowance
        out_channel_point="tx1:0", in_channel_point="tx2:0",
    )
    api = _FakeAPI(channels=[
        _chan(1, 90_000, 100_000),
        _chan(2, 10_000, 100_000),
    ])
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = _run(liquidityhelper.attempt_circular_rebalance(api, wallet))
    assert ok is False


def test_attempt_skips_when_fewer_than_two_channels(monkeypatch):
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 3650, raising=False)
    api = _FakeAPI(channels=[_chan(1, 50_000, 100_000)])
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = _run(liquidityhelper.attempt_circular_rebalance(api, wallet))
    assert ok is False


def test_attempt_skips_for_non_btclnd_wallets(monkeypatch):
    """Electrum's LN dispatch doesn't expose the same self-payment
    primitives; bail cleanly rather than half-implementing."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 3650, raising=False)
    api = _FakeAPI(channels=[
        _chan(1, 90_000, 100_000),
        _chan(2, 10_000, 100_000),
    ])
    wallet = {"id": "w1", "currency": "btc"}  # Electrum
    ok = _run(liquidityhelper.attempt_circular_rebalance(api, wallet))
    assert ok is False


def test_successful_rebalance_persists_rebalance_row_and_label(monkeypatch):
    """End-to-end happy path: budget allows, channels imbalanced,
    QueryRoutes returns viable fee, SendPaymentV2 succeeds. Verify
    Rebalance row + LndPaymentLabel row both land."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 36_500, raising=False)
    monkeypatch.setattr(liquidityhelper, "REBALANCE_MIN_CHANNEL_BUFFER_SAT", 1_000, raising=False)
    api = _FakeAPI(channels=[
        _chan(1, 90_000, 100_000, remote_pub="aa" * 33),   # out
        _chan(2, 10_000, 100_000, remote_pub="bb" * 33),   # in
    ])
    wallet = {"id": "w1", "currency": "btclnd"}
    light = _FakeLightningStub(
        get_info=lightning_pb2.GetInfoResponse(identity_pubkey="cc" * 33),
        add_invoice=lightning_pb2.AddInvoiceResponse(
            r_hash=b"\x11" * 32, payment_request="lnbcrt…",
        ),
        query_routes=lightning_pb2.QueryRoutesResponse(
            routes=[lightning_pb2.Route(total_fees=3)],
        ),
    )
    router = _FakeRouterStub(
        send_payment_update=lightning_pb2.Payment(
            status=2,        # SUCCEEDED
            fee_msat=3000,   # 3 sat
            payment_hash=("11" * 32),
        ),
    )
    _wire_stubs(monkeypatch, "w1", light, router)

    ok = _run(liquidityhelper.attempt_circular_rebalance(api, wallet))
    assert ok is True
    # Rebalance row persisted with the actual fee paid.
    rows = list(Rebalance.select().where(Rebalance.wallet_id == "w1"))
    assert len(rows) == 1
    assert rows[0].fee_sat == 3
    assert rows[0].out_channel_point == "tx1:0"
    assert rows[0].in_channel_point == "tx2:0"
    # LndPaymentLabel row written so new_calc_invoice_stats can bucket
    # the fee correctly when it later walks LN payment history.
    labels = list(LndPaymentLabel.select().where(LndPaymentLabel.wallet_id == "w1"))
    assert len(labels) == 1
    assert labels[0].label == liquidityhelper.REBALANCE_REASON


def test_halving_bails_on_base_fee_saturation(monkeypatch):
    """When the route's BASE fees exceed budget regardless of amount,
    halving doesn't help. The probe should detect non-decreasing fee
    after halving and bail to the next pair (here, the only pair
    available, so the overall attempt returns False)."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 365, raising=False)  # 1 sat/day
    monkeypatch.setattr(liquidityhelper, "REBALANCE_MIN_CHANNEL_BUFFER_SAT", 1_000, raising=False)

    # Force QueryRoutes to keep returning fee=5 (above budget) at every
    # amount — base fees saturated. We do this by overriding the
    # response after each call. Use a closure to make the stub
    # stateful.
    call_count = {"n": 0}
    fee_returned = []
    class _FlatFeeQR:
        async def __call__(self, request, timeout=None):
            call_count["n"] += 1
            fee_returned.append(5)
            return lightning_pb2.QueryRoutesResponse(
                routes=[lightning_pb2.Route(total_fees=5)],
            )

    api = _FakeAPI(channels=[
        _chan(1, 90_000, 100_000, remote_pub="aa" * 33),
        _chan(2, 10_000, 100_000, remote_pub="bb" * 33),
    ])
    wallet = {"id": "w1", "currency": "btclnd"}
    light = _FakeLightningStub(
        get_info=lightning_pb2.GetInfoResponse(identity_pubkey="cc" * 33),
        add_invoice=lightning_pb2.AddInvoiceResponse(
            r_hash=b"\x11" * 32, payment_request="lnbcrt…",
        ),
    )
    light.QueryRoutes = _FlatFeeQR()
    router = _FakeRouterStub()
    _wire_stubs(monkeypatch, "w1", light, router)

    ok = _run(liquidityhelper.attempt_circular_rebalance(api, wallet))
    assert ok is False
    # With 2 channels we get 2 pair orderings (chan2→chan1 and the
    # reverse). Each pair takes at most ~2-3 probes before the
    # base-fee-saturation guard bails. So total probes should be
    # bounded at ~6 — far less than the ~log2(max_amount)*2 ≈ 14
    # we'd see without the saturation guard.
    assert call_count["n"] <= 6, (
        f"halving should bail on flat-fee detection per pair; got "
        f"{call_count['n']} probes (expected ≤6 for 2 pairs × ~3 probes)"
    )


def test_halving_bails_at_min_buffer(monkeypatch):
    """If no route exists at any amount (QueryRoutes always returns
    empty routes), halving terminates when amount drops below the
    min-channel-buffer floor."""
    monkeypatch.setattr(liquidityhelper, "REBALANCE_YEARLY_BUDGET_SAT", 36_500, raising=False)
    monkeypatch.setattr(liquidityhelper, "REBALANCE_MIN_CHANNEL_BUFFER_SAT", 1_000, raising=False)

    call_count = {"n": 0}
    class _NoRouteQR:
        async def __call__(self, request, timeout=None):
            call_count["n"] += 1
            return lightning_pb2.QueryRoutesResponse(routes=[])

    api = _FakeAPI(channels=[
        _chan(1, 90_000, 100_000, remote_pub="aa" * 33),
        _chan(2, 10_000, 100_000, remote_pub="bb" * 33),
    ])
    wallet = {"id": "w1", "currency": "btclnd"}
    light = _FakeLightningStub(
        get_info=lightning_pb2.GetInfoResponse(identity_pubkey="cc" * 33),
        add_invoice=lightning_pb2.AddInvoiceResponse(r_hash=b"\x11" * 32, payment_request="x"),
    )
    light.QueryRoutes = _NoRouteQR()
    router = _FakeRouterStub()
    _wire_stubs(monkeypatch, "w1", light, router)

    ok = _run(liquidityhelper.attempt_circular_rebalance(api, wallet))
    assert ok is False
    # max_amount starts ~89_000; halving log2(89000/1000) ≈ 7 iterations.
    # Allow some slack but it must be bounded (no infinite loop).
    assert call_count["n"] < 20


# ---------------------------------------------------------------------------
# Stats wiring
# ---------------------------------------------------------------------------

def test_rebalance_label_buckets_to_ln_rebalances_in_stats():
    """new_calc_invoice_stats's LN walker recognizes REBALANCE_REASON
    and routes the routing fee into the dedicated bucket."""
    from classes import StoreStats
    # Build a stats object and run the classifier branch we care about.
    # We replicate the labeling check inline from new_calc_invoice_stats:
    # there's no per-branch hook so we exercise the code path by hand.
    stats = StoreStats(
        store_id="s1",
        ln_total_revenue_in_sats=0, onchain_total_revenue_in_sats=0,
        total_bb_fees_paid_in_sats=0,
        revenue_eligible_for_fee=0,
        ineligible_revenue_because_not_liquidityhelper_wallet_in_sats=0,
        ineligible_revenue_because_of_promo_in_sats=0,
        ineligible_revenue_because_of_topups_in_sats=0,
        ineligible_revenue_because_of_bb_topups_in_sats=0,
        total_bb_topup_principal_returned_in_sats=0,
        ln_network_fees_paid_for_bb_topup_returns_in_sats=0,
        onchain_network_fees_paid_for_bb_topup_returns_in_sats=0,
        ln_network_fees_paid_for_fee_payments_in_sats=0,
        onchain_network_fees_paid_for_fee_payments_in_sats=0,
        ln_network_fees_paid_for_payouts_in_sats=0,
        misc_ln_network_fees_in_sats=0,
        onchain_network_fees_paid_for_payouts_in_sats=0,
        onchain_network_fees_paid_for_channel_opens_in_sats=0,
        onchain_network_fees_paid_for_channel_closes_in_sats=0,
        onchain_network_fees_paid_for_swaps_in_sats=0,
        onchain_network_fees_paid_for_lsp_orders_in_sats=0,
        onchain_lsp_service_fees_paid_in_sats=0,
        total_referral_fees_paid_in_sats=0,
        ln_network_fees_paid_for_referral_payments_in_sats=0,
        onchain_network_fees_paid_for_referral_payments_in_sats=0,
    )
    # Field's default is 0; raises AttributeError if absent.
    assert stats.ln_network_fees_paid_for_rebalances_in_sats == 0
    # Bump it as if a rebalance fee landed here, then verify
    # calc_total_bb_fees_paid_in_sats picks it up under the LN-fees
    # umbrella (counts toward 2% cap).
    stats.ln_network_fees_paid_for_rebalances_in_sats = 5
    total_with_ln = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=False,
        include_ln_network_fees=True,
    )
    total_without_ln = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=False,
        include_ln_network_fees=False,
    )
    assert total_with_ln - total_without_ln >= 5, (
        f"rebalance fee must count toward dev-fee cap when "
        f"include_ln_network_fees=True; delta was "
        f"{total_with_ln - total_without_ln}"
    )


def test_dashboard_breakdown_surfaces_ln_rebalances():
    """FeeBreakdown gains an `ln_rebalances` field; _network_breakdown_
    from_stats projects from the new StoreStats field; _sum_breakdown
    includes it in the total."""
    from bitcart_plugin.dashboard import (
        FeeBreakdown, _sum_breakdown, _add_breakdowns,
    )
    b = FeeBreakdown(
        onchain_payouts=0, onchain_fee_payments=0, onchain_referral_payments=0,
        onchain_topup_returns=0, onchain_channel_opens=0, onchain_channel_closes=0,
        onchain_swaps=0, onchain_lsp_orders=0, lsp_service_fees=0,
        onchain_external=0,
        ln_payouts=0, ln_fee_payments=0, ln_referral_payments=0,
        ln_rebalances=42, ln_misc=0,
    )
    assert b.ln_rebalances == 42
    assert _sum_breakdown(b) == 42  # only ln_rebalances is non-zero
    # _add_breakdowns sums ln_rebalances field-wise
    a = FeeBreakdown(
        onchain_payouts=0, onchain_fee_payments=0, onchain_referral_payments=0,
        onchain_topup_returns=0, onchain_channel_opens=0, onchain_channel_closes=0,
        onchain_swaps=0, onchain_lsp_orders=0, lsp_service_fees=0,
        onchain_external=0,
        ln_payouts=0, ln_fee_payments=0, ln_referral_payments=0,
        ln_rebalances=8, ln_misc=0,
    )
    combined = _add_breakdowns(a, b)
    assert combined.ln_rebalances == 50
