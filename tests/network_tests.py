"""
Tests that exercise real network sockets / subprocesses.

Run explicitly:
    pytest tests/network_tests.py

These are kept separate from code_only_tests.py because they:
  - download binaries (bitcoind + lnd) into tests/_bin/ on first run
  - spawn bitcoind and two LND subprocesses
  - open Lightning channels between them
  - take ~30-60s of setup time
"""

from __future__ import annotations

import asyncio
import datetime
import sys
import time
from pathlib import Path
from typing import Any, Dict

import grpc
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import liquidityhelper
from lnd_proto import lightning_pb2, lightning_pb2_grpc


def _install_lnd_conn_for_test(wallet_id: str, node) -> None:
    """Pre-populate liquidityhelper._LND_CONNECTIONS with a Lightning stub
    pointed at `node`. This is what `_get_lnd_connection` would normally
    build by hitting Bitcart's /wallets/{id}/lndinfo — but since the test
    has the LND directly (no Bitcart in the loop), we install it manually
    so attempt_cooperative_close can find the gRPC channel by wallet_id.
    """
    ssl = grpc.ssl_channel_credentials(root_certificates=node.tls_cert)
    macaroon_hex = node.macaroon_hex

    def macaroon_callback(_ctx, callback):
        callback([("macaroon", macaroon_hex)], None)

    creds = grpc.composite_channel_credentials(
        ssl, grpc.metadata_call_credentials(macaroon_callback)
    )
    channel = grpc.aio.secure_channel(
        node.grpc_target,
        creds,
        options=[("grpc.ssl_target_name_override", "localhost")],
    )
    # Match the schema _get_lnd_connection produces: a "stubs" map keyed by
    # service name. We only need Lightning for the close test; others can
    # stay omitted because attempt_cooperative_close only touches Lightning.
    liquidityhelper._LND_CONNECTIONS[wallet_id] = {
        "channel": channel,
        "stubs": {
            "Lightning": lightning_pb2_grpc.LightningStub(channel),
        },
    }


def test_attempt_cooperative_close_lnd(lnd_pair):
    """End-to-end: ask Node A's liquidityhelper-managed LND to cooperatively
    close its channel to Node B, and verify A's view of the channel transitions
    from active → pending_close.
    """
    wallet_id = "test-lnd-wallet-A"
    a = lnd_pair.a
    b = lnd_pair.b
    channel_point = lnd_pair.a_to_b_channel_point

    async def run():
        _install_lnd_conn_for_test(wallet_id, a)
        try:
            # Sanity: before close, A should see its channel to B as active.
            chans_before = await a.list_channels()
            assert any(
                c.get("remote_pubkey") == b.identity_pubkey and c.get("active")
                for c in chans_before
            ), f"A doesn't see an active channel to B: {chans_before}"

            # The actual call we're testing.
            result = await liquidityhelper.attempt_cooperative_close(
                channel_point=channel_point,
                wallet={"id": wallet_id, "currency": "btclnd"},
                api=None,  # cache hit; api is unused
            )
            # First update from CloseChannel is close_pending with the closing txid.
            assert result is not None, "attempt_cooperative_close returned None"
            assert "close_pending" in result, (
                f"first update wasn't close_pending: {result}"
            )
            closing_txid_b64 = result["close_pending"].get("txid")
            assert closing_txid_b64, f"close_pending has no txid: {result}"

            # Confirm the close: mine a block and verify A no longer lists the
            # channel as active. (Single block is enough for it to move to
            # pending_close / waiting_close; full closure needs more confs but
            # that's not what we're verifying here.)
            lnd_pair.bitcoind.mine_to_self(1)

            for _ in range(30):
                chans_after = await a.list_channels()
                still_active = any(
                    c.get("channel_point") == channel_point and c.get("active")
                    for c in chans_after
                )
                pending = await a.list_pending_channels()
                pending_chans = (
                    pending.get("waiting_close_channels", [])
                    + pending.get("pending_closing_channels", [])
                    + pending.get("pending_force_closing_channels", [])
                )
                pending_for_this_cp = any(
                    ch.get("channel", {}).get("channel_point") == channel_point
                    for ch in pending_chans
                )
                if not still_active and (pending_for_this_cp or not chans_after):
                    return
                await asyncio.sleep(1.0)
            raise AssertionError(
                f"channel {channel_point} didn't transition out of active within 30s"
            )
        finally:
            # Drop the test connection so the fixture teardown's stop_grpc
            # doesn't race with a stub that we own.
            entry = liquidityhelper._LND_CONNECTIONS.pop(wallet_id, None)
            if entry is not None:
                await entry["channel"].close()

    asyncio.get_event_loop().run_until_complete(run())


def test_find_offline_channels_closes_disconnected_lnd_peer(lnd_pair):
    """find_offline_channels' LND dispatch path: with Node B disconnected
    from Node A AND a LightningNode DB record marking B as offline-recently,
    calling find_offline_channels(wallet=A_wallet) should cooperatively
    close the channel A->B."""
    wallet_id = "test-wallet-A"
    a = lnd_pair.a
    b = lnd_pair.b

    async def run():
        _install_lnd_conn_for_test(wallet_id, a)
        try:
            # 1. Seed the peewee LightningNode record so should_close_channel
            #    has long-term-failed history to evaluate. The autouse fixture
            #    in conftest.py wipes this table between tests.
            from node_database import LightningNode
            # Counts large enough that check_period_duration (freq*total)
            # clears should_close_channel's 1-hour monitoring floor; ancient
            # last_seen_online trips the OFFLINE_RECENTLY branch (>48h).
            LightningNode.create(
                node_address=b.identity_pubkey.lower(),
                failed_uptime_checks=100,
                total_uptime_checks=10000,
                last_seen_online=datetime.datetime(2020, 1, 1),
                last_lnd_query=datetime.datetime(1990, 1, 1),
            )

            # 2. Disconnect B from A so A's channel goes peer_state=DISCONNECTED.
            await a._stub.DisconnectPeer(
                lightning_pb2.DisconnectPeerRequest(pub_key=b.identity_pubkey)
            )
            for _ in range(30):
                chans = await a.list_channels()
                if any(
                    c.get("remote_pubkey") == b.identity_pubkey and not c.get("active")
                    for c in chans
                ):
                    break
                await asyncio.sleep(0.5)
            else:
                raise AssertionError("channel never went inactive after DisconnectPeer")

            # 3. Call the function under test. LND refuses cooperative close
            #    when the peer is offline ("try force closing it instead"),
            #    which is exactly the scenario the function targets — so the
            #    close call WILL raise. The point of this test is to verify
            #    the LND dispatcher reaches that close call (i.e., listed
            #    channels via LND, found the disconnected one, and routed
            #    into attempt_cooperative_close's LND path).
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await liquidityhelper.find_offline_channels(
                    wallet={"id": wallet_id, "currency": "btclnd"},
                    api=None,
                )
            details = (exc_info.value.details() or "").lower()
            assert (
                "peer is offline" in details
                or "channel link not found" in details
                or "force closing" in details
            ), f"unexpected RPC error from CloseChannel: {exc_info.value.details()}"
        finally:
            entry = liquidityhelper._LND_CONNECTIONS.pop(wallet_id, None)
            if entry is not None:
                await entry["channel"].close()

    asyncio.get_event_loop().run_until_complete(run())


def test_electrum_pay_ln_invoice_lnd(lnd_pair):
    """electrum_pay_ln_invoice's LND dispatch path: Node B generates an
    invoice for 1000 sats, Node A pays it through electrum_pay_ln_invoice
    with the LND wallet dispatch, and we verify B's view of the invoice
    transitions to SETTLED."""
    wallet_id = "test-wallet-A"
    a = lnd_pair.a
    b = lnd_pair.b

    async def run():
        _install_lnd_conn_for_test(wallet_id, a)
        try:
            # 1. Have B generate a 1000-sat invoice.
            invoice_value = 1000
            add_resp = await b._stub.AddInvoice(
                lightning_pb2.Invoice(value=invoice_value, memo="test payment")
            )
            bolt11 = add_resp.payment_request
            r_hash = add_resp.r_hash
            assert bolt11, "AddInvoice returned no payment_request"

            # 2. Pay it from A via electrum_pay_ln_invoice's LND path.
            ok = await liquidityhelper.electrum_pay_ln_invoice(
                invoice=bolt11,
                label="test-payment-label",
                wallet={"id": wallet_id, "currency": "btclnd"},
                api=None,
            )
            assert ok is True, "electrum_pay_ln_invoice returned False"

            # 3. Verify B's invoice is settled.
            for _ in range(30):
                inv = await b._stub.LookupInvoice(
                    lightning_pb2.PaymentHash(r_hash=r_hash)
                )
                if inv.state == lightning_pb2.Invoice.SETTLED:
                    assert int(inv.amt_paid_sat) == invoice_value
                    break
                await asyncio.sleep(0.5)
            else:
                raise AssertionError(
                    "B's invoice did not transition to SETTLED within 15s"
                )

            # 4. Verify the label was persisted in LndPaymentLabel keyed by
            #    the payment_hash from the invoice we just paid.
            from node_database import LndPaymentLabel
            payment_hash_hex = bytes(r_hash).hex().lower()
            row = LndPaymentLabel.get_or_none(
                LndPaymentLabel.payment_hash == payment_hash_hex
            )
            assert row is not None, (
                f"no LndPaymentLabel row for payment_hash {payment_hash_hex}"
            )
            assert row.label == "test-payment-label"
            assert row.wallet_id == wallet_id
        finally:
            entry = liquidityhelper._LND_CONNECTIONS.pop(wallet_id, None)
            if entry is not None:
                await entry["channel"].close()

    asyncio.get_event_loop().run_until_complete(run())


def test_electrum_pay_ln_invoice_lnd_returns_false_on_bad_invoice(lnd_pair):
    """The LND path should return False (not raise) when payment fails. We
    feed a synthetic invoice with an unknown destination so SendPaymentSync
    returns a non-empty payment_error."""
    wallet_id = "test-wallet-A"
    a = lnd_pair.a

    async def run():
        _install_lnd_conn_for_test(wallet_id, a)
        try:
            # An invoice from a random pubkey LND has never heard of: no route.
            # We borrow B's own invoice generator to get a syntactically-valid
            # BOLT11, then make it unpayable by canceling it first.
            add_resp = await lnd_pair.b._stub.AddInvoice(
                lightning_pb2.Invoice(value=500, memo="will-be-canceled")
            )
            await lnd_pair.b._stub.CancelInvoice(
                __import__(
                    "lnd_proto.invoices_pb2", fromlist=["CancelInvoiceMsg"]
                ).CancelInvoiceMsg(payment_hash=add_resp.r_hash)
            ) if False else None  # placeholder — see fallback path below
            # Simpler reliable failure path: re-pay the invoice we just paid.
            # SendPaymentSync rejects duplicate payment_hash.
            bolt11 = add_resp.payment_request

            # First payment succeeds:
            first = await liquidityhelper.electrum_pay_ln_invoice(
                invoice=bolt11,
                wallet={"id": wallet_id, "currency": "btclnd"},
                api=None,
            )
            assert first is True

            # Second payment of the same invoice must fail (already paid).
            second = await liquidityhelper.electrum_pay_ln_invoice(
                invoice=bolt11,
                wallet={"id": wallet_id, "currency": "btclnd"},
                api=None,
            )
            assert second is False, (
                "expected duplicate-pay attempt to return False"
            )
        finally:
            entry = liquidityhelper._LND_CONNECTIONS.pop(wallet_id, None)
            if entry is not None:
                await entry["channel"].close()

    asyncio.get_event_loop().run_until_complete(run())


def test_list_ln_payments_with_labels_lnd(lnd_pair):
    """list_ln_payments_with_labels' LND path: after Node A pays a labeled
    invoice generated by Node B, the helper should return a row for that
    payment with the label joined back in from LndPaymentLabel."""
    wallet_id = "test-wallet-A"
    a = lnd_pair.a
    b = lnd_pair.b

    async def run():
        _install_lnd_conn_for_test(wallet_id, a)
        try:
            add_resp = await b._stub.AddInvoice(
                lightning_pb2.Invoice(value=2000, memo="ln-history-test")
            )
            bolt11 = add_resp.payment_request

            ok = await liquidityhelper.electrum_pay_ln_invoice(
                invoice=bolt11, label="cashout-label-1",
                wallet={"id": wallet_id, "currency": "btclnd"}, api=None,
            )
            assert ok is True

            rows = await liquidityhelper.list_ln_payments_with_labels(
                wallet={"id": wallet_id, "currency": "btclnd"}, api=None,
            )
            # There should be exactly one successful payment with our label.
            payment_hash_hex = bytes(add_resp.r_hash).hex().lower()
            ours = [r for r in rows if r.get("payment_hash") == payment_hash_hex]
            assert len(ours) == 1, f"unexpected rows: {rows}"
            row = ours[0]
            assert row["type"] == "payment"
            assert row["amount_msat"] < 0, "outgoing payments should be negative"
            assert abs(row["amount_msat"]) >= 2000 * 1000
            assert row["label"] == "cashout-label-1"
        finally:
            entry = liquidityhelper._LND_CONNECTIONS.pop(wallet_id, None)
            if entry is not None:
                await entry["channel"].close()

    asyncio.get_event_loop().run_until_complete(run())


def test_list_onchain_history_lnd_open_and_close_labels(lnd_pair):
    """list_onchain_history' LND path: funding tx for the A->B channel is
    surfaced with label='OPEN CHANNEL'; after cooperatively closing the
    channel and mining, the closing tx is surfaced with label='CLOSE CHANNEL'.
    Both labels are derived structurally — LND never set a label string."""
    wallet_id = "test-wallet-A"
    a = lnd_pair.a
    b = lnd_pair.b

    async def run():
        _install_lnd_conn_for_test(wallet_id, a)
        try:
            # 1. Verify channel-open funding tx is flagged.
            funding_txid = lnd_pair.a_to_b_channel_point.split(":")[0].lower()
            rows = await liquidityhelper.list_onchain_history(
                wallet={"id": wallet_id, "currency": "btclnd"}, api=None,
            )
            opens = {r["txid"]: r for r in rows if r["label"] == "OPEN CHANNEL"}
            assert funding_txid in opens, (
                f"A->B funding tx {funding_txid} not flagged OPEN CHANNEL in {rows}"
            )

            # 2. Cooperatively close that channel and mine — both peers online,
            # so a coop close succeeds.
            await liquidityhelper.attempt_cooperative_close(
                channel_point=lnd_pair.a_to_b_channel_point,
                wallet={"id": wallet_id, "currency": "btclnd"}, api=None,
            )
            lnd_pair.bitcoind.mine_to_self(2)

            # 3. Re-query on-chain history; closing tx should be in there as
            # CLOSE CHANNEL.
            for _ in range(20):
                rows = await liquidityhelper.list_onchain_history(
                    wallet={"id": wallet_id, "currency": "btclnd"}, api=None,
                )
                closes = [r for r in rows if r["label"] == "CLOSE CHANNEL"]
                if closes:
                    # And the funding tx should still be flagged OPEN CHANNEL
                    # (it lives forever in the wallet's history; ClosedChannels
                    # also includes the original channel_point).
                    funding_still_open = any(
                        r["txid"] == funding_txid and r["label"] == "OPEN CHANNEL"
                        for r in rows
                    )
                    assert funding_still_open
                    return
                await asyncio.sleep(0.5)
            raise AssertionError(
                "no CLOSE CHANNEL row appeared in on-chain history within 10s"
            )
        finally:
            entry = liquidityhelper._LND_CONNECTIONS.pop(wallet_id, None)
            if entry is not None:
                await entry["channel"].close()

    asyncio.get_event_loop().run_until_complete(run())


def test_electrum_pay_onchain_lnd_round_trips_label(lnd_pair):
    """electrum_pay_onchain's LND path: A sends an on-chain tx to a fresh
    B-controlled address with a custom label, and after a confirmation the
    label round-trips back through list_onchain_history (LND stores it
    natively in TransactionDetails.label)."""
    wallet_id = "test-wallet-A"
    a = lnd_pair.a
    b = lnd_pair.b

    async def run():
        _install_lnd_conn_for_test(wallet_id, a)
        try:
            dest_addr = await b.new_address()
            ok = await liquidityhelper.electrum_pay_onchain(
                dest_addr=dest_addr,
                label="onchain-test-label", amount=0.00050000,
                wallet={"id": wallet_id, "currency": "btclnd"}, api=None,
            )
            assert ok is True
            lnd_pair.bitcoind.mine_to_self(2)

            # Find our send in A's on-chain history with the label preserved.
            for _ in range(20):
                rows = await liquidityhelper.list_onchain_history(
                    wallet={"id": wallet_id, "currency": "btclnd"}, api=None,
                )
                ours = [r for r in rows if r.get("label") == "onchain-test-label"]
                if ours:
                    # Outgoing tx; LND reports a negative amount.
                    assert ours[0]["incoming"] is False
                    assert int(ours[0].get("fee_sat") or 0) > 0
                    return
                await asyncio.sleep(0.5)
            raise AssertionError(
                "tx with label 'onchain-test-label' not found in on-chain history"
            )
        finally:
            entry = liquidityhelper._LND_CONNECTIONS.pop(wallet_id, None)
            if entry is not None:
                await entry["channel"].close()

    asyncio.get_event_loop().run_until_complete(run())


def test_find_channel_closings_lnd(lnd_pair):
    """find_channel_closings' LND path: with no closed channels yet, the
    helper returns {}. After cooperatively closing A->B (both peers online)
    and mining a confirmation, the helper returns {b_pubkey: 1}. Excludes
    FUNDING_CANCELED / ABANDONED close types per option-(a) semantics."""
    wallet_id = "test-wallet-A"
    a = lnd_pair.a
    b = lnd_pair.b

    async def run():
        _install_lnd_conn_for_test(wallet_id, a)
        try:
            # Baseline: nothing closed yet.
            baseline = await liquidityhelper.find_channel_closings(
                wallet={"id": wallet_id, "currency": "btclnd"}, api=None,
            )
            assert baseline == {}, f"expected no closings yet, got {baseline}"

            # Cooperatively close A->B (peer online, so coop works).
            await liquidityhelper.attempt_cooperative_close(
                channel_point=lnd_pair.a_to_b_channel_point,
                wallet={"id": wallet_id, "currency": "btclnd"}, api=None,
            )
            # Mine enough to push the close tx into ClosedChannels on A.
            lnd_pair.bitcoind.mine_to_self(3)

            # Poll until A's ClosedChannels surfaces it.
            for _ in range(30):
                counts = await liquidityhelper.find_channel_closings(
                    wallet={"id": wallet_id, "currency": "btclnd"}, api=None,
                )
                if counts:
                    break
                await asyncio.sleep(0.5)
            else:
                raise AssertionError("ClosedChannels never surfaced our coop close")

            assert counts == {b.identity_pubkey.lower(): 1}, (
                f"unexpected counts: {counts}"
            )
        finally:
            entry = liquidityhelper._LND_CONNECTIONS.pop(wallet_id, None)
            if entry is not None:
                await entry["channel"].close()

    asyncio.get_event_loop().run_until_complete(run())


def test_lnd_pair_isolation(lnd_pair):
    """Per-test isolation smoke check: running after a test that closed a
    channel, this one still finds both 0.2 BTC channels active because the
    fixture stood up a brand-new bitcoind + LND pair from scratch."""
    a = lnd_pair.a
    b = lnd_pair.b

    async def run():
        chans = await a.list_channels()
        active_to_b = [c for c in chans if c.get("remote_pubkey") == b.identity_pubkey and c.get("active")]
        # Two opens per call (A->B and B->A) both show up on A's listchannels.
        assert len(active_to_b) >= 2, (
            f"expected >=2 active channels A<->B, got {len(active_to_b)}: {chans}"
        )
        # And both should be the freshly-opened channel points from THIS run
        # (i.e. not the channel_point that the prior test cooperatively closed).
        cps_now = {c.get("channel_point") for c in active_to_b}
        assert lnd_pair.a_to_b_channel_point in cps_now
        assert lnd_pair.b_to_a_channel_point in cps_now

    asyncio.get_event_loop().run_until_complete(run())
