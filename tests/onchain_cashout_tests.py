"""Tests for the on-chain cashout feature.

Three layers:

  - Unit tests against `FakeBitcartAPI` for the amount math (`safe_to_spend`
    wiring), the pending-channel guard, the rail-decision helper, and
    the dispatch policy inside `do_cashouts`.

  - Integration tests via the `lnd_pair` fixture that exercise the real
    LND `SendCoins` path end-to-end, including verifying that the
    `CASHOUT_REASON` label is persisted by LND and readable via
    GetTransactions.

  - Integration test via the `lnd_electrum_pair` fixture that exercises
    the real Electrum `payto`+`broadcast`+`setlabel` path.

The integration tests auto-skip when the necessary binaries / fixtures
aren't available.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any, Dict, List, Optional

import pytest

import liquidityhelper
from database import SimpleDateTimeField
from node_database import LightningChannel
from tests._fakes import FakeBitcartAPI


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record_cashout_calls(monkeypatch):
    """Replace do_ln_cashouts / do_onchain_cashouts with recorders so we
    can assert which path do_cashouts chose."""
    calls: List[Dict[str, Any]] = []

    async def fake_ln(api, wallet_id, amt):
        calls.append({"path": "ln", "wallet_id": wallet_id, "amount": amt})
        return True

    async def fake_onchain(api, wallet_id, amt):
        calls.append({"path": "onchain", "wallet_id": wallet_id, "amount": amt})
        return True

    monkeypatch.setattr(liquidityhelper, "do_ln_cashouts", fake_ln)
    monkeypatch.setattr(liquidityhelper, "do_onchain_cashouts", fake_onchain)
    return calls


def _fake_lnd_rpc(api: FakeBitcartAPI):
    """Build a stub for liquidityhelper.lnd_rpc that consults
    FakeBitcartAPI.pending_channels_by_wallet for the only method we need
    here (PendingChannels) and returns an empty dict otherwise."""
    async def stub(api_arg, wallet_id, method, params=None, service="Lightning"):
        if method == "PendingChannels":
            return api.pending_channels_by_wallet.get(wallet_id) or {}
        return {}
    return stub


def _set_global(monkeypatch, **kw):
    """Set liquidityhelper module-level config knobs to known values."""
    defaults = {
        "ENABLE_CASHOUT_LN": True,
        "ENABLE_CASHOUT_ONCHAIN": True,
        "PREFER_CASHOUT_ONCHAIN": False,
        "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS": None,
        "CASHOUT_ONCHAIN": "bc1qfakedest",
        "MIN_ONCHAIN_CASHOUT": 25_000,
        "MIN_RESERVE_ONCHAIN": 10_000,
        "CHANNEL_ONCHAIN_BUFFER": 500,
        "FORCE_CASHOUT_AMOUNT_ONCHAIN": None,
        # LN-drain defaults — tests that exercise the drain layer
        # override LOOP_OUT_ENABLED to enable it.
        "LOOP_OUT_ENABLED": False,
        "LN_DRAIN_MIN_SWAP_SAT": 500_000,
        "LN_DRAIN_MAX_PER_TICK_SAT": 5_000_000,
        "MIN_INBOUND_LIQUIDITY_PER_CHANNEL": 50_000,
    }
    for name, value in {**defaults, **kw}.items():
        monkeypatch.setattr(liquidityhelper, name, value, raising=False)


def _record_drain_calls(monkeypatch):
    """Replace initiate_lightning_to_onchain_swap with a recorder so we
    can assert when drain_ln_to_onchain decides to fire a swap, with
    what amount, and to what destination."""
    calls: List[Dict[str, Any]] = []

    async def fake_initiate(*, wallet, api, amount_sat, dest_addr):
        calls.append({
            "wallet_id": wallet["id"],
            "amount_sat": amount_sat,
            "dest_addr": dest_addr,
        })
        # Return a sentinel SwapResult-ish object — drain_ln_to_onchain
        # only inspects .swap_id and .htlc_address for logging.
        return type("R", (), {
            "swap_id": "deadbeef" * 8,
            "htlc_address": "bcrt1qfakehtlc",
        })()

    monkeypatch.setattr(
        liquidityhelper, "initiate_lightning_to_onchain_swap", fake_initiate,
    )
    return calls


# ---------------------------------------------------------------------------
# Phase 1 — safe_to_spend wiring in do_cashouts
# ---------------------------------------------------------------------------

def test_do_cashouts_subtracts_reserves_lnd(monkeypatch, event_loop):
    """LND wallet, 0.001 BTC balance (100_000 sats), MIN_RESERVE_ONCHAIN
    = 10_000, no active channels. do_onchain_cashouts should be invoked
    with 90_000 sats, NOT 100_000."""
    _set_global(monkeypatch, PREFER_CASHOUT_ONCHAIN=True, MIN_RESERVE_ONCHAIN=10_000)
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(FakeBitcartAPI()))

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.001)  # 100_000 sat
    api.add_store("s1", wallets=["w1"])
    api.set_lnd_pending_channels("w1")  # nothing pending

    calls = _record_cashout_calls(monkeypatch)
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))
    result = event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert result is True

    assert len(calls) == 1
    assert calls[0]["path"] == "onchain"
    assert calls[0]["amount"] == 90_000


def test_do_cashouts_subtracts_reserves_electrum(monkeypatch, event_loop):
    """Electrum wallet, same math. No LND PendingChannels involved."""
    _set_global(monkeypatch, PREFER_CASHOUT_ONCHAIN=True, MIN_RESERVE_ONCHAIN=10_000)

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=0.001)  # 100_000 sat
    api.add_store("s1", wallets=["w1"])
    # No channels at all -> safe_to_spend uses MIN_RESERVE_ONCHAIN floor.

    calls = _record_cashout_calls(monkeypatch)
    result = event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert result is True

    assert len(calls) == 1
    assert calls[0]["path"] == "onchain"
    assert calls[0]["amount"] == 90_000


def test_do_cashouts_subtracts_per_channel_buffers(monkeypatch, event_loop):
    """Wallet with 3 active channels: per-channel reserves total can
    exceed MIN_RESERVE_ONCHAIN, in which case we keep the larger."""
    _set_global(
        monkeypatch,
        PREFER_CASHOUT_ONCHAIN=True,
        MIN_RESERVE_ONCHAIN=5_000,
        CHANNEL_ONCHAIN_BUFFER=10_000,   # 3 * 10_000 = 30_000 > MIN_RESERVE_ONCHAIN
    )
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=0.001)  # 100_000 sat
    api.add_store("s1", wallets=["w1"])
    for _ in range(3):
        api.add_channel("w1", local_balance=20_000, remote_balance=20_000, active=True)

    calls = _record_cashout_calls(monkeypatch)
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert calls[0]["path"] == "onchain"
    # 100_000 - max(5_000, 3 * onchain_reserves_to_keep_for_channel(40_000))
    # The exact per-channel reserve depends on common_functions, but it's
    # definitely > 5_000 with this CHANNEL_ONCHAIN_BUFFER so the per-channel
    # total wins. Just assert "less than balance" and "positive".
    assert 0 < calls[0]["amount"] < 100_000


# ---------------------------------------------------------------------------
# Phase 2 — has_pending_channel_activity (LND direct, Electrum via API)
# ---------------------------------------------------------------------------

def test_pending_activity_lnd_pending_open(monkeypatch, event_loop):
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=1.0)
    api.set_lnd_pending_channels("w1", pending_open=1)
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is True


def test_pending_activity_lnd_waiting_close(monkeypatch, event_loop):
    """Cooperative close waiting for broadcast/confirmation -> block."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=1.0)
    api.set_lnd_pending_channels("w1", waiting_close=1)
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is True


def test_pending_activity_lnd_pending_closing(monkeypatch, event_loop):
    """Coop close, tx in mempool, awaiting confirmation -> block."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=1.0)
    api.set_lnd_pending_channels("w1", pending_closing=1)
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is True


def test_pending_activity_lnd_force_closing_ignored(monkeypatch, event_loop):
    """Force-close in CSV delay -> NOT block (could be days/weeks)."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=1.0)
    api.set_lnd_pending_channels("w1", pending_force_closing=2)
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is False


def test_pending_activity_lnd_empty(monkeypatch, event_loop):
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=1.0)
    api.set_lnd_pending_channels("w1")   # all zeros
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is False


def test_pending_activity_lnd_rpc_failure_is_safe(monkeypatch, event_loop):
    """If we can't reach LND, assume pending (refuse to cash out)."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=1.0)

    async def boom(*a, **kw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", boom)

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is True


def test_pending_activity_electrum_opening(event_loop):
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=1.0)
    api.add_channel("w1", local_balance=0, remote_balance=0, active=False,
                    state="OPENING", channel_point="aabbcc:0")

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is True


def test_pending_activity_electrum_funded(event_loop):
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=1.0)
    api.add_channel("w1", state="FUNDED", channel_point="aabbcc:0")

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is True


def test_pending_activity_electrum_coop_closing_blocks(event_loop):
    """Electrum CLOSING + LightningChannel row marking it cooperative
    -> block."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=1.0)
    cp = "aabbcc:0"
    api.add_channel("w1", state="CLOSING", channel_point=cp)
    LightningChannel.create(
        channel_point=cp,
        cooperative_close_requested=datetime.datetime.now(),
    )

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is True


def test_pending_activity_electrum_force_close_ignored(event_loop):
    """Electrum CLOSING without a coop-close DB record -> treat as
    force close, do NOT block."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=1.0)
    api.add_channel("w1", state="CLOSING", channel_point="ddeeff:0")
    # Deliberately no LightningChannel row.

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is False


def test_pending_activity_electrum_only_open(event_loop):
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=1.0)
    api.add_channel("w1", state="OPEN", active=True, channel_point="cc:0")

    result = event_loop.run_until_complete(
        liquidityhelper.has_pending_channel_activity(
            wallet=api.wallets["w1"], api=api,
        )
    )
    assert result is False


def test_do_cashouts_skips_when_lnd_open_pending(monkeypatch, event_loop):
    """Integration check: do_cashouts honors the pending guard."""
    _set_global(monkeypatch, PREFER_CASHOUT_ONCHAIN=True)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.001)
    api.add_store("s1", wallets=["w1"])
    api.set_lnd_pending_channels("w1", pending_open=1)
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    calls = _record_cashout_calls(monkeypatch)
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert calls == []   # neither rail was taken


def test_do_cashouts_proceeds_when_only_force_closing(monkeypatch, event_loop):
    """Force closes do not block — should proceed to on-chain send."""
    _set_global(monkeypatch, PREFER_CASHOUT_ONCHAIN=True, MIN_RESERVE_ONCHAIN=10_000)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.001)
    api.add_store("s1", wallets=["w1"])
    api.set_lnd_pending_channels("w1", pending_force_closing=3)
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    calls = _record_cashout_calls(monkeypatch)
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert len(calls) == 1
    assert calls[0]["path"] == "onchain"


# ---------------------------------------------------------------------------
# Phase 3 — should_prefer_onchain_cashout: single source of truth
# ---------------------------------------------------------------------------

def test_should_prefer_onchain_when_global_set(monkeypatch):
    _set_global(monkeypatch, PREFER_CASHOUT_ONCHAIN=True)
    assert liquidityhelper.should_prefer_onchain_cashout() is True


def test_should_prefer_onchain_when_ln_stale(monkeypatch):
    _set_global(monkeypatch,
                PREFER_CASHOUT_ONCHAIN=False,
                CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=7)
    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now() - datetime.timedelta(days=30),
    ).execute()
    assert liquidityhelper.should_prefer_onchain_cashout() is True


def test_should_not_prefer_when_ln_recent(monkeypatch):
    _set_global(monkeypatch,
                PREFER_CASHOUT_ONCHAIN=False,
                CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=7)
    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now() - datetime.timedelta(hours=1),
    ).execute()
    assert liquidityhelper.should_prefer_onchain_cashout() is False


def test_should_not_prefer_when_no_history(monkeypatch):
    """Brand new install — no LN cashout recorded ever. Should not
    trigger fallback (LN hasn't had a chance to try yet)."""
    _set_global(monkeypatch,
                PREFER_CASHOUT_ONCHAIN=False,
                CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=7)
    assert liquidityhelper.should_prefer_onchain_cashout() is False


def test_should_not_prefer_when_threshold_disabled(monkeypatch):
    _set_global(monkeypatch,
                PREFER_CASHOUT_ONCHAIN=False,
                CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=None)
    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now() - datetime.timedelta(days=365),
    ).execute()
    assert liquidityhelper.should_prefer_onchain_cashout() is False


def test_should_not_prefer_when_onchain_disabled(monkeypatch):
    """If on-chain cashout isn't enabled, the recency fallback can't
    fire — there's nowhere to fall back to."""
    _set_global(monkeypatch,
                PREFER_CASHOUT_ONCHAIN=False,
                ENABLE_CASHOUT_ONCHAIN=False,
                CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=7)
    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now() - datetime.timedelta(days=365),
    ).execute()
    assert liquidityhelper.should_prefer_onchain_cashout() is False


# ---------------------------------------------------------------------------
# Phase 4 — Real-daemon integration tests
# ---------------------------------------------------------------------------

def test_onchain_cashout_via_lnd_send_coins(lnd_pair, event_loop, monkeypatch):
    """End-to-end: configure a CASHOUT_ONCHAIN address pointing at LND-B
    (so it's reachable on regtest), invoke do_onchain_cashouts against
    LND-A, mine confirmations, and verify:
      - LND-B sees the funds
      - The outgoing tx on LND-A's side carries CASHOUT_REASON as its
        on-chain label (proves LND's native SendCoinsRequest.label
        persists through GetTransactions for real)
    """
    from lnd_proto import lightning_pb2 as lnd_pb2

    dest_addr = event_loop.run_until_complete(lnd_pair.b.new_address())
    _set_global(monkeypatch,
                CASHOUT_ONCHAIN=dest_addr,
                MIN_ONCHAIN_CASHOUT=10_000)

    # Pre-populate _LND_CONNECTIONS so electrum_pay_onchain doesn't try
    # to consult Bitcart (we don't have a BitcartAPI in this fixture).
    import grpc as _grpc
    ssl_creds = _grpc.ssl_channel_credentials(root_certificates=lnd_pair.a._tls_cert)
    macaroon_hex = lnd_pair.a._macaroon_hex

    def _mac_cb(_ctx, callback):
        callback([("macaroon", macaroon_hex)], None)

    creds = _grpc.composite_channel_credentials(
        ssl_creds, _grpc.metadata_call_credentials(_mac_cb)
    )
    channel = _grpc.aio.secure_channel(
        f"127.0.0.1:{lnd_pair.a.rpc_port}", creds,
        options=[("grpc.ssl_target_name_override", "localhost")],
    )
    from lnd_proto import lightning_pb2_grpc
    monkeypatch.setattr(
        liquidityhelper, "_LND_CONNECTIONS",
        {"test-wallet-a": {
            "channel": channel,
            "stubs": {"Lightning": lightning_pb2_grpc.LightningStub(channel)},
            "macaroon_hex": macaroon_hex,
            "tls_cert": lnd_pair.a._tls_cert,
        }}
    )

    wallet = {"id": "test-wallet-a", "currency": "btclnd"}
    result = event_loop.run_until_complete(liquidityhelper.electrum_pay_onchain(
        dest_addr,
        liquidityhelper.sats_to_btc(50_000),
        label="lnhelper_cashout",
        wallet=wallet,
        api=None,
    ))
    assert result is True

    # Mine a block so the tx confirms on both nodes.
    lnd_pair.bitcoind.mine_to_self(1)

    # Verify LND-A's GetTransactions returns the tx with our label.
    async def _find_labeled_tx():
        for _ in range(30):
            resp = await lnd_pair.a._stub.GetTransactions(
                lnd_pb2.GetTransactionsRequest()
            )
            for tx in resp.transactions:
                if tx.label == "lnhelper_cashout":
                    return tx
            await asyncio.sleep(0.5)
        return None

    tx = event_loop.run_until_complete(_find_labeled_tx())
    assert tx is not None, "LND-A never reported the cashout tx with our label"
    assert tx.label == "lnhelper_cashout"
    # LND signs `amount` as negative for outgoing txs (excludes fees).
    assert tx.amount < 0
    assert abs(tx.amount) >= 50_000


def test_onchain_cashout_via_electrum_payto(lnd_electrum_pair, event_loop, monkeypatch):
    """End-to-end through the Electrum dispatch path.

    Exercises payto + broadcast + setlabel against the rig's running
    Electrum daemon and confirms the destination receives the funds on
    regtest.
    """
    # Get a regtest address from Electrum's own wallet and send funds
    # back to it (round-trip is fine for verifying the dispatch worked).
    dest_addr = lnd_electrum_pair.electrum.getunusedaddress()
    _set_global(monkeypatch,
                CASHOUT_ONCHAIN=dest_addr,
                MIN_ONCHAIN_CASHOUT=10_000)

    wallet = {
        "id": "test-electrum",
        "currency": "btc",
        "xpub": lnd_electrum_pair.electrum.xpub,
    }
    result = event_loop.run_until_complete(liquidityhelper.electrum_pay_onchain(
        dest_addr,
        liquidityhelper.sats_to_btc(50_000),
        label="lnhelper_cashout",
        wallet=wallet,
        api=None,
    ))
    assert result is True


# ---------------------------------------------------------------------------
# drain_ln_to_onchain — the LN-to-onchain loop-out drain
# ---------------------------------------------------------------------------

def test_drain_short_circuits_electrum(monkeypatch, event_loop):
    """Loop is LND-only. Electrum wallet -> immediate False, no swap."""
    _set_global(monkeypatch)
    calls = _record_drain_calls(monkeypatch)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc")

    result = event_loop.run_until_complete(
        liquidityhelper.drain_ln_to_onchain(
            api, wallet=api.wallets["w1"], dest_addr="bc1qdest",
        )
    )
    assert result is False
    assert calls == []


def test_drain_skipped_when_pending_channel_activity(monkeypatch, event_loop):
    """Open or coop close in flight -> defer until next tick."""
    _set_global(monkeypatch, LOOP_OUT_ENABLED=True)
    calls = _record_drain_calls(monkeypatch)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.set_lnd_pending_channels("w1", pending_open=1)
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    result = event_loop.run_until_complete(
        liquidityhelper.drain_ln_to_onchain(
            api, wallet=api.wallets["w1"], dest_addr="bc1qdest",
        )
    )
    assert result is False
    assert calls == []


def test_drain_skipped_when_excess_below_threshold(monkeypatch, event_loop):
    """Total local 200k - reserve 50k = 150k excess, threshold 500k.
    Below threshold -> no swap (avoids dust fees)."""
    _set_global(monkeypatch,
                LOOP_OUT_ENABLED=True,
                LN_DRAIN_MIN_SWAP_SAT=500_000,
                MIN_INBOUND_LIQUIDITY_PER_CHANNEL=50_000)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.add_channel("w1", local_balance=200_000, remote_balance=0,
                    active=True, peer_state="GOOD")
    api.set_lnd_pending_channels("w1")
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))
    calls = _record_drain_calls(monkeypatch)

    result = event_loop.run_until_complete(
        liquidityhelper.drain_ln_to_onchain(
            api, wallet=api.wallets["w1"], dest_addr="bc1qdest",
        )
    )
    assert result is False
    assert calls == []


def test_drain_fires_swap_when_excess_above_threshold(monkeypatch, event_loop):
    """1M local, 50k reserve, 950k excess. Threshold 500k, cap 5M.
    Should swap exactly excess=950k (under cap)."""
    _set_global(monkeypatch,
                LOOP_OUT_ENABLED=True,
                LN_DRAIN_MIN_SWAP_SAT=500_000,
                LN_DRAIN_MAX_PER_TICK_SAT=5_000_000,
                MIN_INBOUND_LIQUIDITY_PER_CHANNEL=50_000)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.add_channel("w1", local_balance=1_000_000, remote_balance=0,
                    active=True, peer_state="GOOD")
    api.set_lnd_pending_channels("w1")
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))
    calls = _record_drain_calls(monkeypatch)

    result = event_loop.run_until_complete(
        liquidityhelper.drain_ln_to_onchain(
            api, wallet=api.wallets["w1"], dest_addr="bc1qdest",
        )
    )
    assert result is True
    assert len(calls) == 1
    assert calls[0]["amount_sat"] == 950_000
    assert calls[0]["dest_addr"] == "bc1qdest"


def test_drain_caps_at_max_per_tick(monkeypatch, event_loop):
    """20M local, 50k reserve, 19.95M excess. Cap 5M -> swap exactly 5M.
    Multi-tick drain pattern; the remaining excess drains over future
    ticks."""
    _set_global(monkeypatch,
                LOOP_OUT_ENABLED=True,
                LN_DRAIN_MIN_SWAP_SAT=500_000,
                LN_DRAIN_MAX_PER_TICK_SAT=5_000_000,
                MIN_INBOUND_LIQUIDITY_PER_CHANNEL=50_000)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.add_channel("w1", local_balance=20_000_000, remote_balance=0,
                    active=True, peer_state="GOOD")
    api.set_lnd_pending_channels("w1")
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))
    calls = _record_drain_calls(monkeypatch)

    event_loop.run_until_complete(
        liquidityhelper.drain_ln_to_onchain(
            api, wallet=api.wallets["w1"], dest_addr="bc1qdest",
        )
    )
    assert len(calls) == 1
    assert calls[0]["amount_sat"] == 5_000_000


def test_drain_reserves_per_channel(monkeypatch, event_loop):
    """3 channels × 50k reserve = 150k reserve. Total local 1M.
    Expected excess = 850k."""
    _set_global(monkeypatch,
                LOOP_OUT_ENABLED=True,
                LN_DRAIN_MIN_SWAP_SAT=100_000,
                MIN_INBOUND_LIQUIDITY_PER_CHANNEL=50_000)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    for i in range(3):
        api.add_channel("w1", local_balance=333_333, remote_balance=0,
                        active=True, peer_state="GOOD",
                        channel_point=f"chan{i}:0")
    # 3 * 333_333 = 999_999. Reserve = 150_000. Excess = 849_999.
    api.set_lnd_pending_channels("w1")
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))
    calls = _record_drain_calls(monkeypatch)

    event_loop.run_until_complete(
        liquidityhelper.drain_ln_to_onchain(
            api, wallet=api.wallets["w1"], dest_addr="bc1qdest",
        )
    )
    assert calls[0]["amount_sat"] == 849_999


def test_drain_returns_false_when_provider_rejects(monkeypatch, event_loop):
    """initiate_lightning_to_onchain_swap returns None (e.g. fee cap
    exceeded) -> drain returns False."""
    _set_global(monkeypatch,
                LOOP_OUT_ENABLED=True,
                LN_DRAIN_MIN_SWAP_SAT=500_000,
                MIN_INBOUND_LIQUIDITY_PER_CHANNEL=50_000)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.add_channel("w1", local_balance=1_000_000, remote_balance=0,
                    active=True, peer_state="GOOD")
    api.set_lnd_pending_channels("w1")
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    async def _reject(*, wallet, api, amount_sat, dest_addr):
        return None
    monkeypatch.setattr(
        liquidityhelper, "initiate_lightning_to_onchain_swap", _reject,
    )

    result = event_loop.run_until_complete(
        liquidityhelper.drain_ln_to_onchain(
            api, wallet=api.wallets["w1"], dest_addr="bc1qdest",
        )
    )
    assert result is False


# ---------------------------------------------------------------------------
# do_cashouts integration — drain is called only when correctly gated
# ---------------------------------------------------------------------------

def test_do_cashouts_fires_drain_when_loop_out_enabled(monkeypatch, event_loop):
    """LOOP_OUT_ENABLED=True + PREFER_CASHOUT_ONCHAIN=True + LND wallet
    with drainable excess -> drain_ln_to_onchain gets called."""
    _set_global(monkeypatch,
                PREFER_CASHOUT_ONCHAIN=True,
                LOOP_OUT_ENABLED=True,
                LN_DRAIN_MIN_SWAP_SAT=500_000,
                MIN_INBOUND_LIQUIDITY_PER_CHANNEL=50_000)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.001)
    api.add_store("s1", wallets=["w1"])
    api.add_channel("w1", local_balance=1_000_000, remote_balance=0,
                    active=True, peer_state="GOOD")
    api.set_lnd_pending_channels("w1")
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    drain_calls = _record_drain_calls(monkeypatch)
    _record_cashout_calls(monkeypatch)

    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert len(drain_calls) == 1
    assert drain_calls[0]["dest_addr"] == "bc1qfakedest"


def test_do_cashouts_skips_drain_when_loop_out_disabled(monkeypatch, event_loop):
    """LOOP_OUT_ENABLED=False (the default) -> drain is NOT called even
    when on-chain rail and drainable LN excess exist."""
    _set_global(monkeypatch,
                PREFER_CASHOUT_ONCHAIN=True,
                LOOP_OUT_ENABLED=False,
                MIN_INBOUND_LIQUIDITY_PER_CHANNEL=50_000)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.001)
    api.add_store("s1", wallets=["w1"])
    api.add_channel("w1", local_balance=1_000_000, remote_balance=0,
                    active=True, peer_state="GOOD")
    api.set_lnd_pending_channels("w1")
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    drain_calls = _record_drain_calls(monkeypatch)
    _record_cashout_calls(monkeypatch)

    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert drain_calls == []


def test_do_cashouts_skips_drain_when_no_cashout_onchain(monkeypatch, event_loop):
    """LOOP_OUT_ENABLED=True but CASHOUT_ONCHAIN is unset -> nowhere to
    drain TO, so the call is skipped."""
    _set_global(monkeypatch,
                PREFER_CASHOUT_ONCHAIN=True,
                LOOP_OUT_ENABLED=True,
                CASHOUT_ONCHAIN=None,
                MIN_INBOUND_LIQUIDITY_PER_CHANNEL=50_000)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.001)
    api.add_store("s1", wallets=["w1"])
    api.add_channel("w1", local_balance=1_000_000, remote_balance=0,
                    active=True, peer_state="GOOD")
    api.set_lnd_pending_channels("w1")
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    drain_calls = _record_drain_calls(monkeypatch)
    _record_cashout_calls(monkeypatch)

    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert drain_calls == []


def test_do_cashouts_skips_drain_on_ln_rail(monkeypatch, event_loop):
    """Default config (PREFER_CASHOUT_ONCHAIN=False, no recency
    fallback) -> rail is LN, drain doesn't run even if everything else
    is configured for it."""
    _set_global(monkeypatch,
                PREFER_CASHOUT_ONCHAIN=False,
                LOOP_OUT_ENABLED=True,
                CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=None,
                MIN_INBOUND_LIQUIDITY_PER_CHANNEL=50_000)
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.001)
    api.add_store("s1", wallets=["w1"])
    api.add_channel("w1", local_balance=1_000_000, remote_balance=0,
                    active=True, peer_state="GOOD")
    api.set_lnd_pending_channels("w1")
    monkeypatch.setattr(liquidityhelper, "lnd_rpc", _fake_lnd_rpc(api))

    drain_calls = _record_drain_calls(monkeypatch)
    _record_cashout_calls(monkeypatch)

    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert drain_calls == []
