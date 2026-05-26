"""
Network tests for the Electrum branches of every liquidityhelper dispatcher.

Mirror of network_tests.py with the Electrum wallet (talking to Fulcrum,
which talks to bitcoind regtest) as the wallet under test and an LND node
as the channel counterparty. Each test gets a fresh `lnd_electrum_pair`
fixture: its own bitcoind + Fulcrum + LND + Electrum daemon, with 0.2 BTC
channels open both ways and 6 confirmation blocks.

The `electrum_rpc` function in liquidityhelper is hardcoded to talk to
`http://localhost:5000` with basic auth `electrum:electrumz`. The fixture
configures the Electrum daemon to listen on exactly that endpoint, so
production code paths work unmodified.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import liquidityhelper


# wallet dict shape that exercises the Electrum dispatcher branch in every
# function under test. currency != "btclnd" routes to the Electrum path.
ELECTRUM_WALLET: Dict[str, Any] = {"currency": "btc", "xpub": ""}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mine_and_wait(lnd_electrum_pair, blocks: int = 1, sleep_s: float = 1.0):
    lnd_electrum_pair.bitcoind.mine_to_self(blocks)
    return asyncio.sleep(sleep_s)


# ---------------------------------------------------------------------------
# Sanity smoke test (sets up + tears down the full fixture, no business logic).
# ---------------------------------------------------------------------------


def test_lnd_electrum_pair_fixture_smoke(lnd_electrum_pair):
    async def run():
        chans = await lnd_electrum_pair.lnd.list_channels()
        electrum_pk = lnd_electrum_pair.electrum.identity_pubkey
        to_electrum = [
            c for c in chans
            if c.get("remote_pubkey") == electrum_pk and c.get("active")
        ]
        assert len(to_electrum) >= 2, (
            f"expected 2 active channels LND<->Electrum, got: {chans}"
        )

    _run(run())


# ---------------------------------------------------------------------------
# attempt_cooperative_close  (Electrum dispatch path)
# ---------------------------------------------------------------------------


def test_attempt_cooperative_close_electrum(lnd_electrum_pair):
    """Cooperatively close the Electrum->LND channel through the Electrum
    branch of attempt_cooperative_close, then verify Electrum no longer
    sees the channel as OPEN."""
    ep = lnd_electrum_pair

    async def run():
        cp = ep.electrum_to_lnd_channel_point

        before = ep.electrum.list_channels()
        assert any(c.get("channel_point") == cp and c.get("state") == "OPEN"
                   for c in before), f"channel {cp} not OPEN pre-close: {before}"

        close_result = await liquidityhelper.attempt_cooperative_close(
            channel_point=cp, wallet=ELECTRUM_WALLET,
        )
        # Diagnostic: surface what the close call returned so test failures
        # explain themselves.
        print(f"[test] close_result = {close_result!r}")

        # Mine plenty to drive Electrum's channel state machine through
        # OPEN -> CLOSING -> CLOSED -> REDEEMED.
        ep.bitcoind.mine_to_self(10)
        last_states: Dict[str, str] = {}
        for _ in range(60):
            after = ep.electrum.list_channels()
            last_states = {c.get("channel_point"): c.get("state") for c in after}
            if last_states.get(cp) != "OPEN":
                return
            await asyncio.sleep(0.5)
        raise AssertionError(
            f"channel {cp} still OPEN 30s+10 blocks after close; "
            f"close_result={close_result!r}, last_states={last_states}"
        )

    _run(run())


# ---------------------------------------------------------------------------
# find_offline_channels  (Electrum dispatch path)
# ---------------------------------------------------------------------------


def test_find_offline_channels_records_electrum(lnd_electrum_pair):
    """find_offline_channels (Electrum dispatch path): stop LND so Electrum
    sees the channel peer as DISCONNECTED, then call find_offline_channels.
    The function should record a failed uptime sample on the peer's
    LightningNode row. It does NOT initiate any close — that decision lives
    in the daily audit_existing_peer pipeline (see find_offline_channels
    docstring). This test pins the sample-recording responsibility."""
    ep = lnd_electrum_pair
    lnd_pubkey = ep.lnd.identity_pubkey.lower()

    async def run():
        # Knock LND offline so Electrum sees the channel peer as DISCONNECTED.
        await ep.lnd.stop_grpc()
        ep.lnd.stop()
        # Give Electrum a moment to register the disconnect.
        for _ in range(20):
            chans = ep.electrum.list_channels()
            if any(c.get("peer_state") == "DISCONNECTED" for c in chans
                   if c.get("state") == "OPEN"):
                break
            await asyncio.sleep(0.5)

        # Run the function under test. find_offline_channels creates a
        # defensive LightningNode row if one doesn't exist (the autouse
        # fixture in conftest.py wipes this table between tests).
        await liquidityhelper.find_offline_channels(wallet=ELECTRUM_WALLET)

        # Verify the sample landed: LND's peer row has a failed uptime
        # check recorded (both rolling and lifetime). No LightningChannel
        # rows are written by find_offline_channels — close is the daily
        # audit pipeline's job, not this function's.
        from node_database import LightningNode
        row = LightningNode.get_or_none(
            LightningNode.node_address == lnd_pubkey
        )
        assert row is not None, (
            f"find_offline_channels did not create a LightningNode row "
            f"for disconnected peer {lnd_pubkey}"
        )
        assert row.failed_uptime_checks >= 1, (
            f"expected failed_uptime_checks >= 1, got "
            f"{row.failed_uptime_checks}"
        )
        assert row.recent_failed_uptime_checks >= 1, (
            f"expected recent_failed_uptime_checks >= 1, got "
            f"{row.recent_failed_uptime_checks}"
        )

    _run(run())


# ---------------------------------------------------------------------------
# find_channel_closings  (Electrum dispatch path)
# ---------------------------------------------------------------------------


def test_find_channel_closings_electrum_always_empty(lnd_electrum_pair):
    """find_channel_closings (Electrum dispatch path) deliberately returns
    {} for ALL Electrum wallets — Electrum's list_channels doesn't expose
    close_initiator, so we'd misattribute operator-initiated closes as
    remote-closes and self-trigger the REMOTE_CLOSE_COUNT blacklist (see
    find_channel_closings docstring). This test pins that intentional
    behavior: even after a coop close that actually completes, the
    function still returns {} for an Electrum wallet."""
    ep = lnd_electrum_pair

    async def run():
        baseline = await liquidityhelper.find_channel_closings(wallet=ELECTRUM_WALLET)
        assert baseline == {}, f"expected {{}}, got {baseline}"

        # Close a channel and mine confirmations; the function must still
        # return {} for the Electrum wallet — that's the contract.
        await liquidityhelper.attempt_cooperative_close(
            channel_point=ep.electrum_to_lnd_channel_point, wallet=ELECTRUM_WALLET,
        )
        ep.bitcoind.mine_to_self(110)

        # Give the close some time to mature, then assert {} still.
        for _ in range(10):
            await asyncio.sleep(0.5)
        counts = await liquidityhelper.find_channel_closings(wallet=ELECTRUM_WALLET)
        assert counts == {}, (
            f"Electrum dispatch path must return {{}} regardless of actual "
            f"closures, but got {counts}"
        )

    _run(run())


# ---------------------------------------------------------------------------
# electrum_pay_ln_invoice  (Electrum dispatch path)
# ---------------------------------------------------------------------------


def test_electrum_pay_ln_invoice_electrum(lnd_electrum_pair):
    """LND generates a 2000-sat invoice; Electrum pays it via the
    electrum_pay_ln_invoice Electrum path. Verifies True returned + LND
    sees the invoice settled."""
    ep = lnd_electrum_pair

    async def run():
        from lnd_proto import lightning_pb2
        add_resp = await ep.lnd._stub.AddInvoice(
            lightning_pb2.Invoice(value=2000, memo="electrum-side-test")
        )
        bolt11 = add_resp.payment_request
        assert bolt11

        ok = await liquidityhelper.electrum_pay_ln_invoice(
            invoice=bolt11, label="electrum-test-label",
            wallet=ELECTRUM_WALLET,
        )
        assert ok is True

        for _ in range(30):
            inv = await ep.lnd._stub.LookupInvoice(
                lightning_pb2.PaymentHash(r_hash=add_resp.r_hash)
            )
            if inv.state == lightning_pb2.Invoice.SETTLED:
                assert int(inv.amt_paid_sat) == 2000
                return
            await asyncio.sleep(0.5)
        raise AssertionError("LND's invoice never settled within 15s")

    _run(run())


# ---------------------------------------------------------------------------
# electrum_pay_onchain  (Electrum dispatch path)
# ---------------------------------------------------------------------------


def test_electrum_pay_onchain_electrum(lnd_electrum_pair):
    """Electrum sends 50_000 sat on-chain to an LND-controlled address with a
    custom label; verifies True + LND sees the new UTXO after a block."""
    ep = lnd_electrum_pair

    async def run():
        dest_addr = await ep.lnd.new_address()
        before_balance = await ep.lnd.wallet_balance_sats()

        ok = await liquidityhelper.electrum_pay_onchain(
            dest_addr=dest_addr, label="onchain-electrum-test",
            amount=0.00050000, wallet=ELECTRUM_WALLET,
        )
        assert ok is True

        ep.bitcoind.mine_to_self(2)
        for _ in range(30):
            after_balance = await ep.lnd.wallet_balance_sats()
            if after_balance >= before_balance + 50_000 - 1000:  # rough fee allowance
                return
            await asyncio.sleep(0.5)
        raise AssertionError(
            f"LND balance didn't increase by ~50_000 sat: "
            f"before={before_balance} after={after_balance}"
        )

    _run(run())


# ---------------------------------------------------------------------------
# list_onchain_history  (Electrum dispatch path)
# ---------------------------------------------------------------------------


def test_list_onchain_history_electrum(lnd_electrum_pair):
    """Electrum's on-chain history surfaces the channel funding tx with the
    'OPEN CHANNEL' label (Electrum auto-labels these), which is what
    is_ln_open_transaction in liquidityhelper looks for to attribute fees."""
    ep = lnd_electrum_pair

    async def run():
        funding_txid = ep.electrum_to_lnd_channel_point.split(":")[0].lower()
        rows = await liquidityhelper.list_onchain_history(wallet=ELECTRUM_WALLET)
        assert isinstance(rows, list) and rows, "no on-chain history returned"

        opens = [r for r in rows
                 if (r.get("label") or "").upper().startswith("OPEN CHANNEL")]
        # Find our funding tx in the on-chain history (Electrum's txid field
        # may be 'txid' or 'tx_hash' depending on version — accept either).
        found = any(
            (r.get("txid") or r.get("tx_hash") or "").lower() == funding_txid
            for r in opens
        )
        assert found, (
            f"funding tx {funding_txid} not flagged OPEN CHANNEL in history: "
            f"sample row keys = {list(rows[0].keys()) if rows else 'N/A'}"
        )

    _run(run())


# ---------------------------------------------------------------------------
# list_ln_payments_with_labels  (Electrum dispatch path)
# ---------------------------------------------------------------------------


def test_list_ln_payments_with_labels_electrum(lnd_electrum_pair):
    """After Electrum pays a labeled invoice from LND,
    list_ln_payments_with_labels should return a row for that payment with
    the label preserved (Electrum stores labels natively for LN payments)."""
    ep = lnd_electrum_pair
    custom_label = "electrum-cashout-label-xyz"

    async def run():
        from lnd_proto import lightning_pb2
        add_resp = await ep.lnd._stub.AddInvoice(
            lightning_pb2.Invoice(value=1500, memo="for-history-test")
        )
        bolt11 = add_resp.payment_request

        ok = await liquidityhelper.electrum_pay_ln_invoice(
            invoice=bolt11, label=custom_label, wallet=ELECTRUM_WALLET,
        )
        assert ok is True

        for _ in range(20):
            rows = await liquidityhelper.list_ln_payments_with_labels(
                wallet=ELECTRUM_WALLET,
            )
            labeled = [r for r in rows if r.get("label") == custom_label]
            if labeled:
                r = labeled[0]
                assert r.get("type") == "payment"
                # Outgoing payments are reported as negative msat
                assert int(r.get("amount_msat", 0)) < 0
                return
            await asyncio.sleep(0.5)
        raise AssertionError(
            f"no LN history row tagged {custom_label!r} found: {rows[:3]}"
        )

    _run(run())
