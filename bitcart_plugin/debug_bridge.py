"""Cross-process Redis pub/sub bridge for the debug-run-once trigger.

In production Bitcart, the plugin runs as multiple processes:
  - Backend: serves HTTP. POST /debug/run_once handler lives here.
  - Worker:  runs liquidityhelper.run_tick_loop. PyCharm debug-attach
             hooks here; the asyncio.Event `_debug_run_once_trigger`
             defined in liquidityhelper.py is awaited here too.

Module-level state isn't shared across processes — backend's Event
instance is a distinct object from worker's Event instance. When the
operator clicks "Run one tick" the backend handler calls
`trigger_debug_run_once()` which `.set()`s the BACKEND's Event;
nobody is awaiting that one, while worker keeps blocking on its own
unset Event forever. Symptom: log viewer shows no tick activity,
PyCharm breakpoints inside main() never fire.

Fix here: publish a single byte to the "liquidityhelper:debug_run_once"
Redis channel when the backend handler fires the trigger. Worker, on
startup, subscribes to the same channel and sets ITS local Event
whenever a message arrives. Latency is sub-millisecond (Redis is
already in the docker-compose; no new infrastructure).

Standalone mode (no Bitcart, no Redis): both helpers are no-ops.
The standalone engine runs everything in one process so the in-memory
Event mechanism works on its own; the pub/sub layer is only needed
when backend and worker are separate processes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("liquidityhelper.debug_bridge")

# Channel name. Lowercase + colon-separated follows Redis convention.
# Prefixed with the plugin name so other plugins / bitcart itself
# couldn't collide on the same channel by accident.
_RUN_ONCE_CHANNEL = "liquidityhelper:debug_run_once"


def _redis_url() -> str:
    """Derive the Redis URL from Bitcart's REDIS_HOST / REDIS_PORT /
    REDIS_DB env vars. Same defaults as Bitcart's Settings class
    (127.0.0.1:6379/0). Matches what bitcart-fork/api/settings.py
    builds for `redis_url`, so the same connection works."""
    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    port = os.environ.get("REDIS_PORT", "6379")
    db = os.environ.get("REDIS_DB", "0")
    return f"redis://{host}:{port}/{db}"


def _import_redis():
    """Lazy-import `redis.asyncio` so this module is importable in
    standalone mode where the redis package isn't installed (or in
    plugin-mode test environments that don't pull bitcart's deps).
    Returns None if unavailable — callers treat that as 'pub/sub
    bridge is off, fall back to in-process Event semantics'."""
    try:
        import redis.asyncio as redis_async
    except ImportError:
        return None
    return redis_async


async def publish_debug_run_once() -> bool:
    """Fire one cross-process debug-tick signal. Returns True on
    publish, False if Redis isn't reachable (we then rely on the
    caller's local-process Event-set as a best-effort).

    Called from the backend HTTP handler — the worker's subscriber
    sees the message and sets its local Event so run_tick_loop wakes
    and runs one main()."""
    redis_async = _import_redis()
    if redis_async is None:
        logger.debug("publish_debug_run_once: redis package unavailable")
        return False
    client = None
    try:
        client = redis_async.Redis.from_url(_redis_url(), decode_responses=True)
        # Payload content is unused — receivers only care that a
        # message arrived. Empty string keeps the message tiny.
        await client.publish(_RUN_ONCE_CHANNEL, "")
        return True
    except Exception as e:
        logger.warning(
            f"publish_debug_run_once: redis publish failed; "
            f"backend's local Event.set() is the only signal: "
            f"{e} {traceback.format_exc()}"
        )
        return False
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                # Closing is best-effort; redis-py 5.x calls aclose()
                # but close() is also accepted as a deprecated alias.
                pass


async def subscribe_debug_run_once(
    on_message: Callable[[], Awaitable[None]],
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Subscribe to the debug-run-once channel and call `on_message`
    every time a message arrives. Runs forever; the caller spawns
    this as a background asyncio.Task and cancels it on shutdown.

    `stop_event`, when supplied, is checked between Redis poll
    cycles — set it from the plugin's shutdown hook for cooperative
    shutdown.

    No-op if redis is unavailable (matches the publish side's
    contract — standalone runs don't need cross-process bridging).
    """
    redis_async = _import_redis()
    if redis_async is None:
        logger.info(
            "subscribe_debug_run_once: redis package unavailable; "
            "cross-process debug-tick trigger is disabled. Backend's "
            "/debug/run_once button will appear to do nothing until "
            "the plugin is run standalone or redis is installed."
        )
        return

    # Outer retry loop. A redis hiccup (container restart, brief
    # network blip) must not kill the subscriber permanently — we
    # reconnect and resume on the same channel. Backoff caps at 5s
    # so debug iteration feels responsive after a hiccup.
    backoff = 1.0
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        client = None
        pubsub = None
        try:
            client = redis_async.Redis.from_url(_redis_url(), decode_responses=True)
            pubsub = client.pubsub()
            await pubsub.subscribe(_RUN_ONCE_CHANNEL)
            logger.info(
                "subscribe_debug_run_once: subscribed to channel "
                f"{_RUN_ONCE_CHANNEL}; cross-process debug trigger active"
            )
            backoff = 1.0  # connection healthy; reset backoff
            # Inner read loop. `get_message(timeout=...)` blocks up
            # to `timeout` seconds waiting for a message, then
            # returns None — letting us check stop_event between
            # polls without keeping the subscription open
            # indefinitely on shutdown.
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if msg is None:
                    continue
                # Type==message means it's a real publish, not a
                # subscription confirmation. The above arg already
                # filters those out, but defense-in-depth is cheap.
                if msg.get("type") != "message":
                    continue
                try:
                    await on_message()
                except Exception as e:
                    logger.warning(
                        f"subscribe_debug_run_once: on_message callback "
                        f"raised; continuing to listen: "
                        f"{e} {traceback.format_exc()}"
                    )
        except asyncio.CancelledError:
            # Cooperative shutdown — propagate so the parent task
            # exits cleanly.
            raise
        except Exception as e:
            logger.warning(
                f"subscribe_debug_run_once: subscription error "
                f"(will reconnect in {backoff:.1f}s): "
                f"{e} {traceback.format_exc()}"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(_RUN_ONCE_CHANNEL)
                    await pubsub.close()
                except Exception:
                    pass
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
