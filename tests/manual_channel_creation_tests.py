"""Tests for the MANUAL_CHANNEL_CREATION_ENABLED gate.

When False (the default after delegating channel creation to an LSP):
  - `move_onchain_to_ln` short-circuits and returns False without
    invoking api.open_ln_channel.
  - `decide_onchain_to_ln` short-circuits and never reaches its
    per-store loop.
  - `liquidity_check`, when a store needs inbound liquidity, does not
    call `attempt_create_channels`.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import liquidityhelper
from tests._fakes import FakeBitcartAPI


def _set(monkeypatch, **kw):
    """Set module-level config knobs on liquidityhelper."""
    defaults = {
        "MANUAL_CHANNEL_CREATION_ENABLED": False,
        "PREFER_CASHOUT_ONCHAIN": False,
        "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS": None,
        "DRY_RUN_FUNDS": False,
        "MIN_INBOUND_LIQUIDITY": 100_000,
        "MIN_CHANNEL_COUNT": 2,
        "CASHOUT_LIGHTNING_ADDRESS": "test@example.com",
    }
    for name, value in {**defaults, **kw}.items():
        monkeypatch.setattr(liquidityhelper, name, value, raising=False)


# ---------------------------------------------------------------------------
# Default is False (delegating to LSP). This is the central behavioral
# pin — flipping the default would break tests that assume the LSP path.
# ---------------------------------------------------------------------------

def test_manual_channel_creation_default_is_false():
    """The shipped default is to delegate to an LSP. If you want the old
    direct-create behavior, opt in via MANUAL_CHANNEL_CREATION_ENABLED=True."""
    import importlib
    import config
    importlib.reload(config)
    assert config.MANUAL_CHANNEL_CREATION_ENABLED is False


# ---------------------------------------------------------------------------
# move_onchain_to_ln short-circuit
# ---------------------------------------------------------------------------

def test_move_onchain_to_ln_short_circuits_when_disabled(monkeypatch, event_loop):
    """Returns False immediately without invoking api.open_ln_channel."""
    _set(monkeypatch, MANUAL_CHANNEL_CREATION_ENABLED=False)

    api_calls: List[str] = []

    class _RecordingAPI:
        async def open_ln_channel(self, *a, **kw):
            api_calls.append("open_ln_channel")
            return True
        async def get_wallet_ln_channels(self, *a, **kw):
            api_calls.append("get_wallet_ln_channels")
            return []

    result = event_loop.run_until_complete(
        liquidityhelper.move_onchain_to_ln(
            wallet_id="w1", amount_sats=100_000, api=_RecordingAPI(),
        )
    )
    assert result is False
    assert api_calls == []


def test_move_onchain_to_ln_runs_when_enabled(monkeypatch, event_loop):
    """Gate flipped True: function gets past the early return. Inner
    behavior (partner pick, blacklist, etc.) is out of scope here — we
    just confirm the gate doesn't block."""
    _set(monkeypatch, MANUAL_CHANNEL_CREATION_ENABLED=True)

    # pick_best_channel_partners returns no partners -> function returns
    # False at the "no partners found" check, NOT at the new gate. That
    # difference is what we're verifying.
    async def empty_partners(*a, **kw):
        return []
    monkeypatch.setattr(
        liquidityhelper, "pick_best_channel_partners", empty_partners,
    )

    class _StubAPI:
        async def get_wallet(self, wallet_id):
            # move_onchain_to_ln checks currency before partner-pick;
            # btclnd is the only path that reaches the
            # pick_best_channel_partners call we're verifying.
            return {"id": wallet_id, "currency": "btclnd"}
        async def get_wallet_ln_channels(self, *a, **kw):
            return []

    # If the early return fired we'd never reach pick_best_channel_partners.
    # Instrument by setting an attribute on the function.
    called = {"pick": 0}
    async def tracking_partners(*a, **kw):
        called["pick"] += 1
        return []
    monkeypatch.setattr(
        liquidityhelper, "pick_best_channel_partners", tracking_partners,
    )

    event_loop.run_until_complete(
        liquidityhelper.move_onchain_to_ln(
            wallet_id="w1", amount_sats=100_000, api=_StubAPI(),
        )
    )
    assert called["pick"] == 1


# ---------------------------------------------------------------------------
# decide_onchain_to_ln short-circuit
# ---------------------------------------------------------------------------

def test_decide_onchain_to_ln_short_circuits_when_disabled(monkeypatch, event_loop):
    """Function returns before its per-store loop, so api.get_stores
    is never even called."""
    _set(monkeypatch, MANUAL_CHANNEL_CREATION_ENABLED=False)

    calls = []

    class _RecordingAPI:
        async def get_stores(self):
            calls.append("get_stores")
            return []

    event_loop.run_until_complete(
        liquidityhelper.decide_onchain_to_ln(_RecordingAPI())
    )
    assert calls == []


def test_decide_onchain_to_ln_runs_when_enabled(monkeypatch, event_loop):
    """Gate flipped True: function reaches its per-store loop."""
    _set(monkeypatch, MANUAL_CHANNEL_CREATION_ENABLED=True)

    calls = []

    class _RecordingAPI:
        async def get_stores(self):
            calls.append("get_stores")
            return []

    event_loop.run_until_complete(
        liquidityhelper.decide_onchain_to_ln(_RecordingAPI())
    )
    assert calls == ["get_stores"]


# ---------------------------------------------------------------------------
# liquidity_check skips channel-open when disabled
# ---------------------------------------------------------------------------

def test_liquidity_check_skips_open_when_disabled(monkeypatch, event_loop):
    """Store needs inbound liquidity, but we've delegated to an LSP.
    liquidity_check should NOT call attempt_create_channels."""
    _set(monkeypatch, MANUAL_CHANNEL_CREATION_ENABLED=False)

    # Force store_needs_liquidity to report "needs more" — that's what
    # routes the function toward the channel-open block we're gating.
    from liquidityhelper import LiquidityNeed

    async def needs_liquidity(store_id, api, *args, **kwargs):
        return LiquidityNeed(
            liquidity_needed_sat=100_000,
            channels_needed=2,
        )
    monkeypatch.setattr(liquidityhelper, "store_needs_liquidity", needs_liquidity)

    async def no_topup(api, store_id):
        return None
    monkeypatch.setattr(liquidityhelper, "store_needs_topup", no_topup)

    async def no_recent_close(api, wallet_id):
        return None
    monkeypatch.setattr(liquidityhelper, "get_most_recent_channel_close", no_recent_close)

    async def no_offline_channels(*a, **kw):
        return None
    monkeypatch.setattr(liquidityhelper, "find_offline_channels", no_offline_channels)

    create_calls: List[Any] = []

    async def fake_attempt_create_channels(*a, **kw):
        create_calls.append(("attempt_create_channels", a, kw))
        return False
    monkeypatch.setattr(
        liquidityhelper, "attempt_create_channels", fake_attempt_create_channels,
    )

    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd", balance=0.05)
    api.add_store("s1", wallets=["w1"])
    # Stub the methods liquidity_check needs from the API. The fake's
    # is_channel_open_pending returns False by default; get_store_total_liquidity
    # we need to add.

    async def get_total(*a, **kw):
        return 0
    api.get_store_total_liquidity = get_total
    api.is_channel_open_pending = lambda wid: _async_return(False)

    event_loop.run_until_complete(liquidityhelper.liquidity_check(api))

    assert create_calls == [], (
        "attempt_create_channels should not be called when "
        "MANUAL_CHANNEL_CREATION_ENABLED=False"
    )


def test_liquidity_check_falls_through_to_open_when_enabled(monkeypatch, event_loop):
    """Sanity check on the other side of the gate: with the flag True,
    `attempt_create_channels` IS reached when a store needs liquidity.

    Need a liquidity_needed_sat large enough that the per-channel size
    after distribute_sats_over_channels exceeds MIN_CHANNEL_SIZE_IN_SATS
    (60_000 default), or liquidity_check skips at the pre-check."""
    _set(monkeypatch, MANUAL_CHANNEL_CREATION_ENABLED=True)

    from liquidityhelper import LiquidityNeed

    async def needs_liquidity(store_id, api, *args, **kwargs):
        return LiquidityNeed(
            liquidity_needed_sat=500_000,    # 2x channels -> ~250k each, > 60k min
            channels_needed=2,
        )
    monkeypatch.setattr(liquidityhelper, "store_needs_liquidity", needs_liquidity)

    async def no_topup(api, store_id):
        return None
    monkeypatch.setattr(liquidityhelper, "store_needs_topup", no_topup)

    async def no_recent_close(api, wallet_id):
        return None
    monkeypatch.setattr(liquidityhelper, "get_most_recent_channel_close", no_recent_close)

    async def no_offline_channels(*a, **kw):
        return None
    monkeypatch.setattr(liquidityhelper, "find_offline_channels", no_offline_channels)

    create_calls: List[Any] = []

    async def fake_attempt_create_channels(*a, **kw):
        create_calls.append(True)
        return True
    monkeypatch.setattr(
        liquidityhelper, "attempt_create_channels", fake_attempt_create_channels,
    )

    api = FakeBitcartAPI()
    # Funded wallet with enough to cover reserves for 2 channels.
    api.add_wallet("w1", currency="btclnd", balance=0.5)
    api.add_store("s1", wallets=["w1"])

    async def get_total(*a, **kw):
        return 0
    api.get_store_total_liquidity = get_total
    api.is_channel_open_pending = lambda wid: _async_return(False)

    event_loop.run_until_complete(liquidityhelper.liquidity_check(api))
    assert create_calls == [True]


# ---------------------------------------------------------------------------
# small helper
# ---------------------------------------------------------------------------

def _async_return(v):
    async def _coro():
        return v
    return _coro()
