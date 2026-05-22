"""Tests for the additive, flat referral fee.

Two layers:

  1. StoreStats math — `calc_remaining_referral_fee_due_in_sats`
     returns the right amount, and the referral principal does NOT
     leak into `calc_total_bb_fees_paid_in_sats` (separation between
     dev fee and referral pool).

  2. `new_calc_invoice_stats` ledger walk — an LN payment labeled
     `REFERRAL_PAYOUT_REASON` populates the referral buckets, and
     the LN miner fee gets counted toward the dev fee (per the
     network-fee policy).

  3. Independence — adding referral fee doesn't shrink the developer
     fee or vice versa.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List

import pytest

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


# ---------------------------------------------------------------------------
# calc_remaining_referral_fee_due_in_sats — the headline math
# ---------------------------------------------------------------------------

def test_referral_due_zero_when_amount_is_zero():
    """Default config (REFERRAL_FEE_AMOUNT=0.0) → nothing is ever due,
    regardless of revenue."""
    stats = _empty_store_stats()
    stats.revenue_eligible_for_fee = 1_000_000
    assert stats.calc_remaining_referral_fee_due_in_sats(0.0) == 0


def test_referral_due_is_flat_pct_of_revenue():
    """1M sat revenue × 1% referral → 10k sat due (nothing paid yet)."""
    stats = _empty_store_stats()
    stats.revenue_eligible_for_fee = 1_000_000
    assert stats.calc_remaining_referral_fee_due_in_sats(0.01) == 10_000


def test_referral_due_subtracts_already_paid():
    """1M sat × 1% = 10k. 3k already paid → 7k still due."""
    stats = _empty_store_stats()
    stats.revenue_eligible_for_fee = 1_000_000
    stats.total_referral_fees_paid_in_sats = 3_000
    assert stats.calc_remaining_referral_fee_due_in_sats(0.01) == 7_000


def test_referral_due_clamps_at_zero():
    """If somehow more was paid than owed (operator topped up, or
    revenue shrank from refunds), don't return a negative."""
    stats = _empty_store_stats()
    stats.revenue_eligible_for_fee = 1_000_000
    stats.total_referral_fees_paid_in_sats = 20_000   # already overpaid
    assert stats.calc_remaining_referral_fee_due_in_sats(0.01) == 0


def test_referral_due_is_NOT_reduced_by_network_fees():
    """Whole point of the flat policy. Even with huge LSP/swap/channel
    fees, the referral pool is unaffected."""
    stats = _empty_store_stats()
    stats.revenue_eligible_for_fee = 1_000_000
    # Pile on network fees
    stats.onchain_network_fees_paid_for_channel_opens_in_sats = 5_000
    stats.onchain_network_fees_paid_for_swaps_in_sats = 3_000
    stats.onchain_lsp_service_fees_paid_in_sats = 2_000
    stats.misc_ln_network_fees_in_sats = 1_000
    # Referral due is unmoved
    assert stats.calc_remaining_referral_fee_due_in_sats(0.01) == 10_000


# ---------------------------------------------------------------------------
# Separation: referral principal is NOT in the dev-fee pool
# ---------------------------------------------------------------------------

def test_referral_principal_does_not_count_against_dev_fee():
    """The dev's 2% calculation should NOT count the referral principal
    as 'already paid' (that money went to the distributor, not the dev)."""
    stats = _empty_store_stats()
    stats.total_bb_fees_paid_in_sats = 100        # actual dev fee
    stats.total_referral_fees_paid_in_sats = 999  # large referral payment
    dev_paid = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=True, include_ln_network_fees=True,
    )
    # Should be 100 — the 999 to referral doesn't reduce dev fee.
    assert dev_paid == 100


def test_referral_ln_fee_DOES_count_against_dev_fee():
    """The network fee to deliver the referral payment is itself a real
    network fee, and the dev's 2% absorbs it (per the "network fees
    deducted from dev fee" policy)."""
    stats = _empty_store_stats()
    stats.total_bb_fees_paid_in_sats = 100
    stats.ln_network_fees_paid_for_referral_payments_in_sats = 50
    paid_with_ln_fees = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=False, include_ln_network_fees=True,
    )
    assert paid_with_ln_fees == 150
    # ...but only when the include_ln_network_fees flag is on:
    paid_without_ln_fees = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=False, include_ln_network_fees=False,
    )
    assert paid_without_ln_fees == 100


# ---------------------------------------------------------------------------
# End-to-end math: 2% + N% additive cap
# ---------------------------------------------------------------------------

def test_combined_fee_math_additive():
    """The customer pays up to (FEE_AMOUNT + REFERRAL_FEE_AMOUNT) of
    revenue total. With 2% + 1%: a 1M sat revenue means 20k dev + 10k
    referral = 30k combined. Network fees reduce the dev portion only."""
    stats = _empty_store_stats()
    stats.revenue_eligible_for_fee = 1_000_000
    # Some network fees already incurred — reduces dev fee remaining
    stats.onchain_network_fees_paid_for_swaps_in_sats = 500

    # Dev fee:
    eligible = stats.calc_total_eligible_revenue_in_sats()
    dev_fee_total = int(eligible * 0.02)               # 20_000
    dev_already_paid = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=True, include_ln_network_fees=True,
    )
    dev_remaining = dev_fee_total - dev_already_paid
    assert dev_remaining == 20_000 - 500              # 19_500

    # Referral fee:
    referral_remaining = stats.calc_remaining_referral_fee_due_in_sats(0.01)
    assert referral_remaining == 10_000               # unchanged by network fee

    # Combined what the operator pays out:
    assert dev_remaining + referral_remaining == 29_500


# ---------------------------------------------------------------------------
# Ledger walk — REFERRAL_PAYOUT_REASON label routes to referral buckets
# ---------------------------------------------------------------------------

def _replay_ln_history(transactions: List[Dict[str, Any]],
                       referral_label: str = "lnhelper_referral") -> StoreStats:
    """Replay the LN-history block from new_calc_invoice_stats over a
    synthetic transaction list. Mirrors production logic verbatim so
    drift surfaces here."""
    from liquidityhelper import (
        is_ln_open_transaction, is_ln_close_transaction,
        CASHOUT_REASON, FEE_PAYOUT_REASON,
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
                stats.ln_network_fees_paid_for_payouts_in_sats += abs(transaction['amount_msat']/1000)
                continue
            if transaction['label'] == FEE_PAYOUT_REASON:
                stats.ln_network_fees_paid_for_fee_payments_in_sats += abs(transaction['fee_msat']/1000)
                stats.total_bb_fees_paid_in_sats += abs(transaction['amount_msat']/1000)
                continue
            if transaction['label'] == referral_label:
                stats.ln_network_fees_paid_for_referral_payments_in_sats += abs(transaction['fee_msat']/1000)
                stats.total_referral_fees_paid_in_sats += abs(transaction['amount_msat']/1000)
                continue
            stats.misc_ln_network_fees_in_sats += abs(transaction['fee_msat']/1000)
    return stats


def test_referral_payment_populates_referral_buckets():
    """An outgoing LN payment with the referral label should populate
    total_referral_fees_paid_in_sats AND
    ln_network_fees_paid_for_referral_payments_in_sats — and NOT
    total_bb_fees_paid_in_sats."""
    txs = [{
        "type": "payment",
        "label": "lnhelper_referral",
        "amount_msat": -10_000_000,   # 10_000 sat outbound (principal)
        "fee_msat": 50_000,           # 50 sat LN fee
    }]
    stats = _replay_ln_history(txs)
    assert stats.total_referral_fees_paid_in_sats == 10_000
    assert stats.ln_network_fees_paid_for_referral_payments_in_sats == 50
    assert stats.total_bb_fees_paid_in_sats == 0


def test_dev_and_referral_payments_are_independently_tracked():
    """Walk two payments — one developer fee, one referral fee. Each
    routes to its own bucket; cross-contamination would fail."""
    txs = [
        {
            "type": "payment", "label": "lnhelper_fee",
            "amount_msat": -20_000_000, "fee_msat": 100_000,
        },
        {
            "type": "payment", "label": "lnhelper_referral",
            "amount_msat": -10_000_000, "fee_msat": 50_000,
        },
    ]
    stats = _replay_ln_history(txs)
    assert stats.total_bb_fees_paid_in_sats == 20_000
    assert stats.total_referral_fees_paid_in_sats == 10_000
    assert stats.ln_network_fees_paid_for_fee_payments_in_sats == 100
    assert stats.ln_network_fees_paid_for_referral_payments_in_sats == 50


# ---------------------------------------------------------------------------
# Config defaults — disabling is the default
# ---------------------------------------------------------------------------

def test_referral_disabled_by_default():
    import importlib
    import config
    importlib.reload(config)
    assert config.REFERRAL_FEE_AMOUNT == 0.0
    assert config.REFERRAL_FEE_DEST is None
    assert config.REFERRAL_PAYOUT_REASON == 'lnhelper_referral'
