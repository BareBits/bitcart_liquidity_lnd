"""Pin the fee-control parameters passed to every LND-direct call site.

Each test mocks the gRPC stub, captures the request object the engine
constructs, and asserts on its fee-related fields. No regtest LND
needed; these are pure-Python unit tests verifying the engine's
config-driven fee policy.

Why this matters: without these tests, a future refactor could
silently drop `target_conf` / `fee_limit` / `max_fee_per_vbyte`,
re-introducing the pre-fix behaviors:
  - On-chain payments at 1 sat/vbyte → never confirm on mainnet.
  - LN payments with no fee_limit → LND default kicks in, which has
    drifted across versions and once silently allowed multi-thousand-
    sat routing fees on tiny payments.
  - Channel opens at 6-block default → 2x the necessary fee.
  - Cooperative closes with no max_fee_per_vbyte → mempool spikes
    burn channel balance on the close fee.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Dict, Optional

import pytest

import liquidityhelper
from lnd_proto import lightning_pb2


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------

def _run(coro):
    # Use new_event_loop().run_until_complete() — creates a one-shot
    # loop and never touches the thread-current-loop pointer. We
    # cannot use asyncio.run() here because it calls
    # set_event_loop(None) on exit, which unsets the session
    # event_loop fixture loop for every downstream test in the same
    # process — breaking electrum_network_tests / onchain_cashout /
    # own_lightning_nodes when they call asyncio.get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _CapturedCall:
    """Holds the request the engine passed to a gRPC stub method.
    `request` is set on the first call; `response` is what to return."""
    def __init__(self, response: Any = None):
        self.request = None
        self.response = response
        self.called = False

    async def __call__(self, request, timeout: Optional[float] = None):
        self.request = request
        self.called = True
        return self.response


class _StreamingCapture:
    """Async-iterator stub for streaming RPCs (CloseChannel).
    Captures the request, yields a single fake update, then closes."""
    def __init__(self, update: Any = None):
        self.request = None
        self.update = update
        self.called = False

    def __call__(self, request):
        self.request = request
        self.called = True
        return self  # __aiter__ on self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.update is None:
            raise StopAsyncIteration
        u = self.update
        self.update = None
        return u

    def cancel(self):
        pass


class _FakeLightningStub:
    """Captures SendCoins, SendPaymentSync, CloseChannel, and
    DecodePayReq calls. Each captured-call object exposes the
    request the engine constructed."""
    def __init__(
        self,
        send_coins_response=None,
        send_payment_response=None,
        close_channel_update=None,
        decode_pay_req_response=None,
    ):
        self.SendCoins = _CapturedCall(response=send_coins_response)
        self.SendPaymentSync = _CapturedCall(response=send_payment_response)
        self.CloseChannel = _StreamingCapture(update=close_channel_update)
        self.DecodePayReq = _CapturedCall(response=decode_pay_req_response)


def _wire_fake_stub(monkeypatch, wallet_id: str, stub: _FakeLightningStub):
    """Pre-populate liquidityhelper._LND_CONNECTIONS with a fake stub
    so `_get_lnd_connection` returns it without dialing a real LND."""
    monkeypatch.setattr(
        liquidityhelper, "_LND_CONNECTIONS",
        {wallet_id: {
            "channel": object(),   # not used by the fns we're testing
            "stubs": {"Lightning": stub},
        }},
    )


# ---------------------------------------------------------------------------
# _lnd_pay_onchain
# ---------------------------------------------------------------------------

def test_lnd_pay_onchain_uses_target_conf_from_config(monkeypatch):
    """Default config: explicit sat/vbyte is 0 → target_conf wins.
    Pins that the engine passes the configured block target to LND."""
    monkeypatch.setattr(liquidityhelper, "LND_ONCHAIN_TARGET_CONF", 6, raising=False)
    monkeypatch.setattr(liquidityhelper, "LND_ONCHAIN_FEE_RATE_SAT_PER_VBYTE", 0, raising=False)
    stub = _FakeLightningStub(
        send_coins_response=lightning_pb2.SendCoinsResponse(txid="aa" * 32),
    )
    _wire_fake_stub(monkeypatch, "w1", stub)

    ok = _run(liquidityhelper._lnd_pay_onchain(
        api=None, wallet_id="w1", dest_addr="bc1qx", amount_btc=0.0001, label="t",
    ))
    assert ok is True
    req = stub.SendCoins.request
    assert req.target_conf == 6, f"target_conf not propagated: {req.target_conf}"
    assert req.sat_per_vbyte == 0, (
        "explicit override should be unset when LND_ONCHAIN_FEE_RATE_SAT_PER_VBYTE=0; "
        f"got sat_per_vbyte={req.sat_per_vbyte}"
    )
    # Sanity: the prior hardcoded 1 sat/vbyte is gone.
    assert req.sat_per_vbyte != 1, (
        "regression: SendCoins still pins sat_per_vbyte=1 (mainnet-broken)"
    )


def test_lnd_pay_onchain_caller_target_conf_overrides_default(monkeypatch):
    """When the caller passes `target_conf=` explicitly (e.g. cashout
    path passes LND_CASHOUT_TARGET_CONF=144), that wins over the
    config-default LND_ONCHAIN_TARGET_CONF. Pins the per-payment-type
    target_conf wiring."""
    monkeypatch.setattr(liquidityhelper, "LND_ONCHAIN_TARGET_CONF", 6, raising=False)
    monkeypatch.setattr(liquidityhelper, "LND_ONCHAIN_FEE_RATE_SAT_PER_VBYTE", 0, raising=False)
    stub = _FakeLightningStub(
        send_coins_response=lightning_pb2.SendCoinsResponse(txid="cc" * 32),
    )
    _wire_fake_stub(monkeypatch, "w1", stub)

    _run(liquidityhelper._lnd_pay_onchain(
        api=None, wallet_id="w1", dest_addr="bc1qx", amount_btc=0.0001, label="t",
        target_conf=144,
    ))
    req = stub.SendCoins.request
    assert req.target_conf == 144, (
        f"caller-supplied target_conf must win over LND_ONCHAIN_TARGET_CONF; "
        f"got target_conf={req.target_conf}"
    )


def test_electrum_pay_onchain_threads_target_conf_to_lnd_path(monkeypatch):
    """electrum_pay_onchain's btclnd dispatch must thread the
    caller's target_conf through to _lnd_pay_onchain. Pins the
    public-API plumbing so a future cashout caller passing
    target_conf=LND_CASHOUT_TARGET_CONF reaches the actual
    SendCoinsRequest."""
    monkeypatch.setattr(liquidityhelper, "LND_ONCHAIN_TARGET_CONF", 6, raising=False)
    monkeypatch.setattr(liquidityhelper, "LND_ONCHAIN_FEE_RATE_SAT_PER_VBYTE", 0, raising=False)
    stub = _FakeLightningStub(
        send_coins_response=lightning_pb2.SendCoinsResponse(txid="dd" * 32),
    )
    _wire_fake_stub(monkeypatch, "w1", stub)

    wallet = {"id": "w1", "currency": "btclnd"}
    _run(liquidityhelper.electrum_pay_onchain(
        dest_addr="bc1qx", amount=0.0001, label="lnhelper_cashout",
        wallet=wallet, api=None, target_conf=144,
    ))
    req = stub.SendCoins.request
    assert req.target_conf == 144


def test_lnd_pay_onchain_explicit_rate_override_wins(monkeypatch):
    """When the operator sets LND_ONCHAIN_FEE_RATE_SAT_PER_VBYTE > 0,
    that wins over target_conf. Pins the operator-override path."""
    monkeypatch.setattr(liquidityhelper, "LND_ONCHAIN_TARGET_CONF", 6, raising=False)
    monkeypatch.setattr(liquidityhelper, "LND_ONCHAIN_FEE_RATE_SAT_PER_VBYTE", 25, raising=False)
    stub = _FakeLightningStub(
        send_coins_response=lightning_pb2.SendCoinsResponse(txid="bb" * 32),
    )
    _wire_fake_stub(monkeypatch, "w1", stub)

    _run(liquidityhelper._lnd_pay_onchain(
        api=None, wallet_id="w1", dest_addr="bc1qx", amount_btc=0.0001, label="t",
    ))
    req = stub.SendCoins.request
    assert req.sat_per_vbyte == 25
    assert req.target_conf == 0, "target_conf must be unset when an explicit rate is given"


# ---------------------------------------------------------------------------
# _lnd_pay_ln_invoice
# ---------------------------------------------------------------------------

def test_lnd_pay_ln_invoice_sets_fee_limit_from_invoice_amount(monkeypatch):
    """2% of invoice amount with a 50-sat floor. 10,000 sat invoice
    → cap = max(50, 10000*0.02) = 200 sats."""
    monkeypatch.setattr(liquidityhelper, "LN_PAYMENT_FEE_LIMIT_PERCENT", 0.02, raising=False)
    monkeypatch.setattr(liquidityhelper, "LN_PAYMENT_FEE_LIMIT_MIN_SAT", 50, raising=False)
    stub = _FakeLightningStub(
        decode_pay_req_response=lightning_pb2.PayReq(num_satoshis=10_000),
        send_payment_response=lightning_pb2.SendResponse(payment_hash=b"\x00" * 32),
    )
    _wire_fake_stub(monkeypatch, "w1", stub)

    ok = _run(liquidityhelper._lnd_pay_ln_invoice(
        api=None, wallet_id="w1", invoice="lnbc100u…", label="t",
    ))
    assert ok is True
    req = stub.SendPaymentSync.request
    # SendRequest.fee_limit is a FeeLimit message; the oneof we set is `fixed`.
    assert req.HasField("fee_limit"), "fee_limit must be set on every LN payment"
    assert req.fee_limit.fixed == 200, (
        f"expected 2% of 10000 = 200; got {req.fee_limit.fixed}"
    )


def test_lnd_pay_ln_invoice_fee_limit_uses_min_floor_for_tiny_payment(monkeypatch):
    """Tiny payment where 2% rounds below 50 → use the 50-sat floor.
    Pins against a regression where the path-finder rejects on
    fee_limit_sat=0 for very small payments."""
    monkeypatch.setattr(liquidityhelper, "LN_PAYMENT_FEE_LIMIT_PERCENT", 0.02, raising=False)
    monkeypatch.setattr(liquidityhelper, "LN_PAYMENT_FEE_LIMIT_MIN_SAT", 50, raising=False)
    stub = _FakeLightningStub(
        decode_pay_req_response=lightning_pb2.PayReq(num_satoshis=100),
        send_payment_response=lightning_pb2.SendResponse(payment_hash=b"\x00" * 32),
    )
    _wire_fake_stub(monkeypatch, "w1", stub)

    _run(liquidityhelper._lnd_pay_ln_invoice(
        api=None, wallet_id="w1", invoice="lnbc1u…", label="t",
    ))
    req = stub.SendPaymentSync.request
    # 100 * 0.02 = 2; floor lifts it to 50.
    assert req.fee_limit.fixed == 50


def test_lnd_pay_ln_invoice_falls_back_without_fee_limit_when_decode_fails(monkeypatch):
    """If DecodePayReq raises (network blip / malformed invoice), we
    still attempt the payment with LND's default fee policy rather
    than hard-failing. Pins the best-effort fallback."""
    monkeypatch.setattr(liquidityhelper, "LN_PAYMENT_FEE_LIMIT_PERCENT", 0.02, raising=False)
    monkeypatch.setattr(liquidityhelper, "LN_PAYMENT_FEE_LIMIT_MIN_SAT", 50, raising=False)

    class _BrokenDecode:
        called = False
        async def __call__(self, request, timeout=None):
            self.called = True
            raise RuntimeError("simulated DecodePayReq failure")

    stub = _FakeLightningStub(
        send_payment_response=lightning_pb2.SendResponse(payment_hash=b"\x00" * 32),
    )
    stub.DecodePayReq = _BrokenDecode()
    _wire_fake_stub(monkeypatch, "w1", stub)

    ok = _run(liquidityhelper._lnd_pay_ln_invoice(
        api=None, wallet_id="w1", invoice="lnbc…", label="t",
    ))
    assert ok is True
    req = stub.SendPaymentSync.request
    assert not req.HasField("fee_limit"), (
        "fee_limit should be unset when decode failed; engine must "
        "fall back to LND default rather than hard-fail"
    )


# ---------------------------------------------------------------------------
# _lnd_keysend
# ---------------------------------------------------------------------------

def test_lnd_keysend_sets_fee_limit_from_amount(monkeypatch):
    """keysend amount is a parameter (not encoded in an invoice) so
    the fee cap is computed directly. 5000 sats → max(50, 100) = 100."""
    monkeypatch.setattr(liquidityhelper, "LN_PAYMENT_FEE_LIMIT_PERCENT", 0.02, raising=False)
    monkeypatch.setattr(liquidityhelper, "LN_PAYMENT_FEE_LIMIT_MIN_SAT", 50, raising=False)
    stub = _FakeLightningStub(
        send_payment_response=lightning_pb2.SendResponse(payment_hash=b"\x00" * 32),
    )
    _wire_fake_stub(monkeypatch, "w1", stub)

    pk = "ab" * 33  # 66-char hex, parses as 33 bytes
    _run(liquidityhelper._lnd_keysend(
        api=None, wallet_id="w1", dest_pubkey=pk,
        amount_sat=5_000, outgoing_chan_id=1234, label="t",
    ))
    req = stub.SendPaymentSync.request
    assert req.HasField("fee_limit")
    assert req.fee_limit.fixed == 100, f"expected 5000*0.02=100; got {req.fee_limit.fixed}"


# ---------------------------------------------------------------------------
# CloseChannel
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# OpenChannelSync — CSV wiring (path 2: direct-channel cashout, path 3:
# move_onchain_to_ln). Both paths now go DIRECTLY to LND via lnd_rpc
# (move_onchain_to_ln used to go through Bitcart's api.open_ln_channel
# — that path doesn't accept CSV params, so we switched to direct).
# ---------------------------------------------------------------------------

def test_move_onchain_to_ln_uses_direct_lnd_rpc_with_csv_params(monkeypatch, event_loop):
    """move_onchain_to_ln now bypasses api.open_ln_channel and calls
    lnd_rpc directly so it can set max_local_csv (the cap on OUR CSV
    delay if we force-close). Pin both the bypass AND the CSV param.
    """
    import liquidityhelper as lh
    from node_database import LightningNode
    monkeypatch.setattr(lh, "AUTOMATIC_CHANNEL_CREATION_ENABLED", True, raising=False)
    monkeypatch.setattr(lh, "LND_MAX_LOCAL_CSV_BLOCKS", 144, raising=False)
    monkeypatch.setattr(lh, "LND_REMOTE_CSV_DELAY_BLOCKS", 0, raising=False)
    monkeypatch.setattr(lh, "LND_CHANNEL_OPEN_TARGET_CONF", 12, raising=False)
    monkeypatch.setattr(lh, "DRY_RUN_FUNDS", False, raising=False)
    lh._last_decision_state.clear()

    # A peer in the candidate DB (else move_onchain_to_ln skips it).
    LightningNode.delete().execute()
    LightningNode.create(
        node_address="ab" * 33,
        alias="test-peer",
        ipv4_address="1.2.3.4:9735",
        total_capacity=10_000_000,
        channel_count=10,
        median_outbound_fee_rate_ppm=1,
        effective_degree=20,
        two_hop_reach=100,
        last_updated=lh.utcnow_naive(),
    )
    monkeypatch.setattr(lh, "is_node_blacklisted", lambda n: (False, None))

    # Fake pick_best_channel_partners → return our test peer URI.
    peer_uri = "ab" * 33 + "@1.2.3.4:9735"
    async def fake_pick(*a, **kw):
        return [peer_uri]
    monkeypatch.setattr(lh, "pick_best_channel_partners", fake_pick)

    # Track the lnd_rpc call so we can inspect args.
    lnd_calls: list = []
    async def fake_lnd_rpc(api_obj, wallet_id, method, params, service):
        lnd_calls.append((method, params))
        if method == "OpenChannelSync":
            return {"funding_txid_str": "aa" * 32, "output_index": 0}
        return {}
    monkeypatch.setattr(lh, "lnd_rpc", fake_lnd_rpc)

    class _FakeAPI:
        async def get_wallet(self, wid):
            return {"id": wid, "currency": "btclnd"}
        async def get_wallet_ln_channels(self, *a, **kw):
            return []
        # Should NEVER be called — the refactor must NOT route through
        # the Bitcart HTTP open_ln_channel endpoint.
        called_open_ln_channel = False
        async def open_ln_channel(self, *a, **kw):
            type(self).called_open_ln_channel = True
            raise AssertionError(
                "move_onchain_to_ln must use lnd_rpc directly, NOT "
                "api.open_ln_channel — Bitcart's open_channel endpoint "
                "doesn't accept CSV params"
            )

    ok = event_loop.run_until_complete(
        lh.move_onchain_to_ln(wallet_id="w1", amount_sats=200_000, api=_FakeAPI()),
    )
    assert ok is True, "happy path should return True"
    # OpenChannelSync was called with our CSV + fee knobs.
    open_calls = [p for m, p in lnd_calls if m == "OpenChannelSync"]
    assert len(open_calls) == 1, f"expected 1 OpenChannelSync; got {len(open_calls)}"
    params = open_calls[0]
    assert params["node_pubkey_string"] == "ab" * 33
    assert params["local_funding_amount"] == 200_000
    assert params["max_local_csv"] == 144, (
        f"max_local_csv must be threaded from config; got {params.get('max_local_csv')}"
    )
    assert params["target_conf"] == 12
    # remote_csv_delay omitted when config is 0 (let LND auto-scale).
    assert "remote_csv_delay" not in params, (
        "remote_csv_delay=0 should mean 'let LND auto-scale' — don't "
        "send the field explicitly"
    )
    # ConnectPeer was called first.
    assert any(m == "ConnectPeer" for m, p in lnd_calls), (
        "must ConnectPeer before OpenChannelSync — the Bitcart daemon "
        "auto-connected; doing it ourselves now"
    )
    assert _FakeAPI.called_open_ln_channel is False
    LightningNode.delete().execute()


def test_direct_channel_cashout_threads_csv_to_open_channel_sync(monkeypatch, event_loop):
    """The PREFER_LN_CASHOUT direct-channel cashout path (path 2)
    also threads max_local_csv. Pin the wiring at that site too —
    duplicate of the move_onchain_to_ln test for the other channel-
    open site so a future regression on either is caught."""
    import liquidityhelper as lh
    monkeypatch.setattr(lh, "LND_MAX_LOCAL_CSV_BLOCKS", 144, raising=False)
    monkeypatch.setattr(lh, "LND_REMOTE_CSV_DELAY_BLOCKS", 0, raising=False)
    monkeypatch.setattr(lh, "LND_CHANNEL_OPEN_TARGET_CONF", 12, raising=False)
    monkeypatch.setattr(lh, "OWN_LIGHTNING_NODES", ["ef" * 33 + "@1.2.3.4:9735"], raising=False)
    monkeypatch.setattr(lh, "OWN_LIGHTNING_NODES_ANNOUNCE_CHANNELS", False, raising=False)
    monkeypatch.setattr(lh, "DRY_RUN_FUNDS", False, raising=False)
    lh._last_decision_state.clear()

    captured: dict = {}
    async def fake_lnd_rpc(api_obj, wallet_id, method, params, service):
        if method == "OpenChannelSync":
            captured.update(params)
            return {"funding_txid_str": "ee" * 32, "output_index": 0}
        return {}
    monkeypatch.setattr(lh, "lnd_rpc", fake_lnd_rpc)
    # LabelTransaction best-effort; let it noop.
    async def fake_label(*a, **kw):
        return None
    monkeypatch.setattr(lh, "label_onchain_tx", fake_label)
    event_loop.run_until_complete(
        lh._attempt_direct_channel_cashout_to_own_node(
            api=None, wallet_id="w1", channel_size_sats=200_000,
        ),
    )
    assert captured.get("max_local_csv") == 144
    assert captured.get("target_conf") == 12
    assert "remote_csv_delay" not in captured
    """When LND_REMOTE_CSV_DELAY_BLOCKS > 0, that value is passed to
    OpenChannelSync. When 0, the field is omitted (LND auto-scales)."""
    import liquidityhelper as lh
    from node_database import LightningNode
    monkeypatch.setattr(lh, "AUTOMATIC_CHANNEL_CREATION_ENABLED", True, raising=False)
    monkeypatch.setattr(lh, "LND_MAX_LOCAL_CSV_BLOCKS", 144, raising=False)
    monkeypatch.setattr(lh, "LND_REMOTE_CSV_DELAY_BLOCKS", 288, raising=False)
    monkeypatch.setattr(lh, "DRY_RUN_FUNDS", False, raising=False)
    lh._last_decision_state.clear()
    LightningNode.delete().execute()
    LightningNode.create(
        node_address="cd" * 33, alias="test-peer",
        ipv4_address="1.2.3.4:9735",
        total_capacity=10_000_000, channel_count=10,
        median_outbound_fee_rate_ppm=1,
        effective_degree=20, two_hop_reach=100,
        last_updated=lh.utcnow_naive(),
    )
    monkeypatch.setattr(lh, "is_node_blacklisted", lambda n: (False, None))
    async def fake_pick(*a, **kw):
        return ["cd" * 33 + "@1.2.3.4:9735"]
    monkeypatch.setattr(lh, "pick_best_channel_partners", fake_pick)
    captured: dict = {}
    async def fake_lnd_rpc(api_obj, wallet_id, method, params, service):
        if method == "OpenChannelSync":
            captured.update(params)
            return {"funding_txid_str": "bb" * 32, "output_index": 0}
        return {}
    monkeypatch.setattr(lh, "lnd_rpc", fake_lnd_rpc)
    class _FakeAPI:
        async def get_wallet(self, wid):
            return {"id": wid, "currency": "btclnd"}
        async def get_wallet_ln_channels(self, *a, **kw):
            return []
    event_loop.run_until_complete(
        lh.move_onchain_to_ln(wallet_id="w1", amount_sats=100_000, api=_FakeAPI()),
    )
    assert captured.get("remote_csv_delay") == 288, (
        f"remote_csv_delay must be threaded from config when > 0; got "
        f"{captured.get('remote_csv_delay')}"
    )
    LightningNode.delete().execute()


def test_close_channel_passes_target_conf_and_max_fee(monkeypatch):
    """Cooperative close must set both target_conf (predictable
    timing) and max_fee_per_vbyte (caps mempool-spike damage)."""
    monkeypatch.setattr(liquidityhelper, "LND_CHANNEL_CLOSE_TARGET_CONF", 6, raising=False)
    monkeypatch.setattr(liquidityhelper, "LND_CHANNEL_CLOSE_MAX_FEE_SAT_PER_VBYTE", 50, raising=False)
    fake_update = lightning_pb2.CloseStatusUpdate(
        close_pending=lightning_pb2.PendingUpdate(txid=b"\x11" * 32, output_index=0),
    )
    stub = _FakeLightningStub(close_channel_update=fake_update)
    _wire_fake_stub(monkeypatch, "w1", stub)

    _run(liquidityhelper._lnd_close_channel(
        api=None, wallet_id="w1",
        channel_point="aa" * 32 + ":0", force=False,
    ))
    req = stub.CloseChannel.request
    assert req.target_conf == 6, f"target_conf not propagated: {req.target_conf}"
    assert req.max_fee_per_vbyte == 50, (
        f"max_fee_per_vbyte not propagated: {req.max_fee_per_vbyte}"
    )
