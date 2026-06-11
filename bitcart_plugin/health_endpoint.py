"""Unauthenticated `/health` endpoint for the Liquidity Helper.

This is the machine-readable probe the **host-side updater** curls to
decide whether a freshly-applied update started successfully, and to read
the effective auto-update config (see AUTOUPDATE_DESIGN.md §5/§6). It is
deliberately:

  - **Unauthenticated** — the updater is a plain host script with no
    bearer token. Only low-sensitivity data is exposed (plugin version,
    whether the worker is alive, the update channel). No secrets, no
    fund data. Registered WITHOUT an auth dependency on purpose; every
    other plugin router stays authed.
  - **Crash-proof** — if anything goes wrong it still returns 200 with a
    best-effort body. "Our plugin failed to load" is signalled by the
    route being *absent* (Bitcart skips a plugin that fails to import),
    which the updater reads as a failed start. This endpoint's job is to
    confirm the happy path, not to 500.

Contract (JSON):
    {
      "ok": true,
      "running_version": "0.1.0" | null,
      "latest_version":  "0.2.0" | null,
      "update_available": false,
      "update_channel":  "main",
      "auto_update_enabled": false,
      "worker_alive": true,
      "last_tick_at": "2026-06-10T01:30:00" | null,
      "checked_at":  "2026-06-10T01:30:00" | null
    }
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter

logger = logging.getLogger("liquidityhelper.health_endpoint")


def build_health_router() -> APIRouter:
    """Build the unauthenticated health router.

    Mounted at `/plugins/liquidityhelper/health`. Note the bitcart app
    already has root_path="/api", so the public path is
    `/api/plugins/liquidityhelper/health`.
    """
    router = APIRouter(prefix="/plugins/liquidityhelper")

    @router.get("/health")
    async def health() -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "ok": True,
            "running_version": None,
            "latest_version": None,
            "update_available": False,
            "update_channel": "main",
            "auto_update_enabled": False,
            "worker_alive": False,
            "last_tick_at": None,
            "checked_at": None,
        }
        # Effective settings (refresh stored settings first so a live UI
        # toggle is reflected for the updater).
        try:
            from .settings_bridge import refresh_settings_from_bitcart
            await refresh_settings_from_bitcart()
        except Exception:
            pass
        try:
            import config
            body["auto_update_enabled"] = bool(
                getattr(config, "AUTO_UPDATE_ENABLED", False)
            )
            body["update_channel"] = (
                getattr(config, "UPDATE_CHANNEL", "main") or "main"
            )
        except Exception as e:
            logger.warning(f"health: reading config failed: {e}")

        # Version + heartbeat status.
        try:
            import update_check
            status = update_check.get_cached_update_status()
            body["running_version"] = status["running_version"]
            body["latest_version"] = status["latest_version"]
            body["update_available"] = status["update_available"]
            # Cached channel reflects what was last checked; prefer the
            # effective setting we already resolved above when present.
            body["checked_at"] = status["checked_at"]
            last = update_check.get_last_tick()
            body["worker_alive"] = update_check.worker_alive()
            body["last_tick_at"] = (
                last.strftime("%Y-%m-%dT%H:%M:%S") if last is not None else None
            )
        except Exception as e:
            logger.warning(f"health: reading update_check failed: {e}")

        return body

    return router
