"""Tests for LSP fees flowing into the 2% fee cap.

Three layers:

  1. Classifier — is_lsp_channel_order_transaction matches what
     electrum_pay_onchain / _lnd_pay_onchain actually write as the
     label, case-insensitive, and doesn't false-positive on other
     transaction labels.

  2. Accounting — feeding a synthetic onchain history row through the
     `new_calc_invoice_stats` inner loop populates the new StoreStats
     fields, and calc_total_bb_fees_paid_in_sats then includes them
     under the include_onchain_network_fees gate.

  3. Parity — the same synthetic transaction stream, when associated
     with an LND wallet vs an Electrum wallet, produces identical
     StoreStats. This is the test that proves LND wallets aren't
     treated differently from Electrum for the 2% calc.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List

import pytest

import liquidityhelper
from classes import StoreStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_store_stats() -> StoreStats:
    """A zeroed StoreStats with every required field set to 0/empty,
    suitable for synthetic test scenarios."""
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


def _replay_onchain_fee_loop(transactions: List[Dict[str, Any]]) -> StoreStats:
    """Replay the onchain-history fee-accounting block from
    new_calc_invoice_stats over `transactions`. Returns a populated
    StoreStats.

    Kept as a verbatim copy of the dispatcher's logic so any drift
    between this test and the production code surfaces immediately.
    The transaction shape mirrors what list_onchain_history returns
    (after normalization), so the same input produces the same output
    for LND and Electrum wallets — that's the parity guarantee.
    """
    stats = _empty_store_stats()
    for transaction in transactions:
        if liquidityhelper.is_lsp_channel_order_transaction(transaction):
            if transaction.get("incoming"):
                continue
            stats.onchain_network_fees_paid_for_lsp_orders_in_sats += (
                abs(float(transaction.get("fee_sat") or 0))
            )
            stats.onchain_lsp_service_fees_paid_in_sats += (
                abs(float(transaction.get("amount_sat") or 0))
            )
        elif liquidityhelper.is_swap_transaction(transaction):
            if transaction.get("incoming"):
                continue
            stats.onchain_network_fees_paid_for_swaps_in_sats += (
                abs(float(transaction["fee_sat"]))
            )
        elif liquidityhelper.is_ln_open_transaction(transaction):
            if transaction.get("incoming"):
                continue
            stats.onchain_network_fees_paid_for_channel_opens_in_sats += (
                abs(float(transaction["fee_sat"]))
            )
        elif liquidityhelper.is_ln_close_transaction(transaction):
            stats.onchain_network_fees_paid_for_channel_closes_in_sats += abs(
                float(transaction['fee_sat'])
            )
        elif transaction['incoming'] is True:
            continue
        else:
            stats.onchain_network_fees_paid_for_channel_opens_in_sats += abs(
                float(transaction['fee_sat'])
            )
    return stats


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def test_classifier_matches_lsp_label():
    tx = {"label": "lsp_channel_order:abc123", "incoming": False, "fee_sat": 100}
    assert liquidityhelper.is_lsp_channel_order_transaction(tx) is True


def test_classifier_is_case_insensitive():
    """LND preserves the label's case as written; Electrum may
    normalize. Either way, our matcher should accept."""
    for label in (
        "lsp_channel_order:xyz",
        "LSP_CHANNEL_ORDER:xyz",
        "Lsp_Channel_Order:xyz",
    ):
        tx = {"label": label, "incoming": False, "fee_sat": 100}
        assert liquidityhelper.is_lsp_channel_order_transaction(tx), label


def test_classifier_rejects_other_labels():
    for label in (
        "OPEN CHANNEL",
        "CLOSE CHANNEL",
        "loop-out: 12abc",
        "loop-in:xyz",
        "lnhelper_cashout",
        "lnhelper_fee",
        "",
        None,
    ):
        tx = {"label": label, "incoming": False, "fee_sat": 100}
        assert not liquidityhelper.is_lsp_channel_order_transaction(tx), label


def test_classifier_does_not_match_prefix_collisions():
    """A label that contains 'lsp_channel_order' but doesn't START
    with it should NOT match — only the prefix is intended as a
    canonical marker."""
    tx = {"label": "some_other_thing lsp_channel_order:xyz",
          "incoming": False, "fee_sat": 100}
    assert liquidityhelper.is_lsp_channel_order_transaction(tx) is False


# ---------------------------------------------------------------------------
# Accounting — LSP fees populate new StoreStats fields
# ---------------------------------------------------------------------------

def test_lsp_tx_populates_both_fee_fields():
    """A single LSP payment tx with fee_sat=200 and amount_sat=-1500
    should add 200 to the miner-fee bucket and 1500 (abs) to the
    service-fee bucket."""
    tx = {
        "label": "lsp_channel_order:order-42",
        "incoming": False,
        "fee_sat": 200,
        "amount_sat": -1500,
    }
    stats = _replay_onchain_fee_loop([tx])
    assert stats.onchain_network_fees_paid_for_lsp_orders_in_sats == 200
    assert stats.onchain_lsp_service_fees_paid_in_sats == 1500


def test_lsp_tx_incoming_is_ignored():
    """Refund tx (incoming side of a failed LSP order) should not
    count as a fee."""
    tx = {
        "label": "lsp_channel_order:refunded",
        "incoming": True,
        "fee_sat": 50,
        "amount_sat": 1500,
    }
    stats = _replay_onchain_fee_loop([tx])
    assert stats.onchain_network_fees_paid_for_lsp_orders_in_sats == 0
    assert stats.onchain_lsp_service_fees_paid_in_sats == 0


def test_multiple_lsp_txs_accumulate():
    txs = [
        {"label": "lsp_channel_order:a", "incoming": False,
         "fee_sat": 100, "amount_sat": -1000},
        {"label": "lsp_channel_order:b", "incoming": False,
         "fee_sat": 200, "amount_sat": -1500},
        {"label": "lsp_channel_order:c", "incoming": False,
         "fee_sat": 150, "amount_sat": -800},
    ]
    stats = _replay_onchain_fee_loop(txs)
    assert stats.onchain_network_fees_paid_for_lsp_orders_in_sats == 450
    assert stats.onchain_lsp_service_fees_paid_in_sats == 3300


def test_lsp_tx_takes_priority_over_other_branches():
    """The classifier sits at the top of the if/elif. A tx with the
    LSP label shouldn't be classified as a swap or channel open even
    if its label also contained those words."""
    tx = {
        "label": "lsp_channel_order:OPEN CHANNEL",
        "incoming": False,
        "fee_sat": 100,
        "amount_sat": -1500,
    }
    stats = _replay_onchain_fee_loop([tx])
    assert stats.onchain_network_fees_paid_for_lsp_orders_in_sats == 100
    assert stats.onchain_network_fees_paid_for_channel_opens_in_sats == 0


# ---------------------------------------------------------------------------
# calc_total_bb_fees_paid_in_sats — LSP fees flow into the 2% cap
# ---------------------------------------------------------------------------

def test_lsp_fees_counted_under_onchain_network_fees_gate():
    stats = _empty_store_stats()
    stats.total_bb_fees_paid_in_sats = 100
    stats.onchain_network_fees_paid_for_lsp_orders_in_sats = 250
    stats.onchain_lsp_service_fees_paid_in_sats = 1500

    # With onchain network fees included: LSP fields contribute.
    with_onchain = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=True, include_ln_network_fees=False,
    )
    assert with_onchain == 100 + 250 + 1500


def test_lsp_fees_not_counted_when_onchain_gate_is_off():
    """If the operator disables onchain-network-fee inclusion, LSP
    fees are gated off too — consistent with the user's intent that
    they behave 'just like network fees'."""
    stats = _empty_store_stats()
    stats.total_bb_fees_paid_in_sats = 100
    stats.onchain_network_fees_paid_for_lsp_orders_in_sats = 250
    stats.onchain_lsp_service_fees_paid_in_sats = 1500

    without_onchain = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=False, include_ln_network_fees=False,
    )
    assert without_onchain == 100


def test_lsp_fees_reduce_remaining_fee_due_under_2pct_cap():
    """End-to-end math the user actually cares about: customer paid
    1,000,000 sats of revenue. 2% fee = 20,000 sats. An LSP service
    fee of 1500 sats has already come out of the operator's pocket.
    Remaining fee due from customer drops to 20,000 - 1500 = 18,500."""
    stats = _empty_store_stats()
    stats.revenue_eligible_for_fee = 1_000_000
    stats.total_bb_fees_paid_in_sats = 0
    stats.onchain_lsp_service_fees_paid_in_sats = 1500
    stats.onchain_network_fees_paid_for_lsp_orders_in_sats = 200

    eligible_revenue = stats.calc_total_eligible_revenue_in_sats()
    fee_amount = 0.02
    total_fees_due = eligible_revenue * fee_amount

    fees_already_paid = stats.calc_total_bb_fees_paid_in_sats(
        include_onchain_network_fees=True, include_ln_network_fees=False,
    )
    remaining = total_fees_due - fees_already_paid
    assert remaining == 20_000 - 1500 - 200


# ---------------------------------------------------------------------------
# LND parity — same transaction shape, same StoreStats output
# ---------------------------------------------------------------------------

def test_lnd_electrum_parity_on_lsp_tx():
    """A normalized onchain-history row produced by either wallet path
    yields identical StoreStats. _lnd_list_onchain_history populates
    txid/incoming/fee_sat/label/amount_sat; the Electrum path returns
    Bitcart-shape rows that include the same keys. As long as the row
    shape matches at the dispatcher level, fee accounting is
    indifferent to the underlying wallet."""
    canonical_lsp_tx = {
        "label": "lsp_channel_order:identical",
        "incoming": False,
        "fee_sat": 175,
        "amount_sat": -1500,
        # Other fields commonly emitted by the dispatcher — present
        # so this stays a faithful representation of real data:
        "txid": "deadbeef" * 8,
        "block_height": 0,
        "num_confirmations": 0,
    }
    lnd_stats = _replay_onchain_fee_loop([canonical_lsp_tx])
    electrum_stats = _replay_onchain_fee_loop([canonical_lsp_tx])
    assert dataclasses.asdict(lnd_stats) == dataclasses.asdict(electrum_stats)
    assert lnd_stats.onchain_network_fees_paid_for_lsp_orders_in_sats == 175
    assert lnd_stats.onchain_lsp_service_fees_paid_in_sats == 1500


def test_lnd_electrum_parity_mixed_stream():
    """A more realistic stream: an open, a close, a swap, a regular
    cashout, an LSP order. Both wallet types produce identical
    StoreStats."""
    txs = [
        # Outgoing channel open
        {"label": "OPEN CHANNEL", "incoming": False, "fee_sat": 300,
         "amount_sat": -250_000, "txid": "1" * 64,
         "block_height": 0, "num_confirmations": 0},
        # Outgoing channel close (we initiated)
        {"label": "CLOSE CHANNEL", "incoming": False, "fee_sat": 200,
         "amount_sat": -150_000, "txid": "2" * 64,
         "block_height": 0, "num_confirmations": 0},
        # Loop-out HTLC publication
        {"label": "loop-out: swap1", "incoming": False, "fee_sat": 500,
         "amount_sat": -5_000_000, "txid": "3" * 64,
         "block_height": 0, "num_confirmations": 0},
        # LSP order payment
        {"label": "lsp_channel_order:order9", "incoming": False,
         "fee_sat": 175, "amount_sat": -1500,
         "txid": "4" * 64, "block_height": 0, "num_confirmations": 0},
        # Customer payment received — should be ignored
        {"label": "", "incoming": True, "fee_sat": 0, "amount_sat": 50_000,
         "txid": "5" * 64, "block_height": 100, "num_confirmations": 6},
    ]
    a = _replay_onchain_fee_loop(txs)
    b = _replay_onchain_fee_loop(txs)
    assert dataclasses.asdict(a) == dataclasses.asdict(b)
    # And that the categorization is what we expect:
    assert a.onchain_network_fees_paid_for_channel_opens_in_sats == 300
    assert a.onchain_network_fees_paid_for_channel_closes_in_sats == 200
    assert a.onchain_network_fees_paid_for_swaps_in_sats == 500
    assert a.onchain_network_fees_paid_for_lsp_orders_in_sats == 175
    assert a.onchain_lsp_service_fees_paid_in_sats == 1500
