"""Tests for the Bitcart plugin settings layer.

Covers:
  1. Schema completeness: every PluginSettings default matches config.py.
     Catches drift if someone edits one but not the other.
  2. merge_with_config: UI-supplied values override config.py; unspecified
     keys fall back to config.py defaults.
  3. apply_settings: setattr propagates to the engine modules and is
     idempotent.
  4. run_tick_loop in standalone mode: respects stop_event, respects
     SINGLE_RUN, doesn't hang.

Plugin.py itself depends on `api.plugins.BasePlugin` (bitcart-internal),
so we don't import it in standalone tests — only the schema/bridge
helpers, which are deliberately bitcart-free.
"""

from __future__ import annotations

import asyncio
import importlib
import sys

import pytest

# The schema + bridge are independent of bitcart and importable here.
from bitcart_plugin.settings_schema import (
    PluginSettings, SETTING_NAMES, _EXCLUDED, _OVERRIDES,
)
from bitcart_plugin.settings_bridge import apply_settings, merge_with_config


# ---------------------------------------------------------------------------
# Schema completeness
# ---------------------------------------------------------------------------

def test_every_schema_field_has_matching_config_default():
    """Each PluginSettings field MUST have a same-named attr on config
    with an equal default value. Drift detector — if you add a new knob
    to config.py, add it to the schema too (and vice versa).

    One exception where we deliberately differ from config.py:
      - AUTH_TOKEN: schema default is None; config default is also None
        but the plugin overwrites it with the bearer token it acquires.
    """
    import config

    drift: list[str] = []
    for name in SETTING_NAMES:
        if not hasattr(config, name):
            drift.append(f"{name}: missing from config.py")
            continue
        config_val = getattr(config, name)
        schema_default = PluginSettings.model_fields[name].default
        # Pydantic Field with default_factory holds PydanticUndefined here.
        # Compare against the factory's output instead.
        factory = PluginSettings.model_fields[name].default_factory
        if factory is not None:
            schema_default = factory()
        if config_val != schema_default:
            drift.append(f"{name}: config={config_val!r} schema={schema_default!r}")

    assert not drift, "Schema/config drift:\n  " + "\n  ".join(drift)


def test_schema_does_not_expose_internal_constants():
    """Constants like FEE_PAYOUT_REASON, MIN_CHANNEL_SIZE_IN_SATS, and
    the TOPUP_* names are deliberately NOT plugin-tunable — they're
    internal labels with semantics the engine treats as fixed. Keep
    them out of the schema so the UI doesn't invite breakage."""
    forbidden = {
        "FEE_PAYOUT_REASON",  # used as a TX label; engine matches on it
        "CASHOUT_REASON",
        "TOPUP_NAME",
        "TOPUP_BAREBITS",
        "MIN_CHANNEL_SIZE_IN_SATS",  # protocol minimum, not operator-tunable
    }
    overlap = forbidden & SETTING_NAMES
    assert not overlap, f"Internal constants leaked into schema: {overlap}"


def test_excluded_and_override_dicts_reference_real_config_names():
    """Drift detector for the schema-generator curation dicts.

    `_EXCLUDED` and `_OVERRIDES` in settings_schema.py are key-by-name
    against settings declared in config.py. If config.py renames or
    deletes a setting, a stale entry in either dict silently no-ops
    instead of failing loudly — the supposedly-hidden setting could
    quietly start appearing in the UI, or an override that was
    customizing a deleted setting just stops doing anything.

    This test asserts every key in both dicts resolves to a real
    attribute on the live config module. Catches stale entries the
    moment they happen.
    """
    import config

    stale: list[str] = []
    for name in _EXCLUDED:
        if not hasattr(config, name):
            stale.append(f"  _EXCLUDED[{name!r}]: no longer in config.py")
    for name in _OVERRIDES:
        if not hasattr(config, name):
            stale.append(f"  _OVERRIDES[{name!r}]: no longer in config.py")

    assert not stale, (
        "Stale entries found in settings_schema curation dicts — "
        "either remove the entry or restore the setting in config.py:\n"
        + "\n".join(stale)
    )


def test_excluded_and_override_dicts_do_not_overlap():
    """A name should appear in EITHER _EXCLUDED or _OVERRIDES, not both
    — _EXCLUDED means "don't generate this field at all", so an entry
    in _OVERRIDES for the same name is silently dead code (the
    overridden field never gets built). Catch the conflict early
    rather than letting the override quietly do nothing."""
    overlap = set(_EXCLUDED) & set(_OVERRIDES)
    assert not overlap, (
        f"These names are in BOTH _EXCLUDED and _OVERRIDES — pick one. "
        f"_OVERRIDES entries for excluded fields never take effect: {overlap}"
    )


# ---------------------------------------------------------------------------
# merge_with_config
# ---------------------------------------------------------------------------

def test_merge_with_empty_override_yields_config_defaults():
    """No UI overrides → result equals current config.py values."""
    import config

    merged = merge_with_config({})
    for name in SETTING_NAMES:
        if hasattr(config, name):
            assert getattr(merged, name) == getattr(config, name), name


def test_merge_with_partial_override_only_changes_listed_keys():
    """UI sets MIN_RESERVE_ONCHAIN; everything else stays at config defaults."""
    import config

    merged = merge_with_config({"MIN_RESERVE_ONCHAIN": 99_999})
    assert merged.MIN_RESERVE_ONCHAIN == 99_999
    # Unrelated key still tracks config.
    assert merged.LSP_CHANNEL_SIZE_SAT == config.LSP_CHANNEL_SIZE_SAT


def test_merge_drops_unknown_keys():
    """Schema is the source of truth — random keys from old settings
    rows are ignored, not crashed on."""
    merged = merge_with_config({"NOT_A_REAL_SETTING": "anything"})
    assert not hasattr(merged, "NOT_A_REAL_SETTING")


def test_merge_with_none_clears_optional_field():
    """User explicitly clears CASHOUT_ONCHAIN_XPUB in the UI → result
    is None, not the config default."""
    merged = merge_with_config({"CASHOUT_ONCHAIN_XPUB": None})
    assert merged.CASHOUT_ONCHAIN_XPUB is None


# ---------------------------------------------------------------------------
# apply_settings — the setattr bridge
# ---------------------------------------------------------------------------

def test_apply_settings_overrides_config_module():
    import config

    original = config.MIN_RESERVE_ONCHAIN
    try:
        settings = merge_with_config({"MIN_RESERVE_ONCHAIN": 42_000})
        applied = apply_settings(settings, modules=("config",))
        assert config.MIN_RESERVE_ONCHAIN == 42_000
        assert applied["MIN_RESERVE_ONCHAIN"] == 42_000
    finally:
        config.MIN_RESERVE_ONCHAIN = original


def test_apply_settings_overrides_liquidityhelper_module():
    """liquidityhelper.py does `from config import *`, so it has its
    own binding for each name. The bridge must update that copy too,
    otherwise functions inside liquidityhelper see stale values."""
    import config
    import liquidityhelper

    original = liquidityhelper.MIN_RESERVE_ONCHAIN
    try:
        settings = merge_with_config({"MIN_RESERVE_ONCHAIN": 12_345})
        apply_settings(settings, modules=("liquidityhelper",))
        assert liquidityhelper.MIN_RESERVE_ONCHAIN == 12_345
    finally:
        liquidityhelper.MIN_RESERVE_ONCHAIN = original
        config.MIN_RESERVE_ONCHAIN = config.MIN_RESERVE_ONCHAIN  # no-op


def test_apply_settings_only_touches_existing_attrs():
    """The bridge skips modules that don't already have the attr — it
    won't accidentally pollute classes.py with attrs it never
    imported. Verify with a module that DOESN'T `from config import *`."""
    # node_database imports only a few specific names from config; most
    # schema fields aren't bound on it. Make sure we don't add them.
    import node_database

    settings = merge_with_config({})
    apply_settings(settings, modules=("node_database",))
    # `MIN_RESERVE_ONCHAIN` is not used by node_database. It should not
    # have been added.
    assert not hasattr(node_database, "MIN_RESERVE_ONCHAIN")


# ---------------------------------------------------------------------------
# run_tick_loop — standalone-mode entry refactor
# ---------------------------------------------------------------------------

def test_run_tick_loop_respects_stop_event(monkeypatch, event_loop):
    """The plugin sets stop_event on shutdown; the loop must exit on the
    NEXT tick boundary (we don't yank mid-tick).

    Design note: the mocked main() signals the stop event itself from
    inside the loop, rather than relying on a separately-scheduled task.
    A prior version of this test used `asyncio.create_task` to fire
    stop.set() externally — if run_tick_loop ever forgets to yield, that
    sibling task is never scheduled and the loop wedges into an
    OOM-thrashing infinite spin. The new shape can't deadlock: the
    stop signal originates within an awaited coroutine, so it's
    guaranteed to complete before the next iteration check."""
    import liquidityhelper

    stop = asyncio.Event()
    ticks = {"n": 0}
    TICKS_BEFORE_STOP = 3

    async def fake_main():
        ticks["n"] += 1
        if ticks["n"] >= TICKS_BEFORE_STOP:
            stop.set()

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)

    # Tight timeout AS WELL — pytest.ini has a 60s safety kill but this
    # test should finish in milliseconds. Anything longer means
    # run_tick_loop regressed.
    event_loop.run_until_complete(
        asyncio.wait_for(
            liquidityhelper.run_tick_loop(stop_event=stop), timeout=2
        )
    )
    assert ticks["n"] == TICKS_BEFORE_STOP


def test_run_tick_loop_respects_single_run(monkeypatch, event_loop):
    """Standalone mode: SINGLE_RUN=True → exit after one tick."""
    import liquidityhelper

    ticks = []

    async def fake_main():
        ticks.append(True)

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)
    monkeypatch.setattr(liquidityhelper, "SINGLE_RUN", True)

    event_loop.run_until_complete(
        asyncio.wait_for(liquidityhelper.run_tick_loop(), timeout=5)
    )
    assert len(ticks) == 1


def test_run_tick_loop_skips_main_when_liquidity_disabled(monkeypatch, event_loop):
    """When LIQUIDITY_DISABLED=True the loop must NOT call main(); it
    should block on the stop_event-aware wait so shutdown is still
    prompt. Pins the dropdown-driven pause behavior."""
    import liquidityhelper

    stop = asyncio.Event()
    ticks = {"n": 0}

    async def fake_main():
        ticks["n"] += 1

    # Patch the per-iteration wait so the test doesn't sleep 60s — when
    # disabled, run_tick_loop awaits this with a timeout. We trigger
    # the stop event from inside the wait so the loop exits cleanly.
    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):
        # The first time we land here is the disabled-mode wait — fire
        # stop and return immediately so the loop exits.
        stop.set()
        return await real_wait_for(awaitable, timeout=0.1)

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", True)
    monkeypatch.setattr(liquidityhelper.asyncio, "wait_for", short_wait_for)

    try:
        event_loop.run_until_complete(
            asyncio.wait_for(
                liquidityhelper.run_tick_loop(stop_event=stop), timeout=2
            )
        )
        assert ticks["n"] == 0
    finally:
        liquidityhelper.LIQUIDITY_DISABLED = False


def test_run_tick_loop_resumes_when_liquidity_re_enabled(monkeypatch, event_loop):
    """Flipping LIQUIDITY_DISABLED False mid-loop must let main() fire
    on the next iteration. Mirrors what the bridge does when the
    operator changes the dropdown back to LSP/Automatic."""
    import liquidityhelper

    stop = asyncio.Event()
    ticks = {"n": 0}

    async def fake_main():
        ticks["n"] += 1
        # Stop after the first actual tick so the test bounds.
        stop.set()

    # Start disabled; flip enabled from inside the short_wait_for so
    # the second iteration goes through the main() path.
    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", True)

    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):
        liquidityhelper.LIQUIDITY_DISABLED = False
        return await real_wait_for(awaitable, timeout=0.05)

    monkeypatch.setattr(liquidityhelper.asyncio, "wait_for", short_wait_for)

    try:
        event_loop.run_until_complete(
            asyncio.wait_for(
                liquidityhelper.run_tick_loop(stop_event=stop), timeout=2
            )
        )
        assert ticks["n"] == 1
    finally:
        liquidityhelper.LIQUIDITY_DISABLED = False


def test_run_tick_loop_picks_up_live_single_run_flip(monkeypatch, event_loop):
    """If the plugin's settings_changed hook flips SINGLE_RUN mid-loop,
    we honor it on the next iteration boundary. Pins the
    `globals().get('SINGLE_RUN')` re-read at the tail of run_tick_loop."""
    import liquidityhelper

    counter = {"n": 0}

    async def fake_main():
        counter["n"] += 1
        if counter["n"] == 2:
            # Flip the global between ticks, like the bridge would do.
            liquidityhelper.SINGLE_RUN = True

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)
    monkeypatch.setattr(liquidityhelper, "SINGLE_RUN", False)

    try:
        event_loop.run_until_complete(
            asyncio.wait_for(liquidityhelper.run_tick_loop(), timeout=5)
        )
        assert counter["n"] == 2
    finally:
        liquidityhelper.SINGLE_RUN = False


# ---------------------------------------------------------------------------
# Main-loop resilience: uncaught exceptions in main() must NOT stop the loop
# ---------------------------------------------------------------------------
#
# The operator-stated invariant: the tick loop must repeat indefinitely
# regardless of what main() does. These tests pin that contract.

def test_run_tick_loop_survives_uncaught_exception_in_main(monkeypatch, event_loop):
    """main() raises a generic RuntimeError on the first tick. The
    loop MUST NOT exit — it should log the exception, wait, then run
    main() again successfully on the second tick."""
    import liquidityhelper

    ticks = {"n": 0}
    main_calls = {"raised": False, "succeeded": False}

    async def flaky_main():
        ticks["n"] += 1
        if ticks["n"] == 1:
            main_calls["raised"] = True
            raise RuntimeError("simulated failure on first tick")
        main_calls["succeeded"] = True
        # Exit cleanly on the second tick.
        liquidityhelper.SINGLE_RUN = True

    monkeypatch.setattr(liquidityhelper, "main", flaky_main)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)
    monkeypatch.setattr(liquidityhelper, "SINGLE_RUN", False)
    # Patch asyncio.sleep to return instantly — the 60-second backoff
    # in run_tick_loop is conceptually right but we don't want to
    # wait 60s in a unit test.
    real_sleep = asyncio.sleep
    async def fast_sleep(seconds):
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    try:
        event_loop.run_until_complete(
            asyncio.wait_for(liquidityhelper.run_tick_loop(), timeout=5)
        )
    finally:
        liquidityhelper.SINGLE_RUN = False

    assert main_calls["raised"] is True
    assert main_calls["succeeded"] is True
    assert ticks["n"] == 2, (
        "run_tick_loop must continue after main() raises; expected "
        f"2 ticks (raise, then succeed), got {ticks['n']}"
    )


def test_run_tick_loop_survives_many_consecutive_exceptions(
    monkeypatch, event_loop,
):
    """A persistently-broken main() must NOT stop the loop. The loop
    keeps trying — every iteration logs + waits + retries. Pin
    against any future refactor that adds a "give up after N
    failures" shortcut."""
    import liquidityhelper

    ticks = {"n": 0}

    async def always_broken_main():
        ticks["n"] += 1
        if ticks["n"] >= 5:
            # Eventually exit so the test terminates.
            liquidityhelper.SINGLE_RUN = True
            return
        raise ValueError(f"failure #{ticks['n']}")

    monkeypatch.setattr(liquidityhelper, "main", always_broken_main)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)
    monkeypatch.setattr(liquidityhelper, "SINGLE_RUN", False)
    # Capture the real asyncio.sleep BEFORE monkeypatching, otherwise
    # the replacement would call itself and recurse.
    real_sleep = asyncio.sleep
    async def fast_sleep(seconds):
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    try:
        event_loop.run_until_complete(
            asyncio.wait_for(liquidityhelper.run_tick_loop(), timeout=5)
        )
    finally:
        liquidityhelper.SINGLE_RUN = False
    assert ticks["n"] == 5, (
        "run_tick_loop must NOT give up after consecutive failures; "
        f"expected 5 ticks before SINGLE_RUN exit, got {ticks['n']}"
    )


def test_run_tick_loop_propagates_cancelled_error(monkeypatch, event_loop):
    """CancelledError is the one exception the loop MUST propagate —
    it's how cooperative shutdown (plugin.shutdown(), asyncio task
    cancellation) ends the task cleanly. Any other exception class
    is caught and continued; CancelledError ends the loop."""
    import liquidityhelper

    cancelled = {"observed": False}

    async def cancelling_main():
        cancelled["observed"] = True
        raise asyncio.CancelledError()

    monkeypatch.setattr(liquidityhelper, "main", cancelling_main)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)
    monkeypatch.setattr(liquidityhelper, "SINGLE_RUN", False)

    try:
        with pytest.raises(asyncio.CancelledError):
            event_loop.run_until_complete(
                asyncio.wait_for(liquidityhelper.run_tick_loop(), timeout=5)
            )
    finally:
        liquidityhelper.SINGLE_RUN = False
    assert cancelled["observed"] is True


def test_no_synchronous_sleep_in_main():
    """Static check: main() must not contain bare `sleep(` calls.
    The 9 sync sleeps used to block the entire Bitcart worker for
    30-60s on any startup error. Pin against regression — if a
    future contributor re-introduces `from time import sleep` plus a
    bare sleep(N), this test catches it."""
    import inspect
    import liquidityhelper
    src = inspect.getsource(liquidityhelper.main)
    import re
    # Look for bare sleep(N), not time.sleep / asyncio.sleep / await asyncio.sleep
    bare_sleep_calls = re.findall(r"(?<!\.)\bsleep\(", src)
    # Filter out asyncio.sleep references (false positives in comments
    # or strings — though our main() shouldn't have any sleep references
    # outside the await asyncio.sleep pattern).
    real_bare = [
        m for m in bare_sleep_calls
        if not src[max(0, src.find(m) - 10):src.find(m)].endswith("asyncio.")
    ]
    assert len(real_bare) == 0, (
        f"main() must not use synchronous sleep — these would freeze "
        f"the Bitcart worker. Use `await asyncio.sleep` instead. "
        f"Found {len(real_bare)} occurrences."
    )


# ---------------------------------------------------------------------------
# _get_lnd_connection cache race — concurrent callers must NOT build
# two channels for the same wallet
# ---------------------------------------------------------------------------

def test_get_lnd_connection_serializes_concurrent_builds(monkeypatch, event_loop):
    """Two coroutines call _get_lnd_connection(api, "w1") at the same
    time. Without the lock both would proceed past the cache miss,
    both fetch info, both create gRPC channels — one channel leaks.
    With the lock, only ONE call does the build; the other waits and
    returns the cached result. Pin via call-count on api.get_lnd_info."""
    import liquidityhelper

    # Reset cache so this test starts from a clean slate.
    liquidityhelper._LND_CONNECTIONS.clear()
    liquidityhelper._LND_CONNECTION_LOCKS.clear()

    info_calls = {"n": 0}
    channel_builds = {"n": 0}

    async def fake_get_lnd_info(wallet_id):
        info_calls["n"] += 1
        # Yield to the event loop so the OTHER waiter also enters the
        # "not in cache" branch in the unlocked version — this is
        # what makes the race observable in test.
        await asyncio.sleep(0.01)
        return {
            "tls_cert": "AA==",   # base64 of nothing
            "macaroon": "AA==",
            "host": "127.0.0.1",
            "grpc_port": 10009,
        }

    class FakeApi:
        get_lnd_info = staticmethod(fake_get_lnd_info)

    api = FakeApi()

    # Stub the gRPC bits so we don't open real channels — count
    # invocations to confirm only one happens.
    import liquidityhelper as _lh

    class FakeChannel:
        pass

    def fake_secure_channel(*args, **kwargs):
        channel_builds["n"] += 1
        return FakeChannel()

    monkeypatch.setattr(_lh._grpc, "ssl_channel_credentials",
                         lambda **kw: object())
    monkeypatch.setattr(_lh._grpc, "metadata_call_credentials",
                         lambda *a, **kw: object())
    monkeypatch.setattr(_lh._grpc, "composite_channel_credentials",
                         lambda *a, **kw: object())
    monkeypatch.setattr(_lh._grpc.aio, "secure_channel", fake_secure_channel)
    # Stub each stub class to construct trivially from a channel.
    monkeypatch.setattr(_lh, "_LND_SERVICES", {
        "Lightning": (lambda channel: object(), object()),
    })

    async def race():
        a, b = await asyncio.gather(
            liquidityhelper._get_lnd_connection(api, "w-race"),
            liquidityhelper._get_lnd_connection(api, "w-race"),
        )
        return a, b

    a, b = event_loop.run_until_complete(asyncio.wait_for(race(), timeout=5))

    # Both callers got the SAME connection object (cache hit on one
    # side, build on the other).
    assert a is b, (
        "concurrent _get_lnd_connection calls must return the same "
        "cached connection object; got different objects (would "
        "indicate the second caller built a new channel)"
    )
    # And only ONE underlying info-fetch + channel build happened.
    assert info_calls["n"] == 1, (
        f"expected exactly 1 api.get_lnd_info call across both "
        f"coroutines; got {info_calls['n']} (suggests the cache-build "
        f"path ran twice)"
    )
    assert channel_builds["n"] == 1, (
        f"expected exactly 1 secure_channel build; got "
        f"{channel_builds['n']} (channel leak)"
    )
