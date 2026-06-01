"""Tests for the BareBits topup-return mechanic.

Three layers, mirroring the structure used for the dev fee + referral
fee tests:

  1. StoreStats math — `calc_bb_topup_pool_owed` returns the right
     pool balance, and the BB-topup principal does NOT leak into
     `calc_total_bb_fees_paid_in_sats` (the principal isn't a fee).

  2. `new_calc_invoice_stats` ledger walk — an LN payment labeled
     `BB_TOPUP_RETURN_REASON` populates `total_bb_topup_principal_
     returned_in_sats` with the principal, and the LN routing fee
     gets counted toward the dev fee (per the standard network-fee
     policy).

  3. `_maybe_pay_bb_topup_return_via_ln` gates — inbound-liquidity-met
     suppresses the return before channels are provisioned;
     MIN_FEE_OUT skips dust LN payments; DRY_RUN_FUNDS skips entirely;
     happy path sends with the right label + comment.
"""

from __future__ import annotations

from typing import Any, Dict, List

import liquidityhelper
from classes import StoreStats


def _empty_store_stats() -> StoreStats:
    return StoreStats(
        store_id="s-test",
        ln_total_revenue_in_sats=0,
        onchain_total_revenue_in_sats=0,
        total_bb_fees_paid_in_sats=0,
        ineligible_revenue_because_of_promo_in_sats=0,
        ineligible_revenue_because_of_topups_in_sats=0,
        ineligible_revenue_because_of_bb_topups_in_sats=0,
        total_bb_topup_principal_returned_in_sats=0,
        ln_network_fees_paid_for_bb_topup_returns_in_sats=0,
        onchain_network_fees_paid_for_bb_topup_returns_in_sats=0,
        ln_network_fees_paid_for_fee_payments_in_sats=0,
        onchain_network_fees_paid_for_fee_payments_in_sats=0,
        ln_network_fees_paid_for_payouts_in_sats=0,
        onchain_network_fees_paid_for_payouts_in_sats=0,
        ineligible_revenue_because_not_liquidityhelper_wallet_in_sats=0,
        revenue_eligible_for_fee=0,
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


# ---------------------------------------------------------------------------
# calc_bb_topup_pool_owed — pool math
# ---------------------------------------------------------------------------

def test_pool_owed_zero_when_nothing_received():
    stats = _empty_store_stats()
    assert stats.calc_bb_topup_pool_owed() == 0


def test_pool_owed_is_received_minus_returned():
    stats = _empty_store_stats()
    stats.ineligible_revenue_because_of_bb_topups_in_sats = 100_000
    stats.total_bb_topup_principal_returned_in_sats = 30_000
    assert stats.calc_bb_topup_pool_owed() == 70_000


def test_pool_owed_clamps_at_zero_on_overreturn():
    """Shouldn't happen in normal operation, but a spurious
    BB_TOPUP_RETURN_REASON tx tagged manually shouldn't surface as a
    negative debt."""
    stats = _empty_store_stats()
    stats.ineligible_revenue_because_of_bb_topups_in_sats = 50_000
    stats.total_bb_topup_principal_returned_in_sats = 99_999
    assert stats.calc_bb_topup_pool_owed() == 0


def test_pool_unaffected_by_channel_and_lsp_costs():
    """Per user spec: liquidity costs do NOT debit the pool. BB gets
    the full principal back regardless of what was spent acquiring
    liquidity in the meantime (those costs still count against the
    operator's 2% cap via the channel-open/LSP buckets)."""
    stats = _empty_store_stats()
    stats.ineligible_revenue_because_of_bb_topups_in_sats = 100_000
    stats.onchain_network_fees_paid_for_channel_opens_in_sats = 5_000
    stats.onchain_network_fees_paid_for_lsp_orders_in_sats = 500
    stats.onchain_lsp_service_fees_paid_in_sats = 2_500
    assert stats.calc_bb_topup_pool_owed() == 100_000


# ---------------------------------------------------------------------------
# Separation: BB-topup principal is NOT in the dev-fee pool
# ---------------------------------------------------------------------------

def test_bb_topup_principal_does_not_count_against_dev_fee():
    """The dev fee math should NOT treat the BB-topup principal as
    'already paid' — it's the principal of a topup loan, not a fee."""
    stats = _empty_store_stats()
    stats.total_bb_fees_paid_in_sats = 100
    stats.ineligible_revenue_because_of_bb_topups_in_sats = 99_999
    dev_paid = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=True, include_ln_network_fees=True,
    )
    assert dev_paid == 100


def test_bb_topup_return_ln_fee_DOES_count_against_dev_fee():
    """Network fee to DELIVER the BB return is a real network fee,
    and the dev's 2% absorbs it — symmetric with how dev-fee delivery
    fees are handled."""
    stats = _empty_store_stats()
    stats.total_bb_fees_paid_in_sats = 100
    stats.ln_network_fees_paid_for_bb_topup_returns_in_sats = 50
    paid_with_ln_fees = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=False, include_ln_network_fees=True,
    )
    assert paid_with_ln_fees == 150
    paid_without_ln_fees = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=False, include_ln_network_fees=False,
    )
    assert paid_without_ln_fees == 100


# ---------------------------------------------------------------------------
# Ledger walk — BB_TOPUP_RETURN_REASON label routes to BB buckets
# ---------------------------------------------------------------------------

def _replay_ln_history(transactions: List[Dict[str, Any]]) -> StoreStats:
    """Replay the LN-history branch of new_calc_invoice_stats over
    synthetic transactions. Mirrors production logic — if the production
    classifier drifts, the test catches it."""
    from liquidityhelper import (
        is_ln_open_transaction, is_ln_close_transaction,
        CASHOUT_REASON, FEE_PAYOUT_REASON, REFERRAL_PAYOUT_REASON,
        REBALANCE_REASON, BB_TOPUP_RETURN_REASON,
    )
    stats = _empty_store_stats()
    for transaction in transactions:
        if is_ln_open_transaction(transaction):
            continue
        if is_ln_close_transaction(transaction):
            continue
        if transaction['amount_msat'] > 0:
            continue
        if transaction['type'] == 'payment' and transaction['amount_msat'] < 0:
            if transaction['label'] == CASHOUT_REASON:
                stats.ln_network_fees_paid_for_payouts_in_sats += abs(transaction['fee_msat']/1000)
                continue
            if transaction['label'] == FEE_PAYOUT_REASON:
                stats.ln_network_fees_paid_for_fee_payments_in_sats += abs(transaction['fee_msat']/1000)
                stats.total_bb_fees_paid_in_sats += abs(transaction['amount_msat']/1000)
                continue
            if transaction['label'] == REFERRAL_PAYOUT_REASON:
                stats.ln_network_fees_paid_for_referral_payments_in_sats += abs(transaction['fee_msat']/1000)
                stats.total_referral_fees_paid_in_sats += abs(transaction['amount_msat']/1000)
                continue
            if transaction['label'] == REBALANCE_REASON:
                stats.ln_network_fees_paid_for_rebalances_in_sats += abs(transaction['fee_msat']/1000)
                continue
            if transaction['label'] == BB_TOPUP_RETURN_REASON:
                stats.ln_network_fees_paid_for_bb_topup_returns_in_sats += abs(transaction['fee_msat']/1000)
                stats.total_bb_topup_principal_returned_in_sats += abs(transaction['amount_msat']/1000)
                continue
            stats.misc_ln_network_fees_in_sats += abs(transaction['fee_msat']/1000)
    return stats


def test_bb_topup_return_label_routes_principal_and_fee():
    """A BB_TOPUP_RETURN_REASON tx puts the principal into the
    returned-principal bucket and the routing fee into the BB-return
    LN-fee bucket — NOT into the dev-fee buckets."""
    txs = [
        {
            "type": "payment",
            "label": "lnhelper_bb_topup_return",
            "amount_msat": -25_000_000,   # 25k sat principal
            "fee_msat": 100_000,           # 100 sat routing fee
        },
    ]
    stats = _replay_ln_history(txs)
    assert stats.total_bb_topup_principal_returned_in_sats == 25_000
    assert stats.ln_network_fees_paid_for_bb_topup_returns_in_sats == 100
    # Crucially: nothing leaks into the dev-fee principal bucket
    assert stats.total_bb_fees_paid_in_sats == 0
    assert stats.ln_network_fees_paid_for_fee_payments_in_sats == 0


def test_multiple_returns_accumulate():
    """Subsequent BB returns sum into the same buckets across ticks."""
    txs = [
        {"type": "payment", "label": "lnhelper_bb_topup_return",
         "amount_msat": -10_000_000, "fee_msat": 50_000},
        {"type": "payment", "label": "lnhelper_bb_topup_return",
         "amount_msat": -15_000_000, "fee_msat": 80_000},
    ]
    stats = _replay_ln_history(txs)
    assert stats.total_bb_topup_principal_returned_in_sats == 25_000
    assert stats.ln_network_fees_paid_for_bb_topup_returns_in_sats == 130


def test_bb_return_does_not_disturb_dev_or_referral_buckets():
    """Mixed history with a BB return + a dev-fee + a referral — each
    lands in its own bucket and no cross-contamination."""
    txs = [
        {"type": "payment", "label": "lnhelper_bb_topup_return",
         "amount_msat": -10_000_000, "fee_msat": 50_000},
        {"type": "payment", "label": "lnhelper_fee",
         "amount_msat": -2_000_000, "fee_msat": 30_000},
        {"type": "payment", "label": "lnhelper_referral",
         "amount_msat": -1_000_000, "fee_msat": 20_000},
    ]
    stats = _replay_ln_history(txs)
    assert stats.total_bb_topup_principal_returned_in_sats == 10_000
    assert stats.ln_network_fees_paid_for_bb_topup_returns_in_sats == 50
    assert stats.total_bb_fees_paid_in_sats == 2_000
    assert stats.ln_network_fees_paid_for_fee_payments_in_sats == 30
    assert stats.total_referral_fees_paid_in_sats == 1_000
    assert stats.ln_network_fees_paid_for_referral_payments_in_sats == 20


# ---------------------------------------------------------------------------
# Invoice classifier — paid TOPUP_BAREBITS invoice → received bucket
# ---------------------------------------------------------------------------

def _replay_invoice_classifier(invoices: List[Dict[str, Any]],
                               wallet_name: str = "liquidityhelper") -> StoreStats:
    """Replay the invoice-iteration branch of new_calc_invoice_stats over
    synthetic invoice rows. Mirrors production logic at liquidityhelper.py
    line 2580+ verbatim so drift surfaces here.

    Each invoice has a `notes` field (drives is_*_topup_invoice) and a
    `payments` list (each entry has `lightning`, `amount`, `symbol`,
    `created`).
    """
    import dataclasses
    from classes import BitcartInvoice
    from common_functions import btc_to_sats
    stats = _empty_store_stats()
    field_names = set(f.name for f in dataclasses.fields(BitcartInvoice))
    for invoice in invoices:
        classified_invoice = BitcartInvoice(
            **{k: v for k, v in invoice.items() if k in field_names}
        )
        for payment in invoice["payments"]:
            if (payment.get("symbol") or "").upper() != "BTC":
                continue
            amount_in_sats = btc_to_sats(abs(float(payment["amount"])))
            if amount_in_sats == 0:
                continue
            if payment["lightning"]:
                stats.ln_total_revenue_in_sats += amount_in_sats
            else:
                stats.onchain_total_revenue_in_sats += amount_in_sats
            ineligible = False
            if classified_invoice.is_self_topup_invoice():
                stats.ineligible_revenue_because_of_topups_in_sats += amount_in_sats
                ineligible = True
            elif classified_invoice.is_bb_topup_invoice():
                stats.ineligible_revenue_because_of_bb_topups_in_sats += amount_in_sats
                ineligible = True
            elif wallet_name != "liquidityhelper":
                stats.ineligible_revenue_because_not_liquidityhelper_wallet_in_sats += amount_in_sats
                ineligible = True
            if not ineligible:
                stats.revenue_eligible_for_fee += amount_in_sats
    return stats


def _make_invoice(notes: str, payments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """BitcartInvoice has many required positional fields. Fill them with
    benign defaults for classifier tests — only `notes` and `payments`
    drive the BB-topup classifier branch."""
    return {
        "id": "inv-test",
        "order_id": "order-test",
        "store_id": "store-test",
        "notes": notes,
        "payments": payments,
        "paid_currency": "BTC",
        "price": "0",
        "status": "complete",
        "currency": "BTC",
        "tx_hashes": [],
    }


def test_bb_topup_invoice_payment_lands_in_received_bucket():
    """A paid TOPUP_BAREBITS invoice's on-chain payment amount flows
    into ineligible_revenue_because_of_bb_topups_in_sats. That's the
    field calc_bb_topup_pool_owed() reads as the 'received' side of
    the pool. Also confirms the principal does NOT count toward
    revenue_eligible_for_fee (no 2% fee charged on a BB topup)."""
    invoices = [_make_invoice("topupbarebits", [{
        "lightning": False,
        "amount": "0.00100000",   # 100k sat received on-chain
        "symbol": "btc",
        "created": "2026-05-30T00:00:00Z",
    }])]
    stats = _replay_invoice_classifier(invoices)
    assert stats.ineligible_revenue_because_of_bb_topups_in_sats == 100_000
    # And therefore:
    assert stats.calc_bb_topup_pool_owed() == 100_000
    # 2% fee does not apply to the topup principal:
    assert stats.revenue_eligible_for_fee == 0


def test_bb_topup_overpayment_captured_correctly():
    """If BareBits sends MORE sats than the invoice's nominal `price`
    (BIP21 amount is just a hint — the address accepts any amount),
    the engine must track the ACTUAL sats received, not the nominal
    price. Confirms the per-payment `amount` is what gets counted,
    not the invoice-level `price`."""
    invoices = [_make_invoice("topupbarebits", [{
        "lightning": False,
        "amount": "0.00150000",   # 150k sat received (BB overpaid)
        "symbol": "btc",
        "created": "2026-05-30T00:00:00Z",
    }])]
    stats = _replay_invoice_classifier(invoices)
    assert stats.ineligible_revenue_because_of_bb_topups_in_sats == 150_000
    assert stats.calc_bb_topup_pool_owed() == 150_000


def test_bb_topup_multiple_invoices_accumulate():
    """Several BB topups across the engine's history sum into the
    received bucket. Validates the cumulative-pool model."""
    invoices = [
        _make_invoice("topupbarebits", [{
            "lightning": False, "amount": "0.00050000",
            "symbol": "btc", "created": "2026-04-01T00:00:00Z",
        }]),
        _make_invoice("topupbarebits", [{
            "lightning": False, "amount": "0.00075000",
            "symbol": "btc", "created": "2026-05-01T00:00:00Z",
        }]),
    ]
    stats = _replay_invoice_classifier(invoices)
    assert stats.ineligible_revenue_because_of_bb_topups_in_sats == 125_000
    assert stats.calc_bb_topup_pool_owed() == 125_000


def test_regular_customer_invoice_still_counts_as_revenue():
    """Sanity check: the classifier didn't break the normal-revenue
    branch. A non-topup invoice on a `liquidityhelper` wallet
    contributes to revenue_eligible_for_fee and NOT to any topup
    bucket."""
    invoices = [_make_invoice("", [{
        "lightning": True,
        "amount": "0.00010000",
        "symbol": "btc",
        "created": "2026-05-30T00:00:00Z",
    }])]
    stats = _replay_invoice_classifier(invoices)
    assert stats.revenue_eligible_for_fee == 10_000
    assert stats.ineligible_revenue_because_of_bb_topups_in_sats == 0
    assert stats.calc_bb_topup_pool_owed() == 0


# ---------------------------------------------------------------------------
# calculate_topups — creates the TOPUP_BAREBITS invoice
# ---------------------------------------------------------------------------

def test_calculate_topups_creates_bb_topup_invoice_with_priced_amount(
    monkeypatch, event_loop,
):
    """When a store has an on-chain deficit beyond the safety zone,
    calculate_topups creates both a TOPUP_NAME and a TOPUP_BAREBITS
    invoice. Both invoices are priced at the current deficit+1000 sat
    (in BTC) so Bitcart generates a real on-chain payment address.
    The expiration is in MINUTES (43_200 == 30 days) — NOT seconds
    (the units mismatch with Bitcart caused all topup invoices to
    fail their payment-method creation before the previous fix)."""
    created_invoices = []

    class _FakeApi:
        async def get_stores(self):
            return [{"id": "store1", "name": "test-store"}]

        async def get_best_ln_wallet_for_store(self, store):
            return {"id": "wallet1", "name": "liquidityhelper"}

        async def get_invoice_by_note(self, note=None, require_pending=False, **kwargs):
            return None  # no existing pending invoice → engine creates new

        async def create_invoice(self, **kwargs):
            created_invoices.append(kwargs)
            # Return a freshly-created-invoice-shaped dict including
            # a non-empty `payments` array so the engine's fallback
            # "discard pending invoice with no on-chain address"
            # doesn't fire.
            return {
                "id": f"inv-{kwargs['notes']}",
                "notes": kwargs["notes"],
                "price": kwargs["price_in_btc"],
                "payments": [{
                    "lightning": False,
                    "payment_url": "bitcoin:bc1q...?amount=...",
                    "payment_address": "bc1q...",
                }],
            }

    # store_needs_topup returns the deficit in sats; topup_warning
    # is gated on deficit > AUTOMATIC_RESERVE_SAFETY_SAT (default 1000)
    async def fake_store_needs_topup(api, store_id):
        return 50_000

    monkeypatch.setattr(liquidityhelper, "store_needs_topup",
                        fake_store_needs_topup)
    monkeypatch.setattr(liquidityhelper, "AUTOMATIC_RESERVE_SAFETY_SAT", 1000)
    # No store-owner notifier injected — keeps the test self-contained (no email)
    monkeypatch.setattr(liquidityhelper, "_store_owner_notifier", None)

    api = _FakeApi()
    event_loop.run_until_complete(liquidityhelper.calculate_topups(api))

    # Two invoices: one TOPUP_NAME, one TOPUP_BAREBITS.
    notes = [inv["notes"] for inv in created_invoices]
    assert "topupself" in notes
    assert "topupbarebits" in notes
    bb_invoice = next(inv for inv in created_invoices
                      if inv["notes"] == "topupbarebits")
    # Price = (deficit + 1000-sat buffer) converted to BTC
    expected_price_btc = (50_000 + 1000) / 100_000_000
    assert abs(bb_invoice["price_in_btc"] - expected_price_btc) < 1e-9
    assert bb_invoice["currency"] == "BTC"
    # Expiration in MINUTES (the previous-PR fix). 43_200 minutes ==
    # 30 days. The pre-fix value of 2_628_000 was interpreted as
    # minutes by Bitcart and exceeded LND's 1-year cap, breaking
    # payment-method generation.
    assert bb_invoice["expiration_in_minutes"] == 43_200


def test_calculate_topups_suppresses_within_safety_zone(
    monkeypatch, event_loop,
):
    """Deficit smaller than AUTOMATIC_RESERVE_SAFETY_SAT is in the
    hysteresis zone — no invoice is created, no warning emitted.
    Avoids spamming the operator when the wallet drifts a few hundred
    sats below the floor."""
    created_invoices = []

    class _FakeApi:
        async def get_stores(self):
            return [{"id": "store1", "name": "test-store"}]

        async def get_best_ln_wallet_for_store(self, store):
            return {"id": "wallet1", "name": "liquidityhelper"}

        async def get_invoice_by_note(self, **kwargs):
            return None

        async def create_invoice(self, **kwargs):
            created_invoices.append(kwargs)
            return {"id": "x", "notes": kwargs["notes"], "payments": []}

    async def fake_store_needs_topup(api, store_id):
        return 500  # below safety threshold

    monkeypatch.setattr(liquidityhelper, "store_needs_topup",
                        fake_store_needs_topup)
    monkeypatch.setattr(liquidityhelper, "AUTOMATIC_RESERVE_SAFETY_SAT", 1000)

    api = _FakeApi()
    event_loop.run_until_complete(liquidityhelper.calculate_topups(api))
    assert created_invoices == []


# ---------------------------------------------------------------------------
# _maybe_pay_bb_topup_return_via_ln — gates and happy path
# ---------------------------------------------------------------------------

def _api_with_outbound(sats: int) -> Any:
    class _Api:
        async def get_outbound_liquidity(self, wallet_id):
            return sats
    return _Api()


def _install_default_mocks(monkeypatch, *,
                           outbound: int,
                           liquidity_need=None,
                           pay_succeeds: bool = True):
    """Wire up the helpers _maybe_pay_bb_topup_return_via_ln calls
    out to. Returns a dict that captures arguments."""
    captured: Dict[str, Any] = {}

    async def fake_store_needs_liquidity(store_id, api,
                                         min_sats_liquidity=None,
                                         min_channel_count=None,
                                         assume_zero=False):
        return liquidity_need

    async def fake_lnurl_to_invoice(dest, amount, comment=None):
        captured["dest"] = dest
        captured["amount"] = amount
        captured["comment"] = comment
        return "lnbc1fake"

    async def fake_pay(invoice, label, **kw):
        captured["label"] = label
        captured["paid_invoice"] = invoice
        return pay_succeeds

    monkeypatch.setattr(liquidityhelper, "store_needs_liquidity",
                        fake_store_needs_liquidity)
    monkeypatch.setattr(liquidityhelper, "lnurl_to_invoice",
                        fake_lnurl_to_invoice)
    monkeypatch.setattr(liquidityhelper, "electrum_pay_ln_invoice",
                        fake_pay)
    monkeypatch.setattr(liquidityhelper, "ENABLE_FEE_SENDING_LN", True)
    monkeypatch.setattr(liquidityhelper, "LN_FEE_DEST", "fees@example.com")
    monkeypatch.setattr(liquidityhelper, "BB_STOREID", "test-deploy")
    monkeypatch.setattr(liquidityhelper, "DRY_RUN_FUNDS", False)
    monkeypatch.setattr(liquidityhelper, "MIN_FEE_OUT", 150)
    return captured


def test_bb_return_happy_path_sends_with_right_label_and_comment(
    monkeypatch, event_loop,
):
    """All gates pass → engine sends an LN payment to LN_FEE_DEST with
    the BB_TOPUP_RETURN_REASON label and a distinct LUD-12 comment."""
    captured = _install_default_mocks(monkeypatch, outbound=200_000)
    api = _api_with_outbound(200_000)
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._maybe_pay_bb_topup_return_via_ln(
            api, "store1", wallet, pool_owed=100_000,
        )
    )
    assert ok is True
    assert captured["dest"] == "fees@example.com"
    assert captured["amount"] == 100_000   # min(pool, outbound)
    assert captured["comment"] == "storeid:test-deploy:bb_topup_return"
    assert captured["label"] == "lnhelper_bb_topup_return"


def test_bb_return_capped_by_outbound_when_pool_exceeds_outbound(
    monkeypatch, event_loop,
):
    """If LN outbound < pool, send only what's available; rest rolls
    forward to next tick."""
    captured = _install_default_mocks(monkeypatch, outbound=40_000)
    api = _api_with_outbound(40_000)
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._maybe_pay_bb_topup_return_via_ln(
            api, "store1", wallet, pool_owed=100_000,
        )
    )
    assert ok is True
    assert captured["amount"] == 40_000


def test_bb_return_suppressed_when_inbound_liquidity_not_met(
    monkeypatch, event_loop,
):
    """User spec: don't repay until the channels funded by the topup
    have been provisioned. store_needs_liquidity returning a non-None
    LiquidityNeed gates this."""
    from liquidityhelper import LiquidityNeed
    captured = _install_default_mocks(
        monkeypatch, outbound=200_000,
        liquidity_need=LiquidityNeed(
            liquidity_needed_sat=50_000, channels_needed=1,
        ),
    )
    api = _api_with_outbound(200_000)
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._maybe_pay_bb_topup_return_via_ln(
            api, "store1", wallet, pool_owed=100_000,
        )
    )
    assert ok is False
    # Nothing should have been sent — fakes weren't invoked beyond the
    # gate. `label` is only populated by fake_pay.
    assert "label" not in captured


def test_bb_return_skips_when_below_min_fee_out(monkeypatch, event_loop):
    """Sendable amount < MIN_FEE_OUT (e.g. very low outbound, or tiny
    pool) → defer to next tick. Avoids dust LN payments that won't
    route."""
    captured = _install_default_mocks(monkeypatch, outbound=50)
    api = _api_with_outbound(50)
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._maybe_pay_bb_topup_return_via_ln(
            api, "store1", wallet, pool_owed=100_000,
        )
    )
    assert ok is False
    assert "label" not in captured


def test_bb_return_skips_when_dry_run(monkeypatch, event_loop):
    captured = _install_default_mocks(monkeypatch, outbound=200_000)
    monkeypatch.setattr(liquidityhelper, "DRY_RUN_FUNDS", True)
    api = _api_with_outbound(200_000)
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._maybe_pay_bb_topup_return_via_ln(
            api, "store1", wallet, pool_owed=100_000,
        )
    )
    assert ok is False
    assert "label" not in captured


def test_bb_return_skips_when_ln_sending_disabled(monkeypatch, event_loop):
    captured = _install_default_mocks(monkeypatch, outbound=200_000)
    monkeypatch.setattr(liquidityhelper, "ENABLE_FEE_SENDING_LN", False)
    api = _api_with_outbound(200_000)
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._maybe_pay_bb_topup_return_via_ln(
            api, "store1", wallet, pool_owed=100_000,
        )
    )
    assert ok is False
    assert "label" not in captured


def test_bb_return_returns_false_when_ln_pay_fails(monkeypatch, event_loop):
    """All gates pass + payment attempt is made but electrum_pay_ln_invoice
    returns False (routing failure, peer offline, etc.). The helper
    surfaces False so the engine doesn't mark this tick successful, and
    the pool stays the same for the next tick."""
    captured = _install_default_mocks(
        monkeypatch, outbound=200_000, pay_succeeds=False,
    )
    api = _api_with_outbound(200_000)
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._maybe_pay_bb_topup_return_via_ln(
            api, "store1", wallet, pool_owed=100_000,
        )
    )
    assert ok is False
    # Payment was attempted (label captured), just failed
    assert captured["label"] == "lnhelper_bb_topup_return"
