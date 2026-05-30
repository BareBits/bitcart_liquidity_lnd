"""Tests for wallet-currency dispatch guards and the bidialect
channel-state filter.

The recent review identified three production functions that took a
wallet param without checking `wallet["currency"]`:
  - configure_autoloop      (LND-only — loopd talks to LND)
  - pick_best_lsp_for_inbound (LND-only — LSPS1 path)
  - pick_best_swap_provider_for_out (LND-only — loopd-backed)

Each was protected only by its caller checking. These tests pin a
defensive check inside each so a future direct caller can't bypass.

Plus: the get_wallet_ln_channels filter in classes.py used to assume
Electrum-only state vocabulary (OPEN/OPENING/REDEEMED/CLOSED) and
warn on every healthy LND channel. The bidialect rewrite handles
both Electrum and LND state strings, and accepts boolean `active`
in place of peer_state.
"""

from __future__ import annotations

import asyncio

import pytest

import liquidityhelper
import classes as classes_mod


# ---------------------------------------------------------------------------
# Currency-check guards
# ---------------------------------------------------------------------------

def test_configure_autoloop_skips_non_lnd_wallet(monkeypatch, event_loop):
    """An Electrum wallet passed to configure_autoloop must short-
    circuit with False, never touching the loop_provider registry."""
    registry_calls = []
    def fake_registry():
        registry_calls.append(True)
        return []
    monkeypatch.setattr(liquidityhelper, "_swap_provider_registry", fake_registry)

    wallet = {"id": "wE", "currency": "btc"}
    result = event_loop.run_until_complete(
        liquidityhelper.configure_autoloop(wallet, api=None)
    )
    assert result is False
    assert registry_calls == [], (
        "configure_autoloop must not consult the swap-provider "
        "registry for non-LND wallets — loop is LND-only"
    )


def test_configure_autoloop_proceeds_for_lnd_wallet(monkeypatch, event_loop):
    """The guard must NOT block legitimate LND wallets — proceeds past
    the check and (without a real LoopProvider) returns False at the
    next step. What matters is that we got past the guard."""
    registry_calls = []
    def fake_registry():
        registry_calls.append(True)
        return []  # no LoopProvider — function returns False after this
    monkeypatch.setattr(liquidityhelper, "_swap_provider_registry", fake_registry)

    wallet = {"id": "wL", "currency": "btclnd"}
    event_loop.run_until_complete(
        liquidityhelper.configure_autoloop(wallet, api=None)
    )
    assert registry_calls, (
        "configure_autoloop must consult the registry for btclnd "
        "wallets — they're the supported case"
    )


def test_pick_best_lsp_for_inbound_skips_non_lnd_wallet(monkeypatch, event_loop):
    """An Electrum wallet returns None without contacting any LSP
    provider. Defense-in-depth — the caller is already guarded, but
    we don't want a future direct caller bypassing. This test
    explicitly disables AUTOMATIC_CHANNEL_CREATION_ENABLED so the test
    deliberately reaches the currency guard."""
    monkeypatch.setattr(liquidityhelper, "AUTOMATIC_CHANNEL_CREATION_ENABLED", False)
    wallet = {"id": "wE", "currency": "btc"}
    result = event_loop.run_until_complete(
        liquidityhelper.pick_best_lsp_for_inbound(
            wallet=wallet, api=None, network="mainnet",
        )
    )
    assert result is None


def test_pick_best_swap_provider_for_out_skips_non_lnd_wallet(event_loop):
    """Same defensive pattern for the swap-provider picker. Electrum
    wallet → None without quoting any provider."""
    wallet = {"id": "wE", "currency": "btc"}
    result = event_loop.run_until_complete(
        liquidityhelper.pick_best_swap_provider_for_out(
            amount_sat=1_000_000, wallet=wallet, api=None,
        )
    )
    assert result is None


# ---------------------------------------------------------------------------
# Bidialect channel-state filter
# ---------------------------------------------------------------------------
#
# The filter is in BitcartAPI._OPEN_CHANNEL_STATES /
# _NON_OPEN_CHANNEL_STATES / _ONLINE_PEER_STATES. We test the FILTER
# logic by injecting a mock _query that returns known data; bypasses
# any actual HTTP.

def _api_with_channels(monkeypatch, channels: list) -> classes_mod.BitcartAPI:
    """Build a BitcartAPI whose _query returns the given channel list."""
    api = classes_mod.BitcartAPI("http://fake", "token")
    async def fake_query(url, params=None, limit=None):
        return (None, channels)
    monkeypatch.setattr(api, "_query", fake_query)
    return api


def test_filter_no_filtering_returns_all(monkeypatch, event_loop):
    """active_only=False AND online_only=False returns the raw list
    regardless of state. Pin against the early-return path."""
    api = _api_with_channels(monkeypatch, [
        {"state": "OPEN", "peer_state": "GOOD"},
        {"state": "CLOSING", "peer_state": "DISCONNECTED"},
        {"state": "UNKNOWN_FUTURE", "peer_state": "WHO_KNOWS"},
    ])
    result = event_loop.run_until_complete(api.get_wallet_ln_channels("w1"))
    assert len(result) == 3


def test_filter_active_only_accepts_electrum_open(monkeypatch, event_loop):
    api = _api_with_channels(monkeypatch, [
        {"state": "OPEN", "peer_state": "GOOD"},
        {"state": "REDEEMED", "peer_state": "DISCONNECTED"},
        {"state": "OPENING", "peer_state": "GOOD"},
    ])
    result = event_loop.run_until_complete(
        api.get_wallet_ln_channels("w1", active_only=True)
    )
    assert len(result) == 1
    assert result[0]["state"] == "OPEN"


def test_filter_active_only_accepts_lnd_active(monkeypatch, event_loop):
    """LND's btclnd integration may emit 'ACTIVE' instead of 'OPEN'.
    Both should be accepted as "open" for the purpose of active_only."""
    api = _api_with_channels(monkeypatch, [
        {"state": "ACTIVE", "peer_state": "GOOD"},
        {"state": "PENDING_OPEN", "peer_state": "GOOD"},
    ])
    result = event_loop.run_until_complete(
        api.get_wallet_ln_channels("w1", active_only=True)
    )
    assert len(result) == 1
    assert result[0]["state"] == "ACTIVE"


def test_filter_active_only_skips_lnd_pending_states(monkeypatch, event_loop):
    """All LND pending/closing states must be skipped under active_only.
    Pin specifically against the PENDING_OPEN drift the prior filter
    silently passed through (it didn't match any branch)."""
    states_to_skip = [
        "PENDING_OPEN", "PENDING_CLOSE", "PENDING_FORCE_CLOSE",
        "WAITING_CLOSE",
    ]
    api = _api_with_channels(monkeypatch, [
        {"state": s, "peer_state": "GOOD"} for s in states_to_skip
    ] + [{"state": "OPEN", "peer_state": "GOOD"}])
    result = event_loop.run_until_complete(
        api.get_wallet_ln_channels("w1", active_only=True)
    )
    assert len(result) == 1
    assert result[0]["state"] == "OPEN"


def test_filter_online_only_accepts_electrum_GOOD(monkeypatch, event_loop):
    api = _api_with_channels(monkeypatch, [
        {"state": "OPEN", "peer_state": "GOOD"},
        {"state": "OPEN", "peer_state": "DISCONNECTED"},
    ])
    result = event_loop.run_until_complete(
        api.get_wallet_ln_channels("w1", online_only=True)
    )
    assert len(result) == 1
    assert result[0]["peer_state"] == "GOOD"


def test_filter_online_only_accepts_lnd_boolean_active(monkeypatch, event_loop):
    """LND can emit a boolean `active` field instead of peer_state.
    The filter should treat active=True as online, active=False as
    offline, regardless of peer_state contents."""
    api = _api_with_channels(monkeypatch, [
        # LND-style: no peer_state, `active` boolean
        {"state": "OPEN", "active": True},
        {"state": "OPEN", "active": False},
    ])
    result = event_loop.run_until_complete(
        api.get_wallet_ln_channels("w1", online_only=True)
    )
    assert len(result) == 1
    assert result[0]["active"] is True


def test_filter_online_only_accepts_lnd_string_active(monkeypatch, event_loop):
    """LND-via-Bitcart might emit peer_state as the string 'ACTIVE'."""
    api = _api_with_channels(monkeypatch, [
        {"state": "OPEN", "peer_state": "ACTIVE"},
        {"state": "OPEN", "peer_state": "INACTIVE"},
    ])
    result = event_loop.run_until_complete(
        api.get_wallet_ln_channels("w1", online_only=True)
    )
    assert len(result) == 1
    assert result[0]["peer_state"] == "ACTIVE"


def test_filter_lowercase_state_is_normalized(monkeypatch, event_loop):
    """State strings get upper-cased before matching, so 'open'
    is treated the same as 'OPEN'. Defensive against daemon output
    case inconsistency."""
    api = _api_with_channels(monkeypatch, [
        {"state": "open", "peer_state": "good"},
    ])
    result = event_loop.run_until_complete(
        api.get_wallet_ln_channels("w1", online_only=True)
    )
    assert len(result) == 1


def test_filter_unknown_state_skipped_silently(monkeypatch, event_loop, caplog):
    """An unknown state (e.g., a future Bitcart-side change introducing
    a new value) is skipped at DEBUG level — no WARNING flood."""
    import logging
    api = _api_with_channels(monkeypatch, [
        {"state": "BRAND_NEW_FUTURE_STATE", "peer_state": "GOOD"},
        {"state": "OPEN", "peer_state": "GOOD"},
    ])
    with caplog.at_level(logging.DEBUG):
        result = event_loop.run_until_complete(
            api.get_wallet_ln_channels("w1", active_only=True)
        )
    assert len(result) == 1
    assert result[0]["state"] == "OPEN"
    # The unknown state must NOT log a WARNING — old behavior was to
    # log warning, which flooded the log on routine LND state strings.
    warn_records = [
        r for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert not warn_records, (
        f"unknown state must be logged at DEBUG, not WARNING; got "
        f"{[r.getMessage() for r in warn_records]}"
    )


def test_filter_no_log_flood_on_healthy_lnd_channel(monkeypatch, event_loop, caplog):
    """The headline regression-pin: a healthy LND channel emitting
    `peer_state='ACTIVE'` (the pre-fix would have warned because
    'ACTIVE' wasn't in the {'GOOD'} set) must NOT produce a
    warning. Otherwise every tick floods decisions.log per channel."""
    import logging
    api = _api_with_channels(monkeypatch, [
        {"state": "OPEN", "peer_state": "ACTIVE"},
    ])
    with caplog.at_level(logging.WARNING):
        event_loop.run_until_complete(
            api.get_wallet_ln_channels("w1", active_only=True)
        )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "healthy LND channel with peer_state='ACTIVE' must not "
        "trigger a warning (pre-fix behavior flooded logs)"
    )
