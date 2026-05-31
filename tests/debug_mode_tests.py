"""Tests for DEBUG_MODE — the manual-step pause for the tick loop.

When DEBUG_MODE is True, run_tick_loop blocks on _debug_run_once_trigger
before each main() call instead of cycling continuously. The Logs-tab
"Run one tick" button (and any other caller of trigger_debug_run_once)
fires the trigger to step through one iteration.

These tests pin:
  1. DEBUG_MODE=False preserves the existing continuous-loop behavior.
  2. DEBUG_MODE=True blocks the loop forever (asserted via wait_for
     timeout on a loop with NO trigger firing) — proving the gate
     actually gates.
  3. A single trigger fires exactly one tick.
  4. Multiple sequential triggers each fire one tick.
  5. Toggling DEBUG_MODE off while the loop is parked unblocks it
     (the plugin's settings-change hook calls the trigger).
  6. The /debug/run_once endpoint fires the trigger and returns 200.
  7. /debug/run_once with DEBUG_MODE=False returns 200 + an
     explanatory message (NOT an error — UI may be stale).
  8. stop_event interrupts the debug wait cleanly so shutdown doesn't
     hang waiting for a click that won't come.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import liquidityhelper
from bitcart_plugin import log_endpoints


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_debug_state(monkeypatch):
    """Reset DEBUG_MODE and clear the trigger between tests. autouse=True
    because every test in this file touches these globals and leaking
    state would cause confusing cross-test interactions."""
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", False)
    liquidityhelper._debug_run_once_trigger.clear()
    monkeypatch.setattr(liquidityhelper, "SINGLE_RUN", False)
    yield
    liquidityhelper._debug_run_once_trigger.clear()


# ---------------------------------------------------------------------------
# Loop-gating behavior
# ---------------------------------------------------------------------------

def test_debug_mode_off_preserves_continuous_loop(monkeypatch, event_loop):
    """Default behavior: DEBUG_MODE=False → loop cycles freely. Pin
    against a regression where the debug gate accidentally fires even
    when it shouldn't.

    Note: LIQUIDITY_DISABLED defaults to True on a fresh install (so
    operator funds aren't spent before the operator opts in via the
    dashboard). Tests covering DEBUG_MODE behavior have to disable
    that gate explicitly so they exercise the DEBUG_MODE branches and
    not the disabled-mode short-circuit."""
    ticks = {"n": 0}
    stop = asyncio.Event()

    async def fake_main():
        ticks["n"] += 1
        if ticks["n"] >= 3:
            stop.set()

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)

    event_loop.run_until_complete(asyncio.wait_for(
        liquidityhelper.run_tick_loop(stop_event=stop), timeout=2,
    ))
    assert ticks["n"] == 3


def test_debug_mode_on_blocks_without_trigger(monkeypatch, event_loop):
    """DEBUG_MODE=True with no trigger firing → loop never calls main().
    Asserts via wait_for(timeout=0.5) raising — the loop is genuinely
    parked and not just slow."""
    ticks = {"n": 0}

    async def fake_main():
        ticks["n"] += 1

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)

    with pytest.raises(asyncio.TimeoutError):
        event_loop.run_until_complete(asyncio.wait_for(
            liquidityhelper.run_tick_loop(), timeout=0.5,
        ))
    assert ticks["n"] == 0, "main() must NOT fire while the gate blocks"


def test_debug_mode_one_trigger_fires_exactly_one_tick(monkeypatch, event_loop):
    """trigger_debug_run_once() fires exactly one main() call, then
    the loop blocks again. We verify both halves: ticks==1 immediately
    after the trigger, AND a second timeout proves the loop re-parked."""
    ticks = {"n": 0}

    async def fake_main():
        ticks["n"] += 1

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)

    async def driver():
        # Spawn the loop, fire one trigger, then assert.
        loop_task = asyncio.create_task(liquidityhelper.run_tick_loop())
        # Give the loop a moment to reach the gate.
        await asyncio.sleep(0.05)
        liquidityhelper.trigger_debug_run_once()
        # Give the loop a moment to execute the gated tick.
        await asyncio.sleep(0.1)
        # The loop should have re-blocked. Cancel to terminate the
        # test cleanly.
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    event_loop.run_until_complete(asyncio.wait_for(driver(), timeout=2))
    assert ticks["n"] == 1


def test_debug_mode_two_triggers_two_ticks(monkeypatch, event_loop):
    """Sequential triggers each fire one tick. Pin against a regression
    where the trigger never clears between iterations (would result in
    tight-looping)."""
    ticks = {"n": 0}

    async def fake_main():
        ticks["n"] += 1

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)

    async def driver():
        loop_task = asyncio.create_task(liquidityhelper.run_tick_loop())
        await asyncio.sleep(0.05)
        liquidityhelper.trigger_debug_run_once()
        await asyncio.sleep(0.05)
        liquidityhelper.trigger_debug_run_once()
        await asyncio.sleep(0.1)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    event_loop.run_until_complete(asyncio.wait_for(driver(), timeout=2))
    assert ticks["n"] == 2


def test_toggling_debug_mode_off_unblocks_loop(monkeypatch, event_loop):
    """When DEBUG_MODE is flipped False while the loop is parked, the
    settings-change hook fires the trigger. After the gated tick, the
    next loop iteration sees DEBUG_MODE=False and resumes continuous
    cycling. Pin the full transition.

    Note this test doesn't invoke the plugin's settings hook directly;
    it just simulates the hook's side effects: flip the global AND
    fire the trigger. That mirrors what _on_settings_changed does at
    the relevant point in its body."""
    ticks = {"n": 0}
    stop = asyncio.Event()

    async def fake_main():
        ticks["n"] += 1
        if ticks["n"] >= 3:
            stop.set()

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)
    monkeypatch.setattr(liquidityhelper, "LIQUIDITY_DISABLED", False)

    async def driver():
        loop_task = asyncio.create_task(
            liquidityhelper.run_tick_loop(stop_event=stop)
        )
        await asyncio.sleep(0.05)
        # Operator disables DEBUG_MODE in the UI.
        liquidityhelper.DEBUG_MODE = False
        # The settings hook fires the trigger to wake the loop.
        liquidityhelper.trigger_debug_run_once()
        await loop_task

    event_loop.run_until_complete(asyncio.wait_for(driver(), timeout=2))
    # After unblocking, the loop ran 3 ticks freely before stop fired.
    assert ticks["n"] == 3


def test_stop_event_interrupts_debug_wait(monkeypatch, event_loop):
    """Shutdown must not hang waiting for a debug click that won't
    come. With DEBUG_MODE=True and no trigger, setting stop_event
    races against the trigger wait — the loop wakes and exits without
    running any tick."""
    ticks = {"n": 0}

    async def fake_main():
        ticks["n"] += 1

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)

    stop = asyncio.Event()

    async def driver():
        loop_task = asyncio.create_task(
            liquidityhelper.run_tick_loop(stop_event=stop)
        )
        # Park briefly so the loop reaches the gate.
        await asyncio.sleep(0.05)
        # Now signal shutdown without firing the debug trigger.
        stop.set()
        await loop_task

    event_loop.run_until_complete(asyncio.wait_for(driver(), timeout=2))
    assert ticks["n"] == 0, (
        "no tick should fire — the wait was interrupted by stop_event "
        "before any trigger came"
    )


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def debug_client(monkeypatch):
    """FastAPI test client with the debug router mounted, no auth.

    root_path="/api" mirrors production (bitcart's app sets it), since
    the router prefix deliberately omits /api/ to avoid double-mount."""
    app = FastAPI(root_path="/api")
    app.include_router(log_endpoints.build_debug_router(auth_dependency=None))
    return TestClient(app)


def test_debug_endpoint_with_debug_mode_off_returns_explanatory(debug_client, monkeypatch):
    """DEBUG_MODE=False → 200 with triggered=False and a message.
    NOT a 4xx — the UI might be stale on DEBUG_MODE state, and a 4xx
    would render as a red error toast which isn't accurate."""
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", False)
    resp = debug_client.post("/api/plugins/liquidityhelper/debug/run_once")
    assert resp.status_code == 200
    body = resp.json()
    assert body["debug_mode"] is False
    assert body["triggered"] is False
    assert "DEBUG_MODE is False" in body["message"]


def test_debug_endpoint_with_debug_mode_on_fires_trigger(debug_client, monkeypatch):
    """DEBUG_MODE=True → 200 with triggered=True AND the underlying
    trigger event is set (the loop would wake on its next wait_for)."""
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)
    # Pre-condition: trigger event not set.
    liquidityhelper._debug_run_once_trigger.clear()
    assert not liquidityhelper._debug_run_once_trigger.is_set()

    resp = debug_client.post("/api/plugins/liquidityhelper/debug/run_once")
    assert resp.status_code == 200
    body = resp.json()
    assert body["debug_mode"] is True
    assert body["triggered"] is True

    # The endpoint must have fired the underlying trigger.
    assert liquidityhelper._debug_run_once_trigger.is_set()


def test_debug_endpoint_idempotent_when_already_triggered(debug_client, monkeypatch):
    """Two clicks while the loop is mid-tick coalesce via asyncio.Event
    semantics (set on a set event is a no-op). The endpoint still
    returns triggered=True on each call — operator gets feedback."""
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)
    liquidityhelper._debug_run_once_trigger.clear()

    r1 = debug_client.post("/api/plugins/liquidityhelper/debug/run_once")
    r2 = debug_client.post("/api/plugins/liquidityhelper/debug/run_once")
    assert r1.json()["triggered"] is True
    assert r2.json()["triggered"] is True
    assert liquidityhelper._debug_run_once_trigger.is_set()


# ---------------------------------------------------------------------------
# First-run / install safety: stale trigger MUST NOT cause an unwanted
# tick if DEBUG_MODE is True at startup.
# ---------------------------------------------------------------------------

def test_run_tick_loop_clears_stale_trigger_at_start(monkeypatch, event_loop):
    """Headline pin for the operator's "first install must not run"
    guarantee. Simulates the failure case: DEBUG_MODE=True AND the
    trigger event is already set when run_tick_loop is entered. This
    could happen if some startup-time code path (e.g. Bitcart firing
    settings_changed:liquidityhelper during initial registration)
    invoked trigger_debug_run_once before the loop had a chance to
    park.

    Without the defensive clear at the top of run_tick_loop, the
    first iteration would see the set trigger, fall through, and run
    main() — exactly the unwanted auto-run on install. With the
    clear, the loop must park immediately and main() must NOT fire."""
    ticks = {"n": 0}

    async def fake_main():
        ticks["n"] += 1

    monkeypatch.setattr(liquidityhelper, "main", fake_main)
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)

    # Simulate the pre-set stale trigger.
    liquidityhelper._debug_run_once_trigger.set()
    assert liquidityhelper._debug_run_once_trigger.is_set()

    # The loop should park forever — wait_for must time out.
    with pytest.raises(asyncio.TimeoutError):
        event_loop.run_until_complete(asyncio.wait_for(
            liquidityhelper.run_tick_loop(), timeout=0.5,
        ))

    assert ticks["n"] == 0, (
        "stale trigger at run_tick_loop entry must NOT cause main() "
        "to fire — the defensive clear at the top of the loop must "
        "have wiped the stale state."
    )


def test_settings_change_does_not_fire_trigger_unless_debug_mode_off(
    monkeypatch, event_loop,
):
    """The plugin's _on_settings_changed hook must only fire the
    trigger on a True → False transition of DEBUG_MODE — not on every
    settings save, and especially not at startup (when Bitcart may
    fire the hook just to re-apply saved values).

    We test the contract directly by simulating what
    _on_settings_changed does at the relevant point in its body:
    read DEBUG_MODE before, apply, read after, fire ONLY if True→False.
    """
    # Case A: DEBUG_MODE True before, True after (no transition).
    # Operator saved an unrelated setting while in debug mode.
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)
    liquidityhelper._debug_run_once_trigger.clear()
    # Mimicked hook logic:
    debug_was_on = bool(getattr(liquidityhelper, "DEBUG_MODE", False))
    # ... apply_settings would happen here, leaving DEBUG_MODE True ...
    debug_is_on = bool(getattr(liquidityhelper, "DEBUG_MODE", False))
    if debug_was_on and not debug_is_on:
        liquidityhelper.trigger_debug_run_once()
    assert not liquidityhelper._debug_run_once_trigger.is_set(), (
        "trigger MUST NOT fire when DEBUG_MODE was True and stays True"
    )

    # Case B: DEBUG_MODE False before, False after (loop already
    # running freely). Saves shouldn't fire the trigger.
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", False)
    liquidityhelper._debug_run_once_trigger.clear()
    debug_was_on = bool(getattr(liquidityhelper, "DEBUG_MODE", False))
    debug_is_on = bool(getattr(liquidityhelper, "DEBUG_MODE", False))
    if debug_was_on and not debug_is_on:
        liquidityhelper.trigger_debug_run_once()
    assert not liquidityhelper._debug_run_once_trigger.is_set(), (
        "trigger MUST NOT fire when DEBUG_MODE was False and stays False"
    )

    # Case C: DEBUG_MODE False before, True after (operator just
    # entered debug mode). Trigger must NOT fire — operator wants
    # to step manually now, not have a tick run.
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", False)
    liquidityhelper._debug_run_once_trigger.clear()
    debug_was_on = bool(getattr(liquidityhelper, "DEBUG_MODE", False))
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)   # apply
    debug_is_on = bool(getattr(liquidityhelper, "DEBUG_MODE", False))
    if debug_was_on and not debug_is_on:
        liquidityhelper.trigger_debug_run_once()
    assert not liquidityhelper._debug_run_once_trigger.is_set(), (
        "trigger MUST NOT fire when DEBUG_MODE was False and is now "
        "True — operator just entered debug mode, they don't want a "
        "tick to fire automatically"
    )

    # Case D: DEBUG_MODE True before, False after (operator is
    # leaving debug mode). THIS is the one transition that fires
    # the trigger — to unblock the parked loop so it can resume
    # continuous operation without requiring a manual click.
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)
    liquidityhelper._debug_run_once_trigger.clear()
    debug_was_on = bool(getattr(liquidityhelper, "DEBUG_MODE", False))
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", False)   # apply
    debug_is_on = bool(getattr(liquidityhelper, "DEBUG_MODE", False))
    if debug_was_on and not debug_is_on:
        liquidityhelper.trigger_debug_run_once()
    assert liquidityhelper._debug_run_once_trigger.is_set(), (
        "trigger MUST fire on the True → False transition so the "
        "parked loop wakes and resumes continuous operation"
    )


def test_first_install_with_debug_mode_true_does_not_run_loop(
    monkeypatch, event_loop,
):
    """End-to-end pin for the operator's request: simulate a fresh
    plugin install where saved settings have DEBUG_MODE=True. The
    sequence is:
      1. Settings are applied (DEBUG_MODE → True on the module).
      2. Bitcart may fire the settings_changed hook as part of
         initial registration (we simulate this — it's a no-op for
         the trigger because there was no True → False transition).
      3. The plugin spawns run_tick_loop.
      4. The loop must park immediately; main() must NEVER fire.

    The test only fails if main() runs at any point in the simulated
    install sequence."""
    ticks = {"n": 0}

    async def fake_main():
        ticks["n"] += 1

    monkeypatch.setattr(liquidityhelper, "main", fake_main)

    # Step 1: apply_settings happens (we just setattr to mimic that
    # the bridge sets DEBUG_MODE=True). The trigger is initially
    # clear because the engine module just imported.
    liquidityhelper._debug_run_once_trigger.clear()
    monkeypatch.setattr(liquidityhelper, "DEBUG_MODE", True)

    # Step 2: simulate Bitcart firing settings_changed at startup.
    # The hook reads DEBUG_MODE before (True), applies (still True),
    # reads after (True). True → True is NOT a True → False
    # transition, so the trigger does NOT fire.
    debug_was_on = bool(getattr(liquidityhelper, "DEBUG_MODE", False))
    # apply_settings would re-apply the same DEBUG_MODE=True here
    debug_is_on = bool(getattr(liquidityhelper, "DEBUG_MODE", False))
    if debug_was_on and not debug_is_on:
        liquidityhelper.trigger_debug_run_once()
    assert not liquidityhelper._debug_run_once_trigger.is_set()

    # Step 3+4: spawn the loop. It must park; main() must NOT fire.
    with pytest.raises(asyncio.TimeoutError):
        event_loop.run_until_complete(asyncio.wait_for(
            liquidityhelper.run_tick_loop(), timeout=0.5,
        ))
    assert ticks["n"] == 0, (
        "main() must NEVER fire during a first-install scenario with "
        "DEBUG_MODE=True — the loop should park on the gate"
    )
