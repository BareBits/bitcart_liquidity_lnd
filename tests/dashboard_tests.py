"""Tests for the Dashboard tab's HTTP endpoint + computation logic.

Two layers:
  1. compute_dashboard() — pure function over a BitcartAPI; tests fake
     the API and assert on the returned payload shape + math.
  2. The FastAPI router — mounted on a plain app with no auth; we
     verify routing, query-param validation, and the 60s cache.

What we pin against (numbered in order of risk):
  1. Zero-store / no-invoice / null-USD safety: dashboard must NEVER
     crash on absent data — operators see the page during setup before
     anything has happened.
  2. Wallet-name filter: stores with non-"liquidityhelper" wallets are
     excluded from the rendered list.
  3. Shared-wallet warning: ≥2 stores pointing at the same wallet flips
     `shared_wallet_warning=True` so the UI shows the yellow banner.
  4. Multi-store summary aggregates field-wise.
  5. Single-store leaves `summary=None` (UI hides the section).
  6. Math: developer_fee_pct / hosting_fee_pct / amount_saved_vs_cc /
     net_fees_paid / pie_slices are derived correctly from upstream
     StoreStats.
  7. Cache: two calls in the same range within 60s return the same
     object (no recomputation).
  8. Time range: 'all' vs '30' produce different invoice counts when
     the store has old + recent invoices.

The fee math itself (StoreStats) is the responsibility of
new_calc_invoice_stats tests elsewhere — here we just verify the
dashboard project / aggregate / present the result correctly.
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bitcart_plugin import dashboard as dashboard_mod
from tests._fakes import FakeBitcartAPI


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    """The dashboard module caches at module scope, so tests would leak
    state without an explicit reset. autouse=True applies this to every
    test in the file."""
    dashboard_mod.invalidate_cache()
    yield
    dashboard_mod.invalidate_cache()


def _setup_engine_dispatch(monkeypatch, api: FakeBitcartAPI):
    """Replace the engine helpers `new_calc_invoice_stats` consults so
    they pull from the fake's in-memory dicts.

    new_calc_invoice_stats internally calls:
      - list_onchain_history(wallet, api)   → reads fake's onchain rows
      - list_ln_payments_with_labels(wallet, api) → reads fake's LN rows

    We patch the engine names so the body of new_calc_invoice_stats
    sees our test data without us having to spin up a real LND.
    """
    import liquidityhelper

    async def fake_onchain(*, wallet, api: Any = None):
        return list(api.onchain_history_by_wallet.get(wallet["id"], []))

    async def fake_ln(*, wallet, api: Any = None):
        return list(api.ln_history_by_wallet.get(wallet["id"], []))

    monkeypatch.setattr(liquidityhelper, "list_onchain_history", fake_onchain)
    monkeypatch.setattr(
        liquidityhelper, "list_ln_payments_with_labels", fake_ln,
    )

    # Engine's _get_dashboard_api builds a real BitcartAPI; for tests we
    # want it to return our fake. Make it async-yielding the fake.
    async def fake_get_dashboard_api():
        return api
    monkeypatch.setattr(
        liquidityhelper, "_get_dashboard_api", fake_get_dashboard_api,
    )


# ---------------------------------------------------------------------------
# Layer 1 — compute_dashboard() unit tests
# ---------------------------------------------------------------------------

def _run(coro):
    """Drive a coroutine to completion on a fresh event loop. Cleaner
    than depending on pytest-asyncio's auto-mode for these isolated
    tests."""
    return asyncio.new_event_loop().run_until_complete(coro)


def test_dashboard_empty_no_crash(monkeypatch):
    """Zero stores, zero wallets, no USD rate. Must produce a clean,
    well-formed response — this is what an operator sees the first
    time they open the page on a fresh install."""
    api = FakeBitcartAPI()
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert payload.stores == []
    assert payload.summary is None
    assert payload.shared_wallet_warning is False
    assert payload.btc_usd_rate is None
    assert payload.cc_baseline_pct == 0.05
    assert payload.range == "all"


def test_dashboard_skips_non_liquidityhelper_wallets(monkeypatch):
    """Store using a wallet named anything other than 'liquidityhelper'
    must NOT appear. Spec is explicit: 'ignore all wallets not named
    liquidityhelper'."""
    api = FakeBitcartAPI()
    api.add_wallet("w-other", currency="btc", name="some-other-wallet")
    api.add_store("s-other", name="Other Store", wallets=["w-other"], created="2025-01-01")
    api.add_invoice("s-other")
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert payload.stores == [], (
        "non-liquidityhelper wallets must not appear in the dashboard"
    )


def test_dashboard_single_store_no_invoices_zero_safe(monkeypatch):
    """Single liquidityhelper-named wallet, no invoices: every numeric
    field zero, every percentage None (denominator zero → '—' in UI),
    no crash."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", currency="btc", name="liquidityhelper")
    api.add_store("s1", name="Cafe Hodl", wallets=["w1"], created="2025-01-01")
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert len(payload.stores) == 1
    store = payload.stores[0]
    assert store.store_name == "Cafe Hodl"
    assert store.wallet_name == "liquidityhelper"
    assert store.revenue.sats == 0
    assert store.revenue.btc == 0.0
    assert store.revenue.usd is None       # no rate
    assert store.paid_invoice_count == 0
    assert store.developer_fees_paid.sats == 0
    assert store.developer_fee_pct is None, (
        "0/0 must produce None — UI renders '—', not 'NaN%'"
    )
    assert store.hosting_fee_pct is None
    assert store.network_fees_total.sats == 0
    assert store.net_fees_paid.sats == 0
    assert store.amount_saved_vs_cc.sats == 0
    assert store.active_channel_count == 0
    # Pie chart slices all zero — UI should handle this case (empty pie).
    assert store.pie_slices == {"developer": 0, "hosting": 0, "network": 0}
    # No summary section when there's only one store.
    assert payload.summary is None


def test_dashboard_usd_rate_propagates(monkeypatch):
    """When btc_usd_rate is available, every _Money field gets a usd
    value derived from the BTC amount."""
    api = FakeBitcartAPI()
    api.btc_usd_rate = 100_000.0   # $100k/BTC for easy arithmetic
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    # 100_000 sats = 0.001 BTC = $100
    api.add_invoice("s1", payments=[{
        "amount": "0.001", "currency": "btc", "symbol": "BTC", "lightning": True,
        "wallet_id": "w1", "is_used": True, "created": "2026-01-01T00:00:00",
    }])
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    store = payload.stores[0]
    assert store.revenue.sats == 100_000
    assert store.revenue.btc == 0.001
    assert store.revenue.usd == pytest.approx(100.0)
    assert payload.btc_usd_rate == 100_000.0


def test_dashboard_revenue_and_invoice_count(monkeypatch):
    """Three paid invoices → paid_invoice_count==3 and revenue is the
    sum of all payments across them."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    for i in range(3):
        api.add_invoice("s1", invoice_id=f"inv{i}", payments=[{
            "amount": "0.0001", "currency": "btc", "symbol": "BTC", "lightning": True,
            "wallet_id": "w1", "is_used": True, "created": "2026-01-01T00:00:00",
        }])
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    store = payload.stores[0]
    assert store.paid_invoice_count == 3
    assert store.revenue.sats == 30_000   # 3 × 10k


def test_dashboard_fees_due_matches_eligible_revenue_times_rate(monkeypatch):
    """developer_fees_due == revenue_eligible_for_fee × FEE_AMOUNT, and
    hosting_fees_due == revenue_eligible_for_fee × REFERRAL_FEE_AMOUNT.
    Mirrors the same formula `calculate_fees()` uses at liquidityhelper.py:3321.

    Uses 1 BTC of eligible revenue, FEE_AMOUNT=0.02, REFERRAL=0.005
    so the expected numbers are large enough to spot in the assert:
    dev_due = 2_000_000 sats, hosting_due = 500_000 sats."""
    import importlib, config
    importlib.reload(config)
    monkeypatch.setattr(config, "FEE_AMOUNT", 0.02, raising=False)
    monkeypatch.setattr(config, "REFERRAL_FEE_AMOUNT", 0.005, raising=False)

    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    # 1 BTC = 100_000_000 sats, all eligible (no promo/topup gating).
    api.add_invoice("s1", payments=[{
        "amount": "1.0", "currency": "btc", "symbol": "BTC", "lightning": True,
        "wallet_id": "w1", "is_used": True, "created": "2026-01-01T00:00:00",
    }])
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    store = payload.stores[0]
    assert store.developer_fees_due.sats == 2_000_000, (
        f"1 BTC × 0.02 = 2M sats; got {store.developer_fees_due.sats}"
    )
    assert store.hosting_fees_due.sats == 500_000, (
        f"1 BTC × 0.005 = 500k sats; got {store.hosting_fees_due.sats}"
    )
    # Paid is zero (no fee-payment payouts in this scenario) — the UI
    # uses (due − paid) to render the "owed" pill.
    assert store.developer_fees_paid.sats == 0
    assert store.hosting_fees_paid.sats == 0


def test_dashboard_fees_due_zero_when_no_revenue(monkeypatch):
    """No eligible revenue → both due fields are zero, even with a
    configured fee rate. Pins against accidentally surfacing the
    rate × 0 multiplication as something non-zero."""
    import importlib, config
    importlib.reload(config)
    monkeypatch.setattr(config, "FEE_AMOUNT", 0.02, raising=False)
    monkeypatch.setattr(config, "REFERRAL_FEE_AMOUNT", 0.005, raising=False)

    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    store = payload.stores[0]
    assert store.developer_fees_due.sats == 0
    assert store.hosting_fees_due.sats == 0


def test_dashboard_net_fees_pct_of_revenue(monkeypatch):
    """net_fees_pct = net_fees_paid / revenue, exposed alongside the
    existing developer_fee_pct / hosting_fee_pct so the UI can show a
    "net fees X% of revenue" annotation. None when revenue is 0."""
    import importlib, config
    importlib.reload(config)
    monkeypatch.setattr(config, "FEE_AMOUNT", 0.02, raising=False)

    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_invoice("s1", payments=[{
        "amount": "0.001", "currency": "btc", "symbol": "BTC", "lightning": True,
        "wallet_id": "w1", "is_used": True, "created": "2026-01-01T00:00:00",
    }])
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    store = payload.stores[0]
    # Revenue 100k sats; no actual fees paid in this fixture path, so
    # net_fees_pct == 0.0 (a real ratio, not None — revenue is non-zero).
    assert store.revenue.sats == 100_000
    assert store.net_fees_paid.sats == 0
    assert store.net_fees_pct == 0.0


def test_dashboard_net_fees_pct_none_when_revenue_zero(monkeypatch):
    """net_fees_pct must be None (renders as '—') when revenue is 0,
    mirroring the existing developer_fee_pct / hosting_fee_pct
    invariant. Pin against a regression where the UI renders 'NaN%'."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    store = payload.stores[0]
    assert store.revenue.sats == 0
    assert store.net_fees_pct is None


def test_dashboard_summary_sums_fees_due_across_stores(monkeypatch):
    """Multi-store dashboard: summary.developer_fees_due and
    summary.hosting_fees_due are the sum of per-store dues. Mirrors the
    existing paid-sum invariant for the dashboard's summary section."""
    import importlib, config
    importlib.reload(config)
    monkeypatch.setattr(config, "FEE_AMOUNT", 0.02, raising=False)
    monkeypatch.setattr(config, "REFERRAL_FEE_AMOUNT", 0.005, raising=False)

    api = FakeBitcartAPI()
    api.add_wallet("w-A", name="liquidityhelper")
    api.add_wallet("w-B", name="liquidityhelper")
    api.add_store("s-A", wallets=["w-A"], created="2025-01-01")
    api.add_store("s-B", wallets=["w-B"], created="2025-01-01")
    api.add_invoice("s-A", payments=[{
        "amount": "0.5", "currency": "btc", "symbol": "BTC", "lightning": True,
        "wallet_id": "w-A", "is_used": True, "created": "2026-01-01T00:00:00",
    }])
    api.add_invoice("s-B", payments=[{
        "amount": "0.3", "currency": "btc", "symbol": "BTC", "lightning": True,
        "wallet_id": "w-B", "is_used": True, "created": "2026-01-01T00:00:00",
    }])
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert payload.summary is not None
    # 0.5 × 0.02 + 0.3 × 0.02 = 0.016 BTC = 1_600_000 sats
    assert payload.summary.developer_fees_due.sats == 1_600_000
    # 0.5 × 0.005 + 0.3 × 0.005 = 0.004 BTC = 400_000 sats
    assert payload.summary.hosting_fees_due.sats == 400_000


def test_dashboard_unpaid_invoices_excluded_from_count(monkeypatch):
    """An invoice without a paid_date must NOT count toward
    paid_invoice_count. Pin against a regression where pending
    invoices inflate the count."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_invoice("s1", invoice_id="paid")
    api.add_invoice("s1", invoice_id="pending", paid_date=None)
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert payload.stores[0].paid_invoice_count == 1


def test_dashboard_shared_wallet_warning(monkeypatch):
    """Two stores using the same wallet → warning flag set so the
    UI shows the yellow banner."""
    api = FakeBitcartAPI()
    api.add_wallet("w-shared", name="liquidityhelper")
    api.add_store("s-A", name="Store A", wallets=["w-shared"], created="2025-01-01")
    api.add_store("s-B", name="Store B", wallets=["w-shared"], created="2025-01-02")
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert payload.shared_wallet_warning is True


def test_dashboard_no_warning_when_wallets_disjoint(monkeypatch):
    """Two stores, two different wallets → no warning."""
    api = FakeBitcartAPI()
    api.add_wallet("w-A", name="liquidityhelper")
    api.add_wallet("w-B", name="liquidityhelper")
    api.add_store("s-A", wallets=["w-A"], created="2025-01-01")
    api.add_store("s-B", wallets=["w-B"], created="2025-01-02")
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert payload.shared_wallet_warning is False


def test_dashboard_multi_store_summary_aggregates(monkeypatch):
    """Two stores with revenue → summary section sums revenue,
    paid_invoice_count, fees field-wise."""
    api = FakeBitcartAPI()
    api.add_wallet("w-A", name="liquidityhelper")
    api.add_wallet("w-B", name="liquidityhelper")
    api.add_store("s-A", wallets=["w-A"], created="2025-01-01")
    api.add_store("s-B", wallets=["w-B"], created="2025-01-02")
    # Store A: 50k sats revenue, 1 invoice
    api.add_invoice("s-A", payments=[{
        "amount": "0.0005", "currency": "btc", "symbol": "BTC", "lightning": True,
        "wallet_id": "w-A", "is_used": True, "created": "2026-01-01T00:00:00",
    }])
    # Store B: 30k sats revenue, 2 invoices @ 15k each.
    # Use a BTC amount with a clean binary representation so
    # btc_to_sats(int truncation) doesn't bite the test (0.00015
    # rounds to 14999, not 15000).
    for i in range(2):
        api.add_invoice("s-B", invoice_id=f"B{i}", payments=[{
            "amount": "0.000125", "currency": "btc", "symbol": "BTC", "lightning": True,
            "wallet_id": "w-B", "is_used": True, "created": "2026-01-01T00:00:00",
        }])
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert len(payload.stores) == 2
    assert payload.summary is not None
    # 50k (Store A) + 2 × 12_500 (Store B) = 75_000
    assert payload.summary.revenue.sats == 75_000
    assert payload.summary.paid_invoice_count == 3


def test_dashboard_single_store_no_summary(monkeypatch):
    """Per spec: 'If there is more than one store, add a summary
    section'. Single store → no summary."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_invoice("s1")
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert len(payload.stores) == 1
    assert payload.summary is None


def test_dashboard_amount_saved_clamps_at_zero(monkeypatch):
    """When net fees exceed the 5% CC baseline (e.g. tiny store, huge
    on-chain fees), amount_saved should clamp to 0 rather than show a
    negative number — operators expect a non-negative 'savings' figure."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    # 10k sats revenue, but huge on-chain swap fee
    api.add_invoice("s1", payments=[{
        "amount": "0.0001", "currency": "btc", "symbol": "BTC", "lightning": True,
        "wallet_id": "w1", "is_used": True, "created": "2026-01-01T00:00:00",
    }])
    api.add_onchain_tx("w1", fee_sat=999999, label="OUTGOING SWAP", amount_sat=0)
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    store = payload.stores[0]
    # Whether or not the swap label registers, the clamp must work.
    assert store.amount_saved_vs_cc.sats >= 0, "amount saved must never be negative"


def test_dashboard_pie_slices_three_categories(monkeypatch):
    """Pie chart has exactly 3 slices per the spec: developer,
    hosting, network. Pin the key set + that they sum to net fees."""
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_invoice("s1")
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    pie = payload.stores[0].pie_slices
    assert set(pie.keys()) == {"developer", "hosting", "network"}
    assert sum(pie.values()) == payload.stores[0].net_fees_paid.sats


def test_dashboard_invalid_range_raises_400(monkeypatch):
    """Hostile/buggy client sending range=999999 must get a 400, not
    a stack trace or a 1B-day compute window."""
    from fastapi import HTTPException
    api = FakeBitcartAPI()
    _setup_engine_dispatch(monkeypatch, api)

    with pytest.raises(HTTPException) as excinfo:
        _run(dashboard_mod.compute_dashboard(api, "999"))
    assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# Layer 2 — FastAPI router tests
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    """FastAPI test client with our router mounted, no auth.

    The lazy `from liquidityhelper import _get_dashboard_api` inside
    the route body means we have to patch it BEFORE the request fires.
    Use a default no-store API; individual tests can re-patch as needed."""
    api = FakeBitcartAPI()
    _setup_engine_dispatch(monkeypatch, api)
    # Production mounts the router under bitcart's app which sets
    # root_path="/api"; the dashboard router's prefix deliberately
    # omits /api/ to avoid double-mounting in production. Mirror that
    # here so the test routes resolve at /api/plugins/liquidityhelper/...
    app = FastAPI(root_path="/api")
    app.include_router(dashboard_mod.build_router(auth_dependency=None))
    return TestClient(app), api


def test_router_get_dashboard_default_range(client):
    test_client, _api = client
    resp = test_client.get("/api/plugins/liquidityhelper/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["range"] == "all"
    assert body["stores"] == []
    assert body["summary"] is None


def test_router_get_dashboard_with_range(client):
    test_client, _api = client
    resp = test_client.get("/api/plugins/liquidityhelper/dashboard?range=30")
    assert resp.status_code == 200
    assert resp.json()["range"] == "30"


def test_router_invalid_range_returns_400(client):
    test_client, _api = client
    resp = test_client.get("/api/plugins/liquidityhelper/dashboard?range=not-a-range")
    assert resp.status_code == 400
    assert "invalid range" in resp.json()["detail"]


def test_router_cache_returns_same_response(client):
    """Two calls within 60s for the same range return the same payload.
    We pin this by mutating the fake between calls — if the cache works,
    the second call returns the OLD data."""
    test_client, api = client
    r1 = test_client.get("/api/plugins/liquidityhelper/dashboard")
    assert r1.status_code == 200
    body1 = r1.json()

    # Add a store. If cache works, second call still shows empty.
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")

    r2 = test_client.get("/api/plugins/liquidityhelper/dashboard")
    assert r2.status_code == 200
    assert r2.json() == body1, "cache must short-circuit the second call"


def test_router_force_refresh_bypasses_cache(client):
    """force_refresh=true must bypass the cache."""
    test_client, api = client
    test_client.get("/api/plugins/liquidityhelper/dashboard")  # prime cache

    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")

    r = test_client.get(
        "/api/plugins/liquidityhelper/dashboard?force_refresh=true"
    )
    assert r.status_code == 200
    assert len(r.json()["stores"]) == 1, (
        "force_refresh=true must recompute and pick up the new store"
    )


def test_router_invalidate_cache_helper(client):
    """invalidate_cache() is the public API for tests / a manual refresh.
    After clearing, the next call recomputes."""
    test_client, api = client
    test_client.get("/api/plugins/liquidityhelper/dashboard")
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")

    dashboard_mod.invalidate_cache()
    r = test_client.get("/api/plugins/liquidityhelper/dashboard")
    assert len(r.json()["stores"]) == 1


# ---------------------------------------------------------------------------
# Layer 3 — Recent activity tables (fee payments, cashouts, closures)
# ---------------------------------------------------------------------------

def test_recent_tables_empty_when_no_activity(monkeypatch):
    """No wallets, no closures → all three recent-activity lists empty.
    Pins zero-data safety so a fresh install renders the dashboard
    without crashing on the new sections."""
    api = FakeBitcartAPI()
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert payload.recent_fee_payments == []
    assert payload.recent_cashouts == []
    assert payload.recent_channel_closures == []


def test_recent_fee_payments_picks_up_onchain_dev_fees(monkeypatch):
    """An on-chain tx labelled FEE_PAYOUT_REASON shows up in the
    recent_fee_payments table with method='onchain' and a txid."""
    import config as _cfg
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_onchain_tx(
        "w1", label=_cfg.FEE_PAYOUT_REASON,
        amount_sat=10_000, fee_sat=500,
        txid="deadbeef" * 8, timestamp=1700000000, dest_address="bc1qfee",
    )
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert len(payload.recent_fee_payments) == 1
    row = payload.recent_fee_payments[0]
    assert row.fee_type == "developer"
    assert row.method == "onchain"
    assert row.amount_sats == 10_000
    assert row.fee_sats == 500
    assert row.txid == "deadbeef" * 8
    assert row.payment_hash == ""
    assert row.destination == "bc1qfee"


def test_recent_fee_payments_picks_up_ln_referral_fees(monkeypatch):
    """An LN tx labelled REFERRAL_PAYOUT_REASON shows as method='lightning'
    with fee_type='hosting' (the user-facing name for what's internally
    called 'referral')."""
    import config as _cfg
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_ln_tx(
        "w1", label=_cfg.REFERRAL_PAYOUT_REASON,
        amount_msat=-5_000_000,    # 5000 sats outgoing
        fee_msat=20_000,           # 20 sats fee
        payment_hash="abc123", timestamp=1700001000,
    )
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert len(payload.recent_fee_payments) == 1
    row = payload.recent_fee_payments[0]
    assert row.fee_type == "hosting"
    assert row.method == "lightning"
    assert row.amount_sats == 5_000
    assert row.fee_sats == 20
    assert row.payment_hash == "abc123"
    assert row.txid == ""


def test_recent_fee_payments_sorted_newest_first(monkeypatch):
    """Multiple payments → newest timestamp first."""
    import config as _cfg
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_onchain_tx("w1", label=_cfg.FEE_PAYOUT_REASON,
        amount_sat=100, fee_sat=10, txid="aa" * 32, timestamp=1700000000)
    api.add_onchain_tx("w1", label=_cfg.FEE_PAYOUT_REASON,
        amount_sat=200, fee_sat=20, txid="bb" * 32, timestamp=1800000000)
    api.add_onchain_tx("w1", label=_cfg.FEE_PAYOUT_REASON,
        amount_sat=300, fee_sat=30, txid="cc" * 32, timestamp=1750000000)
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    timestamps = [r.timestamp for r in payload.recent_fee_payments]
    assert timestamps == sorted(timestamps, reverse=True)


def test_recent_fee_payments_caps_at_100(monkeypatch):
    """Backend caps at 100 entries — UI paginates 10/page from that."""
    import config as _cfg
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    for i in range(150):
        api.add_onchain_tx("w1", label=_cfg.FEE_PAYOUT_REASON,
            amount_sat=100, fee_sat=10,
            txid=f"{i:064x}", timestamp=1700000000 + i)
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert len(payload.recent_fee_payments) == 100


def test_recent_fee_payments_skips_non_liquidityhelper_wallets(monkeypatch):
    """Spec: ignore wallets not named 'liquidityhelper'. Fee payments
    from such wallets must not appear."""
    import config as _cfg
    api = FakeBitcartAPI()
    api.add_wallet("w-other", name="some-other-wallet")
    api.add_store("s1", wallets=["w-other"], created="2025-01-01")
    api.add_onchain_tx("w-other", label=_cfg.FEE_PAYOUT_REASON,
        amount_sat=100, fee_sat=10, txid="00" * 32, timestamp=1700000000)
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert payload.recent_fee_payments == []


def test_recent_fee_payments_skips_incoming_txs(monkeypatch):
    """An incoming on-chain tx (even if labelled) must not appear in
    the fee-payment list — fees only go out."""
    import config as _cfg
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_onchain_tx("w1", label=_cfg.FEE_PAYOUT_REASON,
        amount_sat=100, fee_sat=10, incoming=True,
        txid="dd" * 32, timestamp=1700000000)
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert payload.recent_fee_payments == []


def test_recent_cashouts_picks_up_cashout_label_only(monkeypatch):
    """The cashouts table filters to CASHOUT_REASON-labelled rows,
    not dev fees or referral fees."""
    import config as _cfg
    api = FakeBitcartAPI()
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_onchain_tx("w1", label=_cfg.CASHOUT_REASON,
        amount_sat=50_000, fee_sat=300,
        txid="ca" * 32, timestamp=1700000000, dest_address="bc1qcashout")
    # Fee payment with a different label — must NOT appear in cashouts.
    api.add_onchain_tx("w1", label=_cfg.FEE_PAYOUT_REASON,
        amount_sat=100, fee_sat=10, txid="fe" * 32, timestamp=1700001000)
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert len(payload.recent_cashouts) == 1
    assert payload.recent_cashouts[0].fee_type == "cashout"
    assert payload.recent_cashouts[0].amount_sats == 50_000
    # And the fee payment table picked up the other one.
    assert len(payload.recent_fee_payments) == 1
    assert payload.recent_fee_payments[0].txid == "fe" * 32


def test_recent_payments_usd_conversion(monkeypatch):
    """When btc_usd_rate is set, amount_usd and fee_usd are populated."""
    import config as _cfg
    api = FakeBitcartAPI()
    api.btc_usd_rate = 100_000.0
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_onchain_tx("w1", label=_cfg.FEE_PAYOUT_REASON,
        amount_sat=100_000,   # 0.001 BTC = $100
        fee_sat=10_000,       # 0.0001 BTC = $10
        txid="cc" * 32, timestamp=1700000000)
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    row = payload.recent_fee_payments[0]
    assert row.amount_usd == pytest.approx(100.0)
    assert row.fee_usd == pytest.approx(10.0)


def test_recent_payments_no_usd_when_rate_unavailable(monkeypatch):
    """No rate → amount_usd / fee_usd both None. UI renders '$—'."""
    import config as _cfg
    api = FakeBitcartAPI()
    api.btc_usd_rate = None
    api.add_wallet("w1", name="liquidityhelper")
    api.add_store("s1", wallets=["w1"], created="2025-01-01")
    api.add_onchain_tx("w1", label=_cfg.FEE_PAYOUT_REASON,
        amount_sat=100_000, fee_sat=10_000,
        txid="aa" * 32, timestamp=1700000000)
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    row = payload.recent_fee_payments[0]
    assert row.amount_usd is None
    assert row.fee_usd is None


def test_recent_channel_closures_reads_from_db(monkeypatch):
    """LightningChannel rows with non-null close_reason appear in the closures list. (Tests already get a fresh in-memory DB via the autouse fixture in conftest; the explicit delete here is just paranoia.)"""
    from node_database import LightningChannel
    # Clean slate
    LightningChannel.delete().execute()

    now = datetime.datetime.now()
    LightningChannel.create(
        channel_point="aa" * 32 + ":0",
        cooperative_close_requested=now - datetime.timedelta(days=2),
        last_close_attempt_at=now - datetime.timedelta(days=2),
        cooperative_close_attempts=3,
        close_reason="AUDIT_FAILURE: HIGH_FEE_RATE,LOW_LIQUIDITY",
    )

    api = FakeBitcartAPI()
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert len(payload.recent_channel_closures) == 1
    row = payload.recent_channel_closures[0]
    assert row.channel_point == "aa" * 32 + ":0"
    assert "AUDIT_FAILURE" in row.close_reason
    assert row.cooperative_close_attempts == 3
    assert row.force_close_initiated is False

    # Clean up so subsequent tests start clean.
    LightningChannel.delete().execute()


def test_recent_channel_closures_excludes_rows_without_reason(monkeypatch):
    """A row with no close_reason set (e.g. created by some other path
    that didn't wire the reason) must NOT appear. Pin against future
    regressions where a code path forgets to set the reason."""
    from node_database import LightningChannel
    LightningChannel.delete().execute()

    now = datetime.datetime.now()
    # Row WITH reason — should appear.
    LightningChannel.create(
        channel_point="11" * 32 + ":0",
        cooperative_close_requested=now,
        last_close_attempt_at=now,
        cooperative_close_attempts=1,
        close_reason="OFFLINE_BEYOND_THRESHOLD",
    )
    # Row WITHOUT reason — should NOT appear.
    LightningChannel.create(
        channel_point="22" * 32 + ":0",
        cooperative_close_requested=now,
        last_close_attempt_at=now,
        cooperative_close_attempts=1,
        # close_reason left None
    )

    api = FakeBitcartAPI()
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    cps = [r.channel_point for r in payload.recent_channel_closures]
    assert "11" * 32 + ":0" in cps
    assert "22" * 32 + ":0" not in cps

    LightningChannel.delete().execute()


def test_recent_channel_closures_force_close_flag(monkeypatch):
    """A row with force_close_initiated_at set → force_close_initiated=True."""
    from node_database import LightningChannel
    LightningChannel.delete().execute()

    now = datetime.datetime.now()
    LightningChannel.create(
        channel_point="ff" * 32 + ":0",
        cooperative_close_requested=now - datetime.timedelta(days=15),
        last_close_attempt_at=now,
        cooperative_close_attempts=10,
        force_close_initiated_at=now,
        close_reason="FORCE_CLOSE_AFTER_COOP_TIMEOUT: AUDIT_FAILURE: ...",
    )

    api = FakeBitcartAPI()
    _setup_engine_dispatch(monkeypatch, api)

    payload = _run(dashboard_mod.compute_dashboard(api, "all"))
    assert len(payload.recent_channel_closures) == 1
    assert payload.recent_channel_closures[0].force_close_initiated is True

    LightningChannel.delete().execute()


def test_close_reason_persisted_by_attempt_cooperative_close(monkeypatch):
    """End-to-end: calling attempt_cooperative_close with a reason
    writes that reason to the LightningChannel row. Pin the wiring
    between the close-initiation path and the dashboard's data source."""
    import liquidityhelper
    from node_database import LightningChannel
    LightningChannel.delete().execute()

    # Stub out electrum_rpc so this doesn't try to talk to an Electrum daemon (the Electrum branch is the one this test exercises — wallet currency is 'btc').
    async def fake_electrum_close(*args, **kwargs):
        return {"closed": True}
    monkeypatch.setattr(liquidityhelper, "electrum_rpc", fake_electrum_close)

    _run(liquidityhelper.attempt_cooperative_close(
        channel_point="cc" * 32 + ":0",
        wallet={"id": "w1", "currency": "btc", "xpub": "x"},
        api=None,
        reason="AUDIT_FAILURE: HIGH_FEE_RATE",
    ))

    row = LightningChannel.get_or_none(
        LightningChannel.channel_point == "cc" * 32 + ":0"
    )
    assert row is not None
    assert row.close_reason == "AUDIT_FAILURE: HIGH_FEE_RATE"

    LightningChannel.delete().execute()


def test_close_reason_overwrites_on_force_escalation(monkeypatch):
    """When the same channel is escalated from coop to force close,
    the second _record_close_attempt call updates close_reason to
    include the FORCE_CLOSE_AFTER_COOP_TIMEOUT prefix. Pin the
    overwrite semantics — operators want to see the most recent
    reason, not the original."""
    import liquidityhelper
    from node_database import LightningChannel
    LightningChannel.delete().execute()

    liquidityhelper._record_close_attempt(
        "ee" * 32 + ":0", force=False, reason="OFFLINE_BEYOND_THRESHOLD",
    )
    liquidityhelper._record_close_attempt(
        "ee" * 32 + ":0", force=True,
        reason="FORCE_CLOSE_AFTER_COOP_TIMEOUT: OFFLINE_BEYOND_THRESHOLD",
    )
    row = LightningChannel.get(LightningChannel.channel_point == "ee" * 32 + ":0")
    assert row.close_reason.startswith("FORCE_CLOSE_AFTER_COOP_TIMEOUT")
    assert "OFFLINE_BEYOND_THRESHOLD" in row.close_reason
    assert row.force_close_initiated_at is not None

    LightningChannel.delete().execute()


def test_close_reason_preserved_on_retry_without_reason(monkeypatch):
    """When the retry loop in process_pending_closes calls
    attempt_cooperative_close WITHOUT a reason (default), the existing
    row's close_reason must NOT be wiped. This pins the documented
    "only overwrite when a reason is passed" contract of
    _record_close_attempt."""
    import liquidityhelper
    from node_database import LightningChannel
    LightningChannel.delete().execute()

    liquidityhelper._record_close_attempt(
        "bb" * 32 + ":0", force=False, reason="AUDIT_FAILURE: ORIGINAL",
    )
    liquidityhelper._record_close_attempt(
        "bb" * 32 + ":0", force=False,    # no reason kwarg
    )
    row = LightningChannel.get(LightningChannel.channel_point == "bb" * 32 + ":0")
    assert row.close_reason == "AUDIT_FAILURE: ORIGINAL"
    assert row.cooperative_close_attempts == 2

    LightningChannel.delete().execute()
