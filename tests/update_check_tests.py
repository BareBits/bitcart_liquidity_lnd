"""Tests for the in-plugin update detector + worker heartbeat
(update_check.py) — Phase 1 of the auto-update system.

These exercise the pure version logic, the DB-backed cache + heartbeat,
the throttle + email-once-per-version behaviour of maybe_check_for_updates
(with the network fetch stubbed), and the unauthenticated /health route.

No network is hit — fetch_latest_version is monkeypatched. The DB is the
shared test SQLite; each test resets only the keys it owns.
"""

from __future__ import annotations

import asyncio

import pytest

import update_check
from database import SimpleVariable, SimpleDateTimeField


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset_update_state():
    """Clear the rows update_check owns so each test starts clean."""
    for key in (
        update_check._K_LATEST_VERSION,
        update_check._K_CHECKED_CHANNEL,
        update_check._K_CHECKED_AT,
        update_check._K_EMAILED_VERSION,
    ):
        SimpleVariable.delete().where(SimpleVariable.name == key).execute()
    SimpleDateTimeField.delete().where(
        SimpleDateTimeField.name == update_check._K_HEARTBEAT
    ).execute()
    yield


# --------------------------------------------------------------------------- #
# Pure version helpers
# --------------------------------------------------------------------------- #

def test_parse_version_numeric_and_v_prefix():
    assert update_check._parse_version("0.1.0") == (0, 1, 0)
    assert update_check._parse_version("v1.2.3") == (1, 2, 3)
    assert update_check._parse_version("0.10.2") == (0, 10, 2)


def test_parse_version_rejects_non_numeric():
    assert update_check._parse_version("abc") is None
    assert update_check._parse_version("1.2.x") is None
    assert update_check._parse_version(None) is None
    assert update_check._parse_version("") is None


def test_is_newer_strict_and_conservative():
    assert update_check.is_newer("0.2.0", "0.1.0") is True
    assert update_check.is_newer("0.1.1", "0.1.0") is True
    assert update_check.is_newer("0.1.0", "0.1.0") is False   # equal
    assert update_check.is_newer("0.1.0", "0.2.0") is False   # older
    # Unparseable on either side → never claim an update.
    assert update_check.is_newer("garbage", "0.1.0") is False
    assert update_check.is_newer("0.2.0", None) is False


def test_normalize_channel():
    assert update_check.normalize_channel("main") == "main"
    assert update_check.normalize_channel("testing") == "testing"
    assert update_check.normalize_channel("evil; rm -rf") == "main"
    assert update_check.normalize_channel(None) == "main"


def test_get_running_version_matches_manifest():
    # manifest.json ships beside the engine; whatever it says is the
    # running version. Just assert it parses to a version tuple.
    v = update_check.get_running_version()
    assert v is not None
    assert update_check._parse_version(v) is not None


# --------------------------------------------------------------------------- #
# Heartbeat / worker liveness
# --------------------------------------------------------------------------- #

def test_heartbeat_roundtrip_and_liveness():
    assert update_check.get_last_tick() is None
    assert update_check.worker_alive() is False
    update_check.record_heartbeat()
    assert update_check.get_last_tick() is not None
    assert update_check.worker_alive() is True


def test_worker_not_alive_when_heartbeat_stale():
    update_check.record_heartbeat()
    # A zero-second tolerance makes any age fail the freshness check.
    assert update_check.worker_alive(max_age_seconds=-1) is False


# --------------------------------------------------------------------------- #
# Cache + warning surfacing
# --------------------------------------------------------------------------- #

def test_cached_status_reports_update_available():
    update_check._kv_set(update_check._K_LATEST_VERSION, "999.0.0")
    update_check._kv_set(update_check._K_CHECKED_CHANNEL, "testing")
    status = update_check.get_cached_update_status()
    assert status["latest_version"] == "999.0.0"
    assert status["update_channel"] == "testing"
    assert status["update_available"] is True


def test_warning_only_when_disabled_and_available():
    update_check._kv_set(update_check._K_LATEST_VERSION, "999.0.0")
    # Disabled + available → warning present, well-formed.
    w = update_check.get_update_health_warning(enabled=False)
    assert w is not None
    assert w["id"] == "update-available"
    assert w["severity"] == "MEDIUM"
    assert set(("id", "severity", "category", "title", "message", "settings")) <= set(w)
    # Enabled → no warning (the host updater handles it).
    assert update_check.get_update_health_warning(enabled=True) is None


def test_no_warning_when_up_to_date():
    running = update_check.get_running_version()
    update_check._kv_set(update_check._K_LATEST_VERSION, running)
    assert update_check.get_update_health_warning(enabled=False) is None


# --------------------------------------------------------------------------- #
# maybe_check_for_updates: throttle + email-once
# --------------------------------------------------------------------------- #

def test_check_stores_version_and_emails_once_when_disabled(monkeypatch):
    async def fake_fetch(channel):
        return "999.0.0"

    monkeypatch.setattr(update_check, "fetch_latest_version", fake_fetch)

    sent = []

    async def notifier(subject, body):
        sent.append((subject, body))
        return True  # email actually went out

    # interval 0 → always due, so we can call repeatedly without waiting.
    _run(update_check.maybe_check_for_updates(
        channel="main", interval_seconds=0, enabled=False,
        admin_notifier=notifier,
    ))
    assert update_check._kv_get(update_check._K_LATEST_VERSION) == "999.0.0"
    assert len(sent) == 1  # emailed about the new version

    # Same version again → no second email, even though we keep checking.
    for _ in range(3):
        _run(update_check.maybe_check_for_updates(
            channel="main", interval_seconds=0, enabled=False,
            admin_notifier=notifier,
        ))
    assert len(sent) == 1  # still exactly one


def test_check_retries_until_email_sent_then_exactly_once(monkeypatch):
    """If SMTP isn't ready (notifier returns False), we must NOT mark the
    version emailed — so we keep trying — and once it sends, the operator
    gets exactly one email and no more."""
    async def fake_fetch(channel):
        return "999.0.0"

    monkeypatch.setattr(update_check, "fetch_latest_version", fake_fetch)

    attempts = []
    can_send = {"ok": False}

    async def notifier(subject, body):
        attempts.append(subject)
        return can_send["ok"]

    # SMTP down: two checks, two attempts, nothing recorded as emailed.
    for _ in range(2):
        _run(update_check.maybe_check_for_updates(
            channel="main", interval_seconds=0, enabled=False,
            admin_notifier=notifier,
        ))
    assert len(attempts) == 2
    assert update_check._kv_get(update_check._K_EMAILED_VERSION) is None

    # SMTP comes up: next check sends and records it.
    can_send["ok"] = True
    _run(update_check.maybe_check_for_updates(
        channel="main", interval_seconds=0, enabled=False,
        admin_notifier=notifier,
    ))
    assert len(attempts) == 3
    assert update_check._kv_get(update_check._K_EMAILED_VERSION) == "999.0.0"

    # Further checks: no more attempts for the same version.
    _run(update_check.maybe_check_for_updates(
        channel="main", interval_seconds=0, enabled=False,
        admin_notifier=notifier,
    ))
    assert len(attempts) == 3  # unchanged


def test_check_does_not_email_when_enabled(monkeypatch):
    async def fake_fetch(channel):
        return "999.0.0"

    monkeypatch.setattr(update_check, "fetch_latest_version", fake_fetch)
    sent = []

    async def notifier(subject, body):
        sent.append(subject)
        return True

    _run(update_check.maybe_check_for_updates(
        channel="main", interval_seconds=0, enabled=True,
        admin_notifier=notifier,
    ))
    # Cached, but no operator email when auto-apply is on.
    assert update_check._kv_get(update_check._K_LATEST_VERSION) == "999.0.0"
    assert sent == []


def test_check_throttled_until_interval_elapses(monkeypatch):
    calls = []

    async def fake_fetch(channel):
        calls.append(channel)
        return "999.0.0"

    monkeypatch.setattr(update_check, "fetch_latest_version", fake_fetch)

    # First call with a long interval records checked_at = now.
    _run(update_check.maybe_check_for_updates(
        channel="main", interval_seconds=99999, enabled=False,
    ))
    assert len(calls) == 1
    # Immediate second call is throttled (interval not elapsed).
    _run(update_check.maybe_check_for_updates(
        channel="main", interval_seconds=99999, enabled=False,
    ))
    assert len(calls) == 1


def test_check_runs_immediately_on_channel_switch(monkeypatch):
    calls = []

    async def fake_fetch(channel):
        calls.append(channel)
        return "999.0.0"

    monkeypatch.setattr(update_check, "fetch_latest_version", fake_fetch)

    _run(update_check.maybe_check_for_updates(
        channel="main", interval_seconds=99999, enabled=False,
    ))
    # Different channel → check immediately despite the long interval.
    _run(update_check.maybe_check_for_updates(
        channel="testing", interval_seconds=99999, enabled=False,
    ))
    assert calls == ["main", "testing"]


# --------------------------------------------------------------------------- #
# /health endpoint
# --------------------------------------------------------------------------- #

def test_health_endpoint_contract_unauthenticated():
    """The route exists, needs no auth, returns 200 with the full
    contract shape, and never 500s.

    NOTE: we assert the CONTRACT (keys/types), not the DB-derived values.
    The test harness rebinds the SQLite DB to a thread-local `:memory:`
    instance (see conftest), and the async route runs in a different
    thread than this test body — so it legitimately sees an empty DB
    here. In production the DB is a real file, so the route reads the
    worker-written rows fine (same cross-process pattern the dashboard
    endpoint already relies on). Value plumbing is covered by the
    get_cached_update_status / worker_alive tests above.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from bitcart_plugin.health_endpoint import build_health_router

    app = FastAPI()
    app.include_router(build_health_router())
    client = TestClient(app)

    resp = client.get("/plugins/liquidityhelper/health")
    assert resp.status_code == 200  # no auth required
    body = resp.json()
    for key in (
        "ok", "running_version", "latest_version", "update_available",
        "update_channel", "auto_update_enabled", "worker_alive",
        "last_tick_at", "checked_at",
    ):
        assert key in body
    assert body["ok"] is True
    assert isinstance(body["update_available"], bool)
    assert isinstance(body["worker_alive"], bool)
    assert isinstance(body["auto_update_enabled"], bool)
    assert isinstance(body["update_channel"], str)
