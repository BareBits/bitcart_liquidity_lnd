"""Tests for the logging refactor.

Covers:
  - Handler level configuration (file=DEBUG+, console=INFO+).
  - The dedicated `decisions` logger writes to decisions.log, does not
    double-log to the main file, but does still surface on stdout.
  - `log_event()` always logs.
  - `log_decision()` dedups on (key, value), logs the first call, logs
    on transitions, ignores no-op repeats.
  - `classes.py` logs now inherit the main logger's handlers (no longer
    an orphan tree).
  - Heartbeat fires every N ticks and not in between.
"""

from __future__ import annotations

import logging
from typing import List

import pytest

import liquidityhelper


# ---------------------------------------------------------------------------
# Helpers — capture log records per logger without touching disk
# ---------------------------------------------------------------------------

class _Capture(logging.Handler):
    """Tiny in-memory handler that collects emitted records for asserts."""
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def capture_loggers():
    """Attach a fresh _Capture to both the main and decisions loggers.
    Yields a tuple (main_capture, decisions_capture). Detaches after."""
    main_cap = _Capture()
    dec_cap = _Capture()
    liquidityhelper.logger.addHandler(main_cap)
    liquidityhelper.decisions_logger.addHandler(dec_cap)
    # Reset the dedup state so each test starts clean.
    liquidityhelper._last_decision_state.clear()
    try:
        yield main_cap, dec_cap
    finally:
        liquidityhelper.logger.removeHandler(main_cap)
        liquidityhelper.decisions_logger.removeHandler(dec_cap)


# ---------------------------------------------------------------------------
# Handler levels
# ---------------------------------------------------------------------------

def test_console_handler_excludes_debug():
    """Console handler is set to INFO, so DEBUG never reaches stdout."""
    assert liquidityhelper.console_handler.level == logging.INFO


def test_file_handler_includes_debug():
    """File handler is set to DEBUG so post-mortems have full detail."""
    assert liquidityhelper.file_handler.level == logging.DEBUG


def test_main_logger_passes_everything_to_handlers():
    """Logger-level filter is DEBUG so per-handler levels do the real
    filtering; if this is higher we'd silently drop debug everywhere."""
    assert liquidityhelper.logger.level == logging.DEBUG


# ---------------------------------------------------------------------------
# Decisions logger routing
# ---------------------------------------------------------------------------

def test_log_event_writes_to_decisions_only(capture_loggers):
    """log_event() lines must NOT also land in liquidityhelper.log; the
    decisions logger has propagate=False for exactly this reason."""
    main_cap, dec_cap = capture_loggers
    liquidityhelper.log_event("opened channel to %s", "node-A")

    assert any(r.getMessage() == "opened channel to node-A" for r in dec_cap.records)
    assert not any(r.getMessage() == "opened channel to node-A" for r in main_cap.records)


def test_main_logger_does_not_leak_into_decisions(capture_loggers):
    """The reverse direction: a logger.info() on the main logger should
    NOT appear in decisions.log."""
    main_cap, dec_cap = capture_loggers
    liquidityhelper.logger.info("routine operational ping")

    assert any(r.getMessage() == "routine operational ping" for r in main_cap.records)
    assert not any(r.getMessage() == "routine operational ping" for r in dec_cap.records)


def test_decisions_logger_propagate_is_false():
    """Sanity check on the structural separation that the routing tests
    rely on. If someone flips this to True later, decision lines start
    appearing in liquidityhelper.log too — that's the regression."""
    assert liquidityhelper.decisions_logger.propagate is False


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------

def test_log_event_always_logs_repeats(capture_loggers):
    """log_event is for discrete events that should appear every time
    they happen — no dedup."""
    _, dec_cap = capture_loggers
    for _ in range(5):
        liquidityhelper.log_event("channel opened to peer X")
    assert sum(1 for r in dec_cap.records if "channel opened" in r.getMessage()) == 5


def test_log_event_supports_lazy_args(capture_loggers):
    """% args render lazily so the formatter is only invoked on emit —
    standard Python logging hygiene."""
    _, dec_cap = capture_loggers
    liquidityhelper.log_event("amount: %d sats to %s", 12345, "addr-A")
    assert dec_cap.records[-1].getMessage() == "amount: 12345 sats to addr-A"


# ---------------------------------------------------------------------------
# log_decision dedup behavior
# ---------------------------------------------------------------------------

def test_log_decision_logs_first_call(capture_loggers):
    """First call for a given key always logs (no prior value)."""
    _, dec_cap = capture_loggers
    liquidityhelper.log_decision("rail", "ln", "rail=%s", "ln")
    assert any(r.getMessage() == "rail=ln" for r in dec_cap.records)


def test_log_decision_dedups_same_value(capture_loggers):
    """Repeated same (key, value) -> no additional log lines."""
    _, dec_cap = capture_loggers
    for _ in range(10):
        liquidityhelper.log_decision("rail", "ln", "rail=%s", "ln")
    assert sum(1 for r in dec_cap.records if "rail=ln" in r.getMessage()) == 1


def test_log_decision_logs_transitions(capture_loggers):
    """Value change for the same key always logs."""
    _, dec_cap = capture_loggers
    liquidityhelper.log_decision("rail", "ln", "rail=%s", "ln")
    liquidityhelper.log_decision("rail", "ln", "rail=%s", "ln")
    liquidityhelper.log_decision("rail", "onchain", "rail=%s", "onchain")
    liquidityhelper.log_decision("rail", "ln", "rail=%s", "ln")

    messages = [r.getMessage() for r in dec_cap.records]
    rail_messages = [m for m in messages if m.startswith("rail=")]
    assert rail_messages == ["rail=ln", "rail=onchain", "rail=ln"]


def test_log_decision_keys_are_independent(capture_loggers):
    """Different keys keep their own dedup state."""
    _, dec_cap = capture_loggers
    liquidityhelper.log_decision("k1", "v", "k1=%s", "v")
    liquidityhelper.log_decision("k2", "v", "k2=%s", "v")
    # Even though both have the same value, they're different keys.
    assert any(r.getMessage() == "k1=v" for r in dec_cap.records)
    assert any(r.getMessage() == "k2=v" for r in dec_cap.records)


def test_log_decision_tuple_keys_supported(capture_loggers):
    """Per-wallet state uses tuple keys (e.g. ('blocked', wallet_id))."""
    _, dec_cap = capture_loggers
    liquidityhelper.log_decision(("blocked", "w1"), True, "w1 blocked")
    liquidityhelper.log_decision(("blocked", "w2"), True, "w2 blocked")
    # Both must log — different tuples even though the value is the same.
    messages = [r.getMessage() for r in dec_cap.records]
    assert "w1 blocked" in messages
    assert "w2 blocked" in messages


# ---------------------------------------------------------------------------
# classes.py orphan-logger fix
# ---------------------------------------------------------------------------

def test_classes_logger_inherits_main_handlers():
    """`classes.logger` is now a child of `liquidityhelper.logger`, so
    its records reach the main file/console handlers via propagation."""
    import classes
    assert classes.logger.name == "liquidityhelper.classes"
    # Walking the parent chain should reach the configured logger.
    assert classes.logger.parent is liquidityhelper.logger


def test_classes_logger_emits_through_main_handler(capture_loggers):
    """End-to-end: a log call from inside classes.py should land in
    the capture attached to the main logger."""
    main_cap, _ = capture_loggers
    import classes
    classes.logger.warning("test from classes module")
    assert any(r.getMessage() == "test from classes module" for r in main_cap.records)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_does_not_fire_every_tick(capture_loggers):
    """maybe_emit_heartbeat increments a counter and only emits every
    Nth tick. 99 calls should produce zero heartbeat lines."""
    _, dec_cap = capture_loggers
    liquidityhelper._tick_counter = 0
    for _ in range(99):
        liquidityhelper.maybe_emit_heartbeat()
    assert not any("heartbeat" in r.getMessage() for r in dec_cap.records)


def test_heartbeat_fires_every_n_ticks(capture_loggers):
    """The 100th call (matching _HEARTBEAT_EVERY_N_TICKS) emits."""
    _, dec_cap = capture_loggers
    liquidityhelper._tick_counter = 0
    for _ in range(liquidityhelper._HEARTBEAT_EVERY_N_TICKS):
        liquidityhelper.maybe_emit_heartbeat()
    heartbeats = [r for r in dec_cap.records if "heartbeat" in r.getMessage()]
    assert len(heartbeats) == 1
