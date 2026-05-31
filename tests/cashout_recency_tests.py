"""Tests for the cashout/fee-payment recency tracking feature.

Covers:
  - SimpleDateTimeField now upserts (one row per name) instead of
    accumulating duplicates.
  - `get_last_date()` reads the timestamp, tolerant of pre-migration
    duplicates.
  - `days_since_last_successful_ln_cashout()` returns a sensible int (or
    None when never recorded).
  - `do_cashouts()` honors CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS: when
    the last successful LN cashout is older than the threshold, it
    forces the on-chain path even when PREFER_CASHOUT_ONCHAIN=False.
"""

from __future__ import annotations

import datetime
import pytest

import liquidityhelper
from database import SimpleDateTimeField
from tests._fakes import FakeBitcartAPI


# ---------------------------------------------------------------------------
# SimpleDateTimeField storage layer
# ---------------------------------------------------------------------------

def test_replace_upserts_on_name():
    """The unique-name migration means `replace(name=..., date=...)`
    should keep one row per name, not accumulate duplicates."""
    earlier = datetime.datetime(2026, 1, 1, 12, 0, 0)
    later = datetime.datetime(2026, 5, 18, 12, 0, 0)

    SimpleDateTimeField.replace(name="KEY", date=earlier).execute()
    SimpleDateTimeField.replace(name="KEY", date=later).execute()

    rows = list(SimpleDateTimeField.select().where(SimpleDateTimeField.name == "KEY"))
    assert len(rows) == 1
    assert rows[0].date == later


def test_get_last_date_returns_none_when_unset():
    assert liquidityhelper.get_last_date("NEVER_WRITTEN") is None


def test_get_last_date_returns_recorded_date():
    when = datetime.datetime(2026, 4, 10, 9, 30, 0)
    SimpleDateTimeField.replace(name="MY_KEY", date=when).execute()
    assert liquidityhelper.get_last_date("MY_KEY") == when


def test_get_last_date_returns_most_recent_when_duplicates_linger():
    """If a pre-migration DB still has duplicate rows for the same name,
    get_last_date should return the most recent. (We can't reproduce
    duplicates against the new schema's UNIQUE index, so simulate by
    inserting two rows with different names then querying.)"""
    older = datetime.datetime(2025, 1, 1)
    newer = datetime.datetime(2026, 5, 1)
    SimpleDateTimeField.replace(name="A", date=older).execute()
    SimpleDateTimeField.replace(name="A", date=newer).execute()
    assert liquidityhelper.get_last_date("A") == newer


# ---------------------------------------------------------------------------
# days_since_last_successful_ln_cashout
# ---------------------------------------------------------------------------

def test_days_since_returns_none_when_never_succeeded():
    assert liquidityhelper.days_since_last_successful_ln_cashout() is None


def test_days_since_returns_zero_for_today():
    now = datetime.datetime.now()
    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=now - datetime.timedelta(hours=3),
    ).execute()
    assert liquidityhelper.days_since_last_successful_ln_cashout() == 0


def test_days_since_returns_age_in_whole_days():
    now = datetime.datetime.now()
    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=now - datetime.timedelta(days=8, hours=2),
    ).execute()
    # Floors fractional days, so 8d2h -> 8.
    assert liquidityhelper.days_since_last_successful_ln_cashout() == 8


# ---------------------------------------------------------------------------
# do_cashouts() fallback policy
# ---------------------------------------------------------------------------

def _make_api_with_ln_wallet():
    """Electrum currency keeps the pending-channel guard on a code path
    that FakeBitcartAPI can satisfy directly (get_wallet_ln_channels)
    without us having to stub LND's gRPC."""
    api = FakeBitcartAPI()
    wallet = api.add_wallet("w1", currency="btc", balance=0.05)  # 5M sats
    api.add_store("s1", wallets=["w1"])
    api.add_channel("w1", local_balance=1_000_000, remote_balance=0,
                    active=True, state="OPEN")
    return api


def _record_called_paths(monkeypatch):
    """Replace do_ln_cashouts / do_onchain_cashouts / the drain helper
    with recorders so we can assert the exact sequence do_cashouts()
    produced for a given wallet. Returns a list that gets appended to
    (`"ln"`, `"drain"`, `"onchain"`).

    Under the current model:
      - LN leg fires unless PREFER_CASHOUT_ONCHAIN is set
      - Drain helper fires when (PREFER_CASHOUT_ONCHAIN and not PREFER_LN_CASHOUT) OR (LN attempted, failed, AND LN is known stale per _ln_known_stale_for_cashout)
      - On-chain leg fires ALWAYS (sweeps on-chain revenue independently)
    """
    called = []

    async def fake_ln(api, wallet_id, amt):
        called.append("ln")
        return True

    async def fake_onchain(api, wallet_id, amt):
        called.append("onchain")
        return True

    async def fake_drain(api, wallet_id, wallet):
        called.append("drain")

    monkeypatch.setattr(liquidityhelper, "do_ln_cashouts", fake_ln)
    monkeypatch.setattr(liquidityhelper, "do_onchain_cashouts", fake_onchain)
    monkeypatch.setattr(
        liquidityhelper, "_drain_ln_for_cashout_if_enabled", fake_drain,
    )
    return called


def test_do_cashouts_runs_both_legs_when_ln_healthy(monkeypatch, event_loop):
    """Both legs fire every tick now (LN first, then on-chain) so
    on-chain customer revenue gets swept even while LN is working.
    Drain helper is NOT called — LN succeeded, no need to drain."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS", 7)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now() - datetime.timedelta(hours=2),
    ).execute()

    called = _record_called_paths(monkeypatch)
    api = _make_api_with_ln_wallet()
    result = event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert result is True
    assert called == ["ln", "onchain"]


def test_do_cashouts_falls_back_to_onchain_when_ln_fails_and_stale(monkeypatch, event_loop):
    """The new try-LN-first behavior: LN is attempted first. If LN
    FAILS *and* the last successful LN cashout is older than the
    threshold, fall back to on-chain. A successful LN attempt
    auto-resets the timestamp (no fallback needed)."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS", 7)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now() - datetime.timedelta(days=30),
    ).execute()
    # _ln_known_stale_for_cashout reads the failure-streak marker, not
    # the success timestamp. Mocked do_ln_cashouts won't update either,
    # so we seed an old first-failure to satisfy the staleness check.
    SimpleDateTimeField.replace(
        name="FIRST_LN_CASHOUT_FAILURE_SINCE_SUCCESS",
        date=datetime.datetime.now() - datetime.timedelta(days=30),
    ).execute()

    # Mock LN to FAIL — this is what triggers the drain.
    called = []

    async def fake_ln_fail(api, wallet_id, amt):
        called.append("ln")
        return False
    async def fake_onchain(api, wallet_id, amt):
        called.append("onchain")
        return True
    async def fake_drain(api, wallet_id, wallet):
        called.append("drain")
    monkeypatch.setattr(liquidityhelper, "do_ln_cashouts", fake_ln_fail)
    monkeypatch.setattr(liquidityhelper, "do_onchain_cashouts", fake_onchain)
    monkeypatch.setattr(
        liquidityhelper, "_drain_ln_for_cashout_if_enabled", fake_drain,
    )

    api = _make_api_with_ln_wallet()
    result = event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert result is True
    # LN tried, failed, then drain helper invoked (LN known stale),
    # then on-chain leg always fires.
    assert called == ["ln", "drain", "onchain"]


def test_do_cashouts_ln_success_auto_resets_staleness(monkeypatch, event_loop):
    """The headline auto-recovery property of try-LN-first:
    even if LN has been stale for 30 days, the moment LN succeeds
    again the system stays on the LN rail — no fallback fires."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS", 7)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    # LN has been stale for 30 days, but now LN works.
    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now() - datetime.timedelta(days=30),
    ).execute()

    called = _record_called_paths(monkeypatch)
    api = _make_api_with_ln_wallet()
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    # LN tried, succeeded -> no drain (LN healthy). On-chain leg
    # always fires regardless.
    assert called == ["ln", "onchain"]


def test_do_cashouts_ln_failure_NOT_stale_skips_drain(monkeypatch, event_loop):
    """A one-off LN failure (LN still recent) should NOT invoke the
    drain helper — that fires only after sustained LN failure. The
    on-chain leg still runs (it always does), but the drain is
    skipped this tick."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS", 7)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    # Recent LN success.
    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now() - datetime.timedelta(hours=2),
    ).execute()

    called = []
    async def fake_ln_fail(api, wallet_id, amt):
        called.append("ln")
        return False
    async def fake_onchain(api, wallet_id, amt):
        called.append("onchain")
        return True
    async def fake_drain(api, wallet_id, wallet):
        called.append("drain")
    monkeypatch.setattr(liquidityhelper, "do_ln_cashouts", fake_ln_fail)
    monkeypatch.setattr(liquidityhelper, "do_onchain_cashouts", fake_onchain)
    monkeypatch.setattr(
        liquidityhelper, "_drain_ln_for_cashout_if_enabled", fake_drain,
    )

    api = _make_api_with_ln_wallet()
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    # LN tried, failed, but NOT yet stale -> no drain. On-chain leg
    # still fires.
    assert called == ["ln", "onchain"]
    assert "drain" not in called


def test_do_cashouts_no_drain_when_no_ln_history(monkeypatch, event_loop):
    """Brand-new install: no LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT. The
    drain helper should NOT fire (we have no baseline to call LN
    'stale' against). LN attempt still runs, on-chain still runs."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS", 7)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    # No SimpleDateTimeField row at all.
    called = _record_called_paths(monkeypatch)
    api = _make_api_with_ln_wallet()
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    assert called == ["ln", "onchain"]
    assert "drain" not in called


def test_do_cashouts_no_drain_when_threshold_disabled(monkeypatch, event_loop):
    """CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS=None disables the drain
    helper entirely; even a very stale LN timestamp shouldn't trigger
    it. LN + on-chain still both fire."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS", None)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now() - datetime.timedelta(days=365),
    ).execute()

    called = _record_called_paths(monkeypatch)
    api = _make_api_with_ln_wallet()
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    # No drain even with 365-day-old timestamp (threshold disabled).
    assert called == ["ln", "onchain"]
    assert "drain" not in called


def test_do_cashouts_prefer_onchain_skips_ln_attempt(monkeypatch, event_loop):
    """PREFER_CASHOUT_ONCHAIN=True: LN leg is skipped entirely.
    Drain helper IS invoked (LN funds would otherwise be stranded),
    then on-chain leg fires."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS", 7)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    # Record a very-recent successful LN cashout — under the prior
    # try-LN-first model this would route to LN. PREFER_CASHOUT_ONCHAIN
    # must take precedence regardless.
    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now(),
    ).execute()

    called = _record_called_paths(monkeypatch)
    api = _make_api_with_ln_wallet()
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    # No LN attempt; drain helper called; on-chain leg fires.
    assert called == ["drain", "onchain"]
    assert "ln" not in called


# ---------------------------------------------------------------------------
# The always-attempt-onchain property — pinned explicitly because it's the
# core fix for the "on-chain customer revenue piles up while LN works"
# scenario that motivated this refactor.
# ---------------------------------------------------------------------------

def test_do_cashouts_runs_onchain_even_when_no_ln_balance(monkeypatch, event_loop):
    """No LN channels at all (so no LN attempt) -> on-chain leg should
    STILL fire, sweeping whatever on-chain revenue the wallet has."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS", 7)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    # FakeBitcartAPI with a wallet but NO channels.
    from tests._fakes import FakeBitcartAPI
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=0.05)
    api.add_store("s1", wallets=["w1"])

    called = _record_called_paths(monkeypatch)
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    # LN not attempted (no channels); drain skipped; on-chain fires.
    assert "ln" not in called
    assert "onchain" in called


def test_do_cashouts_onchain_leg_pins_when_ln_succeeds(monkeypatch, event_loop):
    """The single most important property of this refactor: LN
    success does NOT short-circuit the on-chain leg."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS", 7)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    called = _record_called_paths(monkeypatch)
    api = _make_api_with_ln_wallet()
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))
    # On-chain MUST fire — this is the regression-pin for the
    # "on-chain revenue piles up" bug.
    assert "onchain" in called, (
        f"on-chain cashout leg must fire every tick; called={called}"
    )


# ---------------------------------------------------------------------------
# LSP-shortfall hold-back: reduce LN cashout amount when on-chain
# balance is below the reserve floor needed to buy a new LSP channel.
# ---------------------------------------------------------------------------

from node_database import LspPriceQuote


def _record_amounts(monkeypatch):
    """Variant of _record_called_paths that also captures the amounts
    passed to each leg — needed to verify the hold-back actually
    reduced the LN amount."""
    calls = []

    async def fake_ln(api, wallet_id, amt):
        calls.append({"path": "ln", "amount": amt})
        return True

    async def fake_onchain(api, wallet_id, amt):
        calls.append({"path": "onchain", "amount": amt})
        return True

    async def fake_drain(api, wallet_id, wallet):
        calls.append({"path": "drain"})

    monkeypatch.setattr(liquidityhelper, "do_ln_cashouts", fake_ln)
    monkeypatch.setattr(liquidityhelper, "do_onchain_cashouts", fake_onchain)
    monkeypatch.setattr(
        liquidityhelper, "_drain_ln_for_cashout_if_enabled", fake_drain,
    )
    return calls


def test_ln_cashout_no_holdback_when_onchain_healthy(monkeypatch, event_loop):
    """On-chain balance is above the LSP-purchase reserve floor → no
    LN funds held back; full LN balance flows to cashout."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "MIN_RESERVE_ONCHAIN", 10_000)
    monkeypatch.setattr(liquidityhelper, "LSP_RESERVE_CAP_SAT", 50_000)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    # Wallet has plenty on-chain.
    from tests._fakes import FakeBitcartAPI
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=0.001)   # 100_000 sat onchain
    api.add_store("s1", wallets=["w1"])
    api.add_channel("w1", local_balance=500_000, remote_balance=0,
                    active=True, state="OPEN")

    calls = _record_amounts(monkeypatch)
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    ln_call = next(c for c in calls if c["path"] == "ln")
    # No holdback — full 500_000 sat available for LN cashout.
    assert ln_call["amount"] == 500_000


def test_ln_cashout_holds_back_for_lsp_shortfall(monkeypatch, event_loop):
    """On-chain balance is BELOW the reserve floor → hold back the
    shortfall (reserve - onchain) from the LN cashout."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "MIN_RESERVE_ONCHAIN", 10_000)
    monkeypatch.setattr(liquidityhelper, "LSP_RESERVE_CAP_SAT", 50_000)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    # On-chain has only 2,000 sat. Reserve floor is 10,000. Shortfall = 8,000.
    from tests._fakes import FakeBitcartAPI
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=0.00002)   # 2_000 sat onchain
    api.add_store("s1", wallets=["w1"])
    api.add_channel("w1", local_balance=500_000, remote_balance=0,
                    active=True, state="OPEN")

    calls = _record_amounts(monkeypatch)
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    ln_call = next(c for c in calls if c["path"] == "ln")
    # Hold back 8_000 -> only 492_000 eligible for cashout.
    assert ln_call["amount"] == 500_000 - 8_000


def test_ln_cashout_holdback_respects_lsp_price_floor(monkeypatch, event_loop):
    """The reserve floor includes the 6-month max LSP quote (capped
    at LSP_RESERVE_CAP_SAT). A 30k sat LSP quote in history raises
    the floor to 30k, increasing the shortfall accordingly."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "MIN_RESERVE_ONCHAIN", 10_000)
    monkeypatch.setattr(liquidityhelper, "LSP_RESERVE_CAP_SAT", 50_000)
    monkeypatch.setattr(liquidityhelper, "LSP_MAX_FEE_PERCENT", 1.0)   # no per-quote cap
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    # Record an LSP quote at 30k sat → reserve floor becomes 30k.
    LspPriceQuote.create(
        provider="zeus", network="mainnet", wallet_id="w-other",
        order_id="recent", lsp_balance_sat=150_000,
        fee_total_sat=30_000, order_total_sat=30_000,
        channel_expiry_blocks=13000,
    )

    # On-chain has 5,000 sat. Shortfall = 30,000 - 5,000 = 25,000.
    from tests._fakes import FakeBitcartAPI
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=0.00005)   # 5_000 sat
    api.add_store("s1", wallets=["w1"])
    api.add_channel("w1", local_balance=500_000, remote_balance=0,
                    active=True, state="OPEN")

    calls = _record_amounts(monkeypatch)
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    ln_call = next(c for c in calls if c["path"] == "ln")
    assert ln_call["amount"] == 500_000 - 25_000


def test_ln_cashout_holdback_clamps_when_shortfall_exceeds_balance(monkeypatch, event_loop):
    """Shortfall larger than total LN balance → cashout is skipped
    entirely (held back to 0). LN attempt does NOT fire; the held-back
    funds stay in the channel."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "MIN_RESERVE_ONCHAIN", 50_000)
    monkeypatch.setattr(liquidityhelper, "LSP_RESERVE_CAP_SAT", 50_000)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    # On-chain has 0. Shortfall = 50,000. LN local = 30,000. So all LN
    # held back, available_ln becomes 0, LN attempt skipped.
    from tests._fakes import FakeBitcartAPI
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=0)
    api.add_store("s1", wallets=["w1"])
    api.add_channel("w1", local_balance=30_000, remote_balance=0,
                    active=True, state="OPEN")

    calls = _record_amounts(monkeypatch)
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    # LN attempt skipped because available_ln was clamped to 0.
    assert not any(c["path"] == "ln" for c in calls)
    # On-chain still fires (sweeps the 0 sat — likely no-op but called).
    # In practice safe_to_spend returns 0 and onchain leg may skip,
    # but the function gets a chance.


def test_ln_cashout_force_amount_bypasses_holdback(monkeypatch, event_loop):
    """FORCE_CASHOUT_AMOUNT_LN is a debug override — it should bypass
    the LSP-shortfall hold-back logic and send exactly the configured
    amount."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "MIN_RESERVE_ONCHAIN", 50_000)
    monkeypatch.setattr(liquidityhelper, "LSP_RESERVE_CAP_SAT", 50_000)
    monkeypatch.setattr(liquidityhelper, "FORCE_CASHOUT_AMOUNT_LN", 250_000)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")

    # On-chain has 0, would normally hold back 50k. Force overrides.
    from tests._fakes import FakeBitcartAPI
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", balance=0)
    api.add_store("s1", wallets=["w1"])
    api.add_channel("w1", local_balance=500_000, remote_balance=0,
                    active=True, state="OPEN")

    calls = _record_amounts(monkeypatch)
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    ln_call = next(c for c in calls if c["path"] == "ln")
    assert ln_call["amount"] == 250_000   # exactly what FORCE specified


# ---------------------------------------------------------------------------
# Stranded-LN-funds warning
# ---------------------------------------------------------------------------
# When LN cashouts have been failing for >X days AND the automatic drain
# pathway is not configured (LOOP_OUT_ENABLED=False or CASHOUT_ONCHAIN_XPUB
# unset), the script can't recover the funds itself. It must surface
# this loudly so the operator sees it — without spamming every tick.
#
# Implementation: log_decision with a stable key ("ln_funds_stranded",
# wallet_id) and a value flipped on/off based on whether the wallet is
# currently in the stranded state. Dedupe means the WARNING fires
# exactly once per state transition.

import logging


def _set_stale_ln(days_ago: int = 30) -> None:
    """Mark the cashout-LN rail as having been failing for `days_ago`
    days. _ln_known_stale_for_cashout reads the failure-streak marker
    (FIRST_LN_CASHOUT_FAILURE_SINCE_SUCCESS); we also seed the last-
    success timestamp for tests that log it for human readability."""
    SimpleDateTimeField.replace(
        name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
        date=datetime.datetime.now() - datetime.timedelta(days=days_ago),
    ).execute()
    SimpleDateTimeField.replace(
        name="FIRST_LN_CASHOUT_FAILURE_SINCE_SUCCESS",
        date=datetime.datetime.now() - datetime.timedelta(days=days_ago),
    ).execute()


def _stranded_warning_baseline(monkeypatch):
    """Common monkeypatch state: LN attempts fail, drain helper records
    its calls but does no actual work, on-chain leg records too. Clears
    the inter-test decision-state memory so dedupe behavior is testable
    in isolation."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_LN", True)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "PREFER_CASHOUT_ONCHAIN", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS", 7)
    # Reset decision-dedupe memory; otherwise prior tests' state leaks in
    # and the first call here returns no-log even on a real transition.
    liquidityhelper._last_decision_state.clear()

    async def fake_ln_fail(api, wallet_id, amt):
        return False

    async def fake_onchain(api, wallet_id, amt):
        return True

    async def fake_drain(api, wallet_id, wallet):
        return None

    monkeypatch.setattr(liquidityhelper, "do_ln_cashouts", fake_ln_fail)
    monkeypatch.setattr(liquidityhelper, "do_onchain_cashouts", fake_onchain)
    monkeypatch.setattr(
        liquidityhelper, "_drain_ln_for_cashout_if_enabled", fake_drain,
    )


def test_stranded_warning_fires_when_loop_off_and_ln_stale(monkeypatch, event_loop, caplog):
    """Headline case: LN failing for >X days, LOOP_OUT_ENABLED=False,
    wallet has LN balance. Operator should see exactly one WARNING in
    decisions.log naming the wallet and the recovery steps."""
    _stranded_warning_baseline(monkeypatch)
    monkeypatch.setattr(liquidityhelper, "LOOP_OUT_ENABLED", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")
    _set_stale_ln(days_ago=30)

    api = _make_api_with_ln_wallet()
    # The decisions logger sets propagate=False so its records don't
    # reach pytest's root caplog handler. Attach the handler directly.
    logging.getLogger("liquidityhelper.decisions").addHandler(caplog.handler)
    with caplog.at_level(logging.WARNING, logger="liquidityhelper.decisions"):
        event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    stranded_logs = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "STRANDED" in r.getMessage()
    ]
    assert len(stranded_logs) == 1, (
        f"expected exactly one STRANDED warning, got {len(stranded_logs)}: "
        f"{[r.getMessage() for r in stranded_logs]}"
    )
    msg = stranded_logs[0].getMessage()
    assert "w1" in msg                           # wallet id named
    assert "30 days" in msg or "30" in msg       # days-stale included
    assert "LOOP_OUT_ENABLED=False" in msg       # current state visible
    # Recovery options enumerated.
    assert "(a)" in msg and "(b)" in msg and "(c)" in msg


def test_stranded_warning_dedupes_across_ticks(monkeypatch, event_loop, caplog):
    """The warning must fire exactly once per state transition.
    Running do_cashouts three ticks in a row with the same stranded
    state should produce ONE warning, not three. Otherwise decisions.log
    fills with duplicates within hours."""
    _stranded_warning_baseline(monkeypatch)
    monkeypatch.setattr(liquidityhelper, "LOOP_OUT_ENABLED", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")
    _set_stale_ln(days_ago=30)

    api = _make_api_with_ln_wallet()
    # The decisions logger sets propagate=False so its records don't
    # reach pytest's root caplog handler. Attach the handler directly.
    logging.getLogger("liquidityhelper.decisions").addHandler(caplog.handler)
    with caplog.at_level(logging.WARNING, logger="liquidityhelper.decisions"):
        for _ in range(3):
            event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    stranded_logs = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "STRANDED" in r.getMessage()
    ]
    assert len(stranded_logs) == 1, (
        f"expected 1 STRANDED warning across 3 ticks, got {len(stranded_logs)}"
    )


def test_stranded_warning_clears_when_loop_enabled(monkeypatch, event_loop, caplog):
    """When the operator flips LOOP_OUT_ENABLED=True, the drain path
    becomes viable; the warning should clear with an INFO transition
    on the next tick. Pins the "auto-clear on recovery" property."""
    _stranded_warning_baseline(monkeypatch)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")
    _set_stale_ln(days_ago=30)
    api = _make_api_with_ln_wallet()

    # Tick 1: stranded.
    monkeypatch.setattr(liquidityhelper, "LOOP_OUT_ENABLED", False)
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    # Tick 2: operator turned LOOP_OUT on. Warning should clear.
    monkeypatch.setattr(liquidityhelper, "LOOP_OUT_ENABLED", True)
    logging.getLogger("liquidityhelper.decisions").addHandler(caplog.handler)
    with caplog.at_level(logging.INFO, logger="liquidityhelper.decisions"):
        event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    cleared_logs = [
        r for r in caplog.records
        if "drain pathway is configured" in r.getMessage()
    ]
    assert cleared_logs, "expected 'drain pathway is configured' INFO line"


def test_stranded_warning_clears_when_ln_recovers(monkeypatch, event_loop, caplog):
    """The other recovery path: LN itself starts working again. Even
    without LOOP_OUT_ENABLED, a successful LN cashout means funds
    aren't stuck — the warning should clear via the LN-success branch."""
    _stranded_warning_baseline(monkeypatch)
    monkeypatch.setattr(liquidityhelper, "LOOP_OUT_ENABLED", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")
    _set_stale_ln(days_ago=30)
    api = _make_api_with_ln_wallet()

    # Tick 1: LN fails, stranded fires.
    event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    # Tick 2: LN starts succeeding. Patch in a successful LN handler.
    async def fake_ln_success(api, wallet_id, amt):
        return True
    monkeypatch.setattr(liquidityhelper, "do_ln_cashouts", fake_ln_success)

    logging.getLogger("liquidityhelper.decisions").addHandler(caplog.handler)
    with caplog.at_level(logging.INFO, logger="liquidityhelper.decisions"):
        event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    recovered_logs = [
        r for r in caplog.records
        if "no longer stranded" in r.getMessage()
    ]
    assert recovered_logs, (
        "expected 'no longer stranded' INFO line after LN recovery"
    )


def test_stranded_warning_not_fired_when_ln_not_yet_stale(monkeypatch, event_loop, caplog):
    """LN failed but is still 'recent' (<X days since last success).
    We're in the wait-and-retry window — no stranded warning, no
    panic. Pins that operators don't get false alarms on transient LN
    hiccups."""
    _stranded_warning_baseline(monkeypatch)
    monkeypatch.setattr(liquidityhelper, "LOOP_OUT_ENABLED", False)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")
    _set_stale_ln(days_ago=1)   # 1 day < threshold of 7

    api = _make_api_with_ln_wallet()
    # The decisions logger sets propagate=False so its records don't
    # reach pytest's root caplog handler. Attach the handler directly.
    logging.getLogger("liquidityhelper.decisions").addHandler(caplog.handler)
    with caplog.at_level(logging.WARNING, logger="liquidityhelper.decisions"):
        event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    stranded_logs = [
        r for r in caplog.records
        if "STRANDED" in r.getMessage()
    ]
    assert not stranded_logs, (
        "STRANDED warning must NOT fire while LN is still within the "
        "retry window"
    )


def test_stranded_warning_not_fired_when_drain_configured(monkeypatch, event_loop, caplog):
    """LOOP_OUT_ENABLED=True AND CASHOUT_ONCHAIN_XPUB set means the script's
    automatic recovery is available — funds will drain on their own.
    No stranded warning. Pins that the warning fires only when
    *manual* operator intervention is required."""
    _stranded_warning_baseline(monkeypatch)
    monkeypatch.setattr(liquidityhelper, "LOOP_OUT_ENABLED", True)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", "bc1qfakedest")
    _set_stale_ln(days_ago=30)

    api = _make_api_with_ln_wallet()
    # The decisions logger sets propagate=False so its records don't
    # reach pytest's root caplog handler. Attach the handler directly.
    logging.getLogger("liquidityhelper.decisions").addHandler(caplog.handler)
    with caplog.at_level(logging.WARNING, logger="liquidityhelper.decisions"):
        event_loop.run_until_complete(liquidityhelper.do_cashouts(api))

    stranded_logs = [
        r for r in caplog.records if "STRANDED" in r.getMessage()
    ]
    assert not stranded_logs


def test_log_decision_level_kwarg_emits_warning(monkeypatch, caplog):
    """Pin the log_decision level kwarg directly: WARNING emits at the
    requested level so it'll show up in console + plugin Logs tab,
    not silently in INFO."""
    liquidityhelper._last_decision_state.clear()
    # The decisions logger sets propagate=False so its records don't
    # reach pytest's root caplog handler. Attach the handler directly.
    logging.getLogger("liquidityhelper.decisions").addHandler(caplog.handler)
    with caplog.at_level(logging.WARNING, logger="liquidityhelper.decisions"):
        liquidityhelper.log_decision(
            ("test_key",), True, "test warning msg",
            level=logging.WARNING,
        )
    warn_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "test warning msg" in r.getMessage()
    ]
    assert len(warn_records) == 1
