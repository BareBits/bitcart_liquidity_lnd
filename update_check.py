"""Update detection + worker heartbeat for the Liquidity Helper.

This module is the IN-PLUGIN half of the auto-update system described in
AUTOUPDATE_DESIGN.md (Phase 1: "detect + surface"). It does three things,
all read-only and all safe to call from any process:

  1. Reports the RUNNING version (from manifest.json shipped beside the
     engine) and fetches the LATEST version on a release channel from
     GitHub, caching the result so callers never block on the network.
  2. Records a worker HEARTBEAT each tick, so the /health endpoint can
     tell whether the tick loop is actually alive (the signal the
     host-side updater uses to decide an update "started successfully").
  3. Builds the operator-facing "update available" warning + email body.

Design rules (mirroring DESIGN.md):
  - This module NEVER applies an update or touches plugin code. Applying
    is the host-side updater's job. Here we only read and report.
  - Nothing here may crash a caller. Every public function swallows its
    own exceptions and degrades to a safe default (None / False / empty).
  - State lives in the existing SQLite DB (database.py) — no new files.

Kept deliberately small and separate from the 462KB engine so it stays
easy to reason about and test.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("liquidityhelper.update_check")

# GitHub repo the releases come from. The channel (branch) is templated
# in at call time. We read manifest.json directly off the branch via the
# raw CDN — no API token, no rate-limit surprises for a once-every-few-
# hours poll.
_RAW_MANIFEST_URL = (
    "https://raw.githubusercontent.com/BareBits/bitcart_liquidity_lnd/"
    "{channel}/manifest.json"
)

# Whitelisted channels. An operator-supplied UPDATE_CHANNEL that isn't one
# of these falls back to "main" — we never interpolate an arbitrary string
# into the URL (it would just 404, but better to be explicit).
_VALID_CHANNELS = ("main", "testing")

# DB keys (SimpleVariable rows) used to cache the last check.
_K_LATEST_VERSION = "update_latest_version"
_K_CHECKED_CHANNEL = "update_checked_channel"
_K_CHECKED_AT = "update_checked_at"          # ISO8601 string
_K_EMAILED_VERSION = "update_emailed_version"

# Heartbeat key (SimpleDateTimeField row).
_K_HEARTBEAT = "worker_last_tick"

# A worker that hasn't ticked within this many seconds is considered not
# alive. The tick loop's idle waits are 60s, so 5 min leaves comfortable
# margin for a slow tick without false negatives.
WORKER_ALIVE_MAX_AGE_SECONDS = 300

_ISO_FMT = "%Y-%m-%dT%H:%M:%S"


# --------------------------------------------------------------------------- #
# Version helpers
# --------------------------------------------------------------------------- #

def _manifest_path() -> str:
    """Path to the manifest.json shipped beside this module (repo root)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json")


def get_running_version() -> Optional[str]:
    """Version string from the local manifest.json, or None if unreadable."""
    try:
        with open(_manifest_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        version = data.get("version")
        return str(version) if version else None
    except Exception as e:
        logger.warning(f"get_running_version: could not read manifest: {e}")
        return None


def _parse_version(v: Optional[str]) -> Optional[Tuple[int, ...]]:
    """Parse a dotted numeric version ("0.1.0") into a tuple of ints.

    Returns None for anything non-numeric so callers can be conservative
    (we never claim an update is available off an unparseable version).
    """
    if not v:
        return None
    try:
        parts = str(v).strip().lstrip("v").split(".")
        return tuple(int(p) for p in parts)
    except Exception:
        return None


def is_newer(remote: Optional[str], local: Optional[str]) -> bool:
    """True only if `remote` parses to a strictly-higher version than
    `local`. Any parse failure → False (be conservative; never nag about
    an update we can't actually verify is newer)."""
    r, l = _parse_version(remote), _parse_version(local)
    if r is None or l is None:
        return False
    return r > l


def normalize_channel(channel: Optional[str]) -> str:
    """Coerce an operator-supplied channel to a known-safe value."""
    if channel in _VALID_CHANNELS:
        return channel  # type: ignore[return-value]
    return "main"


# --------------------------------------------------------------------------- #
# Tiny DB-backed key/value + heartbeat (best-effort, never raises)
# --------------------------------------------------------------------------- #

def _kv_set(name: str, value: str) -> None:
    try:
        from database import SimpleVariable
        SimpleVariable.replace(name=name, value=value).execute()
    except Exception as e:
        logger.warning(f"_kv_set({name}) failed: {e}")


def _kv_get(name: str) -> Optional[str]:
    try:
        from database import SimpleVariable
        row = SimpleVariable.get_or_none(SimpleVariable.name == name)
        return row.value if row is not None else None
    except Exception as e:
        logger.warning(f"_kv_get({name}) failed: {e}")
        return None


def record_heartbeat() -> None:
    """Stamp 'the worker tick loop is alive right now'. Called once per
    tick. Best-effort: a DB hiccup must never break the tick."""
    try:
        from database import SimpleDateTimeField
        SimpleDateTimeField.replace(
            name=_K_HEARTBEAT, date=datetime.now()
        ).execute()
    except Exception as e:
        logger.warning(f"record_heartbeat failed: {e}")


def get_last_tick() -> Optional[datetime]:
    """Datetime of the last recorded heartbeat, or None."""
    try:
        from database import SimpleDateTimeField
        row = SimpleDateTimeField.get_or_none(
            SimpleDateTimeField.name == _K_HEARTBEAT
        )
        return row.date if row is not None else None
    except Exception as e:
        logger.warning(f"get_last_tick failed: {e}")
        return None


def worker_alive(max_age_seconds: int = WORKER_ALIVE_MAX_AGE_SECONDS) -> bool:
    """True if a heartbeat was recorded within `max_age_seconds`."""
    last = get_last_tick()
    if last is None:
        return False
    try:
        return (datetime.now() - last).total_seconds() <= max_age_seconds
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Fetch + cache the latest version
# --------------------------------------------------------------------------- #

async def fetch_latest_version(channel: str) -> Optional[str]:
    """GET manifest.json off the channel branch and return its version.

    Network/parse failures return None (caller keeps the cached value).
    """
    channel = normalize_channel(channel)
    url = _RAW_MANIFEST_URL.format(channel=channel)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"Cache-Control": "no-cache"})
        resp.raise_for_status()
        data = resp.json()
        version = data.get("version")
        return str(version) if version else None
    except Exception as e:
        logger.warning(
            f"fetch_latest_version({channel}) failed: {e} "
            f"{traceback.format_exc()}"
        )
        return None


def get_cached_update_status() -> Dict[str, Any]:
    """Read the cached check result + local version and compute whether an
    update is available. PURE — no network. Shape is the /health contract:

        {
          "running_version": "0.1.0" | None,
          "latest_version":  "0.2.0" | None,
          "update_channel":  "main",
          "update_available": bool,
          "checked_at":      "2026-06-10T01:30:00" | None,
        }
    """
    running = get_running_version()
    latest = _kv_get(_K_LATEST_VERSION)
    channel = _kv_get(_K_CHECKED_CHANNEL) or "main"
    checked_at = _kv_get(_K_CHECKED_AT)
    return {
        "running_version": running,
        "latest_version": latest,
        "update_channel": channel,
        "update_available": is_newer(latest, running),
        "checked_at": checked_at,
    }


async def maybe_check_for_updates(
    channel: str,
    interval_seconds: int,
    enabled: bool,
    admin_notifier: Optional[Callable[[str, str], Awaitable[bool]]] = None,
) -> None:
    """Throttled update check, called from the tick loop.

    - Self-throttles: returns early unless `interval_seconds` have passed
      since the last check (or the channel changed).
    - On a newer version, caches it. When automatic updates are OFF and an
      `admin_notifier` is wired, emails the operator ONCE per new version.

    Never raises — the tick loop must not be affected by a check failure.
    """
    try:
        channel = normalize_channel(channel)
        if not _due_for_check(channel, interval_seconds):
            return

        latest = await fetch_latest_version(channel)
        # Record that we checked, regardless of outcome, so a string of
        # failures doesn't hammer GitHub every single tick.
        _kv_set(_K_CHECKED_AT, datetime.now().strftime(_ISO_FMT))
        _kv_set(_K_CHECKED_CHANNEL, channel)
        if latest is None:
            return
        _kv_set(_K_LATEST_VERSION, latest)

        running = get_running_version()
        if not is_newer(latest, running):
            return

        logger.info(
            f"Update available on channel '{channel}': running={running} "
            f"latest={latest} (auto-apply {'on' if enabled else 'off'})"
        )

        # Operator email — only when auto-apply is OFF (otherwise the
        # host updater handles it) and only once per distinct version.
        # We record the version as "emailed" ONLY when the notifier
        # confirms an email actually went out (returns truthy). If SMTP
        # isn't configured yet (or the send fails), we leave it unrecorded
        # and try again next cycle — so the operator still gets exactly
        # one notice once email is working, never zero, never duplicated.
        if not enabled and admin_notifier is not None:
            already = _kv_get(_K_EMAILED_VERSION)
            if already != latest:
                subject, body = build_update_email(running, latest, channel)
                try:
                    sent = await admin_notifier(subject, body)
                    if sent:
                        _kv_set(_K_EMAILED_VERSION, latest)
                except Exception as e:
                    logger.warning(f"update-available email failed: {e}")
    except Exception as e:
        logger.warning(
            f"maybe_check_for_updates failed: {e} {traceback.format_exc()}"
        )


def _due_for_check(channel: str, interval_seconds: int) -> bool:
    """True if it's time to poll again (interval elapsed or channel changed)."""
    last_channel = _kv_get(_K_CHECKED_CHANNEL)
    if last_channel != channel:
        return True  # channel switched — check immediately
    checked_at = _kv_get(_K_CHECKED_AT)
    if not checked_at:
        return True
    try:
        last = datetime.strptime(checked_at, _ISO_FMT)
    except Exception:
        return True
    try:
        return (datetime.now() - last).total_seconds() >= max(0, interval_seconds)
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Operator-facing surfacing (dashboard warning + email body)
# --------------------------------------------------------------------------- #

def get_update_health_warning(enabled: bool) -> Optional[Dict[str, Any]]:
    """Return a HealthWarning dict when a newer version is available AND
    automatic updates are off; otherwise None. PURE (reads cache only).

    Shape matches liquidityhelper._warn / dashboard.HealthWarning.
    """
    try:
        status = get_cached_update_status()
        if not status["update_available"] or enabled:
            return None
        running = status["running_version"] or "?"
        latest = status["latest_version"] or "?"
        channel = status["update_channel"]
        return {
            "id": "update-available",
            "severity": "MEDIUM",
            "category": "update",
            "title": f"Update available: v{latest}",
            "message": (
                f"Liquidity Helper v{latest} is available on the "
                f"'{channel}' channel (you are running v{running}). "
                f"Automatic updates are OFF. Update via your deployment's "
                f"updater, or enable AUTO_UPDATE_ENABLED on deployments "
                f"that run the host-side updater."
            ),
            "settings": ["AUTO_UPDATE_ENABLED", "UPDATE_CHANNEL"],
        }
    except Exception as e:
        logger.warning(f"get_update_health_warning failed: {e}")
        return None


def build_update_email(
    running: Optional[str], latest: Optional[str], channel: str,
) -> Tuple[str, str]:
    """(subject, body) for the 'update available' operator email."""
    subject = f"Liquidity Helper update available: v{latest}"
    body = (
        f"A new version of the Bitcart Liquidity Helper is available.\n\n"
        f"  Running version: v{running}\n"
        f"  Latest version:  v{latest}\n"
        f"  Channel:         {channel}\n\n"
        f"Automatic updates are currently OFF, so nothing has changed on "
        f"your deployment. To update:\n\n"
        f"  - If you run the host-side updater, set AUTO_UPDATE_ENABLED=true "
        f"to have it applied automatically; or\n"
        f"  - Update the plugin manually following your deployment's "
        f"instructions.\n\n"
        f"You are receiving this because you are the site operator and SMTP "
        f"is configured. See AUTOUPDATE_DESIGN.md for details.\n"
    )
    return subject, body
