"""Tests for the QueueListener-based logging architecture (C2 refactor).

After the C2 refactor, all three log sinks (operational file, decisions
file, console) live on the QueueListener — the loggers themselves only
have a QueueHandler. This means:
  - logger.emit() never does disk I/O on the calling thread
  - decisions records still go ONLY to decisions.log (via filter)
  - non-decision records still go ONLY to liquidityhelper.log (via filter)
  - both still appear on console
  - add_async_log_handler appends to the listener, not the logger

These tests pin those properties so a future "let me just .addHandler()"
patch doesn't quietly bring back blocking writes.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

import liquidityhelper


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------

def test_loggers_only_have_queue_handler():
    """The two engine loggers must have exactly ONE handler each, and
    it must be the QueueHandler. Any other handler attached directly to
    a logger would do its emit() on the event-loop thread, defeating
    the whole point of the QueueListener refactor."""
    main = logging.getLogger("liquidityhelper")
    dec = logging.getLogger("liquidityhelper.decisions")
    assert [type(h).__name__ for h in main.handlers] == ["QueueHandler"]
    assert [type(h).__name__ for h in dec.handlers] == ["QueueHandler"]


def test_listener_has_all_sinks_at_import():
    """At module import time, the listener must own file_handler,
    _info_file_handler, _decisions_file_handler, and console_handler.
    Tests that touch listener.handlers (like plugin_log_endpoints_tests)
    rely on this baseline shape."""
    handlers = liquidityhelper.listener.handlers
    classnames = [type(h).__name__ for h in handlers]
    # Order matters for clarity but isn't a hard contract; check membership.
    assert classnames.count("RotatingFileHandler") == 3
    assert classnames.count("StreamHandler") == 1


# ---------------------------------------------------------------------------
# Filter behavior
# ---------------------------------------------------------------------------

def test_not_decisions_filter_rejects_decisions():
    """The operational file handler must reject any record emitted on
    the decisions logger. Otherwise decisions appear in BOTH files —
    the regression the propagate=False guard used to prevent."""
    f = liquidityhelper._NotDecisionsFilter()
    rec_decision = logging.LogRecord(
        name="liquidityhelper.decisions", level=logging.INFO,
        pathname="", lineno=0, msg="x", args=(), exc_info=None,
    )
    rec_main = logging.LogRecord(
        name="liquidityhelper", level=logging.INFO,
        pathname="", lineno=0, msg="x", args=(), exc_info=None,
    )
    assert f.filter(rec_decision) is False
    assert f.filter(rec_main) is True


def test_decisions_only_filter_accepts_only_decisions():
    f = liquidityhelper._DecisionsOnlyFilter()
    rec_decision = logging.LogRecord(
        name="liquidityhelper.decisions", level=logging.INFO,
        pathname="", lineno=0, msg="x", args=(), exc_info=None,
    )
    rec_decision_child = logging.LogRecord(
        name="liquidityhelper.decisions.fee", level=logging.INFO,
        pathname="", lineno=0, msg="x", args=(), exc_info=None,
    )
    rec_main = logging.LogRecord(
        name="liquidityhelper", level=logging.INFO,
        pathname="", lineno=0, msg="x", args=(), exc_info=None,
    )
    assert f.filter(rec_decision) is True
    assert f.filter(rec_decision_child) is True
    assert f.filter(rec_main) is False


# ---------------------------------------------------------------------------
# add_async_log_handler
# ---------------------------------------------------------------------------

def test_add_async_log_handler_appends_to_listener():
    """Handler added via the helper must show up in listener.handlers,
    NOT in either logger's handlers."""
    h = logging.NullHandler()
    try:
        original_listener = list(liquidityhelper.listener.handlers)
        liquidityhelper.add_async_log_handler(h)
        assert h in liquidityhelper.listener.handlers
        # Neither logger should have gained a handler.
        assert h not in logging.getLogger("liquidityhelper").handlers
        assert h not in logging.getLogger("liquidityhelper.decisions").handlers
    finally:
        # Clean up — restore original handler list.
        liquidityhelper.listener.stop()
        liquidityhelper.listener.handlers = tuple(original_listener)
        liquidityhelper.listener.start()


def test_add_async_log_handler_is_idempotent():
    """Adding the same handler twice is a no-op. Important because the
    plugin re-runs install_plugin_log_sinks on settings reload."""
    h = logging.NullHandler()
    try:
        original_listener = list(liquidityhelper.listener.handlers)
        liquidityhelper.add_async_log_handler(h)
        count_after_first = liquidityhelper.listener.handlers.count(h)
        liquidityhelper.add_async_log_handler(h)
        count_after_second = liquidityhelper.listener.handlers.count(h)
        assert count_after_first == 1
        assert count_after_second == 1
    finally:
        liquidityhelper.listener.stop()
        liquidityhelper.listener.handlers = tuple(original_listener)
        liquidityhelper.listener.start()


# ---------------------------------------------------------------------------
# End-to-end record routing
# ---------------------------------------------------------------------------

class _CapturingHandler(logging.Handler):
    """Threadsafe capture of records the listener dispatches to us.
    Used to assert decisions/non-decisions land on the right handlers
    after going through the queue."""
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self._lock = threading.Lock()

    def emit(self, record):
        with self._lock:
            self.records.append(record)


def _drain_queue(timeout=2.0):
    """Wait for the listener's worker to fully process the queue.
    Polls queue.unfinished_tasks via join() — listener.stop() does a
    join under the hood, so we use that proxy by polling qsize()."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if liquidityhelper.log_queue.empty():
            # Even after empty(), the worker may be mid-dispatch.
            time.sleep(0.05)
            if liquidityhelper.log_queue.empty():
                return
        time.sleep(0.02)


def test_decision_record_only_hits_decisions_handlers():
    """Emit on the decisions logger; assert ONLY the decisions-only
    capture handler sees the record, not the operational-only one."""
    decisions_capture = _CapturingHandler()
    decisions_capture.addFilter(liquidityhelper._DecisionsOnlyFilter())

    operational_capture = _CapturingHandler()
    operational_capture.addFilter(liquidityhelper._NotDecisionsFilter())

    original_listener = list(liquidityhelper.listener.handlers)
    try:
        liquidityhelper.add_async_log_handler(decisions_capture)
        liquidityhelper.add_async_log_handler(operational_capture)

        logging.getLogger("liquidityhelper.decisions").info(
            "decision-test-message-xyz"
        )
        _drain_queue()

        dec_msgs = [r.getMessage() for r in decisions_capture.records]
        op_msgs = [r.getMessage() for r in operational_capture.records]
        assert "decision-test-message-xyz" in dec_msgs
        assert "decision-test-message-xyz" not in op_msgs
    finally:
        liquidityhelper.listener.stop()
        liquidityhelper.listener.handlers = tuple(original_listener)
        liquidityhelper.listener.start()


def test_non_decision_record_only_hits_operational_handlers():
    """Symmetric inverse: a normal logger.info() must land on the
    operational handler but NOT the decisions-only handler."""
    decisions_capture = _CapturingHandler()
    decisions_capture.addFilter(liquidityhelper._DecisionsOnlyFilter())

    operational_capture = _CapturingHandler()
    operational_capture.addFilter(liquidityhelper._NotDecisionsFilter())

    original_listener = list(liquidityhelper.listener.handlers)
    try:
        liquidityhelper.add_async_log_handler(decisions_capture)
        liquidityhelper.add_async_log_handler(operational_capture)

        logging.getLogger("liquidityhelper").info(
            "normal-test-message-xyz"
        )
        _drain_queue()

        dec_msgs = [r.getMessage() for r in decisions_capture.records]
        op_msgs = [r.getMessage() for r in operational_capture.records]
        assert "normal-test-message-xyz" in op_msgs
        assert "normal-test-message-xyz" not in dec_msgs
    finally:
        liquidityhelper.listener.stop()
        liquidityhelper.listener.handlers = tuple(original_listener)
        liquidityhelper.listener.start()
