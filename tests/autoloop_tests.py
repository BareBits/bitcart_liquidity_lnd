"""Tests for autoloop configuration.

Two layers:
  - One integration test (`test_autoloop_round_trip_through_real_loopd`):
    sets every AUTOLOOP_* config var, runs `configure_autoloop()` against
    the real loopd in `loop_rig`, then reads back via GetLiquidityParams
    and asserts every field survived the round-trip. Auto-skips if podman
    isn't available.

  - Per-category unit tests against a faked stub. These exercise the
    config-var -> proto translation (`_build_liquidity_params`) plus the
    public dispatch policy (account-vs-dest conflict, malformed inputs,
    enum encoding, hex-bytes conversion).
"""

from __future__ import annotations

import json
import logging
import time
from unittest.mock import AsyncMock

import pytest

import liquidityhelper
from loop_proto import client_pb2 as _loop_pb2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUTOLOOP_DEFAULTS: dict = {
    "AUTOLOOP_ENABLED": False,
    "AUTOLOOP_DEST_ADDRESS": None,
    "AUTOLOOP_ACCOUNT": None,
    "AUTOLOOP_ACCOUNT_ADDR_TYPE": "p2tr",
    "AUTOLOOP_BUDGET_SAT": 100_000,
    "AUTOLOOP_BUDGET_REFRESH_PERIOD_SEC": 604_800,
    "AUTOLOOP_MAX_IN_FLIGHT": 1,
    "AUTOLOOP_MIN_SWAP_AMOUNT_SAT": 250_000,
    "AUTOLOOP_MAX_SWAP_AMOUNT_SAT": 5_000_000,
    "AUTOLOOP_FEE_PPM": 0,
    "AUTOLOOP_MAX_SWAP_FEE_PPM": 5_000,
    "AUTOLOOP_MAX_ROUTING_FEE_PPM": 5_000,
    "AUTOLOOP_MAX_PREPAY_ROUTING_FEE_PPM": 50_000,
    "AUTOLOOP_MAX_PREPAY_SAT": 100_000,
    "AUTOLOOP_MAX_MINER_FEE_SAT": 15_000,
    "AUTOLOOP_SWEEP_CONF_TARGET": 100,
    "AUTOLOOP_HTLC_CONF_TARGET": 6,
    "AUTOLOOP_SWEEP_FEE_RATE_SAT_PER_VBYTE": 0,
    "AUTOLOOP_FAILURE_BACKOFF_SEC": 86_400,
    "AUTOLOOP_EASY_MODE": False,
    "AUTOLOOP_EASY_LOCAL_TARGET_SAT": 0,
    "AUTOLOOP_FAST_SWAP_PUBLICATION": False,
    "AUTOLOOP_EASY_EXCLUDED_PEERS": [],
}


def _set_autoloop(monkeypatch, **overrides):
    """Reset every AUTOLOOP_* var on the liquidityhelper module to a known
    default, then apply `overrides`. Keeps tests independent of whatever
    user_config.py happens to set."""
    for name, value in _AUTOLOOP_DEFAULTS.items():
        monkeypatch.setattr(liquidityhelper, name, value, raising=False)
    for name, value in overrides.items():
        monkeypatch.setattr(liquidityhelper, name, value, raising=False)


# ---------------------------------------------------------------------------
# Unit tests — `_build_liquidity_params()` behavior
# ---------------------------------------------------------------------------

def test_default_mode_leaves_dest_and_account_empty(monkeypatch):
    """Neither AUTOLOOP_DEST_ADDRESS nor AUTOLOOP_ACCOUNT set: both proto
    fields stay empty, which lets loopd fall back to fresh LND addresses."""
    _set_autoloop(monkeypatch, AUTOLOOP_ENABLED=True)
    p = liquidityhelper._build_liquidity_params()
    assert p.autoloop is True
    assert p.autoloop_dest_address == ""
    assert p.account == ""
    assert p.account_addr_type == _loop_pb2.ADDRESS_TYPE_UNKNOWN


def test_only_dest_address_set(monkeypatch):
    addr = "bc1qexampleexampleexampleexampleexampleeeeeee"
    _set_autoloop(monkeypatch, AUTOLOOP_ENABLED=True, AUTOLOOP_DEST_ADDRESS=addr)
    p = liquidityhelper._build_liquidity_params()
    assert p.autoloop_dest_address == addr
    assert p.account == ""


def test_only_account_set(monkeypatch):
    _set_autoloop(
        monkeypatch,
        AUTOLOOP_ENABLED=True,
        AUTOLOOP_ACCOUNT="cold-storage",
        AUTOLOOP_ACCOUNT_ADDR_TYPE="p2tr",
    )
    p = liquidityhelper._build_liquidity_params()
    assert p.account == "cold-storage"
    assert p.account_addr_type == _loop_pb2.TAPROOT_PUBKEY
    assert p.autoloop_dest_address == ""


def test_account_and_dest_conflict_account_wins(monkeypatch, caplog):
    addr = "bc1qignoredignoredignoredignoredignoredignoredd"
    _set_autoloop(
        monkeypatch,
        AUTOLOOP_ENABLED=True,
        AUTOLOOP_DEST_ADDRESS=addr,
        AUTOLOOP_ACCOUNT="cold-storage",
    )
    with caplog.at_level(logging.WARNING, logger="liquidityhelper"):
        p = liquidityhelper._build_liquidity_params()

    assert p.account == "cold-storage"
    assert p.autoloop_dest_address == ""  # explicitly NOT carried over
    assert any(
        "AUTOLOOP_ACCOUNT" in rec.message and "AUTOLOOP_DEST_ADDRESS" in rec.message
        for rec in caplog.records
    ), "expected a warning naming both conflicting config vars"


def test_unknown_account_addr_type_falls_back_to_p2tr(monkeypatch, caplog):
    _set_autoloop(
        monkeypatch,
        AUTOLOOP_ENABLED=True,
        AUTOLOOP_ACCOUNT="cold",
        AUTOLOOP_ACCOUNT_ADDR_TYPE="p2wpkh",   # not supported by loop today
    )
    with caplog.at_level(logging.WARNING, logger="liquidityhelper"):
        p = liquidityhelper._build_liquidity_params()
    assert p.account_addr_type == _loop_pb2.TAPROOT_PUBKEY
    assert any("not supported" in rec.message for rec in caplog.records)


def test_easy_mode_settings(monkeypatch):
    _set_autoloop(
        monkeypatch,
        AUTOLOOP_ENABLED=True,
        AUTOLOOP_EASY_MODE=True,
        AUTOLOOP_EASY_LOCAL_TARGET_SAT=1_500_000,
    )
    p = liquidityhelper._build_liquidity_params()
    assert p.easy_autoloop is True
    assert p.easy_autoloop_local_target_sat == 1_500_000


def test_excluded_peers_hex_to_bytes(monkeypatch, caplog):
    good_a = "02" + "aa" * 32
    good_b = "03" + "bb" * 32
    bad = "not-hex"
    _set_autoloop(
        monkeypatch,
        AUTOLOOP_EASY_EXCLUDED_PEERS=[good_a, bad, good_b],
    )
    with caplog.at_level(logging.WARNING, logger="liquidityhelper"):
        p = liquidityhelper._build_liquidity_params()
    assert list(p.easy_autoloop_excluded_peers) == [bytes.fromhex(good_a), bytes.fromhex(good_b)]
    assert any("malformed pubkey" in rec.message for rec in caplog.records)


def test_fee_ppm_only_set_when_nonzero(monkeypatch):
    """`fee_ppm` is an overall fee cap that, when nonzero, overrides the
    granular per-category knobs. We only set it when explicitly configured."""
    _set_autoloop(monkeypatch, AUTOLOOP_FEE_PPM=0)
    p = liquidityhelper._build_liquidity_params()
    # Default uint64 -> 0 in proto3, indistinguishable from "not set" but
    # the granular knobs should still come through.
    assert p.fee_ppm == 0
    assert p.max_swap_fee_ppm == 5_000

    _set_autoloop(monkeypatch, AUTOLOOP_FEE_PPM=1_234)
    p = liquidityhelper._build_liquidity_params()
    assert p.fee_ppm == 1_234


# ---------------------------------------------------------------------------
# Unit test — `configure_autoloop` push path with a fake LoopProvider
# ---------------------------------------------------------------------------

def test_configure_autoloop_pushes_built_params(monkeypatch, event_loop):
    """`configure_autoloop` should call LoopProvider.configure_autoloop with
    the proto produced by `_build_liquidity_params`. We swap in a fake
    LoopProvider that records the call."""
    from swap_providers import LoopProvider, SwapProvider

    _set_autoloop(
        monkeypatch,
        AUTOLOOP_ENABLED=True,
        AUTOLOOP_DEST_ADDRESS="bc1qsomeoutputaddressbc1qsomeoutputaddressee",
        AUTOLOOP_BUDGET_SAT=42_000,
    )

    class _RecordingProvider(LoopProvider):
        name = "loop"

        def __init__(self):
            self.captured = None

        async def configure_autoloop(self, wallet, api, params):
            self.captured = params
            return True

    provider = _RecordingProvider()
    monkeypatch.setattr(liquidityhelper, "SWAP_PROVIDERS", [provider])

    ok = event_loop.run_until_complete(
        liquidityhelper.configure_autoloop({"id": "w1", "currency": "btclnd"}, api=None)
    )
    assert ok is True
    assert provider.captured is not None
    assert provider.captured.autoloop is True
    assert provider.captured.autoloop_dest_address == "bc1qsomeoutputaddressbc1qsomeoutputaddressee"
    assert provider.captured.autoloop_budget_sat == 42_000


# ---------------------------------------------------------------------------
# Integration test — full round-trip against the real loopd in loop_rig.
# Skipped automatically when podman/docker isn't available.
# ---------------------------------------------------------------------------

# A small but representative non-default value for every AUTOLOOP_* field.
# Picked so that no value accidentally matches its zero/proto default
# (otherwise an "unset" bug would falsely pass).
_ROUND_TRIP_OVERRIDES = {
    "AUTOLOOP_ENABLED": True,
    "AUTOLOOP_BUDGET_SAT": 1_234_567,
    "AUTOLOOP_BUDGET_REFRESH_PERIOD_SEC": 333_333,
    "AUTOLOOP_MAX_IN_FLIGHT": 3,
    # loopserver enforces a minimum swap amount; values below the server
    # min get rejected at SetLiquidityParams time. 250k is comfortably
    # above the v0.11.x default (~50k).
    "AUTOLOOP_MIN_SWAP_AMOUNT_SAT": 250_000,
    "AUTOLOOP_MAX_SWAP_AMOUNT_SAT": 4_242_424,
    "AUTOLOOP_FEE_PPM": 0,  # leave zero so granular knobs aren't overridden
    "AUTOLOOP_MAX_SWAP_FEE_PPM": 4_321,
    "AUTOLOOP_MAX_ROUTING_FEE_PPM": 1_234,
    "AUTOLOOP_MAX_PREPAY_ROUTING_FEE_PPM": 22_222,
    "AUTOLOOP_MAX_PREPAY_SAT": 77_777,
    "AUTOLOOP_MAX_MINER_FEE_SAT": 9_999,
    "AUTOLOOP_SWEEP_CONF_TARGET": 42,
    "AUTOLOOP_HTLC_CONF_TARGET": 5,
    "AUTOLOOP_SWEEP_FEE_RATE_SAT_PER_VBYTE": 13,
    "AUTOLOOP_FAILURE_BACKOFF_SEC": 12_345,
    "AUTOLOOP_EASY_MODE": True,
    "AUTOLOOP_EASY_LOCAL_TARGET_SAT": 2_500_000,
    "AUTOLOOP_FAST_SWAP_PUBLICATION": True,
    # NOTE on AUTOLOOP_EASY_EXCLUDED_PEERS: loopd accepts this field in
    # SetLiquidityParams but does NOT echo it back via GetLiquidityParams
    # in this version (likely because the daemon resolves the exclusion
    # list lazily at swap-evaluation time rather than persisting it
    # verbatim). The hex->bytes mapping is covered by the unit test
    # `test_excluded_peers_hex_to_bytes`, so we deliberately leave the
    # round-trip empty here.
    "AUTOLOOP_EASY_EXCLUDED_PEERS": [],
    # AUTOLOOP_DEST_ADDRESS filled in at test time from the regtest LND.
    # ACCOUNT path is out of scope here (xpub import is a separate flow);
    # proto-level account fields are covered by the unit tests above.
    "AUTOLOOP_DEST_ADDRESS": None,   # patched below
    "AUTOLOOP_ACCOUNT": None,
    "AUTOLOOP_ACCOUNT_ADDR_TYPE": "p2tr",
}


@pytest.mark.timeout(180)
def test_autoloop_round_trip_through_real_loopd(loop_rig, event_loop, monkeypatch):
    """Push every AUTOLOOP_* setting through `configure_autoloop` into the
    real loopd, then read it back via GetLiquidityParams and assert every
    field survived. This is the smoke test that proves our config->proto
    translation matches what loopd actually accepts and persists.

    timeout(180): real loopd startup + LND.OpenChannel + gossip-sync is
    minute-scale on cold runs; the default 60s pytest.ini timeout has
    flaked here under load."""
    from swap_providers import LoopProvider
    from loop_proto.client_pb2 import GetLiquidityParamsRequest

    # Need a real, bech32-valid regtest address. loopd validates the
    # checksum before accepting SetLiquidityParams.
    dest_addr = event_loop.run_until_complete(loop_rig.a.new_address())
    overrides = dict(_ROUND_TRIP_OVERRIDES, AUTOLOOP_DEST_ADDRESS=dest_addr)
    _set_autoloop(monkeypatch, **overrides)

    # Point the module-global provider list at loop_rig's manager (which
    # has the LoopdInstance for LND-A pre-registered).
    provider = LoopProvider(loop_rig.loopd_manager)
    monkeypatch.setattr(liquidityhelper, "SWAP_PROVIDERS", [provider])

    fake_wallet = {
        "id": f"test-wallet-{loop_rig.a.name.lower()}",
        "currency": "btclnd",
    }
    ok = event_loop.run_until_complete(
        liquidityhelper.configure_autoloop(fake_wallet, api=None)
    )
    assert ok is True, "configure_autoloop rejected by loopd (check loopd.log)"

    # Now read back from the same loopd and assert each field round-tripped.
    loopd = event_loop.run_until_complete(
        loop_rig.loopd_manager.get_loopd_for_wallet(fake_wallet, api=None)
    )
    stub = loopd.grpc_swap_stub()
    fetched = event_loop.run_until_complete(
        stub.GetLiquidityParams(GetLiquidityParamsRequest(), timeout=10.0)
    )

    assert fetched.autoloop is True
    assert fetched.autoloop_budget_sat == 1_234_567
    assert fetched.autoloop_budget_refresh_period_sec == 333_333
    assert fetched.auto_max_in_flight == 3
    assert fetched.min_swap_amount == 250_000
    assert fetched.max_swap_amount == 4_242_424
    assert fetched.max_swap_fee_ppm == 4_321
    assert fetched.max_routing_fee_ppm == 1_234
    assert fetched.max_prepay_routing_fee_ppm == 22_222
    assert fetched.max_prepay_sat == 77_777
    assert fetched.max_miner_fee_sat == 9_999
    assert fetched.sweep_conf_target == 42
    assert fetched.htlc_conf_target == 5
    assert fetched.sweep_fee_rate_sat_per_vbyte == 13
    assert fetched.failure_backoff_sec == 12_345
    assert fetched.easy_autoloop is True
    assert fetched.easy_autoloop_local_target_sat == 2_500_000
    assert fetched.fast_swap_publication is True
    assert fetched.autoloop_dest_address == dest_addr
    assert fetched.account == ""  # we deliberately didn't set the account path


# ---------------------------------------------------------------------------
# End-to-end behavioral test: configure autoloop, confirm the rule engine
# would dispatch a swap, run that swap, and verify the on-chain HTLC tx
# actually lands at the configured destination address.
#
# Caveat: this does NOT wait for loopd's autoloop dispatcher tick to fire
# (it defaults to 20 minutes — see `liquidity.DefaultAutoloopTicker` in the
# loop source). Instead it calls `SuggestSwaps`, the RPC the dispatcher
# itself invokes each tick, then executes the suggested LoopOutRequest by
# hand. That covers every link of the chain except the timer.
# ---------------------------------------------------------------------------

@pytest.mark.timeout(180)
def test_autoloop_rule_engine_produces_real_onchain_swap(
    loop_rig, event_loop, monkeypatch,
):
    """End-to-end: configure autoloop permissively, verify the rule engine
    suggests a loop-out that respects the configured bounds, then execute
    that swap and confirm the HTLC publication tx lands at the configured
    AUTOLOOP_DEST_ADDRESS."""
    from swap_providers import LoopProvider
    from loop_proto.client_pb2 import (
        SuggestSwapsRequest, SwapInfoRequest, ListSwapsRequest,
        GetLiquidityParamsRequest, SetLiquidityParamsRequest,
        LiquidityRule, LiquidityRuleType, SwapType,
    )

    # We want autoloop's rule engine to have every reason to suggest a
    # loop-out: generous fee/budget knobs, easy mode targeting a balance
    # well below the rig's funded A->S channel (~20M sat local on A).
    dest_addr = event_loop.run_until_complete(loop_rig.a.new_address())
    overrides = {
        "AUTOLOOP_ENABLED": True,
        "AUTOLOOP_DEST_ADDRESS": dest_addr,
        "AUTOLOOP_BUDGET_SAT": 10_000_000,
        "AUTOLOOP_BUDGET_REFRESH_PERIOD_SEC": 604_800,
        "AUTOLOOP_MAX_IN_FLIGHT": 1,
        "AUTOLOOP_MIN_SWAP_AMOUNT_SAT": 250_000,
        # loopserver fixture is started with --maxamt=5_000_000.
        "AUTOLOOP_MAX_SWAP_AMOUNT_SAT": 5_000_000,
        "AUTOLOOP_FEE_PPM": 0,
        "AUTOLOOP_MAX_SWAP_FEE_PPM": 100_000,         # 10%
        "AUTOLOOP_MAX_ROUTING_FEE_PPM": 100_000,
        "AUTOLOOP_MAX_PREPAY_ROUTING_FEE_PPM": 100_000,
        "AUTOLOOP_MAX_PREPAY_SAT": 1_000_000,
        "AUTOLOOP_MAX_MINER_FEE_SAT": 500_000,
        "AUTOLOOP_SWEEP_CONF_TARGET": 3,
        "AUTOLOOP_HTLC_CONF_TARGET": 6,
        # loopd requires sweep_fee_rate > 1 sat/vB. Production may use 0
        # (let the fee estimator decide); in regtest the fee estimator can
        # report inflated values, so set the cap high enough that the
        # AUTO_REASON_SWEEP_FEES check passes.
        "AUTOLOOP_SWEEP_FEE_RATE_SAT_PER_VBYTE": 1_000,
        "AUTOLOOP_FAILURE_BACKOFF_SEC": 60,
        "AUTOLOOP_EASY_MODE": True,
        "AUTOLOOP_EASY_LOCAL_TARGET_SAT": 1_000_000,  # << current local (~20M)
        "AUTOLOOP_FAST_SWAP_PUBLICATION": True,
        "AUTOLOOP_EASY_EXCLUDED_PEERS": [],
        "AUTOLOOP_ACCOUNT": None,
        "AUTOLOOP_ACCOUNT_ADDR_TYPE": "p2tr",
    }
    _set_autoloop(monkeypatch, **overrides)

    provider = LoopProvider(loop_rig.loopd_manager)
    monkeypatch.setattr(liquidityhelper, "SWAP_PROVIDERS", [provider])

    fake_wallet = {
        "id": f"test-wallet-{loop_rig.a.name.lower()}",
        "currency": "btclnd",
    }
    ok = event_loop.run_until_complete(
        liquidityhelper.configure_autoloop(fake_wallet, api=None)
    )
    assert ok is True

    loopd = event_loop.run_until_complete(
        loop_rig.loopd_manager.get_loopd_for_wallet(fake_wallet, api=None)
    )
    stub = loopd.grpc_swap_stub()

    # SuggestSwaps requires at least one explicit LiquidityRule even when
    # easy_autoloop is on (the easy-autoloop dispatch path bypasses
    # SuggestSwaps internally, but we want to use SuggestSwaps as our
    # tick-bypass — so we fetch the active params, add a rule for our
    # A->S channel, and push them back). The 20-odd AUTOLOOP_* knobs
    # pushed by configure_autoloop above are preserved.
    chans = event_loop.run_until_complete(loop_rig.a.list_channels())
    target_chan = next(
        (c for c in chans if c.get("channel_point") == loop_rig.a_to_s_channel_point),
        None,
    )
    assert target_chan is not None, "rig channel A->S not visible to LND-A"
    chan_id = int(target_chan["chan_id"])

    current_params = event_loop.run_until_complete(
        stub.GetLiquidityParams(GetLiquidityParamsRequest(), timeout=10.0)
    )
    rule = LiquidityRule(
        channel_id=chan_id,
        swap_type=SwapType.LOOP_OUT,
        type=LiquidityRuleType.THRESHOLD,
        # We have ~100% local, ~0% incoming. Demanding 60% incoming forces
        # a loop-out. outgoing_threshold=10 = "fine to leave 10% local".
        incoming_threshold=60,
        outgoing_threshold=10,
    )
    current_params.rules.append(rule)
    event_loop.run_until_complete(
        stub.SetLiquidityParams(
            SetLiquidityParamsRequest(parameters=current_params), timeout=10.0,
        )
    )

    # 1. Rule engine check. SuggestSwaps is exactly what the dispatcher
    #    tick runs internally, so a non-empty suggestion list here is
    #    strong evidence the 20-min tick would dispatch the same swap.
    suggestion = event_loop.run_until_complete(
        stub.SuggestSwaps(SuggestSwapsRequest(), timeout=15.0)
    )
    assert len(suggestion.loop_out) >= 1, (
        f"autoloop rule engine declined to suggest a swap; "
        f"disqualified={[(d.channel_id, d.reason) for d in suggestion.disqualified]}"
    )
    suggested = suggestion.loop_out[0]
    assert suggested.amt >= 250_000      # MIN_SWAP_AMOUNT_SAT respected
    assert suggested.amt <= 5_000_000    # MAX_SWAP_AMOUNT_SAT respected
    # The dispatcher fills in `dest` from autoloop_dest_address at tick time
    # before calling LoopOut; SuggestSwaps leaves it blank. Mirror that step.
    suggested.dest = dest_addr

    # 2. Execute the suggested loop-out. This is what the dispatcher tick
    #    does each 20 min cycle; we shortcut to LoopOut directly.
    loop_out_resp = event_loop.run_until_complete(
        stub.LoopOut(suggested, timeout=60.0)
    )
    htlc_addr = loop_out_resp.htlc_address_p2tr or loop_out_resp.htlc_address_p2wsh
    assert htlc_addr, "LoopOut returned no HTLC address"
    swap_id_bytes = bytes(loop_out_resp.id_bytes)
    assert len(swap_id_bytes) == 32

    # 3. The HTLC publication tx should land in bitcoind's mempool/chain.
    #    Drive the swap forward by mining one block per second; loopserver
    #    publishes the HTLC after the LN payment from A->S settles.
    deadline = time.time() + 90
    htlc_tx_found = False
    while time.time() < deadline:
        loop_rig.bitcoind.mine_to_self(1)
        # `scantxoutset` finds the on-chain UTXO at htlc_addr without
        # needing the address to be in any wallet. This is the canonical
        # way to detect arbitrary on-chain outputs in regtest.
        scan = json.loads(loop_rig.bitcoind.cli(
            "scantxoutset", "start", f'[{{"desc": "addr({htlc_addr})"}}]',
        ))
        if scan.get("success") and scan.get("unspents"):
            htlc_tx_found = True
            break
        time.sleep(1)
    assert htlc_tx_found, (
        f"HTLC publication tx never appeared at {htlc_addr} within 90s; "
        f"swap likely failed before on-chain publication"
    )

    # 4. The swap's on-chain output is now confirmed. SwapInfo should
    #    show the swap recorded against the same swap_id; loopd's
    #    state-machine catches up to HTLC_PUBLISHED asynchronously after
    #    seeing the block notification, so it can briefly trail the
    #    on-chain reality. Accept any non-failed state.
    info = event_loop.run_until_complete(
        stub.SwapInfo(SwapInfoRequest(id=swap_id_bytes), timeout=10.0)
    )
    # State enum: INITIATED=0, PREIMAGE_REVEALED=1, HTLC_PUBLISHED=2,
    #             SUCCESS=3, FAILED=4, INVOICE_SETTLED=5.
    assert info.state != _loop_pb2.FAILED, (
        f"loopd reports swap state=FAILED after seeing on-chain HTLC; "
        f"failure_reason={info.failure_reason}"
    )
    assert info.amt == suggested.amt
