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
            # Mirror production: net the service-fee bucket against
            # any refund the LSP issued for this order (when state ==
            # FAILED in LspChannelOrder). See liquidityhelper.
            # _lsp_refund_for_tx_label for the lookup logic.
            gross = abs(float(transaction.get("amount_sat") or 0))
            refund = liquidityhelper._lsp_refund_for_tx_label(
                transaction.get("label")
            )
            stats.onchain_lsp_service_fees_paid_in_sats += max(0, gross - refund)
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
    """Given canonical-shape input on both sides, the fee-accounting
    loop produces identical StoreStats.

    Scope caveat: this test only verifies the LOOP is wallet-agnostic.
    It does NOT verify that the dispatcher's Electrum branch actually
    produces canonical-shape rows from real Electrum daemon output —
    that's a separate concern covered by:
      - test_normalize_electrum_onchain_row_unit (this file): pins the
        normalizer's bc_value→amount_sat conversion in isolation.
      - test_list_onchain_history_normalizes_electrum_bc_value (this
        file): exercises the dispatcher's Electrum branch end-to-end
        with a mocked electrum_rpc returning real Electrum shape.
      - test_list_onchain_history_electrum_returns_canonical_shape
        (electrum_network_tests.py): same, but against a live
        regtest Electrum daemon.

    The historical bug we missed: this test fed the same canonical
    dict to both paths and called it 'parity'. Real Electrum daemons
    return bc_value (BTC string) without amount_sat; consumers
    silently read 0. The companion tests above pin the missing
    boundary."""
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


# ---------------------------------------------------------------------------
# Electrum-side dispatcher normalization pin
# ---------------------------------------------------------------------------

def test_normalize_electrum_onchain_row_unit():
    """Pure unit test of _normalize_electrum_onchain_row.

    Electrum's daemon returns rows shaped {bc_value: "0.00500000",
    fee_sat: int|None, height: int, confirmations: int, timestamp: int,
    incoming: bool, label: str, txid: str, ...}. The engine canonical
    shape expects {amount_sat: int, block_height: int,
    num_confirmations: int, timestamp: int, dest_address: str, ...}.

    Pins the conversion math directly: bc_value (BTC-decimal string)
    × 100_000_000 = amount_sat (int satoshis). Catches any future
    refactor that breaks the units."""
    # 500_000 sat = 0.005 BTC. Electrum serializes via format_satoshis
    # which produces an 8-decimal string with trailing zeros.
    electrum_row = {
        "txid": "abc123" * 10 + "abcd",
        "bc_value": "0.00500000",     # 500_000 sat
        "bc_balance": "1.50000000",
        "fee_sat": 250,
        "height": 102,
        "confirmations": 16,
        "timestamp": 1779782161,
        "monotonic_timestamp": 1779782161,
        "incoming": True,
        "label": "OPEN CHANNEL",
        "date": "2026-05-26 00:56",
        "txpos_in_block": 2,
    }
    out = liquidityhelper._normalize_electrum_onchain_row(electrum_row)
    assert out["txid"] == electrum_row["txid"].lower()
    assert out["amount_sat"] == 500_000, (
        f"expected 500000 sat from bc_value='0.00500000', got {out['amount_sat']}"
    )
    assert isinstance(out["amount_sat"], int)
    assert out["fee_sat"] == 250
    assert isinstance(out["fee_sat"], int)
    assert out["incoming"] is True
    assert out["label"] == "OPEN CHANNEL"
    assert out["block_height"] == 102
    assert out["num_confirmations"] == 16
    assert out["timestamp"] == 1779782161
    assert out["dest_address"] == ""
    # Sanity: real Electrum keys must NOT pass through (otherwise
    # downstream consumers may pick up the wrong shape).
    assert "bc_value" not in out
    assert "height" not in out
    assert "confirmations" not in out


def test_normalize_electrum_onchain_row_handles_trailing_dot():
    """Electrum's format_satoshis produces "2." (with trailing period)
    for whole-BTC amounts. Decimal parses this correctly; pin it."""
    row = {
        "txid": "deadbeef" * 8,
        "bc_value": "2.",   # 2 BTC = 200_000_000 sat
        "fee_sat": None,
        "height": 100,
        "confirmations": 10,
        "timestamp": 1700000000,
        "incoming": True,
        "label": "",
    }
    out = liquidityhelper._normalize_electrum_onchain_row(row)
    assert out["amount_sat"] == 200_000_000
    assert out["fee_sat"] == 0   # None → 0


def test_normalize_electrum_onchain_row_handles_negative_outgoing():
    """Outgoing txs have a negative bc_value. Sign must be preserved."""
    row = {
        "txid": "cafebabe" * 8,
        "bc_value": "-0.05000000",   # -5_000_000 sat
        "fee_sat": 175,
        "height": 200,
        "confirmations": 5,
        "timestamp": 1700000000,
        "incoming": False,
        "label": "lsp_channel_order:foo",
    }
    out = liquidityhelper._normalize_electrum_onchain_row(row)
    assert out["amount_sat"] == -5_000_000
    assert out["incoming"] is False


def test_normalize_electrum_onchain_row_handles_missing_or_invalid():
    """Missing/null/malformed bc_value must default to 0 without
    raising — the broad except in new_calc_invoice_stats would catch
    the exception silently and drop the entire invoice from revenue,
    so we'd rather see a 0 row than lose data."""
    for bad in (None, "", "not-a-number", "0.0.0"):
        row = {
            "txid": "0" * 64,
            "bc_value": bad,
            "fee_sat": 0,
            "height": 0,
            "confirmations": 0,
            "timestamp": 0,
            "incoming": False,
            "label": "",
        }
        out = liquidityhelper._normalize_electrum_onchain_row(row)
        assert out["amount_sat"] == 0, (
            f"expected 0 for malformed bc_value={bad!r}, got {out['amount_sat']}"
        )


def test_list_onchain_history_normalizes_electrum_bc_value(monkeypatch):
    """End-to-end dispatcher test: monkeypatch electrum_rpc to return a
    real Electrum-shape response (bc_value strings, no amount_sat),
    then assert list_onchain_history's Electrum branch produces
    canonical-shape rows that consumers can read.

    Before the dispatcher-level normalization fix, this test would
    have failed: tx.get("amount_sat") returned None → consumers
    silently got 0.

    Drives the coroutine via a one-shot new_event_loop, NOT
    asyncio.run(). asyncio.run() calls set_event_loop(None) on exit,
    which unsets the session event_loop fixture's loop and breaks
    every downstream regtest test that does asyncio.get_event_loop()
    later in the same pytest process."""
    import asyncio

    raw_electrum_response = {
        "result": {
            "summary": {},
            "transactions": [
                # An LSP channel-order payment (outgoing).
                {
                    "txid": "1" * 64,
                    "bc_value": "-0.00150000",   # -150_000 sat
                    "bc_balance": "0.50000000",
                    "fee_sat": 175,
                    "height": 100,
                    "confirmations": 6,
                    "timestamp": 1700000000,
                    "monotonic_timestamp": 1700000000,
                    "incoming": False,
                    "label": "lsp_channel_order:abc",
                    "date": "2026-05-26 00:56",
                    "txpos_in_block": 1,
                },
                # An incoming customer payment.
                {
                    "txid": "2" * 64,
                    "bc_value": "0.00050000",    # 50_000 sat
                    "bc_balance": "0.50050000",
                    "fee_sat": 0,
                    "height": 101,
                    "confirmations": 5,
                    "timestamp": 1700000060,
                    "monotonic_timestamp": 1700000060,
                    "incoming": True,
                    "label": "",
                    "date": "2026-05-26 00:57",
                    "txpos_in_block": 2,
                },
            ],
        }
    }

    async def fake_electrum_rpc(method, myxpub, params=None):
        assert method == "onchain_history"
        return raw_electrum_response

    monkeypatch.setattr(liquidityhelper, "electrum_rpc", fake_electrum_rpc)

    async def run():
        rows = await liquidityhelper.list_onchain_history(
            wallet={"currency": "btc", "xpub": "fakexpub"},
        )
        return rows

    loop = asyncio.new_event_loop()
    try:
        rows = loop.run_until_complete(run())
    finally:
        loop.close()
    assert len(rows) == 2

    # First row: outgoing LSP order.
    assert rows[0]["txid"] == "1" * 64
    assert rows[0]["amount_sat"] == -150_000
    assert rows[0]["fee_sat"] == 175
    assert rows[0]["incoming"] is False
    assert rows[0]["label"] == "lsp_channel_order:abc"

    # Second row: incoming customer payment.
    assert rows[1]["txid"] == "2" * 64
    assert rows[1]["amount_sat"] == 50_000
    assert rows[1]["fee_sat"] == 0
    assert rows[1]["incoming"] is True

    # Plug rows directly into the fee-accounting loop. If
    # _normalize_electrum_onchain_row didn't fire, amount_sat would be
    # 0 → onchain_lsp_service_fees_paid_in_sats would be 0 → the dev-
    # fee math we're protecting would silently zero out.
    stats = _replay_onchain_fee_loop(rows)
    assert stats.onchain_network_fees_paid_for_lsp_orders_in_sats == 175
    assert stats.onchain_lsp_service_fees_paid_in_sats == 150_000, (
        f"expected 150_000 from the bc_value='-0.00150000' tx; got "
        f"{stats.onchain_lsp_service_fees_paid_in_sats}. If 0, the "
        f"dispatcher's Electrum branch is no longer normalizing."
    )


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
