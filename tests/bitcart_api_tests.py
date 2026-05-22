"""
Unit tests for liquidityhelper functions that consume the BitcartAPI but
don't actually need a live Bitcart instance or the LND regtest fixture.

These tests use `tests/_fakes.FakeBitcartAPI` — a duck-typed in-memory fake
with the BitcartAPI methods our code under test calls. They run in
milliseconds and require no fixtures beyond the autouse in-memory DB.

If you're tempted to mock-out something that isn't in FakeBitcartAPI,
extend FakeBitcartAPI with the real method's shape rather than
monkey-patching per-test — keeps the test surface coherent.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import liquidityhelper
from liquidityhelper import LiquidityNeed, store_needs_liquidity
from tests._fakes import FakeBitcartAPI


def _run(coro):
    """Helper — these tests are single-shot async; no need for pytest-asyncio."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# store_needs_liquidity
# ---------------------------------------------------------------------------

def test_store_needs_liquidity_assume_zero_returns_full_need():
    """assume_zero=True bypasses the wallet/channel lookup entirely and treats
    the store as cold-start: needs full min liquidity + min channel count.
    This is the path topup_goal_amount uses to compute funding requirements."""
    api = FakeBitcartAPI()
    # No store/wallets registered — assume_zero=True should still return a need.
    need = _run(store_needs_liquidity(
        "any-store", api, min_sats_liquidity=100_000, min_channel_count=2,
        assume_zero=True,
    ))
    assert isinstance(need, LiquidityNeed)
    assert need.channels_needed == 2
    assert need.liquidity_needed_sat >= 100_000


def test_store_needs_liquidity_returns_none_when_satisfied():
    """Wallet with > min liquidity AND > min channel count -> None."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.add_store("s1", wallets=["w1"])
    # 3 channels, plenty of liquidity across both sides.
    for _ in range(3):
        api.add_channel("w1", local_balance=200_000, remote_balance=200_000)

    need = _run(store_needs_liquidity(
        "s1", api, min_sats_liquidity=100_000, min_channel_count=2,
    ))
    assert need is None


def test_store_needs_liquidity_no_channels_yet():
    """Fresh wallet with no channels -> needs min count of channels and
    enough liquidity (possibly bumped above the bare minimum to satisfy
    the MIN_CHANNEL_SIZE_IN_SATS-per-slice constraint)."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.add_store("s1", wallets=["w1"])

    need = _run(store_needs_liquidity(
        "s1", api, min_sats_liquidity=100_000, min_channel_count=2,
    ))
    assert isinstance(need, LiquidityNeed)
    assert need.channels_needed == 2
    # Could be bumped above 100_000 if 100_000/2 < MIN_CHANNEL_SIZE_IN_SATS,
    # but never *below* the requested amount.
    assert need.liquidity_needed_sat >= 100_000


def test_store_needs_liquidity_needs_more_liquidity_only():
    """Enough channel count, not enough liquidity -> request more liquidity."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.add_store("s1", wallets=["w1"])
    # 5 channels with very little balance — exceeds min_channel_count but
    # well under min_sats_liquidity.
    for _ in range(5):
        api.add_channel("w1", local_balance=1_000, remote_balance=1_000)

    need = _run(store_needs_liquidity(
        "s1", api, min_sats_liquidity=1_000_000, min_channel_count=2,
    ))
    assert isinstance(need, LiquidityNeed)
    # 5 channels * 2_000 sat = 10_000 found; shortfall = ~990_000.
    assert need.liquidity_needed_sat >= 990_000
    # channels_needed = max(2 - 5, 0) = 0.
    assert need.channels_needed == 0


def test_store_needs_liquidity_skips_inactive_channels():
    """get_wallet_ln_channels(active_only=True) is what
    store_needs_liquidity asks for — the fake should drop inactive
    channels from the count, so an offline-only wallet looks empty."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.add_store("s1", wallets=["w1"])
    # All channels inactive — should be ignored by the active_only filter.
    for _ in range(3):
        api.add_channel("w1", local_balance=200_000, remote_balance=200_000, active=False)

    need = _run(store_needs_liquidity(
        "s1", api, min_sats_liquidity=100_000, min_channel_count=2,
    ))
    assert isinstance(need, LiquidityNeed)
    # Saw 0 active channels and 0 liquidity, so needs both back.
    assert need.channels_needed == 2


def test_store_needs_liquidity_skips_offline_peer_channels():
    """get_wallet_ln_channels(online_only=True) -> peers in DISCONNECTED
    state aren't counted toward channel/liquidity totals."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.add_store("s1", wallets=["w1"])
    # Active but peer offline — store_needs_liquidity asks online_only=True
    # so these shouldn't count.
    for _ in range(3):
        api.add_channel(
            "w1", local_balance=200_000, remote_balance=200_000,
            active=True, peer_state="DISCONNECTED",
        )

    need = _run(store_needs_liquidity(
        "s1", api, min_sats_liquidity=100_000, min_channel_count=2,
    ))
    assert isinstance(need, LiquidityNeed)
    assert need.channels_needed == 2


def test_store_needs_liquidity_counts_both_balance_sides():
    """`found_inbound_liquidity` sums local AND remote balance (per the
    docstring: 'any balance in LN is "inbound" since it will be converted
    to inbound next time cashout is run')."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btclnd")
    api.add_store("s1", wallets=["w1"])
    # 3 channels x (local=60k + remote=60k) = 360k total counted.
    for _ in range(3):
        api.add_channel("w1", local_balance=60_000, remote_balance=60_000)

    # min_liquidity = 100k, total counted = 360k -> satisfied
    need = _run(store_needs_liquidity(
        "s1", api, min_sats_liquidity=100_000, min_channel_count=2,
    ))
    assert need is None

    # Raise the bar: 400k > 360k -> need shortfall
    need = _run(store_needs_liquidity(
        "s1", api, min_sats_liquidity=400_000, min_channel_count=2,
    ))
    assert isinstance(need, LiquidityNeed)
    assert need.liquidity_needed_sat >= 40_000  # 400k - 360k
    assert need.channels_needed == 0
