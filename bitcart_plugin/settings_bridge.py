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

from typing import Any, Iterable

from .settings_schema import PluginSettings, SETTING_NAMES


# Module names that may hold a copy of a config value. Anything that does
# `from config import *` or `from config import X` ends up here.
_TARGET_MODULES = ("config", "liquidityhelper", "classes")


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
