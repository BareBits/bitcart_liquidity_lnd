"""HTTP endpoints + helpers for the plugin's log-viewer tab.

Two endpoints, both mounted under `/api/plugins/liquidityhelper/logs/`:
  - GET `/streams`              — list available log streams.
  - GET `/{stream}?tail=N`      — return the last N lines of `stream`.

Why a *stream allow-list* and not "read any file you ask for":
  This endpoint runs inside an authenticated admin context, so the risk
  surface is limited, but file-path inputs trivially become a directory
  traversal foothold if you let the client name them. Keeping a fixed
  enum (`operational`, `decisions`) means the client can only see what we
  intentionally publish — no `../../etc/passwd`, no `liquidityhelper.log.1`
  rotated-archives leaking by accident.

Why we read from the plugin's *data dir*, not the engine's CWD:
  When the engine runs standalone, its `RotatingFileHandler` writes to
  the current working directory. When it runs as a plugin embedded in
  Bitcart, the CWD belongs to Bitcart — logs would land somewhere
  inconvenient and outside the plugin's purview. We install an extra
  handler at plugin startup (see `install_plugin_log_sinks`) that
  duplicates every record into `<data_dir>/liquidityhelper.log` and
  `<data_dir>/decisions.log`, and these endpoints read from there.
"""

from __future__ import annotations

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Security
from pydantic import BaseModel

# These two stream names are the ONLY valid values. Mapping is set up by
# install_plugin_log_sinks() at plugin startup.
STREAM_OPERATIONAL = "operational"
STREAM_DECISIONS = "decisions"
STREAMS: tuple[str, ...] = (STREAM_OPERATIONAL, STREAM_DECISIONS)

# Filled in by install_plugin_log_sinks(); maps stream-name → absolute
# path to its current log file. Pre-resolved so the endpoint never has
# to do filename guessing.
_STREAM_PATHS: dict[str, str] = {}

# Hard upper bound on tail size. Prevents an admin from accidentally
# asking for "the last 1_000_000 lines" and pulling hundreds of MB into
# memory. The viewer UI defaults to far less.
MAX_TAIL_LINES = 5_000
DEFAULT_TAIL_LINES = 500


class StreamInfo(BaseModel):
    name: str
    path: str
    size_bytes: int
    exists: bool


class TailResponse(BaseModel):
    stream: str
    lines: list[str]
    truncated: bool   # True if the file had more than `tail` lines


def install_plugin_log_sinks(data_dir: str) -> None:
    """Add rotating-file handlers behind the engine's background log
    listener, pointing into the plugin's data_dir. Idempotent — safe
    to call from plugin startup or reloads.

    Why we attach to the listener instead of the loggers:
      Attaching directly to a logger makes the handler's emit() run on
      whichever thread emitted the record (usually the asyncio event
      loop). A 10MB rotation rename can freeze that thread for tens of
      ms. The engine exposes `add_async_log_handler()` which appends
      handlers to its QueueListener, so disk I/O runs on a worker
      thread off the event loop.

    The engine's *own* file handlers (set up in liquidityhelper.py at
    import time) are left alone; they may write to CWD which, in plugin
    mode, is Bitcart's root. We don't try to disable them; instead we
    ADD our own handlers pointing at the plugin's data dir, and the
    endpoints below read only from those.
    """
    # Imported lazily so this module is still importable for tests that
    # don't have the engine loaded yet.
    from ..liquidityhelper import (
        add_async_log_handler, listener as _engine_listener,
    )
    from logging import Filter, LogRecord

    class _DecisionsOnlyFilter(Filter):
        def filter(self, record: LogRecord) -> bool:
            return record.name.startswith("liquidityhelper.decisions")

    class _NotDecisionsFilter(Filter):
        def filter(self, record: LogRecord) -> bool:
            return not record.name.startswith("liquidityhelper.decisions")

    os.makedirs(data_dir, exist_ok=True)

    operational_path = os.path.join(data_dir, "liquidityhelper.log")
    decisions_path = os.path.join(data_dir, "decisions.log")

    # Idempotence: tag our handlers so reloads don't double-add. We look
    # at the engine listener's handlers, not the logger's, because
    # that's where our handlers now live.
    existing = [getattr(h, "_plugin_sink", None) for h in _engine_listener.handlers]
    if "operational" not in existing:
        h_main = RotatingFileHandler(
            operational_path, maxBytes=10_000_000, backupCount=5
        )
        h_main.setLevel(logging.DEBUG)
        h_main.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        h_main.addFilter(_NotDecisionsFilter())
        h_main._plugin_sink = "operational"   # type: ignore[attr-defined]
        add_async_log_handler(h_main)

    if "decisions" not in existing:
        h_dec = RotatingFileHandler(
            decisions_path, maxBytes=10_000_000, backupCount=10
        )
        h_dec.setLevel(logging.INFO)
        h_dec.setFormatter(logging.Formatter(
            "%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        ))
        h_dec.addFilter(_DecisionsOnlyFilter())
        h_dec._plugin_sink = "decisions"    # type: ignore[attr-defined]
        add_async_log_handler(h_dec)

    _STREAM_PATHS[STREAM_OPERATIONAL] = operational_path
    _STREAM_PATHS[STREAM_DECISIONS] = decisions_path


def _tail_lines(path: str, n: int) -> tuple[list[str], bool]:
    """Return up to `n` last lines from `path`, plus a `truncated` flag
    indicating whether earlier lines exist beyond what we returned.

    Naive implementation: read the whole file. The log files are
    rotation-bounded to 10MB, so this caps at well under 10MB of work
    per request. Good enough; not worth a seek-from-end byte parser.
    """
    if not os.path.exists(path):
        return ([], False)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        # Best-effort: a transient read error during rotation, etc.
        return ([], False)
    truncated = len(all_lines) > n
    tail = all_lines[-n:] if truncated else all_lines
    return ([ln.rstrip("\n") for ln in tail], truncated)


def build_router(auth_dependency: Any | None = None) -> APIRouter:
    """Construct the APIRouter with optional Bitcart auth dependency.

    Splitting the build step out lets tests mount the router on a stub
    FastAPI app without dragging in Bitcart's auth machinery. In
    production, the plugin's setup_app passes the real dependency so
    the routes require a server-management-scoped admin token.
    """
    # prefix is "/plugins/...", not "/api/plugins/..." — root_path=/api.
    router = APIRouter(prefix="/plugins/liquidityhelper/logs")

    if auth_dependency is not None:
        deps = [Security(auth_dependency, scopes=["server_management"])]
    else:
        deps = []

    @router.get("/streams", response_model=list[StreamInfo], dependencies=deps)
    async def list_streams() -> list[StreamInfo]:
        """List every log stream the viewer can show, with current file
        size. Empty/missing files are reported with `exists=False` so
        the UI can render a 'no log yet' state instead of erroring."""
        out: list[StreamInfo] = []
        for name in STREAMS:
            path = _STREAM_PATHS.get(name, "")
            if path and os.path.exists(path):
                size = os.path.getsize(path)
                out.append(StreamInfo(
                    name=name, path=path, size_bytes=size, exists=True,
                ))
            else:
                out.append(StreamInfo(
                    name=name, path=path, size_bytes=0, exists=False,
                ))
        return out

    @router.get("/{stream}", response_model=TailResponse, dependencies=deps)
    async def tail_stream(
        stream: str,
        tail: int = Query(
            DEFAULT_TAIL_LINES, ge=1, le=MAX_TAIL_LINES,
            description="Number of trailing lines to return (capped at 5000).",
        ),
    ) -> TailResponse:
        """Return the last `tail` lines of the requested stream.

        Rejects anything outside the allow-list — the `{stream}` path
        parameter is NEVER joined with a base path or otherwise
        interpreted as a filename. Path-traversal-as-input is therefore
        a 404, not a foothold.
        """
        if stream not in _STREAM_PATHS:
            raise HTTPException(
                status_code=404, detail=f"unknown stream: {stream!r}"
            )
        path = _STREAM_PATHS[stream]
        # File read happens on a worker thread so the FastAPI event
        # loop isn't blocked while we slurp up to ~10MB off disk.
        lines, truncated = await asyncio.to_thread(_tail_lines, path, tail)
        return TailResponse(stream=stream, lines=lines, truncated=truncated)

    return router


class DebugStatusResponse(BaseModel):
    debug_mode: bool        # current value of DEBUG_MODE
    triggered: bool         # whether the trigger was fired this request
    message: str            # human-readable explanation for the UI


def build_debug_router(auth_dependency: Any | None = None) -> APIRouter:
    """Construct the debug-control router. Lives alongside the log
    router on the same auth scope. Currently exposes a single endpoint:

      POST /api/plugins/liquidityhelper/debug/run_once
        Fires the debug-mode trigger if DEBUG_MODE is on. Returns
        immediately — does NOT wait for the tick to finish (ticks can
        take minutes; an HTTP request blocking that long would time
        out and confuse the UI). The operator watches the log viewer
        to see progress.

    When DEBUG_MODE is off, the endpoint still returns 200 but with
    `triggered=False` and an explanatory message. We deliberately
    don't 4xx in that case — flipping DEBUG_MODE off and the UI not
    having refreshed yet shouldn't show as an error.
    """
    router = APIRouter(prefix="/plugins/liquidityhelper/debug")
    deps = (
        [Security(auth_dependency, scopes=["server_management"])]
        if auth_dependency is not None else []
    )

    @router.post(
        "/run_once",
        response_model=DebugStatusResponse,
        dependencies=deps,
    )
    async def run_once() -> DebugStatusResponse:
        """Trigger one tick when in debug mode."""
        # Lazy-import the engine so this module is importable in
        # focused unit tests that don't load the full liquidityhelper.
        from ..liquidityhelper import trigger_debug_run_once
        import liquidityhelper as _engine
        debug_on = bool(getattr(_engine, "DEBUG_MODE", False))
        if not debug_on:
            return DebugStatusResponse(
                debug_mode=False,
                triggered=False,
                message=(
                    "DEBUG_MODE is False — the tick loop is already "
                    "cycling continuously. Enable DEBUG_MODE in the "
                    "Settings tab first."
                ),
            )
        trigger_debug_run_once()
        return DebugStatusResponse(
            debug_mode=True,
            triggered=True,
            message=(
                "Tick triggered. Watch the log viewer for the run to "
                "complete; the next tick will block again until you "
                "click this button or disable DEBUG_MODE."
            ),
        )

    return router
