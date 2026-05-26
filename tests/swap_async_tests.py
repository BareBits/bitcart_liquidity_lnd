"""Tests for the async conversion of swap_providers.LoopdInstance.

Pins:
  - stop() is a coroutine
  - stop() with no proc is a no-op
  - stop() on already-exited proc is a no-op
  - stop() awaits the subprocess wait (doesn't block)
  - ensure_loop_binaries is a coroutine
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

import swap_providers


def test_ensure_loop_binaries_is_async():
    """The download path must be a coroutine — if a future change
    accidentally reverts it to def, plugin-mode workers would freeze
    for the full multi-MB download on first-run install."""
    assert inspect.iscoroutinefunction(swap_providers.ensure_loop_binaries)


def test_loopd_instance_start_is_async():
    assert inspect.iscoroutinefunction(swap_providers.LoopdInstance.start)


def test_loopd_instance_stop_is_async():
    """stop() must be a coroutine. The pre-refactor sync stop() called
    proc.wait(timeout=15) which would freeze the event loop for the
    full 15s when loopd ignored SIGTERM."""
    assert inspect.iscoroutinefunction(swap_providers.LoopdInstance.stop)


# ---------------------------------------------------------------------------
# stop() behavior
# ---------------------------------------------------------------------------

def _make_instance(tmp_path: Path, wallet_id: str = "test") -> swap_providers.LoopdInstance:
    """Build a LoopdInstance without spawning anything — just enough
    fields populated to call .stop()."""
    return swap_providers.LoopdInstance(
        wallet_id=wallet_id,
        bin_dir=tmp_path / "bin",
        data_dir=tmp_path / "data",
        lnd_grpc_host="127.0.0.1:10009",
        lnd_tls_cert_bytes=b"",
        lnd_macaroon_bytes=b"",
        network="regtest",
    )


def test_stop_with_no_proc_is_noop(tmp_path, event_loop):
    """LoopdInstance constructed but never started → proc is None →
    stop() must just return without raising."""
    inst = _make_instance(tmp_path)
    inst.proc = None
    event_loop.run_until_complete(inst.stop())


def test_stop_skips_when_already_exited(tmp_path, event_loop):
    """If the subprocess has already exited (returncode set), stop()
    must not try to terminate it again. Pin: that would otherwise raise
    ProcessLookupError on Linux."""
    inst = _make_instance(tmp_path)

    class _FakeProc:
        returncode = 0   # Already exited
        terminated = False
        killed = False
        def terminate(self):
            self.terminated = True
        def kill(self):
            self.killed = True
        async def wait(self):
            return 0

    fake = _FakeProc()
    inst.proc = fake  # type: ignore[assignment]

    event_loop.run_until_complete(inst.stop())
    assert fake.terminated is False
    assert fake.killed is False


def test_stop_awaits_wait_within_timeout(tmp_path, event_loop):
    """Happy path: stop() calls terminate, then awaits wait(), which
    completes within timeout. No kill needed."""
    inst = _make_instance(tmp_path)

    class _FakeProc:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.killed = False
        def terminate(self):
            self.terminated = True
            self.returncode = -15
        def kill(self):
            self.killed = True
        async def wait(self):
            return self.returncode if self.returncode is not None else 0

    fake = _FakeProc()
    inst.proc = fake  # type: ignore[assignment]

    event_loop.run_until_complete(inst.stop())
    assert fake.terminated is True
    assert fake.killed is False


def test_stop_force_kills_on_timeout(tmp_path, event_loop, monkeypatch):
    """If wait() doesn't return within 15s, stop() must SIGKILL.
    Patch wait_for to raise TimeoutError so the test doesn't actually
    wait 15s."""
    inst = _make_instance(tmp_path)

    class _FakeProc:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.killed = False
            self._wait_calls = 0
        def terminate(self):
            self.terminated = True
        def kill(self):
            self.killed = True
            self.returncode = -9
        async def wait(self):
            self._wait_calls += 1
            return -9

    fake = _FakeProc()
    inst.proc = fake  # type: ignore[assignment]

    # Force the wait_for inside stop() to time out immediately.
    real_wait_for = asyncio.wait_for
    async def fast_timeout(coro, timeout):
        # Schedule the coro so we don't leak it.
        task = asyncio.ensure_future(coro)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        raise asyncio.TimeoutError()
    monkeypatch.setattr(asyncio, "wait_for", fast_timeout)

    event_loop.run_until_complete(inst.stop())
    assert fake.terminated is True
    assert fake.killed is True


# ---------------------------------------------------------------------------
# LoopdManager.stop_all awaits each stop()
# ---------------------------------------------------------------------------

def test_stop_all_awaits_each_instance(tmp_path, event_loop):
    """LoopdManager.stop_all() iterates instances and awaits each one's
    stop(). Pin against a regression where stop() goes back to sync but
    stop_all still works incidentally."""
    # Distinct wallet_ids so LoopdManager._instances stores both (it's
    # keyed by wallet_id; same-key registers would overwrite, and the
    # original test's set-equality assertion silently masked that).
    inst1 = _make_instance(tmp_path / "i1", wallet_id="test1")
    inst2 = _make_instance(tmp_path / "i2", wallet_id="test2")
    stopped = []

    class _FakeProc:
        returncode = 0
        def terminate(self): pass
        def kill(self): pass
        async def wait(self): return 0

    for i in (inst1, inst2):
        i.proc = _FakeProc()  # type: ignore[assignment]
        async def _record(self=i):
            stopped.append(self.wallet_id)
        # Wrap the real stop() so we capture the call order.
        original_stop = i.stop
        async def chained(self=i, orig=original_stop):
            stopped.append(self.wallet_id)
            await orig()
        i.stop = chained  # type: ignore[assignment]

    manager = swap_providers.LoopdManager(bin_dir=tmp_path, data_root=tmp_path)
    manager.register_existing(inst1)
    manager.register_existing(inst2)

    event_loop.run_until_complete(manager.stop_all())
    assert stopped == ["test1", "test2"]   # both stop()s were awaited
    assert manager._instances == {}
