"""Tests for the per-destination LN-staleness fallback in the fee path.

Mirrors `tests/cashout_recency_tests.py` but for the developer fee and
referral fee destinations. Specifically pins:

  1. days_since_last_successful_ln_fee_payment() / referral counterpart
     read their OWN timestamps (cashout staleness doesn't trigger them).
  2. should_prefer_onchain_fee_payment() and referral counterpart
     decide independently — fee staleness doesn't switch the referral
     rail, and vice versa.
  3. FORCE_FEE_ONCHAIN_INSTEAD_OF_LN works as a manual override on the
     fee path (no parallel knob for referral by current design).
"""

from __future__ import annotations

import datetime

import pytest

import liquidityhelper
from database import SimpleDateTimeField


def _set(monkeypatch, **kw):
    """Bring the relevant config knobs to a known-clean state."""
    defaults = {
        "FORCE_FEE_ONCHAIN_INSTEAD_OF_LN": False,
        "FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS": 30,
        "REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS": 30,
        "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS": 30,
        "ENABLE_CASHOUT_ONCHAIN": True,
        "PREFER_CASHOUT_ONCHAIN": False,
    }
    for name, value in {**defaults, **kw}.items():
        monkeypatch.setattr(liquidityhelper, name, value, raising=False)


def _record_ln_event(name: str, days_ago: int):
    """Set a SimpleDateTimeField timestamp to `days_ago` days in the past."""
    SimpleDateTimeField.replace(
        name=name,
        date=datetime.datetime.now() - datetime.timedelta(days=days_ago),
    ).execute()


# ---------------------------------------------------------------------------
# days_since_* helpers
# ---------------------------------------------------------------------------

def test_days_since_fee_returns_none_when_never_recorded():
    assert liquidityhelper.days_since_last_successful_ln_fee_payment() is None


def test_days_since_referral_returns_none_when_never_recorded():
    assert liquidityhelper.days_since_last_successful_ln_referral_payment() is None


def test_days_since_fee_returns_correct_days():
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=5)
    assert liquidityhelper.days_since_last_successful_ln_fee_payment() == 5


def test_days_since_referral_returns_correct_days():
    _record_ln_event("LAST_SUCCESSFUL_LN_REFERRAL_PAYMENT", days_ago=8)
    assert liquidityhelper.days_since_last_successful_ln_referral_payment() == 8


def test_fee_and_referral_timestamps_are_independent():
    """Cashout staleness does not advance the fee or referral
    timestamps. Each destination has its own counter."""
    _record_ln_event("LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT", days_ago=100)
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=2)
    _record_ln_event("LAST_SUCCESSFUL_LN_REFERRAL_PAYMENT", days_ago=10)
    assert liquidityhelper.days_since_last_successful_ln_cashout() == 100
    assert liquidityhelper.days_since_last_successful_ln_fee_payment() == 2
    assert liquidityhelper.days_since_last_successful_ln_referral_payment() == 10


# ---------------------------------------------------------------------------
# should_prefer_onchain_fee_payment
# ---------------------------------------------------------------------------

def test_fee_rail_ln_when_no_history(monkeypatch):
    """No LN fee history → don't preemptively fall back to on-chain."""
    _set(monkeypatch)
    assert liquidityhelper.should_prefer_onchain_fee_payment() is False


def test_fee_rail_ln_when_recent(monkeypatch):
    _set(monkeypatch)
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=10)
    assert liquidityhelper.should_prefer_onchain_fee_payment() is False


def test_fee_rail_onchain_when_stale(monkeypatch):
    _set(monkeypatch, FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=30)
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=45)
    assert liquidityhelper.should_prefer_onchain_fee_payment() is True


def test_fee_rail_force_onchain_overrides(monkeypatch):
    """FORCE_FEE_ONCHAIN_INSTEAD_OF_LN is the manual operator override —
    forces on-chain regardless of LN timestamp."""
    _set(monkeypatch, FORCE_FEE_ONCHAIN_INSTEAD_OF_LN=True)
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=1)  # very recent
    assert liquidityhelper.should_prefer_onchain_fee_payment() is True


def test_fee_rail_threshold_disabled(monkeypatch):
    """Setting FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=None disables the
    automatic fallback. Stale LN timestamps no longer trigger it."""
    _set(monkeypatch, FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=None)
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=365)
    assert liquidityhelper.should_prefer_onchain_fee_payment() is False


# ---------------------------------------------------------------------------
# should_prefer_onchain_referral_payment
# ---------------------------------------------------------------------------

def test_referral_rail_ln_when_no_history(monkeypatch):
    _set(monkeypatch)
    assert liquidityhelper.should_prefer_onchain_referral_payment() is False


def test_referral_rail_onchain_when_stale(monkeypatch):
    _set(monkeypatch, REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=30)
    _record_ln_event("LAST_SUCCESSFUL_LN_REFERRAL_PAYMENT", days_ago=45)
    assert liquidityhelper.should_prefer_onchain_referral_payment() is True


def test_referral_rail_threshold_disabled(monkeypatch):
    _set(monkeypatch, REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=None)
    _record_ln_event("LAST_SUCCESSFUL_LN_REFERRAL_PAYMENT", days_ago=365)
    assert liquidityhelper.should_prefer_onchain_referral_payment() is False


# ---------------------------------------------------------------------------
# Independence: per-destination staleness doesn't bleed across destinations
# ---------------------------------------------------------------------------

def test_cashout_staleness_does_not_force_fee_onchain(monkeypatch):
    """The exact concern you flagged: 'A failure for cashouts to work
    via lightning should not trigger fees to be paid via on-chain
    transaction.'"""
    _set(monkeypatch)
    _record_ln_event("LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT", days_ago=200)
    # No fee timestamp at all -> fee path stays LN even though cashout is stale.
    assert liquidityhelper.should_prefer_onchain_fee_payment() is False
    # And conversely, cashout DOES go on-chain.
    assert liquidityhelper.should_prefer_onchain_cashout() is True


def test_fee_staleness_does_not_force_cashout_onchain(monkeypatch):
    _set(monkeypatch)
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=200)
    assert liquidityhelper.should_prefer_onchain_cashout() is False


def test_referral_staleness_does_not_force_fee_onchain(monkeypatch):
    _set(monkeypatch)
    _record_ln_event("LAST_SUCCESSFUL_LN_REFERRAL_PAYMENT", days_ago=200)
    assert liquidityhelper.should_prefer_onchain_fee_payment() is False


def test_fee_staleness_does_not_force_referral_onchain(monkeypatch):
    _set(monkeypatch)
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=200)
    assert liquidityhelper.should_prefer_onchain_referral_payment() is False


def test_all_three_can_be_on_different_rails(monkeypatch):
    """Realistic operational scenario: LN works fine for cashouts
    (recent), broken for the dev fee endpoint (stale), and the
    referral has never been tried yet."""
    _set(monkeypatch)
    _record_ln_event("LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT", days_ago=2)
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=60)
    # No referral history recorded.

    assert liquidityhelper.should_prefer_onchain_cashout() is False    # LN
    assert liquidityhelper.should_prefer_onchain_fee_payment() is True  # onchain
    assert liquidityhelper.should_prefer_onchain_referral_payment() is False  # LN (no history)


# ---------------------------------------------------------------------------
# FORCE_REFERRAL_ONCHAIN_INSTEAD_OF_LN — symmetric override
# ---------------------------------------------------------------------------

def test_force_referral_onchain_overrides(monkeypatch):
    """The new manual override knob — matches FORCE_FEE_ONCHAIN_INSTEAD_OF_LN
    in shape and effect, for symmetry / testing utility."""
    _set(monkeypatch, FORCE_REFERRAL_ONCHAIN_INSTEAD_OF_LN=True)
    # No threshold trigger, no LN history -> would normally be LN.
    assert liquidityhelper.should_prefer_onchain_referral_payment() is True


def test_force_referral_onchain_independent_of_fee_override(monkeypatch):
    """The fee FORCE flag and referral FORCE flag are independent.
    Flipping one doesn't affect the other."""
    _set(monkeypatch,
         FORCE_FEE_ONCHAIN_INSTEAD_OF_LN=True,
         FORCE_REFERRAL_ONCHAIN_INSTEAD_OF_LN=False)
    assert liquidityhelper.should_prefer_onchain_fee_payment() is True
    assert liquidityhelper.should_prefer_onchain_referral_payment() is False


# ---------------------------------------------------------------------------
# _ln_known_stale_* helpers — independent of FORCE flags, used by the
# post-LN-failure decision in calculate_fees / do_cashouts.
# ---------------------------------------------------------------------------

def test_ln_known_stale_for_fee_ignores_force_flag(monkeypatch):
    """The post-failure helper checks staleness only — the FORCE flag
    is consumed earlier in the dispatch."""
    _set(monkeypatch, FORCE_FEE_ONCHAIN_INSTEAD_OF_LN=True)
    # Force flag is on but no LN history exists.
    assert liquidityhelper._ln_known_stale_for_fee_payment() is False


def test_ln_known_stale_for_fee_when_stale(monkeypatch):
    _set(monkeypatch, FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=30)
    # _ln_known_stale_for_fee_payment now reads the failure-streak
    # marker (FIRST_LN_FEE_FAILURE_SINCE_SUCCESS), not the success
    # timestamp. An old success + an old failure-since-success means a
    # streak that has been failing for longer than the threshold.
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=60)
    _record_ln_event("FIRST_LN_FEE_FAILURE_SINCE_SUCCESS", days_ago=60)
    assert liquidityhelper._ln_known_stale_for_fee_payment() is True


def test_ln_known_stale_for_referral_when_stale(monkeypatch):
    _set(monkeypatch, REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=30)
    _record_ln_event("LAST_SUCCESSFUL_LN_REFERRAL_PAYMENT", days_ago=60)
    _record_ln_event("FIRST_LN_REFERRAL_FAILURE_SINCE_SUCCESS", days_ago=60)
    assert liquidityhelper._ln_known_stale_for_referral_payment() is True


def test_ln_known_stale_for_cashout_when_stale(monkeypatch):
    _set(monkeypatch, CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=30)
    _record_ln_event("LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT", days_ago=60)
    _record_ln_event("FIRST_LN_CASHOUT_FAILURE_SINCE_SUCCESS", days_ago=60)
    assert liquidityhelper._ln_known_stale_for_cashout() is True


def test_ln_known_stale_false_when_threshold_none(monkeypatch):
    """When the threshold is None, no amount of LN staleness should
    trigger fallback — disables the auto-fallback entirely."""
    _set(monkeypatch, FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=None)
    _record_ln_event("LAST_SUCCESSFUL_LN_FEE_PAYMENT", days_ago=999)
    assert liquidityhelper._ln_known_stale_for_fee_payment() is False
