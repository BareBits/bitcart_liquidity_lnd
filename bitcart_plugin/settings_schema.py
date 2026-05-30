"""Pydantic settings schema for the Bitcart Liquidity Helper plugin.

This module GENERATES the `PluginSettings` class from `config.py` at
import time, using `config_doc_parser.parse_config_module()`. The
upshot is: `config.py` is the single source of truth for every
operator-tunable knob — its default, type annotation, description, and
group label are read directly. Adding a setting to `config.py` makes
it automatically appear in the admin UI on next plugin start.

How it works at runtime:

  1. parse_config_module() imports the engine's config.py + reads its
     source. For each setting it returns:
       - name, description (from the comment block above),
       - group (from the `# === ... ===` banner),
       - type_hint (from typing.get_type_hints on the imported module),
       - default_value (the live module attribute, post env-var override).
  2. We iterate that mapping and call `pydantic.create_model(...)` with
     `(type, Field(default=..., description=...))` tuples — one per
     setting — to produce the schema class.
  3. _EXCLUDED hides settings the operator should never set via UI
     (internal tx-label constants, protocol-shape constants like
     MIN_CHANNEL_SIZE_IN_SATS, gossip-readiness internals).
  4. _OVERRIDES tweaks specific descriptions where the developer-facing
     config.py docstring isn't ideal for the operator audience.

To deliberately keep a knob OUT of the UI: add it to _EXCLUDED with a
one-line justification. To customize how a knob renders in the UI
without changing its config.py docstring: add to _OVERRIDES.

Bitcart's plugin framework calls `register_settings(PluginSettings)`;
values stored under `plugin:liquidityhelper` in Bitcart's settings
table then override the corresponding config.py defaults at runtime
(see `settings_bridge`).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# Bitcart's Schema base lives at api.schemas.base; in plugin context it
# resolves naturally. Standalone tests use a thin shim that maps to
# pydantic.BaseModel — see _BaseSchema below.
try:  # pragma: no cover - import path depends on whether we're inside bitcart
    from api.schemas.base import Schema as _BaseSchema
except Exception:  # standalone or test mode
    from pydantic import BaseModel as _BaseSchema  # type: ignore

from pydantic import Field, create_model

from .config_doc_parser import parse_config_module


# Settings INTENTIONALLY hidden from the admin UI. Curated, with a
# one-line justification so a future reader sees WHY each is excluded.
# Adding to this set requires no other code change.
# Settings that are valid (still persisted, still applied by the
# settings_bridge) but should NOT appear in the schema-groups expansion
# panels — typically because a higher-level widget owns them. The
# dashboard Settings tab's "Liquidity management mode" dropdown is the
# authoritative entry point for these.
_HIDDEN_FROM_UI: Dict[str, str] = {
    "AUTOMATIC_CHANNEL_CREATION_ENABLED": (
        "owned by the Liquidity management mode dropdown"
    ),
    "LIQUIDITY_DISABLED": (
        "owned by the Liquidity management mode dropdown"
    ),
}


_EXCLUDED: Dict[str, str] = {
    # Transaction-label constants. Changing them after deploy orphans
    # all existing label-tagged history rows the dashboard reads.
    "CASHOUT_REASON": "tx-label constant; changing breaks history lookup",
    "CASHOUT_DIRECT_CHANNEL_REASON": "tx-label constant; same",
    "FEE_PAYOUT_REASON": "tx-label constant; same",
    "REFERRAL_PAYOUT_REASON": "tx-label constant; same",
    "TOPUP_NAME": "tx-label constant; same",
    "TOPUP_BAREBITS": "tx-label constant; same",
    # Protocol-shape / daemon-minimum constants. Setting these to
    # arbitrary values would just produce immediate daemon rejections.
    "MIN_CHANNEL_SIZE_IN_SATS": "daemon minimum; below 60_000 channels are rejected",
    # Gossip-readiness internals. Defaults are tuned for the worked-out
    # safety/failure shape; surfacing invites accidental misconfigs.
    "GOSSIP_MIN_NODE_COUNT": "gossip-readiness internal; tuned default",
    "GOSSIP_MIN_UPTIME_SECONDS": "gossip-readiness internal; tuned default",
    "GOSSIP_MAX_STALENESS_DAYS": "gossip-readiness internal; tuned default",
    # Loopd network-topology constants. Set via env at deploy time, not
    # the UI — getting these wrong yields confusing 'no swap server' errors.
    "LOOPD_NETWORK": "loopd topology constant; deploy-time env, not UI",
    "LOOPD_SERVER_HOST": "loopd topology constant; deploy-time env, not UI",
    "LOOPD_SERVER_NOTLS": "loopd topology constant; deploy-time env, not UI",
}


# Per-field UI overrides. Only for fields where the operator-facing
# tooltip should differ from the developer-facing config.py docstring.
# Most fields don't need an entry — config.py prose is fine for both
# audiences. Each value is a dict that can carry any subset of:
#   "description" — replaces the config.py docstring
#   "field_type"  — forces a specific Pydantic field type
_OVERRIDES: Dict[str, Dict[str, Any]] = {
    # config.py's LOG_LEVEL docstring is a one-paragraph deprecation
    # notice; the UI version below is more explicit + tells the
    # operator what to do instead.
    "LOG_LEVEL": {
        "description": (
            "INFORMATIONAL ONLY — no-op in current code. File logs are "
            "always routed at DEBUG and stdout at INFO regardless of "
            "this value. Adjust handlers in liquidityhelper.py directly "
            "if you need different thresholds. Kept here for back-compat "
            "with operator runbooks that still expect the field to exist."
        ),
    },
}


def _resolve_field_type(name: str, type_hint: Any, default: Any) -> Any:
    """Pick the Pydantic field type for `name`.

    Priority: _OVERRIDES["field_type"] > config.py annotation > infer
    from default value. Returns Any as a last-resort sentinel so
    create_model doesn't fail on a single missing hint (the field is
    still usable; it just becomes unconstrained).
    """
    override = _OVERRIDES.get(name, {}).get("field_type")
    if override is not None:
        return override
    if type_hint is not None:
        return type_hint
    if default is None:
        # No annotation, no value to infer from. Fall back to Optional[str]
        # so the field at least accepts user input + an explicit clear.
        return Optional[str]
    return type(default)


def _build_field(
    name: str, type_hint: Any, default: Any, description: str,
) -> Tuple[Any, Any]:
    """Build the (annotation, FieldInfo) tuple create_model expects."""
    field_type = _resolve_field_type(name, type_hint, default)
    field_description = _OVERRIDES.get(name, {}).get("description") or description or ""
    # Mutable defaults (list, dict) MUST use default_factory in
    # pydantic v2 — otherwise instances share the same object.
    if isinstance(default, list):
        snapshot = list(default)
        field = Field(
            default_factory=lambda: list(snapshot),
            description=field_description,
        )
    elif isinstance(default, dict):
        snapshot = dict(default)
        field = Field(
            default_factory=lambda: dict(snapshot),
            description=field_description,
        )
    else:
        field = Field(default=default, description=field_description)
    return (field_type, field)


def _build_settings_class():
    """Generate the PluginSettings class from config.py.

    Field order follows source order in config.py — important because
    Bitcart's admin UI renders the schema as a flat list and the
    grouping comes from that order alone.
    """
    parsed = parse_config_module()
    fields: Dict[str, Tuple[Any, Any]] = {}
    for name, info in parsed.items():
        if name in _EXCLUDED:
            continue
        fields[name] = _build_field(
            name=info.name,
            type_hint=info.type_hint,
            default=info.default_value,
            description=info.description,
        )
    return create_model("PluginSettings", __base__=_BaseSchema, **fields)


PluginSettings = _build_settings_class()


# Convenience: the explicit set of names this schema covers. Used by the
# settings-bridge to know which module attrs are eligible for override.
SETTING_NAMES: FrozenSet[str] = frozenset(PluginSettings.model_fields.keys())


def get_settings_groups() -> List[Tuple[str, List[str]]]:
    """Return PluginSettings fields grouped by their config.py banner.

    Each entry is `(group_name, [setting_name, ...])`. Groups are in
    source-declaration order (matches config.py top-to-bottom); within
    a group, settings are in source order too. _EXCLUDED settings are
    omitted; settings declared without a preceding `# === ... ===`
    banner land in a synthetic "Other" group.

    Consumed by plugin.py's `/schema` endpoint to render the admin UI
    in tabbed sections.
    """
    parsed = parse_config_module()
    grouped: "OrderedDict[str, List[str]]" = OrderedDict()
    for name, info in parsed.items():
        if name in _EXCLUDED:
            continue
        if name in _HIDDEN_FROM_UI:
            continue
        if name not in SETTING_NAMES:
            continue
        group = info.group or "Other"
        grouped.setdefault(group, []).append(name)
    return list(grouped.items())
