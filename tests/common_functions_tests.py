"""Unit tests for `common_functions.py`.

These are pure arithmetic / classification helpers used across the
engine — sats/BTC conversions, channel-reserve estimation, sizing.
Test goals:

  - Pin the conversion factor (100_000_000 sats per BTC) so a
    refactor can't silently change the unit math.
  - Pin the channel-sizing math (dust limit, 1% reserve floor)
    against the lightning-spec constants the production code
    encodes by value.
  - Pin the reserve calculation against the hardcoded 11_000
    sats so future tuning shows up as a deliberate test edit.
  - Pin the target-from-channel-sizes calculation (max of
    own-reserve vs electrum-reserve) because callers in the
    topup-goal path depend on the higher-of-the-two semantics.

`distribute_sats_over_channels` is already covered in
`code_only_tests.py::test_distribute_sats_over_channels`.
"""

from __future__ import annotations

import math

import pytest

import common_functions as cf


# ---------------------------------------------------------------------------
# is_integer / is_float
# ---------------------------------------------------------------------------

class TestIsInteger:
    def test_positive_integer_string(self):
        assert cf.is_integer("42") is True

    def test_negative_integer_string(self):
        assert cf.is_integer("-42") is True

    def test_zero_string(self):
        assert cf.is_integer("0") is True

    def test_float_string_is_not_integer(self):
        """A decimal string fails int() — float strings must report
        False. This is the discriminator between is_integer and
        is_float at the input-validation seam."""
        assert cf.is_integer("3.14") is False

    def test_text_string_is_not_integer(self):
        assert cf.is_integer("abc") is False

    def test_empty_string_is_not_integer(self):
        assert cf.is_integer("") is False

    def test_whitespace_string_is_not_integer(self):
        assert cf.is_integer("   ") is False

    def test_actual_int_input(self):
        """Passing an int (not a string) also succeeds because int(int) is
        a no-op. Pin this in case callers rely on it for already-typed
        values."""
        assert cf.is_integer(42) is True

    def test_actual_float_input_succeeds(self):
        """is_integer(3.14) is True because int(3.14) doesn't raise —
        it just truncates. This is a known surprising behavior; pin
        it so a future "stricter" change is intentional."""
        assert cf.is_integer(3.14) is True


class TestIsFloat:
    def test_positive_float_string(self):
        assert cf.is_float("3.14") is True

    def test_negative_float_string(self):
        assert cf.is_float("-3.14") is True

    def test_integer_string_is_not_float(self):
        """is_float specifically rejects whole-number floats — that's
        why it has the `.is_integer()` check. So `"5"` returns False
        even though `float("5") == 5.0` parses. This is the asymmetry
        with is_integer."""
        assert cf.is_float("5") is False

    def test_zero_point_zero_string_is_not_float(self):
        """5.0 → float(5.0).is_integer() is True → False per the
        is_float impl. Pin against a future "treat 5.0 as float"
        change that would alter classifier semantics."""
        assert cf.is_float("5.0") is False

    def test_text_string_is_not_float(self):
        assert cf.is_float("abc") is False

    def test_empty_string_is_not_float(self):
        assert cf.is_float("") is False

    def test_scientific_notation_float(self):
        """1e-2 parses as 0.01, not an integer → True."""
        assert cf.is_float("1e-2") is True

    def test_scientific_notation_integer_is_not_float(self):
        """1e2 parses as 100.0, which IS an integer → False."""
        assert cf.is_float("1e2") is False


# ---------------------------------------------------------------------------
# sats_to_btc / btc_to_sats
# ---------------------------------------------------------------------------

class TestSatsToBtc:
    def test_one_btc(self):
        assert cf.sats_to_btc(100_000_000) == 1.0

    def test_one_sat(self):
        assert cf.sats_to_btc(1) == 1e-8

    def test_zero(self):
        assert cf.sats_to_btc(0) == 0.0

    def test_one_thousand_sats(self):
        assert cf.sats_to_btc(1_000) == 0.00001

    def test_returns_float(self):
        """The division operator produces a float even on whole-BTC
        amounts. Important for callers that downstream-format with
        `f"{x:.8f}"`."""
        assert isinstance(cf.sats_to_btc(100_000_000), float)


class TestBtcToSats:
    def test_one_btc(self):
        assert cf.btc_to_sats(1.0) == 100_000_000

    def test_zero(self):
        assert cf.btc_to_sats(0.0) == 0

    def test_one_satoshi_via_btc(self):
        assert cf.btc_to_sats(0.00000001) == 1

    def test_fractional_btc_truncates(self):
        """The impl Decimal-multiplies and then `int()`-truncates
        toward zero. For 0.000000005 BTC = 0.5 sats, we expect 0
        (truncation), not 1 (round). Pin this so a future switch to
        `round()` is intentional."""
        assert cf.btc_to_sats(0.000000005) == 0

    def test_decimal_route_no_float_loss(self):
        """Regression: `int(0.29 * 1e8)` returns 28_999_999 due to binary
        float rounding; the Decimal route returns 29_000_000. Same for
        2.675 BTC. Both numbers are sat-aligned (29M and 267.5M sats)
        and must round-trip cleanly through btc_to_sats — including
        when the input is a string (Electrum returns BTC-as-string)."""
        assert cf.btc_to_sats(0.29) == 29_000_000
        assert cf.btc_to_sats(2.675) == 267_500_000
        assert cf.btc_to_sats("0.29") == 29_000_000
        assert cf.btc_to_sats("2.675") == 267_500_000

    def test_large_value(self):
        """21M BTC supply ceiling — confirm we can represent it."""
        assert cf.btc_to_sats(21_000_000.0) == 2_100_000_000_000_000

    def test_returns_int(self):
        assert isinstance(cf.btc_to_sats(1.0), int)


class TestSatsBtcRoundTrip:
    """sats_to_btc(btc_to_sats(x)) should equal x for any sat-aligned
    value (i.e., any x representable in whole sats). This pins the
    inverse relationship across the conversion boundary."""

    @pytest.mark.parametrize("sats", [
        0, 1, 100, 100_000, 100_000_000, 21_000_000 * 100_000_000,
    ])
    def test_roundtrip_sats_to_btc_to_sats(self, sats):
        btc = cf.sats_to_btc(sats)
        assert cf.btc_to_sats(btc) == sats


# ---------------------------------------------------------------------------
# onchain_reserves_to_keep_for_channel
# ---------------------------------------------------------------------------

class TestOnchainReservesToKeepForChannel:
    """Hardcoded to 11_000 regardless of input. Pin THIS BEHAVIOR so
    a future dynamic-reserve change is a deliberate test edit, not
    a silent regression."""

    def test_returns_eleven_thousand_for_small_channel(self):
        assert cf.onchain_reserves_to_keep_for_channel(50_000) == 11_000

    def test_returns_eleven_thousand_for_large_channel(self):
        assert cf.onchain_reserves_to_keep_for_channel(10_000_000) == 11_000

    def test_returns_eleven_thousand_for_zero(self):
        assert cf.onchain_reserves_to_keep_for_channel(0) == 11_000


# ---------------------------------------------------------------------------
# sats_to_max_channel_size
# ---------------------------------------------------------------------------

class TestSatsToMaxChannelSize:
    """Search for the largest channel size such that
    `size + onchain_reserves_to_keep_for_channel(size) < total`.
    Since reserve is 11_000 flat, the answer is `total - 11_001`
    for any total > 11_001."""

    def test_zero_input_returns_zero(self):
        """Edge: with 0 sats available, no channel is possible."""
        assert cf.sats_to_max_channel_size(0) == 0

    def test_below_reserve_returns_zero(self):
        """With fewer sats than the 11_000 reserve, no channel fits."""
        assert cf.sats_to_max_channel_size(5_000) == 0

    def test_exactly_reserve_returns_zero(self):
        """11_000 sats available, 11_000 reserve required → no room
        for the channel itself."""
        assert cf.sats_to_max_channel_size(11_000) == 0

    def test_just_above_reserve_returns_small_channel(self):
        """11_002 sats available — need 11_000 reserve plus the channel
        itself. Largest channel that satisfies `chan + 11_000 < 11_002`
        is 1 sat (1 + 11_000 = 11_001 < 11_002)."""
        assert cf.sats_to_max_channel_size(11_002) == 1

    def test_normal_value(self):
        """For 100_000 sats available, expected max channel = 100_000 -
        11_000 - 1 = 88_999. The inequality is strict (`<`)."""
        assert cf.sats_to_max_channel_size(100_000) == 88_999

    def test_million_sats(self):
        assert cf.sats_to_max_channel_size(1_000_000) == 988_999


# ---------------------------------------------------------------------------
# liquidity_to_channel_size
# ---------------------------------------------------------------------------

class TestLiquidityToChannelSize:
    """Channel size = sats + 546 (dust limit) + max(1%-of-sats, 546).
    Pin the dust-limit constant and the 1% reserve floor against the
    spec they encode."""

    def test_large_value_uses_one_percent_reserve(self):
        """At 1_000_000 sats, 1% = 10_000 > 546, so reserve = 10_000.
        Expected = 1_000_000 + 546 + 10_000 = 1_010_546."""
        assert cf.liquidity_to_channel_size(1_000_000) == 1_010_546

    def test_small_value_uses_dust_limit_as_reserve_floor(self):
        """At 10_000 sats, 1% = 100 < 546, so reserve = 546.
        Expected = 10_000 + 546 + 546 = 11_092."""
        assert cf.liquidity_to_channel_size(10_000) == 11_092

    def test_exactly_at_one_percent_equals_dust_boundary(self):
        """At 54_600 sats, 1% = 546 = dust_limit. Both maxes give 546.
        Expected = 54_600 + 546 + 546 = 55_692."""
        assert cf.liquidity_to_channel_size(54_600) == 55_692

    def test_just_above_one_percent_boundary_uses_percent(self):
        """At 54_700 sats, 1% = 547 > 546 → reserve = 547.
        Expected = 54_700 + 546 + 547 = 55_793."""
        assert cf.liquidity_to_channel_size(54_700) == 55_793

    def test_zero_input(self):
        """0 sats requested → 0 + 546 + 546 = 1092 channel size.
        Slightly odd edge but pins the math; callers shouldn't pass
        0 in practice."""
        assert cf.liquidity_to_channel_size(0) == 1092

    def test_returns_int(self):
        """math.ceil ensures integer output even when 1% produces
        a fractional value."""
        assert isinstance(cf.liquidity_to_channel_size(10_001), int)

    def test_ceils_fractional_one_percent(self):
        """At 10_001 sats: 1% = 100.01. Reserve = max(100.01, 546) = 546.
        Channel = ceil(10_001 + 546 + 546) = 11_093."""
        assert cf.liquidity_to_channel_size(10_001) == 11_093


# ---------------------------------------------------------------------------
# target_from_channel_sizes
# ---------------------------------------------------------------------------

class TestTargetFromChannelSizes:
    """`max(own_reserve, electrum_reserve) + sum(channels)`, where:
      own_reserve     = channel_buffer * len(channels)
      electrum_reserve = sum( max(channel_size // 100, 20_001) for each)

    Pin the max-of-two semantics — this is the topup-goal calculation
    in the manual-channel-creation path."""

    def test_single_channel_dust_floor_dominates(self):
        """One 100_000 sat channel: own_reserve = buffer*1, electrum_reserve =
        max(1000, 20_001) = 20_001. With buffer=5_000, electrum dominates.
        Expected = 100_000 + max(5_000, 20_001) = 120_001."""
        result = cf.target_from_channel_sizes(
            channels=[100_000], channel_buffer=5_000,
        )
        assert result == 120_001

    def test_single_channel_one_percent_dominates(self):
        """One 5_000_000 sat channel: 1% = 50_000 > 20_001 dust floor.
        With buffer=5_000, own_reserve=5_000. Max(5_000, 50_000)=50_000.
        Expected = 5_000_000 + 50_000 = 5_050_000."""
        result = cf.target_from_channel_sizes(
            channels=[5_000_000], channel_buffer=5_000,
        )
        assert result == 5_050_000

    def test_own_reserve_can_dominate(self):
        """With a huge buffer and tiny channels, own_reserve wins.
        buffer=100_000, two 30_000-sat channels:
          own_reserve = 100_000 * 2 = 200_000
          electrum_reserve = max(300, 20_001)*2 = 40_002
          Expected = 60_000 + max(200_000, 40_002) = 260_000."""
        result = cf.target_from_channel_sizes(
            channels=[30_000, 30_000], channel_buffer=100_000,
        )
        assert result == 260_000

    def test_empty_channel_list(self):
        """Edge: zero channels requested. own_reserve = buffer*0 = 0,
        electrum_reserve = sum() = 0. Expected = 0 + max(0, 0) = 0."""
        assert cf.target_from_channel_sizes(channels=[], channel_buffer=5_000) == 0

    def test_zero_buffer(self):
        """own_reserve = 0; electrum_reserve dominates always.
        Two 1M sat channels: 1% = 10_000 < 20_001 dust → 20_001 each.
        electrum_reserve = 40_002. Expected = 2_000_000 + 40_002 = 2_040_002."""
        result = cf.target_from_channel_sizes(
            channels=[1_000_000, 1_000_000], channel_buffer=0,
        )
        assert result == 2_040_002

    def test_mixed_channel_sizes(self):
        """Pin per-channel max() decision: one channel where 1%
        dominates dust, one where dust dominates 1%.
          [100_000, 5_000_000], buffer=2_000:
            own_reserve = 2_000 * 2 = 4_000
            electrum_reserve = max(1_000, 20_001) + max(50_000, 20_001)
                             = 20_001 + 50_000 = 70_001
            Expected = 5_100_000 + max(4_000, 70_001) = 5_170_001."""
        result = cf.target_from_channel_sizes(
            channels=[100_000, 5_000_000], channel_buffer=2_000,
        )
        assert result == 5_170_001
