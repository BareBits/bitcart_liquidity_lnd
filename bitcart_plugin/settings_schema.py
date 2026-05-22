"""Pydantic settings schema for the Bitcart Liquidity Helper plugin.

This single schema mirrors every public knob in `config.py`. Bitcart's
plugin framework calls `register_settings(PluginSettings)`; values stored
under `plugin:liquidityhelper` in Bitcart's settings table then override
the corresponding `config.py` defaults at runtime (see `_settings_bridge`).

Conventions:
  - Field defaults equal the config.py defaults exactly. If you change a
    default in config.py, change it here too. The tests in
    `tests/plugin_settings_tests.py` cross-check this.
  - Optional fields use `T | None` to surface "clear me" in the UI.
  - `description=` strings become tooltips/labels in the Bitcart admin
    settings page; phrase them as instructions to a non-technical operator.
  - Fields are grouped (and ordered) by concern. Bitcart renders a flat
    list, so order matters — keep related knobs adjacent.
"""

from __future__ import annotations

from typing import Optional, List

# Bitcart's Schema base lives at api.schemas.base; in plugin context it
# resolves naturally. Standalone tests use a thin shim that maps to
# pydantic.BaseModel — see _BaseSchema below.
try:  # pragma: no cover - import path depends on whether we're inside bitcart
    from api.schemas.base import Schema as _BaseSchema
except Exception:  # standalone or test mode
    from pydantic import BaseModel as _BaseSchema  # type: ignore

from pydantic import Field


class PluginSettings(_BaseSchema):
    """Liquidity helper settings. Every key here mirrors a name in config.py.

    On plugin startup these are loaded, merged with config.py defaults
    (UI wins), then pushed as module-level attributes onto config,
    liquidityhelper, and classes.
    """

    # ---- Core liquidity targets ----
    MIN_CHANNEL_COUNT: int = Field(
        default=2,
        description="Always maintain at least this many channels with inbound liquidity.",
    )
    MIN_INBOUND_LIQUIDITY: int = Field(
        default=100_000,
        description="Minimum total inbound liquidity to maintain across all channels, in sats.",
    )
    MIN_INBOUND_LIQUIDITY_PER_CHANNEL: int = Field(
        default=50_000,
        description="At least one channel must have this much inbound liquidity (prevents satisfying MIN_INBOUND_LIQUIDITY via many tiny channels).",
    )
    MIN_RESERVE_TOTAL: int = Field(
        default=20_000,
        description="Total sats to keep in reserve for opening new channels.",
    )
    MIN_RESERVE_ONCHAIN: int = Field(
        default=10_000,
        description="Sats kept on-chain in reserve for opening new channels.",
    )
    CHANNEL_ONCHAIN_BUFFER: int = Field(
        default=500,
        description="Per-channel on-chain buffer kept in reserve so the channel can be closed if needed.",
    )

    # ---- Cashout ----
    MIN_LN_CASHOUT_IN_SATS: int = Field(
        default=150,
        description="Smallest LN cashout to send (Strike's minimum is 100 sat).",
    )
    CASHOUT_LIGHTNING_ADDRESS: Optional[str] = Field(
        default="cashout@getbarebits.com",
        description="Lightning Address for LN cashouts (e.g. myname@strike.me).",
    )
    CASHOUT_ONCHAIN: Optional[str] = Field(
        default=None,
        description="On-chain Bitcoin address for cashouts. Required if ENABLE_CASHOUT_ONCHAIN is True.",
    )
    MIN_ONCHAIN_CASHOUT: int = Field(
        default=25_000,
        description="Minimum sat amount before an on-chain cashout is fired.",
    )
    ENABLE_CASHOUT_LN: bool = Field(
        default=True,
        description="Enable LN cashouts.",
    )
    ENABLE_CASHOUT_ONCHAIN: bool = Field(
        default=False,
        description="Enable on-chain cashouts. Off by default — funds normally stay in LN.",
    )
    PREFER_CASHOUT_ONCHAIN: bool = Field(
        default=False,
        description="Prefer on-chain cashouts when possible (prevents funds from being moved into LN).",
    )
    CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS: Optional[int] = Field(
        default=30,
        description="If LN cashouts have been failing for this many days, fall back to on-chain. Set blank to disable.",
    )

    # ---- Fees ----
    MIN_FEE_OUT: int = Field(
        default=150,
        description="Send fee payments only when amount due exceeds this.",
    )
    LN_FEE_DEST: str = Field(
        default="fees@getbarebits.com",
        description="Lightning Address for the developer fee.",
    )
    BB_STOREID: str = Field(default="default")
    ONCHAIN_FEE_DEST: str = Field(
        default="bc1q586um24k7zr6swxqny5qqgqn8xt43pk4xeeg9g",
        description="On-chain address for the developer fee (used when fee-onchain fallback fires).",
    )
    FEE_AMOUNT: float = Field(
        default=0.02,
        description="Developer fee rate (0.02 = 2% of revenue).",
    )
    FEE_START_DATE: Optional[str] = Field(
        default="1999/11/30",
        description="Start date for fee calculation (YYYY/MM/DD).",
    )
    FEE_START_REVENUE: int = Field(
        default=0,
        description="Don't charge a fee on the first X sats of revenue.",
    )
    ENABLE_FEE_SENDING: bool = Field(
        default=True,
        description="Master switch for fee payments. Off = no fees ever paid.",
    )
    ENABLE_FEE_SENDING_LN: bool = Field(
        default=True,
        description="Enable LN as a fee-payment rail.",
    )
    CHARGE_FEE_FOR_LN_TRANSACTIONS: bool = Field(default=True)
    CHARGE_FEE_FOR_ONCHAIN_TRANSACTIONS: bool = Field(default=True)
    FEES_PAID_INCLUDES_ONCHAIN_NETWORK_FEES: bool = Field(
        default=True,
        description="Count on-chain network fees against total fees-paid.",
    )
    FEES_PAID_INCLUDES_LN_NETWORK_FEES: bool = Field(
        default=True,
        description="Count LN network/routing fees against total fees-paid.",
    )
    FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS: Optional[int] = Field(
        default=30,
        description="If LN dev-fee payments have been failing for this many days, switch to on-chain. Blank to disable.",
    )

    # ---- Referral fee (additive, flat — for whitelabel distributors) ----
    REFERRAL_FEE_AMOUNT: float = Field(
        default=0.0,
        description="Referral fee rate, additive on top of FEE_AMOUNT. 0.0 = no referral fee.",
    )
    REFERRAL_FEE_DEST: Optional[str] = Field(
        default=None,
        description="Lightning Address for the referral payment.",
    )
    REFERRAL_ONCHAIN_DEST: Optional[str] = Field(
        default=None,
        description="On-chain address used after LN staleness fallback fires.",
    )
    REFERRAL_PAYOUT_REASON: str = Field(
        default="lnhelper_referral",
        description="Transaction label used to identify referral payments.",
    )
    REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS: Optional[int] = Field(
        default=30,
        description="If LN referral payments have been failing for this many days, switch to on-chain. Blank to disable.",
    )

    # ---- LSP-funded inbound liquidity ----
    MANUAL_CHANNEL_CREATION_ENABLED: bool = Field(
        default=False,
        description="If True, open channels directly (legacy behavior). Default False = delegate to LSP.",
    )
    LSP_CHANNEL_SIZE_SAT: int = Field(
        default=150_000,
        description="Inbound liquidity (channel size) to request from the LSP, in sats.",
    )
    LSP_CHANNEL_EXPIRY_BLOCKS: int = Field(
        default=13_000,
        description="Channel lease length in blocks. Zeus caps at 13_000 (~90 days); 13_000 is safe upper bound for both Zeus and Megalithic.",
    )
    LSP_MIN_ONCHAIN_FOR_QUOTE_SAT: int = Field(
        default=1_000,
        description="Don't even quote an LSP when on-chain balance is below this.",
    )
    LSP_QUOTE_THROTTLE_HOURS: int = Field(
        default=24,
        description="Throttle: only query each LSP once per this many hours per wallet.",
    )
    LSP_RESERVE_CAP_SAT: int = Field(
        default=50_000,
        description="Cap on the dynamic on-chain reserve floor.",
    )
    LSP_MAX_FEE_PERCENT: float = Field(
        default=0.01,
        description="Reject LSP quotes whose total fee exceeds this fraction of channel size (0.01 = 1%).",
    )
    LSP_AUTO_PEER: bool = Field(
        default=True,
        description="Automatically peer with LSP nodes at startup.",
    )
    LSP_DEV_MODE: bool = Field(
        default=False,
        description="Use testnet endpoints for LSP queries.",
    )

    # ---- Notification (SMTP) ----
    SMTP_SERVER: Optional[str] = Field(default=None)
    SMTP_PORT: Optional[int] = Field(default=None)
    SMTP_TLS: bool = Field(default=False)
    SMTP_SSL: bool = Field(default=False)
    SMTP_FROM_EMAIL: Optional[str] = Field(default=None)
    SMTP_FROM_NAME: Optional[str] = Field(default="LiquidityHelper")
    SMTP_TO_EMAIL: Optional[str] = Field(default=None)
    SMTP_USERNAME: Optional[str] = Field(default=None)
    SMTP_PASSWORD: Optional[str] = Field(default=None)

    # ---- Standalone-bootstrap settings ----
    # These remain configurable for symmetry but are unused in plugin mode:
    # the plugin acquires its own token via DI, never via setup_first_user.
    AUTH_TOKEN: Optional[str] = Field(
        default=None,
        description="Bitcart API token (standalone mode only — plugin mode acquires its own).",
    )
    LOG_LEVEL: str = Field(
        default="WARNING",
        description="Legacy log-level knob. Kept for back-compat; current code routes file logs at DEBUG and stdout at INFO regardless.",
    )
    STORE_NAME: str = Field(
        default="mystore",
        description="Store name created on first standalone run.",
    )
    ADMIN_EMAIL: Optional[str] = Field(
        default=None,
        description="Admin email for first-run setup (standalone only).",
    )
    ADMIN_PASSWORD: Optional[str] = Field(
        default=None,
        description="Admin password for first-run setup (standalone only).",
    )

    # ---- Debug / override flags ----
    DRY_RUN_FUNDS: bool = Field(
        default=False,
        description="If True, run all logic but don't actually move funds.",
    )
    SINGLE_RUN: bool = Field(
        default=False,
        description="If True, run one tick then exit (standalone-mode only; plugin ignores it).",
    )
    DEBUG_STEPS: bool = Field(default=False)
    FORCE_FEE_AMOUNT: Optional[int] = Field(default=None)
    FORCE_FEE_CHECK: bool = Field(default=False)
    FORCE_FEE_INVOICE: Optional[str] = Field(default=None)
    FORCE_FEE_ONCHAIN_INSTEAD_OF_LN: Optional[bool] = Field(default=False)
    FORCE_REFERRAL_ONCHAIN_INSTEAD_OF_LN: Optional[bool] = Field(default=False)
    FORCE_CASHOUT_AMOUNT_ONCHAIN: Optional[int] = Field(default=None)
    FORCE_CASHOUT_AMOUNT_LN: Optional[int] = Field(default=None)
    FORCE_CASHOUT_INVOICE: Optional[str] = Field(default=None)
    FORCE_EXTERNAL_IP_AND_PORT_LN: Optional[str] = Field(default=None)
    SKIP_WALLET_DELAY: bool = Field(default=False)

    # ---- Node selection (for the legacy manual-channel-open path) ----
    NODE_CRITERIA_MINIMUM_CAPACITY: int = Field(default=1_000_000)
    NODE_CRITERIA_MINIMUM_CHANNELCOUNT: int = Field(default=10)
    NODE_CRITERIA_MINIMUM_AGE: int = Field(default=730)
    NODE_CRITERIA_MAX_FEE_RATE_PPM: int = Field(default=10_000)
    NODE_CRITERIA_OUTBOUND_CAPACITY_MULTIPLIER: int = Field(default=10)
    NODE_CRITERIA_MIN_EFFECTIVE_DEGREE: int = Field(default=10)
    NODE_CRITERIA_MIN_TWO_HOP_REACH: int = Field(default=500)
    NODE_FEE_BUCKET_PPM: int = Field(default=1000)
    NODE_CRITERIA_MAX_MIN_HTLC_MSAT: int = Field(default=75_000)
    NODE_CRITERIA_MIN_MAX_HTLC_FRACTION: float = Field(default=0.5)
    CHANNEL_AUDIT_ENABLED: bool = Field(default=True)
    CHANNEL_AUDIT_CONSECUTIVE_FAILURES_TO_CLOSE: int = Field(default=3)
    CHANNEL_AUDIT_MAX_CLOSES_PER_DAY: int = Field(default=1)
    CHANNEL_AUDIT_BLACKLIST_DAYS: int = Field(default=180)
    CHANNEL_COOP_CLOSE_RETRY_ENABLED: bool = Field(default=True)
    CHANNEL_COOP_CLOSE_RETRY_INTERVAL_HOURS: int = Field(default=1)
    CHANNEL_COOP_CLOSE_TIMEOUT_DAYS: int = Field(default=10)
    CHANNEL_FORCE_CLOSE_MAX_PER_DAY_PER_WALLET: int = Field(default=1)
    CHANNEL_FORCE_CLOSE_BLACKLIST_DAYS: int = Field(default=365)
    UPTIME_CHECK_INTERVAL_MINUTES: int = Field(default=10)
    UPTIME_ROLLING_WINDOW_DAYS: int = Field(default=180)
    UPTIME_MIN_OBSERVATION_DAYS: int = Field(default=90)
    UPTIME_MAX_FAILURE_RATIO: float = Field(default=0.05)
    UPTIME_LONG_OUTAGE_DAYS: int = Field(default=14)

    # ---- Run frequencies (seconds) ----
    RUN_FREQUENCY_LIQUIDITYCHECK: int = Field(default=1)
    RUN_FREQUENCY_FEE_CALCULATION: int = Field(default=86_400)
    RUN_FREQUENCY_PULL_DEV_NODES: int = Field(default=86_400)
    RUN_FREQUENCY_FEE_PAYMENT: int = Field(default=46_400)

    # ---- Legacy (unused but referenced) ----
    MIN_INBOUND_LIQUIDITY_REQUEST_AMOUNT: int = Field(default=50_000)
    MIN_ONCHAIN_TO_LN_MOVEMENT: int = Field(default=20_000)
    INITIAL_CHANNEL_SIZE: int = Field(default=20_000)
    TARGET_INBOUND_LIQUIDITY: int = Field(default=500_000)

    # ---- Submarine swaps (LN -> on-chain) ----
    MAX_SWAP_FLAT: int = Field(
        default=50_000,
        description="Reject swap quotes whose total fee exceeds this flat sat amount.",
    )
    MAX_SWAP_PERCENT: float = Field(
        default=0.01,
        description="Reject swap quotes whose fee/amount ratio exceeds this (0.01 = 1%).",
    )
    LOOP_OUT_ENABLED: bool = Field(
        default=False,
        description="Master gate for actually firing swap-outs. False = detect/log only.",
    )
    LOOP_OUT_TRIGGER_LOCAL_BALANCE_SAT: int = Field(default=20_000)
    LN_DRAIN_MIN_SWAP_SAT: int = Field(default=500_000)
    LN_DRAIN_MAX_PER_TICK_SAT: int = Field(default=5_000_000)

    # ---- Autoloop (Lightning Labs loop daemon) ----
    AUTOLOOP_ENABLED: bool = Field(default=False)
    AUTOLOOP_DEST_ADDRESS: Optional[str] = Field(default=None)
    AUTOLOOP_ACCOUNT: Optional[str] = Field(default=None)
    AUTOLOOP_ACCOUNT_ADDR_TYPE: str = Field(default="p2tr")
    AUTOLOOP_BUDGET_SAT: int = Field(default=100_000)
    AUTOLOOP_BUDGET_REFRESH_PERIOD_SEC: int = Field(default=604_800)
    AUTOLOOP_MIN_SWAP_AMOUNT_SAT: int = Field(default=250_000)
    AUTOLOOP_MAX_SWAP_AMOUNT_SAT: int = Field(default=5_000_000)
    AUTOLOOP_MAX_IN_FLIGHT: int = Field(default=1)
    AUTOLOOP_FEE_PPM: int = Field(default=0)
    AUTOLOOP_MAX_SWAP_FEE_PPM: int = Field(default=5_000)
    AUTOLOOP_MAX_ROUTING_FEE_PPM: int = Field(default=5_000)
    AUTOLOOP_MAX_PREPAY_ROUTING_FEE_PPM: int = Field(default=50_000)
    AUTOLOOP_MAX_PREPAY_SAT: int = Field(default=100_000)
    AUTOLOOP_MAX_MINER_FEE_SAT: int = Field(default=15_000)
    AUTOLOOP_SWEEP_CONF_TARGET: int = Field(default=100)
    AUTOLOOP_HTLC_CONF_TARGET: int = Field(default=6)
    AUTOLOOP_SWEEP_FEE_RATE_SAT_PER_VBYTE: int = Field(default=0)
    AUTOLOOP_FAILURE_BACKOFF_SEC: int = Field(default=86_400)
    AUTOLOOP_EASY_MODE: bool = Field(default=False)
    AUTOLOOP_EASY_LOCAL_TARGET_SAT: int = Field(default=0)
    AUTOLOOP_FAST_SWAP_PUBLICATION: bool = Field(default=False)
    AUTOLOOP_EASY_EXCLUDED_PEERS: List[str] = Field(default_factory=list)


# Convenience: the explicit set of names this schema covers. Used by the
# settings-bridge to know which module attrs are eligible for override.
SETTING_NAMES = frozenset(PluginSettings.model_fields.keys())


# ---------------------------------------------------------------------------
# Pull descriptions + group labels from config.py at import time.
#
# Why we override Field(description=...) here instead of writing the prose
# inline above each field: the source of truth lives in config.py. Operators
# editing config.py see the explanations right next to the defaults, and the
# plugin admin UI's tooltips read the *same* strings via the schema. One
# place to edit, no drift.
#
# The schema's hand-written description strings (above) serve as fallbacks
# for any setting that doesn't have a comment block in config.py — useful
# during incremental rollout of the new format, and as a safety net.
# ---------------------------------------------------------------------------

try:
    from .config_doc_parser import parse_config_module
    _CONFIG_DOCS = parse_config_module()
except Exception:
    # If parsing fails (config.py temporarily malformed during an edit,
    # import path issues during tests) we silently fall back to the
    # inline descriptions. Better than refusing to load the schema.
    _CONFIG_DOCS = {}


def _apply_config_docs() -> None:
    """Patch each Field's description with the parsed value from config.py
    if one is available. Also records the parsed group label on the
    FieldInfo's `json_schema_extra` so the admin UI can render section
    headers."""
    for name, field_info in PluginSettings.model_fields.items():
        doc = _CONFIG_DOCS.get(name)
        if doc is None:
            continue
        if doc.description:
            field_info.description = doc.description
        if doc.group:
            extra = field_info.json_schema_extra
            if not isinstance(extra, dict):
                extra = {}
            extra["group"] = doc.group
            field_info.json_schema_extra = extra
    # Rebuild the model so pydantic recomputes the cached JSON schema
    # with the new descriptions. Without this, Field.description is
    # updated on the FieldInfo object but model_json_schema() still
    # returns the old strings.
    PluginSettings.model_rebuild(force=True)


_apply_config_docs()


def get_settings_groups() -> "list[tuple[str | None, list[str]]]":
    """Return settings grouped by their parsed group label, in the order
    config.py declared them. The admin UI calls this to render section
    headers above each cluster of settings.

    Returns: [(group_label_or_None, [setting_name, ...]), ...]
    """
    by_group: "dict[str | None, list[str]]" = {}
    order: list[str | None] = []
    # Walk config.py order, falling back to schema order for any
    # settings that aren't documented yet.
    seen: set[str] = set()
    for name, doc in _CONFIG_DOCS.items():
        if name not in PluginSettings.model_fields:
            continue
        if doc.group not in by_group:
            by_group[doc.group] = []
            order.append(doc.group)
        by_group[doc.group].append(name)
        seen.add(name)
    leftover = [n for n in PluginSettings.model_fields if n not in seen]
    if leftover:
        by_group.setdefault(None, []).extend(leftover)
        if None not in order:
            order.append(None)
    return [(g, by_group[g]) for g in order]
