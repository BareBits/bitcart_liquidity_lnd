"""End-to-end test of the OWN_LIGHTNING_NODES feature, exercising the
3-step cycle described in the operator-facing docstring:

  Phase 1: on-chain → LN cashout opens a NEW channel directly to one of
           OWN_LIGHTNING_NODES with push_sat=98% of capacity. Almost all
           of the funds land on the peer's side (the cashout
           destination), satisfying the "fee-free LN cashout" goal.

  Phase 2: With that channel open, the operator's external node
           ("clientnode") sends some sats BACK to the bitcart wallet.
           Simulates the operator topping bitcart up via LN for any
           reason (channel rebalance, customer-payment that ended up
           on the wrong side, etc.).

  Phase 3: A subsequent LN-cashout cycle observes the local balance on
           the OWN-node channel and KEYSENDS it back to that peer via
           the direct channel. No invoice, no public-graph routing,
           zero LN fees beyond the single-hop forward.

All three phases run as one big test because each depends on the state
the previous phase left behind. The whole thing uses the new
`lnd_pair_no_channels` fixture (bitcoind + LND-A + LND-B, funded but no
auto-opened channels) so phase 1 actually has work to do.

Configured to run in <30s on regtest. Skips automatically when the
necessary binaries / fixtures aren't available.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from typing import Any, Dict, List

import pytest

import liquidityhelper
from database import SimpleDateTimeField
from lnd_proto import lightning_pb2 as lnd_pb2
from lnd_proto import lightning_pb2_grpc as lnd_pb2_grpc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Same TLV record id the engine uses for keysend (BOLT04 / Lightning spec).
_LN_KEYSEND_RECORD = 5482373484


def _wire_lnd_connection(monkeypatch, *, wallet_id: str, lnd_node) -> None:
    """Pre-populate liquidityhelper._LND_CONNECTIONS for `wallet_id` so
    the engine's lnd_rpc() can dispatch into our test LND directly,
    bypassing BitcartAPI (which we don't have a regtest stub of)."""
    import grpc as _grpc
    ssl_creds = _grpc.ssl_channel_credentials(root_certificates=lnd_node._tls_cert)
    macaroon_hex = lnd_node._macaroon_hex

    def _mac_cb(_ctx, callback):
        callback([("macaroon", macaroon_hex)], None)

    creds = _grpc.composite_channel_credentials(
        ssl_creds, _grpc.metadata_call_credentials(_mac_cb)
    )
    channel = _grpc.aio.secure_channel(
        f"127.0.0.1:{lnd_node.rpc_port}", creds,
        options=[("grpc.ssl_target_name_override", "localhost")],
    )
    # The engine accesses connection["stubs"][service]. We populate
    # Lightning (used for OpenChannelSync, ConnectPeer, SendPaymentSync,
    # ListChannels) — that's all the OWN_LIGHTNING_NODES path needs.
    # WalletKit (for LabelTransaction) is intentionally NOT wired up;
    # the LabelTransaction call inside _attempt_direct_channel_cashout_to_own_node
    # is best-effort and logged-but-tolerated when it fails.
    monkeypatch.setattr(
        liquidityhelper, "_LND_CONNECTIONS",
        {wallet_id: {
            "channel": channel,
            "stubs": {"Lightning": lnd_pb2_grpc.LightningStub(channel)},
            "macaroon_hex": macaroon_hex,
            "tls_cert": lnd_node._tls_cert,
        }}
    )


async def _wait_for_channel_active(
    lnd_node, channel_point: str, timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Poll until `channel_point` shows up active on `lnd_node`, OR raise."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        chans = await lnd_node.list_channels()
        for ch in chans:
            if ch.get("channel_point") == channel_point and ch.get("active"):
                return ch
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"channel {channel_point} on {lnd_node.name} never became active "
        f"within {timeout_s}s"
    )


async def _send_keysend(
    from_node, dest_pubkey: str, amount_sat: int,
    outgoing_chan_id: int = 0,
) -> Dict[str, Any]:
    """Send a keysend from `from_node` to `dest_pubkey`. Uses the
    Lightning.SendPaymentSync RPC the same way the engine's
    _lnd_keysend helper does."""
    preimage = secrets.token_bytes(32)
    payment_hash = hashlib.sha256(preimage).digest()
    kwargs = {
        "dest": bytes.fromhex(dest_pubkey),
        "amt": int(amount_sat),
        "payment_hash": payment_hash,
        "dest_custom_records": {_LN_KEYSEND_RECORD: preimage},
    }
    if outgoing_chan_id:
        kwargs["outgoing_chan_id"] = int(outgoing_chan_id)
    request = lnd_pb2.SendRequest(**kwargs)
    response = await from_node._stub.SendPaymentSync(request)
    if response.payment_error:
        raise AssertionError(
            f"keysend from {from_node.name} to {dest_pubkey[:16]}… "
            f"failed: {response.payment_error}"
        )
    return {
        "payment_hash": bytes(response.payment_hash).hex(),
        "fee_sat": response.payment_route.total_fees if response.payment_route else 0,
    }


# ---------------------------------------------------------------------------
# 3-phase integration test
# ---------------------------------------------------------------------------

def test_own_lightning_nodes_full_roundtrip(
    lnd_pair_no_channels, event_loop, monkeypatch,
):
    """End-to-end: open direct channel via push_sat → push back → cashout
    via keysend. See module docstring for phase breakdown."""
    pair = lnd_pair_no_channels
    bitcart_lnd = pair.a       # the "bitcart" wallet
    clientnode = pair.b        # the operator's own LN node
    wallet_id = "test-wallet-a"
    clientnode_uri = (
        f"{clientnode.identity_pubkey}@127.0.0.1:{clientnode.p2p_port}"
    )
    channel_size_sats = int(0.05 * 100_000_000)   # 5,000,000 sat / 0.05 BTC

    # Wire bitcart_lnd into the engine's _LND_CONNECTIONS so lnd_rpc()
    # routes there without needing a real BitcartAPI.
    _wire_lnd_connection(monkeypatch, wallet_id=wallet_id, lnd_node=bitcart_lnd)

    # Engine config: turn on PREFER_LN_CASHOUT (so the direct-channel
    # path is the one under test); set OWN_LIGHTNING_NODES to point at
    # clientnode; private channel (default) so the test doesn't need
    # the public graph to converge before keysends work.
    monkeypatch.setattr(liquidityhelper, "PREFER_LN_CASHOUT", True, raising=False)
    monkeypatch.setattr(liquidityhelper, "OWN_LIGHTNING_NODES", [clientnode_uri], raising=False)
    monkeypatch.setattr(
        liquidityhelper, "OWN_LIGHTNING_NODES_ANNOUNCE_CHANNELS",
        False, raising=False,
    )
    # Lower MIN_LN_CASHOUT_IN_SATS so phase 3 doesn't get gated out.
    monkeypatch.setattr(liquidityhelper, "MIN_LN_CASHOUT_IN_SATS", 100, raising=False)

    # =====================================================================
    # Phase 1: on-chain → LN cashout opens new channel with push_sat
    # =====================================================================
    result_phase1 = event_loop.run_until_complete(
        liquidityhelper._attempt_direct_channel_cashout_to_own_node(
            api=None,                # not used; _LND_CONNECTIONS is pre-wired
            wallet_id=wallet_id,
            channel_size_sats=channel_size_sats,
        )
    )
    assert result_phase1 is True, (
        "_attempt_direct_channel_cashout_to_own_node should return True "
        "when OWN_LIGHTNING_NODES has a reachable peer and channel-open "
        "succeeds"
    )

    # Confirm the funding tx so the channel becomes active. The engine's
    # OpenChannelSync returned when the funding tx was broadcast; we need
    # 6 confs to mark the channel "active" for routing.
    pair.bitcoind.mine_to_self(10)

    async def _find_a_to_b_channel():
        """Poll bitcart_lnd.list_channels() until we see the channel to
        clientnode appear and become active. Returns the channel dict."""
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            for ch in await bitcart_lnd.list_channels():
                if ch.get("remote_pubkey", "").lower() == clientnode.identity_pubkey.lower():
                    if ch.get("active"):
                        return ch
            await asyncio.sleep(0.5)
        raise AssertionError(
            "channel from bitcart_lnd to clientnode never became active"
        )

    channel = event_loop.run_until_complete(_find_a_to_b_channel())

    # *** The core phase-1 assertion: nearly all the channel capacity
    # is on the REMOTE (clientnode's) side because of push_sat ***
    local_sat = int(channel.get("local_balance") or 0)
    remote_sat = int(channel.get("remote_balance") or 0)
    capacity_sat = int(channel.get("capacity") or 0)
    assert capacity_sat == channel_size_sats, (
        f"channel capacity {capacity_sat} != requested {channel_size_sats}"
    )
    # push_sat in the engine is int(capacity * 0.98). We expect roughly
    # that much on the remote side. Allow some slack for commit-tx fees /
    # channel reserve / dust calculations done by LND internally.
    push_target = int(channel_size_sats * 0.98)
    assert remote_sat >= push_target - 10_000, (
        f"expected remote_balance ≥ {push_target - 10_000} (push_sat target "
        f"{push_target}) but got {remote_sat}"
    )
    assert local_sat <= 100_000, (
        f"local_balance should be near zero after push_sat (reserve only); "
        f"got {local_sat}"
    )

    # =====================================================================
    # Phase 2: clientnode sends a small keysend back to bitcart_lnd
    # =====================================================================
    # Establish the reverse peering already exists (lnd_pair_no_channels
    # did a.connect_peer(b)). For the keysend FROM b, b uses the same
    # channel — gossip is needed for routing in general but a directly-
    # connected peer can pay over a private channel via direct dispatch.
    push_back_sat = 200_000   # 200k sat back to bitcart_lnd
    chan_id_str = channel.get("chan_id") or ""
    chan_id_int = int(chan_id_str) if chan_id_str else 0

    # Wait for clientnode (B) to also see the channel as active with the
    # same SCID before initiating the keysend FROM B. _find_a_to_b_channel
    # only polled A's view; if we keysend before B has finished processing
    # the channel-open on its own side, LND surfaces it as
    # "insufficient_balance" (LND's error when it can't construct a route
    # from the specified outgoing channel, even though usable balance is
    # actually there).
    async def _wait_for_clientnode_channel():
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            for ch in await clientnode.list_channels():
                if (ch.get("remote_pubkey", "").lower() == bitcart_lnd.identity_pubkey.lower()
                        and ch.get("active")
                        and (ch.get("chan_id") or "") == chan_id_str):
                    return ch
            await asyncio.sleep(0.5)
        raise AssertionError(
            "clientnode never saw the channel as active with matching SCID"
        )

    event_loop.run_until_complete(_wait_for_clientnode_channel())

    event_loop.run_until_complete(
        _send_keysend(
            from_node=clientnode,
            dest_pubkey=bitcart_lnd.identity_pubkey,
            amount_sat=push_back_sat,
            outgoing_chan_id=chan_id_int,
        )
    )

    # Read back the channel state on bitcart_lnd's side — local_balance
    # should have grown by ~push_back_sat (minus a small forward fee
    # which for a single-hop private channel is typically zero).
    async def _wait_for_local_balance(min_local: int, timeout_s: float = 15.0):
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            for ch in await bitcart_lnd.list_channels():
                if ch.get("remote_pubkey", "").lower() == clientnode.identity_pubkey.lower():
                    if int(ch.get("local_balance") or 0) >= min_local:
                        return ch
            await asyncio.sleep(0.5)
        raise AssertionError(
            f"bitcart_lnd's local_balance on the OWN-node channel never "
            f"reached {min_local} sat within {timeout_s}s"
        )

    channel_after_pushback = event_loop.run_until_complete(
        _wait_for_local_balance(min_local=push_back_sat - 1000)
    )
    local_sat_after_pushback = int(channel_after_pushback.get("local_balance") or 0)
    assert local_sat_after_pushback >= push_back_sat - 1000, (
        f"after pushback expected local_balance ≥ {push_back_sat - 1000} "
        f"but got {local_sat_after_pushback}"
    )

    # =====================================================================
    # Phase 3: cashout mechanism keysends the new local balance back
    # =====================================================================
    # _do_keysend_cashouts_to_own_nodes is the cashout-side helper. It
    # iterates the channel list, identifies channels whose remote_pubkey
    # is in OWN_LIGHTNING_NODES, and keysends each one's local_balance
    # (minus reserve) back to that peer.
    own_channels = [
        ch for ch in event_loop.run_until_complete(bitcart_lnd.list_channels())
        if ch.get("remote_pubkey", "").lower() == clientnode.identity_pubkey.lower()
    ]
    assert own_channels, "expected one OWN-node channel at phase 3"

    result_phase3 = event_loop.run_until_complete(
        liquidityhelper._do_keysend_cashouts_to_own_nodes(
            api=None,                       # _LND_CONNECTIONS is pre-wired
            wallet_id=wallet_id,
            own_channels=own_channels,
        )
    )
    assert result_phase3 is True, (
        "_do_keysend_cashouts_to_own_nodes should return True when a "
        "keysend lands on a directly-connected OWN_LIGHTNING_NODES peer"
    )

    # After the cashout, local_balance should be back near the channel
    # reserve floor (very little left on our side; almost all balance
    # back on clientnode's side again).
    async def _wait_for_local_drained(max_local: int, timeout_s: float = 15.0):
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            for ch in await bitcart_lnd.list_channels():
                if ch.get("remote_pubkey", "").lower() == clientnode.identity_pubkey.lower():
                    if int(ch.get("local_balance") or 0) <= max_local:
                        return ch
            await asyncio.sleep(0.5)
        raise AssertionError(
            f"bitcart_lnd's local_balance never drained back below "
            f"{max_local} sat within {timeout_s}s"
        )

    channel_after_cashout = event_loop.run_until_complete(
        _wait_for_local_drained(max_local=10_000)
    )
    local_sat_after_cashout = int(channel_after_cashout.get("local_balance") or 0)
    assert local_sat_after_cashout < local_sat_after_pushback, (
        "local_balance should have decreased after keysend cashout"
    )

    # Recency timestamp bumped — confirms the engine treats this as a
    # real successful LN cashout for staleness-fallback purposes.
    last_success = liquidityhelper.get_last_date("LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT")
    assert last_success is not None, (
        "LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT should have been updated by "
        "the successful keysend"
    )
