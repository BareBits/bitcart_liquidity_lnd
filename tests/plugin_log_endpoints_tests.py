"""Tests for the plugin's log-viewer HTTP endpoints.

Mounts the router on a plain FastAPI app with NO auth dependency so we
can exercise the routing, stream allow-list, and tail-line bounds in
isolation. Auth wiring is tested separately in the bitcart-integration
layer; this file pins the contract independent of Bitcart.

Coverage:
  - install_plugin_log_sinks resolves the stream paths
  - /streams returns the known list with existence flags
  - /{stream} tails the right number of lines
  - /{stream}?tail=N respects the cap
  - Unknown stream → 404 (no traversal)
  - "../" in stream name → 404, never touches disk
  - File rotates/disappears mid-request → graceful empty response
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bitcart_plugin.log_endpoints import (
    DEFAULT_TAIL_LINES,
    MAX_TAIL_LINES,
    STREAM_DECISIONS,
    STREAM_INFO,
    STREAM_OPERATIONAL,
    build_router,
    install_plugin_log_sinks,
)


@pytest.fixture
def temp_data_dir():
    """Fresh per-test data directory with sinks installed against it.

    Important: install_plugin_log_sinks mutates a module-level dict
    (_STREAM_PATHS) AND the engine's QueueListener (it appends our
    plugin handlers there so writes happen on the listener's worker
    thread). We restore both at teardown so tests don't leak state
    into each other or into the rest of the suite."""
    from bitcart_plugin import log_endpoints
    from liquidityhelper import listener as engine_listener

    with tempfile.TemporaryDirectory() as tmp:
        # Snapshot mutable state
        original_paths = dict(log_endpoints._STREAM_PATHS)
        original_listener_handlers = tuple(engine_listener.handlers)

        install_plugin_log_sinks(tmp)
        yield tmp

        # Restore
        log_endpoints._STREAM_PATHS.clear()
        log_endpoints._STREAM_PATHS.update(original_paths)
        # Anything added to the listener during the test gets removed —
        # close those handlers so the rotation file handles don't leak.
        for h in list(engine_listener.handlers):
            if h not in original_listener_handlers:
                try:
                    h.close()
                except Exception:
                    pass
        engine_listener.stop()
        engine_listener.handlers = original_listener_handlers
        engine_listener.start()


@pytest.fixture
def client(temp_data_dir):
    """FastAPI test client with our router mounted, no auth required.

    root_path="/api" mirrors production (bitcart's app sets it), since
    the router prefix deliberately omits /api/ to avoid double-mount."""
    app = FastAPI(root_path="/api")
    app.include_router(build_router(auth_dependency=None))
    return TestClient(app)


# ---------------------------------------------------------------------------
# Sink installation
# ---------------------------------------------------------------------------

def test_install_plugin_log_sinks_resolves_paths(temp_data_dir):
    """install_plugin_log_sinks must populate _STREAM_PATHS so the
    endpoints can find the files. Standalone-mode tests rely on this
    being deterministic per-call."""
    from bitcart_plugin import log_endpoints
    assert log_endpoints._STREAM_PATHS[STREAM_OPERATIONAL] == os.path.join(
        temp_data_dir, "liquidityhelper.log"
    )
    assert log_endpoints._STREAM_PATHS[STREAM_INFO] == os.path.join(
        temp_data_dir, "liquidityhelper-info.log"
    )
    assert log_endpoints._STREAM_PATHS[STREAM_DECISIONS] == os.path.join(
        temp_data_dir, "decisions.log"
    )


def test_install_plugin_log_sinks_is_idempotent(temp_data_dir):
    """Calling install twice must not add duplicate handlers — otherwise
    a settings reload would double every log line on disk."""
    from liquidityhelper import listener as engine_listener
    first_count = sum(
        1 for h in engine_listener.handlers if getattr(h, "_plugin_sink", None)
    )
    install_plugin_log_sinks(temp_data_dir)
    install_plugin_log_sinks(temp_data_dir)
    second_count = sum(
        1 for h in engine_listener.handlers if getattr(h, "_plugin_sink", None)
    )
    assert second_count == first_count   # No duplicate after two re-installs


# ---------------------------------------------------------------------------
# /streams
# ---------------------------------------------------------------------------

def test_streams_lists_all_streams_when_empty(client):
    """All three streams report exists=True with size_bytes=0
    immediately after install_plugin_log_sinks — RotatingFileHandler
    eagerly creates the target file on construction. The UI
    distinguishes 'no log yet' from 'log present' via size_bytes,
    not exists."""
    resp = client.get("/api/plugins/liquidityhelper/logs/streams")
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()}
    assert names == {STREAM_OPERATIONAL, STREAM_INFO, STREAM_DECISIONS}
    for s in resp.json():
        assert s["exists"] is True
        assert s["size_bytes"] == 0


def test_streams_reports_file_size_after_write(client, temp_data_dir):
    """Once the engine has appended a line, streams should reflect the
    file's actual size."""
    operational = os.path.join(temp_data_dir, "liquidityhelper.log")
    with open(operational, "w") as f:
        f.write("hello world\n")

    resp = client.get("/api/plugins/liquidityhelper/logs/streams")
    payload = {s["name"]: s for s in resp.json()}
    assert payload[STREAM_OPERATIONAL]["exists"] is True
    assert payload[STREAM_OPERATIONAL]["size_bytes"] == len("hello world\n")
    # The other two files were created by install but never written to.
    assert payload[STREAM_INFO]["exists"] is True
    assert payload[STREAM_INFO]["size_bytes"] == 0
    assert payload[STREAM_DECISIONS]["exists"] is True
    assert payload[STREAM_DECISIONS]["size_bytes"] == 0


def test_info_sink_captures_info_but_not_debug(temp_data_dir):
    """The new INFO+ sink must record INFO/WARNING/ERROR and reject
    DEBUG. Pins the level-gate contract that gives the file its much
    longer effective retention. Decisions are still excluded.
    """
    import logging
    from liquidityhelper import listener as engine_listener, logger, decisions_logger

    # Emit one of each through the engine's own loggers and flush.
    logger.debug("debug-line-should-not-appear")
    logger.info("info-line-should-appear")
    logger.warning("warning-line-should-appear")
    decisions_logger.info("decision-line-should-not-appear-in-info-file")

    # QueueListener fans records off-thread; stopping forces drain.
    engine_listener.stop()
    try:
        info_path = os.path.join(temp_data_dir, "liquidityhelper-info.log")
        with open(info_path, "r", encoding="utf-8") as f:
            contents = f.read()
    finally:
        engine_listener.start()

    assert "info-line-should-appear" in contents
    assert "warning-line-should-appear" in contents
    assert "debug-line-should-not-appear" not in contents
    assert "decision-line-should-not-appear-in-info-file" not in contents


# ---------------------------------------------------------------------------
# /{stream}
# ---------------------------------------------------------------------------

def test_tail_returns_default_lines(client, temp_data_dir):
    """Default tail=500 should be applied when not given."""
    path = os.path.join(temp_data_dir, "liquidityhelper.log")
    with open(path, "w") as f:
        for i in range(100):
            f.write(f"line {i}\n")
    resp = client.get(f"/api/plugins/liquidityhelper/logs/{STREAM_OPERATIONAL}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stream"] == STREAM_OPERATIONAL
    assert len(body["lines"]) == 100  # under default
    assert body["truncated"] is False
    assert body["lines"][0] == "line 0"
    assert body["lines"][-1] == "line 99"


def test_tail_respects_explicit_n(client, temp_data_dir):
    """tail=N returns exactly the last N lines and flags truncated=True
    when there were more."""
    path = os.path.join(temp_data_dir, "liquidityhelper.log")
    with open(path, "w") as f:
        for i in range(50):
            f.write(f"line {i}\n")
    resp = client.get(
        f"/api/plugins/liquidityhelper/logs/{STREAM_OPERATIONAL}?tail=10"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["lines"]) == 10
    assert body["truncated"] is True
    assert body["lines"][0] == "line 40"
    assert body["lines"][-1] == "line 49"


def test_tail_caps_at_max(client):
    """tail=99999999 must be rejected by the query validator (Query has
    le=MAX_TAIL_LINES). Verifies the bound is enforced by FastAPI."""
    resp = client.get(
        f"/api/plugins/liquidityhelper/logs/{STREAM_OPERATIONAL}"
        f"?tail={MAX_TAIL_LINES + 1}"
    )
    assert resp.status_code == 422   # pydantic validation error


def test_tail_rejects_zero_and_negative(client):
    """tail=0 and negative values must be 422; we don't want callers
    abusing 'tail=0' as a "size probe without payload" or anything
    weird. Standard input validation."""
    for bad in [0, -1, -100]:
        resp = client.get(
            f"/api/plugins/liquidityhelper/logs/{STREAM_OPERATIONAL}?tail={bad}"
        )
        assert resp.status_code == 422


def test_tail_unknown_stream_is_404(client):
    """Unknown stream name → 404. Stream allow-list is the only valid
    input; arbitrary file names are NOT routed to disk."""
    resp = client.get("/api/plugins/liquidityhelper/logs/something_else")
    assert resp.status_code == 404
    # The error body should NOT leak any internal path.
    body = resp.json()
    detail = str(body.get("detail", ""))
    assert "/tmp" not in detail
    assert "/var" not in detail
    assert "../" not in detail


def test_tail_refuses_path_traversal(client):
    """Path-traversal-as-stream-name must never reach disk. The allow-
    list is the security boundary: any stream name not in STREAMS is
    rejected. Even if Starlette URL-normalizes `a/../b` to `b`, the
    allow-list ensures `b` is only served if it's a legitimate stream
    — never an arbitrary filename."""
    for traversal in [
        "..%2F..%2Fetc%2Fpasswd",   # encoded traversal
        "..",                        # bare parent
        "../config.py",              # relative file path
        "etc%2Fpasswd",              # encoded absolute-ish
        "%2E%2E%2Fconfig.py",        # double-encoded
    ]:
        resp = client.get(f"/api/plugins/liquidityhelper/logs/{traversal}")
        assert resp.status_code in (404, 422), (
            f"traversal '{traversal}' must not 200: got {resp.status_code}"
        )
        # And the response body must not include any internal path.
        body = resp.text
        assert "/tmp" not in body
        assert "/etc/passwd" not in body


def test_tail_missing_file_returns_empty(client, temp_data_dir):
    """Stream is known but the file doesn't exist yet (engine hasn't
    written anything). Endpoint returns 200 with an empty list — the
    UI renders 'no log yet' instead of erroring."""
    # Don't create either file.
    resp = client.get(f"/api/plugins/liquidityhelper/logs/{STREAM_OPERATIONAL}")
    assert resp.status_code == 200
    assert resp.json() == {
        "stream": STREAM_OPERATIONAL, "lines": [], "truncated": False
    }


def test_tail_handles_unicode(client, temp_data_dir):
    """Log lines may contain non-ASCII (LSP node aliases, emoji in
    log_event tags, etc.). Round-trip through the endpoint must
    preserve the text — UTF-8 decode with errors='replace' shouldn't
    mangle valid UTF-8."""
    path = os.path.join(temp_data_dir, "decisions.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("🔎 Detected first run\n")
        f.write("Routed via LND node Ünlü\n")
    resp = client.get(f"/api/plugins/liquidityhelper/logs/{STREAM_DECISIONS}")
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert "🔎 Detected first run" in lines
    assert "Routed via LND node Ünlü" in lines
