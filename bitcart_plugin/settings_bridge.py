"""Push plugin settings onto the runtime config of liquidityhelper.

The standalone code reads its configuration from `config.py` at module
load time and propagates values through `from config import *` into
sibling modules (`liquidityhelper`, `classes`). Once those imports have
happened, each importing module owns its own binding for every name —
mutating `config.FOO` no longer affects `liquidityhelper.FOO`.

This module bridges plugin settings (the values an admin set via the
Bitcart settings page) onto the relevant module namespaces, AFTER they
have been imported. Functions in the engine read these names lazily
(inside function bodies), so a `setattr` here takes effect on the next
tick.

Why not env-var injection?
  We could write everything to `os.environ` before importing
  liquidityhelper — `config.py` already has an env-var override loop.
  But that path is one-shot at startup; settings changed later in the
  UI wouldn't reapply without a process restart. setattr-bridging
  supports both startup AND live updates.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Iterable, Optional

from .settings_schema import PluginSettings, SETTING_NAMES

logger = logging.getLogger("liquidityhelper.settings_bridge")


# Module names that may hold a copy of a config value. Anything that does
# `from config import *` or `from config import X` ends up here.
_TARGET_MODULES = ("config", "liquidityhelper", "classes")

# Module-level reference to the live Plugin instance. Populated by
# register_plugin_instance() from the plugin's startup hook so the
# free functions below can reach bitcart's plugin_registry without
# threading the instance through every call site (the engine tick
# loop and dashboard endpoints are NOT methods on the Plugin class,
# so they can't access self.get_plugin_settings() directly).
#
# Stays None in standalone runs (no bitcart) and during the small
# window before the Plugin's startup hook fires. refresh_settings...
# treats both as "nothing to refresh" and falls through.
_plugin_instance_ref: Optional[Any] = None


def register_plugin_instance(plugin: Any) -> None:
    """Save a reference to the live Plugin instance so the free
    functions in this module can call self.get_plugin_settings()
    without being methods on the Plugin class themselves.

    Called once from the plugin's startup hook. Idempotent — repeated
    calls just overwrite the reference, which is what we want if
    bitcart ever reloads the plugin in place."""
    global _plugin_instance_ref
    _plugin_instance_ref = plugin


async def refresh_settings_from_bitcart() -> bool:
    """Pull current saved plugin settings from bitcart's storage and
    apply them to the engine's module globals (`config`,
    `liquidityhelper`, `classes`).

    This is the eventual-consistency anchor: instead of relying on
    bitcart's `settings_changed:liquidityhelper` hook to propagate
    settings changes (which is local to the process that received
    the POST — backend gets it, worker doesn't), every place that
    consumes settings can call this to guarantee a fresh view.

    Concrete call sites:
      - run_tick_loop's top-of-iteration so the worker sees changes
        within one tick interval without cross-process IPC.
      - compute_health_warnings (dashboard endpoint) so warnings
        reflect what the operator just saved on the Settings tab.

    Returns True if a refresh was actually performed, False if the
    plugin instance isn't registered yet OR the fetch raised (we
    keep the existing module-global values in that case rather than
    blanking them — partial-failure tolerance over correctness here,
    because a transient bitcart hiccup shouldn't suddenly change
    the engine's behavior). Failures are logged at WARNING.
    """
    if _plugin_instance_ref is None:
        return False
    try:
        stored = await _plugin_instance_ref.get_plugin_settings()
    except Exception as e:
        logger.warning(
            f"refresh_settings_from_bitcart: get_plugin_settings raised, "
            f"keeping current module-global values: {e} "
            f"{traceback.format_exc()}"
        )
        return False
    stored_dict = stored.model_dump() if stored is not None else {}
    merged = merge_with_config(stored_dict)
    apply_settings(merged)
    return True


def apply_settings(settings: PluginSettings, *, modules: Iterable[str] = _TARGET_MODULES) -> dict[str, Any]:
    """Apply `settings` to the listed module namespaces.

    Only attributes already present on the target are overwritten — this
    avoids polluting modules with names they never imported. Returns the
    flat dict that was applied (useful for logging/tests).
    """
    import importlib

    applied: dict[str, Any] = {}
    payload = settings.model_dump()
    for name in modules:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        for key, value in payload.items():
            if key not in SETTING_NAMES:
                continue
            if hasattr(mod, key):
                setattr(mod, key, value)
                applied[key] = value
    return applied


def merge_with_config(settings_dict: dict[str, Any]) -> PluginSettings:
    """Build a PluginSettings instance using config.py values as the base
    and `settings_dict` (typically: what Bitcart returned from storage)
    as the override layer.

    If a key exists in both, the override wins. If a key exists only in
    config.py, the config value is kept. If a key exists only in
    settings_dict, it is included only if the schema declares it (extras
    are dropped — the schema is the source of truth for what is
    plugin-tunable).
    """
    import importlib

    config_mod = importlib.import_module("config")
    base: dict[str, Any] = {}
    for key in SETTING_NAMES:
        if hasattr(config_mod, key):
            base[key] = getattr(config_mod, key)
    base.update({k: v for k, v in settings_dict.items() if k in SETTING_NAMES})
    return PluginSettings(**base)
