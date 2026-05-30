"""HTTP endpoint that powers the plugin's Dashboard tab.

Surfaces per-store revenue, fees, and inbound-liquidity figures pulled
from the same `new_calc_invoice_stats` the fee-payment loop already
uses — no parallel calculation pipeline, so dashboard numbers match
what `calculate_fees` is acting on.

Data sources:
  - Revenue / fee breakdown : `new_calc_invoice_stats(api, since_date)`
                              (counts revenue from ALL wallets but tags
                              non-`liquidityhelper` wallets via the
                              ineligible_revenue_*_not_liquidityhelper_*
                              fields. This module's `compute_dashboard`
                              applies the wallet-name filter itself before
                              surfacing per-store stats. Walks on-chain +
                              LN histories.)
  - USD conversion          : `api.get_btc_usd_rate()`  (Bitcart's
                              /cryptos/rate endpoint; null on failure.)
  - Inbound liquidity       : For btclnd wallets, direct LND ListChannels
                              via `lnd_rpc` (Bitcart's btclnd channel
                              proxy doesn't reliably expose remote_balance);
                              for Electrum wallets, `api.get_wallet_ln_channels`.

The endpoint caches its response for 60s by `range` key. Cache is best-
effort (process-local dict) — if the engine restarts the cache resets.

Safety:
  - Every numeric field defaults to 0 if upstream data is missing/null.
  - USD figures default to None (rendered as "—" in the UI) when the
    rate fetch fails.
  - A store with no invoices renders all-zero rows, not "no data" —
    operators want to SEE that the store has produced nothing yet, not
    have it omitted.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Security
from pydantic import BaseModel

logger = logging.getLogger("liquidityhelper.dashboard")

# Hardcoded baseline — what an operator would typically pay a card
# processor as a percentage of revenue. Used to compute "amount saved"
# vs going the Bitcoin route.
CREDIT_CARD_BASELINE_PCT = 0.05

# Cache TTL: 60s feels right for a dashboard refresh — long enough that
# a busy admin clicking around doesn't recompute every page paint, short
# enough that "I just paid a fee" updates appear quickly.
_DASHBOARD_CACHE_TTL_SEC = 60

# Range keyword → number of days. "all" maps to None (no since_date
# filter). Validated at the endpoint boundary so a hostile/buggy
# client can't make us compute over a 1B-day window.
_RANGE_DAYS: Dict[str, Optional[int]] = {
    "all": None,
    "30": 30,
    "90": 90,
    "365": 365,
}


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------

class _Money(BaseModel):
    """A monetary amount expressed in three units. `usd` is None when
    we couldn't fetch a rate; UI renders that as '—' so a missing rate
    doesn't crash the page."""
    sats: int
    btc: float
    usd: Optional[float]


class FeeBreakdown(BaseModel):
    """All fee subcategories surfaced individually. The UI shows them
    in an indented section under "Network fees" with each row labelled.
    Every field is sats; UI does sats→BTC/USD render-side."""
    onchain_payouts: int
    onchain_fee_payments: int
    onchain_referral_payments: int
    onchain_topup_returns: int
    onchain_channel_opens: int
    onchain_channel_closes: int
    onchain_swaps: int
    onchain_lsp_orders: int
    lsp_service_fees: int
    # Miner fees for outgoing on-chain txs that weren't initiated by an
    # engine-labeled path (operator manual sends via Bitcart UI /
    # `lncli sendcoins`, LND anchor sweeps, etc.). Real fees the wallet
    # paid — included in network_fees_total — but bucketed separately
    # so the breakdown row reads "external/manual" instead of being
    # silently lumped under channel opens.
    onchain_external: int
    ln_payouts: int
    ln_fee_payments: int
    ln_referral_payments: int
    # Routing fees paid on circular-rebalance self-payments. Surfaced
    # with its own row + tooltip so operators can see the
    # channel-maintenance cost separately from real payouts.
    ln_rebalances: int
    ln_misc: int


class StoreDashboard(BaseModel):
    """Per-store snapshot."""
    store_id: str
    store_name: str
    wallet_id: str
    wallet_name: str
    revenue: _Money
    paid_invoice_count: int
    developer_fees_paid: _Money
    # Gross developer fees ever owed: eligible_revenue × FEE_AMOUNT.
    # The "balance" the engine will try to charge next tick is
    # `due − paid` (clamped at 0); UI can compute that client-side.
    # Network-fee credit toward the `due` total depends on the engine's
    # FEES_PAID_INCLUDES_*_NETWORK_FEES flags and is NOT applied here —
    # we expose principals so the UI math stays unambiguous.
    developer_fees_due: _Money
    developer_fee_pct: Optional[float]   # paid / revenue; None if revenue==0
    hosting_fees_paid: _Money            # called "referral" internally
    hosting_fees_due: _Money             # eligible_revenue × REFERRAL_FEE_AMOUNT
    hosting_fee_pct: Optional[float]
    network_fees_total: _Money
    network_fee_breakdown: FeeBreakdown
    amount_saved_vs_cc: _Money           # CC_BASELINE_PCT*revenue - total fees paid
    net_fees_paid: _Money                # dev + hosting + network
    net_fees_pct: Optional[float]        # net_fees_paid / revenue; None if revenue==0
    pie_slices: Dict[str, int]           # 3-slice pie: {dev, hosting, network} in sats
    inbound_liquidity: _Money            # sum of remote_balance for active channels
    active_channel_count: int


class SummaryDashboard(BaseModel):
    """Aggregated totals across stores. Omits per-store-only fields
    like inbound_liquidity per the spec."""
    revenue: _Money
    paid_invoice_count: int
    developer_fees_paid: _Money
    developer_fees_due: _Money
    hosting_fees_paid: _Money
    hosting_fees_due: _Money
    network_fees_total: _Money
    network_fee_breakdown: FeeBreakdown
    amount_saved_vs_cc: _Money
    net_fees_paid: _Money
    net_fees_pct: Optional[float]
    pie_slices: Dict[str, int]


class PaymentRow(BaseModel):
    """One row in the Recent Fee Payments / Recent Cashouts tables.

    `txid` is non-empty only for on-chain payments (the UI uses it to
    render the mempool.space link); LN payments leave it empty and
    surface `payment_hash` instead.
    """
    timestamp: int             # unix seconds; 0 if unknown
    iso_date: str              # ISO string for UI display
    amount_sats: int
    amount_usd: Optional[float]
    fee_sats: int              # cost-to-send (network fee)
    fee_usd: Optional[float]
    destination: str           # on-chain address OR LN address from config
    fee_type: str              # "developer" | "hosting" | "cashout"
    method: str                # "onchain" | "lightning"
    txid: str                  # empty for LN
    payment_hash: str          # empty for on-chain


class ChannelDetailRow(BaseModel):
    """One channel under a wallet in the Liquidity stats panel.

    `local_balance` is the wallet's spendable outbound, `remote_balance`
    is the wallet's available inbound. `capacity` is the LN protocol's
    notion (local + remote at open time); the live sum can drift by
    fee-rounding sats so we report local+remote directly, which is what
    the operator actually has to work with.

    `is_active` reflects peer reachability: True when the peer is
    currently connected. UI uses this to grey out channels whose
    inbound capacity isn't actually routable right now.
    """
    channel_point: str
    peer_pubkey: Optional[str] = None
    peer_alias: Optional[str] = None
    local_balance: int
    remote_balance: int
    capacity: int
    is_active: bool


class WalletLiquidityRow(BaseModel):
    """One row in the Liquidity stats table — one per liquidityhelper-
    named wallet. Inbound = sum of remote_balance on active channels;
    outbound = sum of local_balance on active channels."""
    wallet_id: str               # full id; stable React key
    wallet_short: str            # first 8 chars of wallet_id for display
    wallet_name: str             # always "liquidityhelper" today (filter)
    inbound: _Money              # remote_balance sum, in BTC + sats + USD
    outbound: _Money             # local_balance sum, in BTC + sats + USD
    active_channel_count: int
    # Per-channel breakdown shown indented under each wallet row.
    # Ordered by capacity descending so the biggest channels lead.
    channels: List[ChannelDetailRow] = []
    # Names of stores whose best LN wallet resolves to this wallet.
    # Sourced from the wallet_to_stores map built in compute_dashboard
    # — empty list when no store currently points to this wallet
    # (would happen if e.g. an old liquidityhelper wallet is still on
    # the bitcart instance but no store uses it anymore).
    store_names: List[str] = []


class LiquidityStats(BaseModel):
    """Aggregated liquidity-stats section shown above the recent-activity
    tables. Surfaces per-wallet inbound/outbound balances + channel
    count, with totals across all liquidityhelper wallets. The `mode`
    string tells the operator at a glance which liquidity-management
    strategy is configured."""
    # Human-readable label of the current liquidity-management mode.
    # Mirrors the engine gate at liquidityhelper.move_onchain_to_ln:
    #   AUTOMATIC_CHANNEL_CREATION_ENABLED=True  → "Automatic channel management"
    #   AUTOMATIC_CHANNEL_CREATION_ENABLED=False → "LSP-managed liquidity"
    # The False default means the engine delegates new-channel acquisition
    # to an LSP via pick_best_lsp_for_inbound.
    mode: str
    wallets: List[WalletLiquidityRow]
    # Totals across all listed wallets. Computed server-side so the UI
    # doesn't have to recompute on every render.
    total_inbound: _Money
    total_outbound: _Money
    total_channel_count: int
    # {lowercase_peer_pubkey: provider_name} for every URI the configured
    # LSP providers (Zeus, Megalithic) advertise on the current network.
    # Combines hardcoded fallbacks with the LSP's dynamic get_info.uris.
    # Frontend joins this against each channel's peer_pubkey to render
    # an inline "(zeus)" / "(megalithic)" tag next to the peer alias.
    # Empty dict on regtest / unknown network or when providers fail.
    lsp_provider_pubkeys: Dict[str, str] = {}


class NetworkFeeRow(BaseModel):
    """One row in the Recent Network Fees table.

    Surfaces every individual transaction where the engine paid a
    network fee — on-chain miner fees (txid populated, link to
    mempool.space in the UI) or Lightning routing fees (payment_hash
    populated, no on-chain link). `amount_sats` is the principal of
    the underlying transaction so the operator can see "we sent X
    sats and paid Y in fees" at a glance.

    Categories include the existing fee-payment / cashout labels plus
    channel-open, channel-close, and lsp-order labels — anything that
    incurred a non-zero fee. Channel-close txs are included even though
    they're net-inbound to the wallet, because we still paid the fee.
    """
    timestamp: int             # unix seconds; 0 if unknown
    iso_date: str              # ISO string for UI display
    category: str              # see _label_to_network_fee_category()
    fee_sats: int              # the network/routing fee we paid
    fee_usd: Optional[float]
    amount_sats: int           # principal of the underlying tx (positive)
    amount_usd: Optional[float]
    method: str                # "onchain" | "lightning"
    txid: str                  # empty for LN
    payment_hash: str          # empty for on-chain
    destination: str           # peer / address from the tx label or current config
    # On-chain only: sat/vbyte fee rate, computed locally from LND's
    # raw_tx_hex via the BIP141 weight formula. Lets operators audit
    # at-a-glance whether a tx paid a reasonable rate for the mempool
    # conditions at the time. None for LN rows (no concept) and for
    # Electrum-source rows (raw_tx_hex isn't exposed by their
    # onchain_history endpoint).
    fee_rate_sat_per_vbyte: Optional[float] = None


class ChannelClosureRow(BaseModel):
    """One row in the Recent Channel Closures table."""
    timestamp: int             # unix seconds of last_close_attempt_at or closed_at
    iso_date: str
    channel_point: str
    close_reason: str
    cooperative_close_attempts: int
    force_close_initiated: bool      # True if force_close_initiated_at is set
    # Peer pubkey + alias, looked up from LND's ClosedChannels +
    # GetNodeInfo at dashboard-build time. Best-effort: aliases are
    # populated when the peer is still in the gossip graph; closed-
    # channel peers can disappear from gossip, in which case alias is
    # None and the frontend renders "no name". peer_pubkey is None
    # only when the wallet backend can't return ClosedChannels (e.g.
    # Electrum wallets; the channel_point lookup yields nothing).
    peer_pubkey: Optional[str] = None
    peer_alias: Optional[str] = None


class LspOrderRow(BaseModel):
    """One row in the Recent LSP Orders table.

    Surfaces the full LSP-order lifecycle: provider, state, what we
    paid, what (if anything) we got refunded, the resulting channel
    if the order opened one, and the refund tx if the order failed.

    The Vue UI renders mempool.space links wherever a *_txid value
    is non-empty:
      - channel_funding_txid → https://mempool.space/tx/<txid>
      - refund_txid → https://mempool.space/tx/<txid>
    All txids are exposed in full (66-char hex); the UI does any
    truncation it wants for display while linking the full value."""
    timestamp: int                              # unix seconds (LspChannelOrder.created)
    iso_date: str
    provider: str                               # "zeus" / "megalithic" / ...
    order_id: str                               # full LSP-side order id
    short_order_id: str                         # 8-char head for display
    state: str                                  # ORDERED|PAID|COMPLETED|FAILED|EXPIRED
    lsps1_state: Optional[str]                  # last observed remote state
    paid_sats: int                              # gross we sent to the LSP
    paid_usd: Optional[float]
    refund_sats: int                            # observed on-chain (0 until verified)
    refund_usd: Optional[float]
    refund_observed_onchain: bool               # has the refund tx confirmed?
    # Net cost = paid - refund. Equals the gross when no refund (e.g.
    # COMPLETED orders); equals paid-refund_actual once the refund
    # lands and is reconciled. For an LSP that fails and only retains
    # the refund-tx miner fee, net_cost_sats will be tiny.
    net_cost_sats: int
    net_cost_usd: Optional[float]
    channel_funding_txid: Optional[str]         # from channel_point's "txid:vout"
    channel_point: Optional[str]                # full "txid:vout" for copy
    refund_txid: Optional[str]
    refund_onchain_address: Optional[str]
    age_hours: int


class CashoutDestination(BaseModel):
    """One rail's cashout method and destination. `method` is
    "lightning" or "onchain"; `destination` is the configured address
    (Lightning Address for LN, bech32/legacy/taproot for on-chain).
    The frontend truncates on-chain addresses for display; LN addresses
    are short enough to show verbatim."""
    method: str             # "lightning" | "onchain"
    destination: str        # full address; "" if unset


class CashoutSummary(BaseModel):
    """Resolved preferred + fallback cashout configuration.

    `primary` is the rail the engine will use first this tick;
    `fallback` is the other enabled rail (or None if only one is
    enabled). Selection rules (mirrors the engine's tick gates):
      - PREFER_LN_CASHOUT=True   → primary=LN (wins over PREFER_ONCHAIN)
      - PREFER_CASHOUT_ONCHAIN=True → primary=on-chain
      - Otherwise: whichever ENABLE_* is true; LN wins ties (the
        engine's default cashout-attempt order is LN-first).
    `fallback` is populated only when the *other* rail is also enabled
    AND has a destination configured. Otherwise it's None and the UI
    omits the parenthetical.
    """
    primary: Optional[CashoutDestination]   # None if no rail enabled
    fallback: Optional[CashoutDestination]  # None if only one enabled


class TopupDeficitRow(BaseModel):
    """One store-with-deficit entry shown in the top-up warning.

    `own_address` / `barebits_address` are the unlimited TOPUP_NAME and
    TOPUP_BAREBITS invoice addresses created by the worker's
    calculate_topups path. The barebits address is included only when
    DEBUG_MODE is enabled (the operator-pays path is the normal flow;
    the BareBits-pays path is debug-only). Empty string when not
    applicable so the frontend doesn't have to deal with undefined.
    """
    store_id: str
    store_name: str
    wallet_id: str
    wallet_name: str
    amount_sats: int        # deficit + 1000-sat engine buffer (matches calculate_topups)
    own_address: str
    barebits_address: str   # always "" unless debug_mode=True


class TopupWarning(BaseModel):
    """Dashboard top-up warning. Renders only when `rows` is non-empty.

    The warning is suppressed entirely when the worker hasn't yet
    created the corresponding unlimited top-up invoice for a deficit
    store — the dashboard endpoint is read-only and won't create
    invoices itself. In steady state the worker creates the invoice
    on its first tick after a deficit appears, so the worst-case
    staleness window is one tick interval (operator still gets the
    log warning + email in the meantime).
    """
    rows: List[TopupDeficitRow]


class HealthWarning(BaseModel):
    """One config-sanity or runtime-health warning shown on the
    dashboard. Each warning has a stable `id` that doubles as the
    log_decision key, so the same condition can be cross-referenced
    between the dashboard banner and decisions.log.

    Severity: "HIGH" = funds can be stuck / payments will silently fail;
    "MEDIUM" = a quieter footgun the operator should know about.
    """
    id: str           # stable, kebab-snake_case, used as log_decision key
    severity: str     # "HIGH" or "MEDIUM"
    category: str     # "cashout" | "channel" | "reserves" | "loop" | "smtp" | "ln_health"
    title: str        # short label for the dashboard banner
    message: str      # longer explanation including offending values
    # Config-setting names this warning references. Populated by each
    # _check_*_config helper so the Settings tab can highlight every
    # expansion-panel containing a setting that has an active warning.
    # Empty for runtime-only warnings (e.g. ln-cashout-failing) that
    # don't trace back to a specific setting.
    settings: List[str] = []


class DashboardResponse(BaseModel):
    """Top-level shape returned by GET /dashboard."""
    range: str
    btc_usd_rate: Optional[float]
    cc_baseline_pct: float
    # Bitcoin network the liquidityhelper wallets are on. Used by the
    # UI to construct mempool.space URLs with the right network prefix
    # (e.g. mempool.space/testnet4/tx/<txid>). One of:
    #   "mainnet" | "testnet" | "testnet4" | "signet" | "regtest" | ""
    # Empty string means we couldn't determine — UI then renders
    # txids/addresses as plain text without a link.
    bitcoin_network: str
    # LND readiness probe. False means at least one btclnd-backed
    # liquidityhelper wallet's LND daemon isn't responding to
    # /wallets/{id}/lndinfo yet (typical during the 5-10s window after
    # a bitcart container restart / rebuild). When False, the rest of
    # the payload is the empty skeleton — UI shows a "waiting for LND"
    # banner and auto-refreshes every 5s until lnd_ready flips True.
    # Always True for Electrum-only deployments (no LND to wait for).
    lnd_ready: bool = True
    # Short ids of the btclnd wallets whose LND wasn't reachable. Only
    # populated when lnd_ready is False. Lets the UI tell the operator
    # which wallets are still spinning up rather than a generic message.
    lnd_not_ready_wallets: List[str] = []
    # Engine-side debug-mode flag (config.DEBUG_MODE). Frontend uses
    # this to gate debug-only UI affordances — e.g. showing the
    # BareBits-pays top-up address alongside the operator-pays one.
    debug_mode: bool = False
    # Resolved preferred+fallback cashout method/destination, shown in
    # the dashboard header. None if config couldn't be read (rare —
    # _preferred_cashout_summary defaults to LN-mode on import errors).
    cashout_summary: Optional[CashoutSummary] = None
    # Per-store top-up deficits with paste-target addresses. Empty
    # rows list = no warning shown.
    topup_warning: Optional[TopupWarning] = None
    stores: List[StoreDashboard]
    summary: Optional[SummaryDashboard]    # None when only one store
    shared_wallet_warning: bool            # True if ≥2 stores share a wallet
    health_warnings: List[HealthWarning]   # config-sanity + runtime checks
    # Recent activity tables — capped at 100 rows so the response stays
    # small. The UI paginates 10/page client-side. Sorted newest-first.
    liquidity_stats: LiquidityStats
    recent_fee_payments: List[PaymentRow]
    recent_cashouts: List[PaymentRow]
    recent_channel_closures: List[ChannelClosureRow]
    recent_lsp_orders: List[LspOrderRow]
    recent_network_fees: List[NetworkFeeRow]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: Dict[str, Tuple[float, DashboardResponse]] = {}


def _cache_get(range_key: str) -> Optional[DashboardResponse]:
    entry = _cache.get(range_key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        _cache.pop(range_key, None)
        return None
    return value


def _cache_set(range_key: str, value: DashboardResponse) -> None:
    _cache[range_key] = (time.monotonic() + _DASHBOARD_CACHE_TTL_SEC, value)


def invalidate_cache() -> None:
    """Test-hook entry point. The dashboard endpoint's
    force_refresh=true query param skips (but does not clear) this
    cache; tests call this between assertions to drop everything."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Money helpers — all rendering goes through _money so zero/None are
# handled in exactly one place.
# ---------------------------------------------------------------------------

_SATS_PER_BTC = 100_000_000


def _money(sats: int, usd_rate: Optional[float]) -> _Money:
    """Build a _Money triple. Always positive ints in; USD computed
    only if rate is provided.

    Guards: negative sats are clamped to 0 (revenue/fees should never
    be negative on the dashboard — if upstream returns negatives that's
    an upstream bug and we want the UI to keep rendering)."""
    s = max(0, int(sats or 0))
    btc = s / _SATS_PER_BTC
    usd = (btc * usd_rate) if (usd_rate is not None) else None
    return _Money(sats=s, btc=btc, usd=usd)


def _safe_pct(numerator: int, denominator: int) -> Optional[float]:
    """numerator / denominator as a float, None when denominator <= 0.
    The UI renders None as '—' rather than NaN/Infinity."""
    if not denominator or denominator <= 0:
        return None
    return float(numerator) / float(denominator)


# ---------------------------------------------------------------------------
# Inbound liquidity — per-wallet
# ---------------------------------------------------------------------------

async def _get_wallet_liquidity_stats(
    wallet: Dict[str, Any], api: Any,
) -> Tuple[int, int, int, List[ChannelDetailRow]]:
    """Return (inbound_sats, outbound_sats, active_channel_count,
    channel_details) for `wallet`.

    For btclnd wallets we go straight to LND's `Lightning.ListChannels`
    via `lnd_rpc` — Bitcart's btclnd channel proxy doesn't always pass
    through `remote_balance`, so the direct path is the source of truth.
    For Electrum wallets we have no alternative and use Bitcart's
    `get_wallet_ln_channels`.

    The returned channel_details list contains one row per active
    channel with peer pubkey (alias left unresolved here — filled
    in by build_liquidity_stats in a single batched pass) and
    balance/capacity fields. Inactive channels are excluded from the
    totals AND the detail list so the operator's mental model
    (inbound = what can route right now) matches the rows below.

    Failure is silent: returns (0, 0, 0, []) on any error. The UI
    shows zeros rather than an error banner — the dashboard should
    still render if one wallet's LND is briefly unreachable.
    """
    wallet_id = wallet.get("id")
    currency = wallet.get("currency")
    try:
        if currency == "btclnd":
            from liquidityhelper import lnd_rpc
            resp = await lnd_rpc(api, wallet_id, "ListChannels", {}, "Lightning")
            if not isinstance(resp, dict):
                return (0, 0, 0, [])
            inbound = 0
            outbound = 0
            count = 0
            details: List[ChannelDetailRow] = []
            for c in (resp.get("channels") or []):
                # `active` is a bool from LND. Treat missing as inactive
                # (defensive — older LND builds may have omitted it).
                if not c.get("active"):
                    continue
                local = int(c.get("local_balance") or 0)
                remote = int(c.get("remote_balance") or 0)
                inbound += remote
                outbound += local
                count += 1
                details.append(ChannelDetailRow(
                    channel_point=(c.get("channel_point") or ""),
                    peer_pubkey=(c.get("remote_pubkey") or "").lower() or None,
                    peer_alias=None,
                    local_balance=local,
                    remote_balance=remote,
                    capacity=local + remote,
                    is_active=True,
                ))
            details.sort(key=lambda d: d.capacity, reverse=True)
            return (inbound, outbound, count, details)
        # Electrum / btc path — Bitcart's API is the only source.
        channels = await api.get_wallet_ln_channels(
            wallet_id, active_only=True, online_only=False,
        )
        if not channels:
            return (0, 0, 0, [])
        inbound = 0
        outbound = 0
        details = []
        for c in channels:
            local = int(float(c.get("local_balance") or 0))
            remote = int(float(c.get("remote_balance") or 0))
            inbound += remote
            outbound += local
            # Electrum exposes remote pubkey under several names across
            # versions; try the common ones in order.
            pk = (
                c.get("remote_pubkey")
                or c.get("node_id")
                or c.get("remote_node_id")
                or ""
            )
            pk = pk.lower() if isinstance(pk, str) else ""
            details.append(ChannelDetailRow(
                channel_point=(c.get("channel_point") or c.get("funding_outpoint") or ""),
                peer_pubkey=pk or None,
                peer_alias=None,
                local_balance=local,
                remote_balance=remote,
                capacity=local + remote,
                # active_only=True above means every row here is
                # already gated to an OPEN state. Treat as active.
                is_active=True,
            ))
        details.sort(key=lambda d: d.capacity, reverse=True)
        return (inbound, outbound, len(channels), details)
    except Exception as e:
        logger.warning(
            f"liquidity-stats fetch failed for wallet {wallet_id} "
            f"(currency={currency}): {e} {traceback.format_exc()}"
        )
        return (0, 0, 0, [])


# Back-compat alias so existing call sites (StoreCard inbound row,
# tests) keep working without churn. New code should use the
# liquidity-stats helper above.
async def _get_inbound_liquidity(wallet: Dict[str, Any], api: Any) -> Tuple[int, int]:
    inbound, _outbound, count, _details = await _get_wallet_liquidity_stats(wallet, api)
    return (inbound, count)


# ---------------------------------------------------------------------------
# Recent activity helpers
# ---------------------------------------------------------------------------

_RECENT_ROW_CAP = 100   # backend caps; UI paginates 10/page within these.


def _iso(ts: int) -> str:
    """Unix seconds → '2026-05-20 14:32:01' for UI display.

    Returns '—' for ts == 0 (unknown timestamp from history rows that
    didn't carry one). The dashboard sorts by `timestamp` numerically,
    so a 0 falls to the bottom regardless of how it renders.
    """
    if not ts:
        return "—"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "—"


def _destination_for_label(label: str) -> str:
    """Map a payment label to its CURRENT configured destination.

    We don't record historical destinations at send time, so this
    shows where TODAY's config would route a payment of the same type.
    The UI footnotes the table to make that semantics clear.
    """
    # Lazy import: config is heavy and we only need it on demand.
    try:
        import config as _cfg
    except Exception as e:
        logger.warning(f"_destination_for_label: config import failed: {e} {traceback.format_exc()}")
        return ""
    # Note: config knobs can be None (unset) — `getattr(default)` only
    # fires when the attr is MISSING, not None. Wrap each lookup with
    # `or ""` so None coalesces to an empty string the Pydantic
    # PaymentRow.destination string field accepts.
    name = (label or "").strip()
    if name == _cfg.FEE_PAYOUT_REASON:
        return (
            (getattr(_cfg, "LN_FEE_DEST", "") or "")
            or (getattr(_cfg, "ONCHAIN_FEE_DEST", "") or "")
        )
    if name == _cfg.REFERRAL_PAYOUT_REASON:
        return (
            (getattr(_cfg, "REFERRAL_FEE_DEST", "") or "")
            or (getattr(_cfg, "REFERRAL_ONCHAIN_DEST", "") or "")
        )
    if name == _cfg.CASHOUT_REASON:
        return getattr(_cfg, "CASHOUT_LIGHTNING_ADDRESS", "") or ""
    return ""


def _payment_row(
    *, timestamp: int, amount_sats: int, fee_sats: int, fee_type: str,
    method: str, destination: str, txid: str = "", payment_hash: str = "",
    usd_rate: Optional[float],
) -> PaymentRow:
    """Build one row + compute USD lazily from the rate."""
    amount_usd = (amount_sats / _SATS_PER_BTC * usd_rate) if usd_rate else None
    fee_usd = (fee_sats / _SATS_PER_BTC * usd_rate) if usd_rate else None
    return PaymentRow(
        timestamp=timestamp,
        iso_date=_iso(timestamp),
        amount_sats=amount_sats,
        amount_usd=amount_usd,
        fee_sats=fee_sats,
        fee_usd=fee_usd,
        destination=destination,
        fee_type=fee_type,
        method=method,
        txid=txid,
        payment_hash=payment_hash,
    )


async def _gather_payment_rows(
    api: Any,
    *,
    labels_to_type: Dict[str, str],
    method_overrides: Optional[Dict[str, str]] = None,
    usd_rate: Optional[float],
) -> List[PaymentRow]:
    """Walk every liquidityhelper wallet's on-chain + LN history and
    project rows that match any of the `labels_to_type` keys into
    PaymentRow records.

    `labels_to_type`: maps the label string (e.g. FEE_PAYOUT_REASON)
    to the user-visible category (e.g. "developer"). Caller decides
    what set of labels to surface — fee payments table uses dev+hosting,
    cashouts table uses CASHOUT_REASON only.

    `method_overrides`: optional map from the same label key to a
    method string that overrides the default ("onchain" for on-chain
    txs / "lightning" for LN payments). Used by the cashouts table to
    surface CASHOUT_DIRECT_CHANNEL_REASON channel-open txs with
    method="direct_channel" so the UI can render them distinctly from
    regular on-chain cashout sweeps.

    Suffix handling: some labels carry an inline `:<suffix>` payload
    (the direct-channel label embeds the destination peer's pubkey
    there). We strip the suffix before looking up the label in
    `labels_to_type`, and surface the suffix as the destination column
    when present.
    """
    # Lazy import so this module remains importable without engine.
    from liquidityhelper import list_onchain_history, list_ln_payments_with_labels

    method_overrides = method_overrides or {}
    rows: List[PaymentRow] = []
    wallets = await api.get_wallets() or []
    for wallet in wallets:
        if wallet.get("name") != "liquidityhelper":
            continue
        # On-chain side
        try:
            onchain = await list_onchain_history(wallet=wallet, api=api)
        except Exception as e:
            logger.warning(
                f"recent payments: list_onchain_history failed for "
                f"wallet {wallet.get('id')}: {e} {traceback.format_exc()}"
            )
            onchain = []
        for tx in onchain:
            raw_label = (tx.get("label") or "").strip()
            # `<base>:<suffix>` labels carry an inline payload (the
            # direct-channel cashout embeds the peer pubkey there).
            # Split for lookup; preserve suffix for the destination column.
            base_label, _, suffix = raw_label.partition(":")
            ftype = labels_to_type.get(base_label)
            if ftype is None:
                continue
            if tx.get("incoming"):
                continue   # only outgoing payments count
            method = method_overrides.get(base_label, "onchain")
            destination = (
                suffix
                or tx.get("dest_address")
                or _destination_for_label(base_label)
            )
            rows.append(_payment_row(
                timestamp=int(tx.get("timestamp") or 0),
                amount_sats=int(abs(float(tx.get("amount_sat") or 0))),
                fee_sats=int(abs(float(tx.get("fee_sat") or 0))),
                fee_type=ftype,
                method=method,
                destination=destination,
                txid=tx.get("txid") or "",
                usd_rate=usd_rate,
            ))
        # LN side
        try:
            ln_rows = await list_ln_payments_with_labels(wallet=wallet, api=api)
        except Exception as e:
            logger.warning(
                f"recent payments: list_ln_payments_with_labels failed "
                f"for wallet {wallet.get('id')}: {e} {traceback.format_exc()}"
            )
            ln_rows = []
        for ln in ln_rows:
            raw_label = (ln.get("label") or "").strip()
            # `<base>:<suffix>` labels carry an inline payload. For LN
            # payments the suffix carries the destination payload
            # (e.g. peer pubkey on CASHOUT_DIRECT_CHANNEL_REASON rows).
            # _destination_for_label has no entry for that label, so
            # suffix is the only source of a destination for those rows.
            base_label, _, suffix = raw_label.partition(":")
            ftype = labels_to_type.get(base_label)
            if ftype is None:
                continue
            amount_msat = int(ln.get("amount_msat") or 0)
            if amount_msat >= 0:
                continue   # incoming
            destination = suffix or _destination_for_label(base_label)
            rows.append(_payment_row(
                timestamp=int(ln.get("timestamp") or 0),
                amount_sats=abs(amount_msat) // 1000,
                fee_sats=int(ln.get("fee_msat") or 0) // 1000,
                fee_type=ftype,
                method="lightning",
                destination=destination,
                payment_hash=ln.get("payment_hash") or "",
                usd_rate=usd_rate,
            ))

    # Newest first; cap.
    rows.sort(key=lambda r: r.timestamp, reverse=True)
    return rows[:_RECENT_ROW_CAP]


async def list_recent_fee_payments(
    api: Any, usd_rate: Optional[float],
) -> List[PaymentRow]:
    """Recent developer-fee + hosting/referral-fee payments combined.
    Sorted newest first. Capped at 100."""
    import config as _cfg
    return await _gather_payment_rows(
        api,
        labels_to_type={
            _cfg.FEE_PAYOUT_REASON: "developer",
            _cfg.REFERRAL_PAYOUT_REASON: "hosting",
        },
        usd_rate=usd_rate,
    )


async def list_recent_cashouts(
    api: Any, usd_rate: Optional[float],
) -> List[PaymentRow]:
    """Recent cashout payments. Sorted newest first. Capped at 100.

    Surfaces two label families:
      - CASHOUT_REASON ("lnhelper_cashout"): the original cashout
        rails (LN payment to a Lightning Address or on-chain payment
        to a Bitcoin address).
      - CASHOUT_DIRECT_CHANNEL_REASON ("lnhelper_cashout_direct"):
        PREFER_LN_CASHOUT direct-channel-push cashouts. These are
        channel-open txs with push_sat set so the cashout amount lands
        on the operator's own LN node in one atomic op. Method is
        marked "direct_channel" so the dashboard renders them
        distinctly from regular on-chain cashouts; the peer pubkey
        comes from the label's `:<suffix>` payload.
    """
    import config as _cfg
    return await _gather_payment_rows(
        api,
        labels_to_type={
            _cfg.CASHOUT_REASON: "cashout",
            _cfg.CASHOUT_DIRECT_CHANNEL_REASON: "cashout",
        },
        method_overrides={
            _cfg.CASHOUT_DIRECT_CHANNEL_REASON: "direct_channel",
        },
        usd_rate=usd_rate,
    )


async def _check_lnd_ready(api: Any) -> Tuple[bool, List[str]]:
    """Probe each btclnd-backed liquidityhelper wallet's lndinfo
    endpoint. Returns (ready, list_of_wallet_short_ids_not_ready).

    Called early in compute_dashboard so a freshly-restarted bitcart
    container (LND daemon still spinning up, ~5-10s window after a
    restart) doesn't 500 the entire dashboard endpoint. When at least
    one btclnd wallet's LND is unreachable we return a "not ready"
    skeleton; the UI shows a banner and auto-refreshes every 5s.

    Electrum-only deployments (no btclnd wallets) trivially return
    ready=True — nothing to wait for.

    Defensive: any unexpected exception during the probe is treated
    as not-ready (better to show a banner than to 500). The probe
    itself is cheap — get_lnd_info is cached per BitcartAPI instance,
    so subsequent calls in the same compute_dashboard invocation hit
    the cache.
    """
    not_ready: List[str] = []
    try:
        wallets = await api.get_wallets() or []
    except Exception as e:
        logger.warning(
            f"_check_lnd_ready: get_wallets failed: {e} {traceback.format_exc()}"
        )
        return (False, ["<wallet list unavailable>"])
    for w in wallets:
        if w.get("name") != "liquidityhelper":
            continue
        if w.get("currency") != "btclnd":
            continue
        wid = w.get("id") or ""
        try:
            info = await api.get_lnd_info(wid)
        except Exception as e:
            logger.warning(
                f"_check_lnd_ready: get_lnd_info raised for wallet {wid}: "
                f"{e} {traceback.format_exc()}"
            )
            not_ready.append(wid[:8] or "<unknown>")
            continue
        if not info:
            not_ready.append(wid[:8] or "<unknown>")
    return (len(not_ready) == 0, not_ready)


async def _detect_bitcoin_network(api: Any) -> str:
    """Best-effort detection of the Bitcoin network the liquidityhelper
    wallets are on. Walks wallets in get_wallets order, returns the
    first network found from a btclnd wallet via api.get_lnd_info; for
    Electrum-only deployments falls back to "". Empty string signals
    "unknown" to the UI, which then renders txids/addresses as plain
    text without a mempool.space link.

    Normalizes 'testnet3' → 'testnet' to match the LSP-network
    convention; preserves 'testnet4', 'signet', 'regtest', 'mainnet'
    as-is so the UI can pick the right mempool subdomain.
    """
    try:
        wallets = await api.get_wallets() or []
    except Exception as e:
        logger.warning(f"_detect_bitcoin_network: get_wallets failed: {e} {traceback.format_exc()}")
        return ""
    for w in wallets:
        if w.get("name") != "liquidityhelper":
            continue
        if w.get("currency") != "btclnd":
            continue
        try:
            info = await api.get_lnd_info(w["id"])
        except Exception as e:
            logger.warning(
                f"_detect_bitcoin_network: get_lnd_info failed for "
                f"wallet {w.get('id')}: {e} {traceback.format_exc()}"
            )
            continue
        if not info:
            continue
        raw = (info.get("network") or "").lower()
        if raw == "testnet3":
            return "testnet"
        if raw in ("mainnet", "testnet", "testnet4", "signet", "regtest"):
            return raw
    return ""


def _liquidity_mode_label() -> str:
    """Human-readable label for the current liquidity-management mode.

    Reads LIQUIDITY_DISABLED + AUTOMATIC_CHANNEL_CREATION_ENABLED from
    config. Mirrors the engine gates at liquidityhelper.run_tick_loop
    (paused when LIQUIDITY_DISABLED) and move_onchain_to_ln (automatic
    vs LSP). Defaults to the LSP label on any config-read failure so
    the UI still renders.
    """
    try:
        import config as _cfg
        if bool(getattr(_cfg, "LIQUIDITY_DISABLED", False)):
            return "Disabled (tick loop paused)"
        if bool(getattr(_cfg, "AUTOMATIC_CHANNEL_CREATION_ENABLED", False)):
            return "Automatic channel management"
        return "LSP-managed liquidity"
    except Exception as e:
        logger.warning(f"_liquidity_mode_label: config import failed: {e} {traceback.format_exc()}")
        return "LSP-managed liquidity"


async def _build_lsp_pubkey_map(network: str) -> Dict[str, str]:
    """Return {lowercase_pubkey: provider_name} for every URI advertised
    by the configured LSP providers on `network`.

    Combines the hardcoded fallback pubkeys from `network_endpoints`
    with the dynamic URIs each provider exposes via `get_all_peer_uris`
    (which itself caches `get_info().uris`). Best-effort: any provider
    that raises or times out contributes only its static fallback.

    Used by the dashboard to tag channels in the liquidity-stats panel
    with the originating LSP. `network` of "" / regtest / unknown
    returns an empty map — providers don't serve those.
    """
    if not network:
        return {}
    out: Dict[str, str] = {}
    try:
        from lsp_providers import get_lsp_providers
    except Exception as e:
        logger.debug(f"_build_lsp_pubkey_map: lsp_providers import failed: {e}")
        return {}

    def _record(uri: str, name: str) -> None:
        # URI format is "pubkey@host:port"; we only care about the pubkey.
        # Skip the UNKNOWN sentinel and any malformed entries.
        if not uri or "@" not in uri or uri.startswith("UNKNOWN@"):
            return
        pk = uri.split("@", 1)[0].strip().lower()
        if len(pk) == 66:  # 33-byte compressed pubkey, hex-encoded
            out.setdefault(pk, name)

    for provider in get_lsp_providers():
        name = getattr(provider, "name", "lsp")
        endpoint = getattr(provider, "network_endpoints", {}).get(network)
        if endpoint is not None:
            _record(getattr(endpoint, "lsp_peer_uri", ""), name)
        # Dynamic — short timeout so a slow LSP can't stall dashboard
        # render. get_all_peer_uris is per-provider-instance cached;
        # the timeout only matters on the very first call.
        try:
            uris = await asyncio.wait_for(
                provider.get_all_peer_uris(network=network), timeout=2.0,
            )
            for u in uris or []:
                _record(u, name)
        except Exception as e:
            logger.debug(
                f"_build_lsp_pubkey_map: {name} get_all_peer_uris "
                f"on {network} skipped: {e}"
            )
    return out


def _preferred_cashout_summary() -> Optional[CashoutSummary]:
    """Resolve which rail the engine prefers right now and what the
    other enabled rail (if any) is.

    Selection mirrors the engine's gates at the tick layer:
      - PREFER_LN_CASHOUT wins over PREFER_CASHOUT_ONCHAIN when both
        are True (see PREFER_CASHOUT_ONCHAIN docstring in config.py).
      - With no PREFER_* flag set, the first available ENABLE_* rail
        is primary; LN wins ties because that's the engine's
        default cashout-attempt order. The "other enabled+addressed"
        rail (if any) becomes the fallback.
      - A rail with ENABLE_*=True but its destination unset is
        treated as not configured (health warnings H1/H2 catch this
        separately); we don't surface a useless primary/fallback that
        would never actually fire.

    Returns None only if config can't be imported at all (extremely
    rare — every other dashboard helper would already be failing).
    """
    try:
        import config as _cfg
    except Exception as e:
        logger.warning(f"_preferred_cashout_summary: config import failed: {e} {traceback.format_exc()}")
        return None

    ln_enabled = bool(getattr(_cfg, "ENABLE_CASHOUT_LN", False))
    onchain_enabled = bool(getattr(_cfg, "ENABLE_CASHOUT_ONCHAIN", False))
    prefer_ln = bool(getattr(_cfg, "PREFER_LN_CASHOUT", False))
    prefer_onchain = bool(getattr(_cfg, "PREFER_CASHOUT_ONCHAIN", False))
    ln_addr = getattr(_cfg, "CASHOUT_LIGHTNING_ADDRESS", None) or ""
    onchain_addr = getattr(_cfg, "CASHOUT_ONCHAIN", None) or ""

    ln_rail = CashoutDestination(method="lightning", destination=ln_addr) if ln_enabled and ln_addr else None
    onchain_rail = CashoutDestination(method="onchain", destination=onchain_addr) if onchain_enabled and onchain_addr else None

    # Pick primary based on PREFER_* with the documented tiebreaker;
    # fallback is whichever of the remaining rails is also usable.
    if prefer_ln and ln_rail:
        return CashoutSummary(primary=ln_rail, fallback=onchain_rail)
    if prefer_onchain and onchain_rail:
        return CashoutSummary(primary=onchain_rail, fallback=ln_rail)
    # No PREFER_* override (or the preferred rail isn't usable): LN
    # wins ties; whichever is missing becomes the fallback slot.
    if ln_rail and onchain_rail:
        return CashoutSummary(primary=ln_rail, fallback=onchain_rail)
    if ln_rail:
        return CashoutSummary(primary=ln_rail, fallback=None)
    if onchain_rail:
        return CashoutSummary(primary=onchain_rail, fallback=None)
    # No rail is both enabled AND addressed. Health warnings already
    # flag this; return an empty summary so the header simply omits
    # the "preferred cashout" line.
    return CashoutSummary(primary=None, fallback=None)


def _engine_debug_mode() -> bool:
    """Whether the engine has DEBUG_MODE=True right now. Read via the
    engine module (where the live, refresh_settings_from_bitcart-
    updated globals live) — config.DEBUG_MODE is the import-time
    default and wouldn't reflect settings changes."""
    try:
        import liquidityhelper as _engine
        return bool(getattr(_engine, "DEBUG_MODE", False))
    except Exception as e:
        logger.warning(f"_engine_debug_mode: import failed: {e} {traceback.format_exc()}")
        return False


async def _compute_topup_warning(api: Any) -> TopupWarning:
    """Build the per-store top-up deficit list shown above
    liquidity_stats on the dashboard.

    For each store the engine considers, check whether its best LN
    wallet's on-chain balance is below the reserve floor returned by
    topup_goal_amount(). When it is AND the worker has already
    created the corresponding unlimited TOPUP_NAME invoice, surface a
    row with the paste-target address(es) and the deficit amount
    (plus the same 1000-sat buffer calculate_topups adds, so the
    operator pays exactly what the worker would otherwise solicit).

    Suppression rules:
      - Deficit <= AUTOMATIC_RESERVE_SAFETY_SAT: hysteresis zone — same
        threshold the worker uses to suppress the email warning;
        keeps the dashboard from flapping when the wallet drifts
        within fee-rounding distance of the floor.
      - No TOPUP_NAME invoice yet: the worker hasn't ticked since
        the deficit appeared. Skip the row entirely so the operator
        is never shown an "address: ???" entry. Comes back on the
        next tick.

    The TOPUP_BAREBITS address is included only when DEBUG_MODE is
    on (debug-only UI per operator preference).
    """
    rows: List[TopupDeficitRow] = []
    try:
        from liquidityhelper import (
            store_needs_topup, AUTOMATIC_RESERVE_SAFETY_SAT,
            TOPUP_NAME, TOPUP_BAREBITS,
            btc_address_from_invoice,
        )
    except Exception as e:
        logger.warning(f"_compute_topup_warning: engine imports failed: {e} {traceback.format_exc()}")
        return TopupWarning(rows=[])

    debug_on = _engine_debug_mode()
    try:
        stores = await api.get_stores() or []
    except Exception as e:
        logger.warning(f"_compute_topup_warning: get_stores failed: {e} {traceback.format_exc()}")
        return TopupWarning(rows=[])

    for store in stores:
        try:
            deficit = await store_needs_topup(api, store["id"])
            if not deficit:
                continue
            if deficit <= AUTOMATIC_RESERVE_SAFETY_SAT:
                # Same hysteresis the engine applies to its own warning
                # path — keeps small fee-rounding deficits silent.
                continue
            own_invoice = await api.get_invoice_by_note(
                note=TOPUP_NAME, require_unlimited=True,
            )
            if not own_invoice:
                # Worker hasn't created the invoice yet; suppress this
                # store until it does. The engine's logger.warning /
                # email path still fires regardless.
                continue
            own_addr = btc_address_from_invoice(own_invoice) or ""
            if not own_addr:
                continue
            bb_addr = ""
            if debug_on:
                bb_invoice = await api.get_invoice_by_note(
                    note=TOPUP_BAREBITS, require_unlimited=True,
                )
                if bb_invoice:
                    bb_addr = btc_address_from_invoice(bb_invoice) or ""
            try:
                wallet = await api.get_best_ln_wallet_for_store(store)
            except Exception as e:
                logger.warning(f"_compute_topup_warning: wallet lookup failed for store {store.get('id')}: {e} {traceback.format_exc()}")
                wallet = {}
            rows.append(TopupDeficitRow(
                store_id=store.get("id") or "",
                store_name=store.get("name") or "",
                wallet_id=(wallet or {}).get("id") or "",
                wallet_name=(wallet or {}).get("name") or "",
                # Matches the +1000 sat buffer in calculate_topups so
                # the address the operator pastes accepts the exact
                # amount the worker would otherwise ask for.
                amount_sats=int(deficit) + 1000,
                own_address=own_addr,
                barebits_address=bb_addr,
            ))
        except Exception as e:
            logger.warning(f"_compute_topup_warning: store {store.get('id')} failed: {e} {traceback.format_exc()}")
            continue
    return TopupWarning(rows=rows)


async def _resolve_aliases_for_pubkeys(
    api: Any, pubkeys: List[str],
) -> Dict[str, Optional[str]]:
    """Batched GetNodeInfo for a list of peer pubkeys.

    Dedupes the input, parallelizes the RPC calls, and returns
    {pubkey → alias_or_None}. Best-effort: peers not in the gossip
    graph get None (UI then renders "no name") instead of raising
    or omitting the entry.

    Requires at least one btclnd wallet to route GetNodeInfo
    through. If no btclnd wallet is configured, returns an empty
    dict — every alias falls back to None and the UI renders the
    pubkey-only form (which is still the right thing for an
    Electrum-only operator who can't query LND gossip anyway).
    """
    unique = sorted(set(pk for pk in pubkeys if pk))
    if not unique:
        return {}
    try:
        from liquidityhelper import lnd_rpc
    except Exception as e:
        logger.warning(f"_resolve_aliases_for_pubkeys: lnd_rpc import failed: {e} {traceback.format_exc()}")
        return {}
    try:
        wallets = await api.get_wallets() or []
    except Exception as e:
        logger.warning(f"_resolve_aliases_for_pubkeys: get_wallets failed: {e} {traceback.format_exc()}")
        return {}
    btclnd_wid: Optional[str] = None
    for w in wallets:
        if (w.get("currency") or "").lower() == "btclnd":
            btclnd_wid = w.get("id")
            if btclnd_wid:
                break
    if btclnd_wid is None:
        return {pk: None for pk in unique}

    import asyncio
    async def _one(pk: str) -> Tuple[str, Optional[str]]:
        try:
            info = await lnd_rpc(api, btclnd_wid, "GetNodeInfo", {"pub_key": pk}, "Lightning") or {}
        except Exception as e:
            logger.debug(f"_resolve_aliases_for_pubkeys: GetNodeInfo({pk[-8:]}) failed: {e}")
            return pk, None
        node = info.get("node") if isinstance(info, dict) else None
        alias = None
        if isinstance(node, dict):
            raw = node.get("alias")
            if isinstance(raw, str) and raw.strip():
                alias = "".join(ch for ch in raw if ord(ch) >= 0x20)[:32].strip() or None
        return pk, alias

    pairs = await asyncio.gather(*(_one(pk) for pk in unique), return_exceptions=False)
    return dict(pairs)


async def build_liquidity_stats(
    api: Any, btc_usd_rate: Optional[float],
    wallet_to_store_names: Optional[Dict[str, List[str]]] = None,
    bitcoin_network: str = "",
) -> LiquidityStats:
    """Walk every liquidityhelper-named wallet, collect per-wallet
    inbound/outbound/channel-count stats + per-channel breakdown,
    and sum into the totals row.

    Wallet uniqueness: keys on wallet_id. Sorting: by wallet_id so the
    order is stable across renders (operators dislike rows reshuffling
    between refreshes).

    `wallet_to_store_names` maps wallet_id → [store_name, ...] for
    every store whose best LN wallet currently resolves to that
    wallet. Built upstream in compute_dashboard so we don't have to
    re-walk every store here. Missing wallet ids resolve to [].
    """
    mode = _liquidity_mode_label()
    rows: List[WalletLiquidityRow] = []
    total_inbound = 0
    total_outbound = 0
    total_channels = 0
    name_map = wallet_to_store_names or {}

    try:
        wallets = await api.get_wallets() or []
    except Exception as e:
        logger.warning(
            f"build_liquidity_stats: get_wallets failed: {e} {traceback.format_exc()}"
        )
        wallets = []

    # Filter to liquidityhelper-named wallets only. Same rule the rest
    # of the dashboard uses to decide what's billable / relevant.
    lh_wallets = [w for w in wallets if w.get("name") == "liquidityhelper"]
    # Dedupe by id (defensive — get_wallets shouldn't return dupes but
    # we don't enforce it).
    seen: set = set()
    pending: List[Tuple[str, str, int, int, int, List[ChannelDetailRow]]] = []
    for w in sorted(lh_wallets, key=lambda x: x.get("id") or ""):
        wid = w.get("id") or ""
        if not wid or wid in seen:
            continue
        seen.add(wid)
        inbound, outbound, count, details = await _get_wallet_liquidity_stats(w, api)
        pending.append((wid, w.get("name") or "", inbound, outbound, count, details))
        total_inbound += inbound
        total_outbound += outbound
        total_channels += count

    # One batched alias lookup for every unique peer across every
    # wallet's active channels. Saves N round-trips when the same
    # peer shows up on multiple channels (typical when an LSP funds
    # several channels to the same peer over time).
    all_pubkeys = [
        d.peer_pubkey
        for _wid, _wname, _i, _o, _c, ds in pending
        for d in ds
        if d.peer_pubkey
    ]
    alias_map = await _resolve_aliases_for_pubkeys(api, all_pubkeys)

    for wid, wname, inbound, outbound, count, details in pending:
        # Attach aliases. Pubkeys not in alias_map (or mapped to None)
        # stay alias=None and render as "no name" in the UI.
        enriched = [
            ChannelDetailRow(
                channel_point=d.channel_point,
                peer_pubkey=d.peer_pubkey,
                peer_alias=alias_map.get(d.peer_pubkey) if d.peer_pubkey else None,
                local_balance=d.local_balance,
                remote_balance=d.remote_balance,
                capacity=d.capacity,
                is_active=d.is_active,
            )
            for d in details
        ]
        rows.append(WalletLiquidityRow(
            wallet_id=wid,
            wallet_short=wid[:8],
            wallet_name=wname,
            inbound=_money(inbound, btc_usd_rate),
            outbound=_money(outbound, btc_usd_rate),
            active_channel_count=count,
            channels=enriched,
            store_names=sorted(name_map.get(wid, [])),
        ))

    lsp_provider_pubkeys = await _build_lsp_pubkey_map(bitcoin_network)

    return LiquidityStats(
        mode=mode,
        wallets=rows,
        total_inbound=_money(total_inbound, btc_usd_rate),
        total_outbound=_money(total_outbound, btc_usd_rate),
        total_channel_count=total_channels,
        lsp_provider_pubkeys=lsp_provider_pubkeys,
    )


def _label_to_network_fee_category(label: str) -> Optional[str]:
    """Map a tx/payment label to the user-visible network-fee category.

    Categories surface what KIND of payment incurred the fee, not the
    fee itself. None means "ignore this row" entirely — used for LN
    payments with no label that have a positive (incoming) amount or
    no associated fee. For outgoing on-chain transactions we always
    return a category so every fee-incurring tx is accounted for; an
    `'external'` or blank label maps to "external_send" (operator-
    initiated manual sends, anchor sweeps, etc.).
    """
    try:
        import config as _cfg
    except Exception as e:
        logger.warning(f"_label_to_network_fee_category: config import failed: {e} {traceback.format_exc()}")
        return None
    name = (label or "").strip()
    # Labels of the form `<base>:<suffix>` carry an inline payload
    # (peer pubkey, lsp order id, …). Match on the base.
    base = name.partition(":")[0]
    mapping = {
        _cfg.FEE_PAYOUT_REASON: "developer_fee",
        _cfg.REFERRAL_PAYOUT_REASON: "hosting_fee",
        _cfg.CASHOUT_REASON: "cashout",
        _cfg.CASHOUT_DIRECT_CHANNEL_REASON: "cashout",
        getattr(_cfg, "REBALANCE_REASON", "lnhelper_rebalance"): "rebalance",
        "OPEN CHANNEL": "channel_open",
        "CLOSE CHANNEL": "channel_close",
        "lsp_channel_order": "lsp_order",
    }
    if base in mapping:
        return mapping[base]
    # Fallback: any other label (including the common LND `'external'`
    # and blank labels) is an external/manual outgoing tx. Bucket it
    # so the Recent network fees table sums to the dashboard's
    # network_fees_total instead of silently dropping these rows.
    return "external_send"


async def list_recent_network_fees(
    api: Any, usd_rate: Optional[float],
) -> List[NetworkFeeRow]:
    """Recent network-fee-incurring transactions across all
    liquidityhelper wallets. Sorted newest first. Capped at 100.

    Includes on-chain miner fees (developer/hosting/cashout/channel
    opens/closes/LSP orders) and Lightning routing fees (LN payouts,
    LN fee/referral payments). Rows with fee_sat <= 0 are skipped —
    the table only surfaces events where we actually paid a fee, so
    zero-fee txs (free same-bank-day LN payments, accelerated 0-sat
    closes, etc.) don't clutter the view.

    Channel-close txs ARE included even though they're net-inbound to
    the wallet (channel balance returns to us). The "outgoing-only"
    filter the other tables apply doesn't fit here because we still
    paid the miner fee on the close even when the net was inbound.
    """
    from liquidityhelper import list_onchain_history, list_ln_payments_with_labels

    rows: List[NetworkFeeRow] = []
    wallets = await api.get_wallets() or []
    for wallet in wallets:
        if wallet.get("name") != "liquidityhelper":
            continue
        # On-chain side. Walk every tx; only emit rows where the label
        # matches AND fee_sat > 0. We do NOT filter on tx.incoming
        # because channel-close txs are inbound but still incurred a
        # miner fee we want to surface.
        try:
            onchain = await list_onchain_history(wallet=wallet, api=api)
        except Exception as e:
            logger.warning(
                f"recent network fees: list_onchain_history failed for "
                f"wallet {wallet.get('id')}: {e} {traceback.format_exc()}"
            )
            onchain = []
        for tx in onchain:
            raw_label = (tx.get("label") or "").strip()
            category = _label_to_network_fee_category(raw_label)
            if category is None:
                continue
            fee_sats = int(abs(float(tx.get("fee_sat") or 0)))
            if fee_sats <= 0:
                continue
            base_label, _, suffix = raw_label.partition(":")
            destination = (
                suffix
                or tx.get("dest_address")
                or _destination_for_label(base_label)
                or ""
            )
            ts = int(tx.get("timestamp") or 0)
            # fee_rate_sat_per_vbyte is populated by
            # _lnd_list_onchain_history; absent (None) for Electrum-
            # source rows. Round to 2 decimals — operators read this
            # like sat/vB on mempool.space, no point in more precision.
            raw_rate = tx.get("fee_rate_sat_per_vbyte")
            fee_rate_for_row: Optional[float] = (
                round(float(raw_rate), 2)
                if raw_rate is not None
                else None
            )
            rows.append(NetworkFeeRow(
                timestamp=ts,
                iso_date=_iso(ts),
                category=category,
                fee_sats=fee_sats,
                fee_usd=(fee_sats / _SATS_PER_BTC * usd_rate) if usd_rate else None,
                amount_sats=int(abs(float(tx.get("amount_sat") or 0))),
                amount_usd=(int(abs(float(tx.get("amount_sat") or 0))) / _SATS_PER_BTC * usd_rate) if usd_rate else None,
                method="onchain",
                txid=tx.get("txid") or "",
                payment_hash="",
                destination=destination,
                fee_rate_sat_per_vbyte=fee_rate_for_row,
            ))
        # LN side. Only outgoing payments incur routing fees from our
        # side; incoming forwards/receives don't cost us anything.
        try:
            ln_rows = await list_ln_payments_with_labels(wallet=wallet, api=api)
        except Exception as e:
            logger.warning(
                f"recent network fees: list_ln_payments_with_labels failed "
                f"for wallet {wallet.get('id')}: {e} {traceback.format_exc()}"
            )
            ln_rows = []
        for ln in ln_rows:
            raw_label = (ln.get("label") or "").strip()
            category = _label_to_network_fee_category(raw_label)
            if category is None:
                continue
            amount_msat = int(ln.get("amount_msat") or 0)
            if amount_msat >= 0:
                continue   # incoming forwards/receives don't cost us routing fees
            fee_msat = int(ln.get("fee_msat") or 0)
            fee_sats = abs(fee_msat) // 1000
            if fee_sats <= 0:
                continue
            base_label, _, suffix = raw_label.partition(":")
            amount_sats = abs(amount_msat) // 1000
            ts = int(ln.get("timestamp") or 0)
            rows.append(NetworkFeeRow(
                timestamp=ts,
                iso_date=_iso(ts),
                category=category,
                fee_sats=fee_sats,
                fee_usd=(fee_sats / _SATS_PER_BTC * usd_rate) if usd_rate else None,
                amount_sats=amount_sats,
                amount_usd=(amount_sats / _SATS_PER_BTC * usd_rate) if usd_rate else None,
                method="lightning",
                txid="",
                payment_hash=ln.get("payment_hash") or "",
                destination=suffix or _destination_for_label(base_label) or "",
            ))

    rows.sort(key=lambda r: r.timestamp, reverse=True)
    return rows[:_RECENT_ROW_CAP]


async def _resolve_closed_channel_peers(
    api: Any,
) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """Build {channel_point → (peer_pubkey, peer_alias)} for every
    closed channel the connected btclnd wallets know about.

    Strategy:
      - Walk every wallet; for each btclnd one, call ClosedChannels
        and harvest `channel_point → remote_pubkey`. Dedupe pubkeys
        across wallets.
      - For each unique pubkey, call GetNodeInfo and capture
        `node.alias`. Closed-channel peers commonly disappear from
        the gossip graph (both sides drop the announcement), so
        GetNodeInfo legitimately returns NotFound; we record alias
        as None in that case and the UI renders "no name".

    Best-effort throughout: any RPC failure is logged at WARNING
    and the affected entries are simply missing from the returned
    map. The closures table then renders peer_pubkey=None /
    peer_alias=None for those rows, which the UI shows as "—".
    """
    point_to_pubkey: Dict[str, str] = {}
    try:
        wallets = await api.get_wallets() or []
    except Exception as e:
        logger.warning(f"_resolve_closed_channel_peers: get_wallets failed: {e} {traceback.format_exc()}")
        return {}

    # Lazy import — same pattern other dashboard helpers use to avoid
    # a hard module-load dependency on the engine.
    try:
        from liquidityhelper import lnd_rpc
    except Exception as e:
        logger.warning(f"_resolve_closed_channel_peers: lnd_rpc import failed: {e} {traceback.format_exc()}")
        return {}

    for w in wallets:
        if (w.get("currency") or "").lower() != "btclnd":
            # Electrum wallets don't expose a ClosedChannels-equivalent
            # surface — UI will fall back to "—" for their closures.
            continue
        wid = w.get("id") or ""
        if not wid:
            continue
        try:
            resp = await lnd_rpc(api, wid, "ClosedChannels", {}, "Lightning") or {}
        except Exception as e:
            logger.warning(f"_resolve_closed_channel_peers: ClosedChannels failed for wallet {wid}: {e} {traceback.format_exc()}")
            continue
        for c in (resp.get("channels") or []):
            cp = (c.get("channel_point") or "").lower()
            pk = (c.get("remote_pubkey") or "").lower()
            if cp and pk:
                # Don't overwrite a prior entry — same channel_point
                # showing up on two wallets would be very unusual but
                # if it does we keep the first reading.
                point_to_pubkey.setdefault(cp, pk)

    if not point_to_pubkey:
        return {}

    # Dedupe pubkeys → one GetNodeInfo each; run in parallel.
    import asyncio
    unique_pubkeys = sorted(set(point_to_pubkey.values()))
    btclnd_wid: Optional[str] = None
    for w in wallets:
        if (w.get("currency") or "").lower() == "btclnd":
            btclnd_wid = w.get("id")
            if btclnd_wid:
                break
    if btclnd_wid is None:
        # We had ClosedChannels rows but no usable btclnd wallet to
        # query GetNodeInfo against; return what we have without aliases.
        return {cp: (pk, None) for cp, pk in point_to_pubkey.items()}

    async def _alias_for(pk: str) -> Tuple[str, Optional[str]]:
        try:
            info = await lnd_rpc(
                api, btclnd_wid, "GetNodeInfo",
                {"pub_key": pk}, "Lightning",
            ) or {}
        except Exception as e:
            logger.debug(f"_resolve_closed_channel_peers: GetNodeInfo({pk[-8:]}) failed: {e}")
            return pk, None
        # LND's GetNodeInfo returns {"node": {"alias": "...", ...}}
        # for known nodes. For unknown (closed-channel-only) peers it
        # returns an error, which lnd_rpc surfaces as either an
        # exception (caught above) or an empty/error dict.
        node = info.get("node") if isinstance(info, dict) else None
        alias = None
        if isinstance(node, dict):
            raw_alias = node.get("alias")
            if isinstance(raw_alias, str) and raw_alias.strip():
                # Strip control chars and cap length consistently with
                # what lnd_graph_pull.parse_alias does for stored
                # aliases (LN spec caps the alias at 32 bytes).
                alias = "".join(ch for ch in raw_alias if ord(ch) >= 0x20)[:32].strip() or None
        return pk, alias

    alias_pairs = await asyncio.gather(
        *(_alias_for(pk) for pk in unique_pubkeys),
        return_exceptions=False,
    )
    pubkey_to_alias: Dict[str, Optional[str]] = dict(alias_pairs)
    return {cp: (pk, pubkey_to_alias.get(pk)) for cp, pk in point_to_pubkey.items()}


def list_recent_channel_closures(
    peer_map: Optional[Dict[str, Tuple[Optional[str], Optional[str]]]] = None,
) -> List[ChannelClosureRow]:
    """Recent channel closures the script initiated.

    Reads from LightningChannel rows with a non-null `close_reason`
    (we set this in every close-initiation path). Sorts by
    `closed_at` if set, otherwise `last_close_attempt_at`. Returns
    up to 100 rows. Pure DB query — no network calls.

    `peer_map`: optional {channel_point_lowercase → (pubkey, alias)}
    mapping built by `_resolve_closed_channel_peers`. Passed in by
    the orchestrator so this function stays sync-friendly and so a
    single ClosedChannels/GetNodeInfo pass per dashboard request
    serves every closure row.

    Channels closed PEER-initiated where the script never decided to
    close them won't appear here because we don't have a row for them.
    A future iteration could backfill from LND's ClosedChannels.
    """
    # Lazy import so the module is importable without the engine.
    from node_database import LightningChannel
    pm = peer_map or {}
    rows: List[ChannelClosureRow] = []
    try:
        # peewee's Optional[datetime] columns: filter via != None (peewee
        # converts to SQL IS NOT NULL).
        query = (
            LightningChannel
            .select()
            .where(LightningChannel.close_reason.is_null(False))
        )
        for r in query:
            # Pick the most recent timestamp we have for this row.
            when = r.closed_at or r.last_close_attempt_at
            ts = int(when.timestamp()) if when is not None else 0
            cp_key = (r.channel_point or "").lower()
            peer_pubkey, peer_alias = pm.get(cp_key, (None, None))
            rows.append(ChannelClosureRow(
                timestamp=ts,
                iso_date=_iso(ts),
                channel_point=r.channel_point,
                close_reason=r.close_reason or "(no reason recorded)",
                cooperative_close_attempts=int(r.cooperative_close_attempts or 0),
                force_close_initiated=(r.force_close_initiated_at is not None),
                peer_pubkey=peer_pubkey,
                peer_alias=peer_alias,
            ))
    except Exception as e:
        logger.warning(f"list_recent_channel_closures query failed: {e} {traceback.format_exc()}")
        return []
    rows.sort(key=lambda r: r.timestamp, reverse=True)
    return rows[:_RECENT_ROW_CAP]


def list_recent_lsp_orders(usd_rate: Optional[float]) -> List[LspOrderRow]:
    """Recent LSP channel orders the engine has created.

    Reads every LspChannelOrder row, projects to LspOrderRow with
    USD conversions and convenience fields (short_order_id, age,
    channel_funding_txid extracted from channel_point). Sorted
    newest-first, capped at _RECENT_ROW_CAP.

    Net cost semantics:
      - COMPLETED orders: the channel opened. paid = service fee +
        miner fee; refund = 0; net_cost == paid.
      - FAILED with on-chain-observed refund: paid - actual_refund.
        Typically tiny (just the LSP's miner-fee on the refund tx).
      - FAILED without observed refund (LSP didn't refund, or refund
        not yet confirmed): net_cost == paid. The dashboard shows
        these clearly via refund_observed_onchain=False.
      - ORDERED / PAID: not yet terminal; net_cost reflects gross
        paid so the operator sees the in-flight exposure.
    """
    from node_database import LspChannelOrder
    rows: List[LspOrderRow] = []
    now_ts = int(datetime.datetime.now().timestamp())
    try:
        for r in LspChannelOrder.select():
            created_ts = (
                int(r.created.timestamp()) if r.created is not None else 0
            )
            # Gross paid: service fee + miner fee from our side. The
            # service fee is the LSP's stated `fee_total_sat` (which
            # we paid at order time); we don't have the original
            # outgoing-payment miner fee directly on the order row,
            # so the "paid" column reflects the LSP-quoted service
            # amount only. (Operators wanting the on-chain miner fee
            # too can look at the Recent Fee Payments / wallet history
            # — the lsp_channel_order tx label is unambiguous.)
            paid_sats = int(r.fee_total_sat or 0)
            refund_sats = int(r.refund_amount_sat or 0) if r.refund_observed_onchain else 0
            net_cost_sats = max(0, paid_sats - refund_sats)

            channel_point = r.channel_point
            funding_txid: Optional[str] = None
            if channel_point and ":" in channel_point:
                funding_txid = channel_point.split(":")[0]

            age_hours = max(0, (now_ts - created_ts) // 3600) if created_ts else 0

            paid_usd = (paid_sats / _SATS_PER_BTC * usd_rate) if usd_rate else None
            refund_usd = (refund_sats / _SATS_PER_BTC * usd_rate) if usd_rate else None
            net_usd = (net_cost_sats / _SATS_PER_BTC * usd_rate) if usd_rate else None

            short_order_id = (r.order_id or "")[:8]

            rows.append(LspOrderRow(
                timestamp=created_ts,
                iso_date=_iso(created_ts),
                provider=r.provider or "",
                order_id=r.order_id or "",
                short_order_id=short_order_id,
                state=r.state or "",
                lsps1_state=r.lsps1_state,
                paid_sats=paid_sats,
                paid_usd=paid_usd,
                refund_sats=refund_sats,
                refund_usd=refund_usd,
                refund_observed_onchain=bool(r.refund_observed_onchain),
                net_cost_sats=net_cost_sats,
                net_cost_usd=net_usd,
                channel_funding_txid=funding_txid,
                channel_point=channel_point,
                refund_txid=r.refund_txid,
                refund_onchain_address=r.refund_onchain_address,
                age_hours=int(age_hours),
            ))
    except Exception as e:
        logger.warning(f"list_recent_lsp_orders query failed: {e} {traceback.format_exc()}")
        return []
    rows.sort(key=lambda r: r.timestamp, reverse=True)
    return rows[:_RECENT_ROW_CAP]


# ---------------------------------------------------------------------------
# Compose per-store payload
# ---------------------------------------------------------------------------

def _network_breakdown_from_stats(stats: Any) -> FeeBreakdown:
    """Project a StoreStats dataclass into the dashboard's
    `FeeBreakdown` shape. Every field defaults to 0 if missing,
    matching the dataclass's own defaults — but defending against a
    future StoreStats schema rename is cheap."""
    g = lambda name: int(getattr(stats, name, 0) or 0)   # noqa: E731
    return FeeBreakdown(
        onchain_payouts=g("onchain_network_fees_paid_for_payouts_in_sats"),
        onchain_fee_payments=g("onchain_network_fees_paid_for_fee_payments_in_sats"),
        onchain_referral_payments=g("onchain_network_fees_paid_for_referral_payments_in_sats"),
        onchain_topup_returns=g("onchain_network_fees_paid_for_bb_topup_returns_in_sats"),
        onchain_channel_opens=g("onchain_network_fees_paid_for_channel_opens_in_sats"),
        onchain_channel_closes=g("onchain_network_fees_paid_for_channel_closes_in_sats"),
        onchain_swaps=g("onchain_network_fees_paid_for_swaps_in_sats"),
        onchain_lsp_orders=g("onchain_network_fees_paid_for_lsp_orders_in_sats"),
        lsp_service_fees=g("onchain_lsp_service_fees_paid_in_sats"),
        onchain_external=g("onchain_network_fees_paid_for_external_in_sats"),
        ln_payouts=g("ln_network_fees_paid_for_payouts_in_sats"),
        ln_fee_payments=g("ln_network_fees_paid_for_fee_payments_in_sats"),
        ln_referral_payments=g("ln_network_fees_paid_for_referral_payments_in_sats"),
        ln_rebalances=g("ln_network_fees_paid_for_rebalances_in_sats"),
        ln_misc=g("misc_ln_network_fees_in_sats"),
    )


def _sum_breakdown(b: FeeBreakdown) -> int:
    return (
        b.onchain_payouts + b.onchain_fee_payments + b.onchain_referral_payments
        + b.onchain_topup_returns + b.onchain_channel_opens + b.onchain_channel_closes
        + b.onchain_swaps + b.onchain_lsp_orders + b.lsp_service_fees
        + b.onchain_external
        + b.ln_payouts + b.ln_fee_payments + b.ln_referral_payments
        + b.ln_rebalances + b.ln_misc
    )


def _add_breakdowns(a: FeeBreakdown, b: FeeBreakdown) -> FeeBreakdown:
    """Field-wise sum. Used for the cross-store summary section."""
    return FeeBreakdown(
        onchain_payouts=a.onchain_payouts + b.onchain_payouts,
        onchain_fee_payments=a.onchain_fee_payments + b.onchain_fee_payments,
        onchain_referral_payments=a.onchain_referral_payments + b.onchain_referral_payments,
        onchain_topup_returns=a.onchain_topup_returns + b.onchain_topup_returns,
        onchain_channel_opens=a.onchain_channel_opens + b.onchain_channel_opens,
        onchain_channel_closes=a.onchain_channel_closes + b.onchain_channel_closes,
        onchain_swaps=a.onchain_swaps + b.onchain_swaps,
        onchain_lsp_orders=a.onchain_lsp_orders + b.onchain_lsp_orders,
        lsp_service_fees=a.lsp_service_fees + b.lsp_service_fees,
        onchain_external=a.onchain_external + b.onchain_external,
        ln_rebalances=a.ln_rebalances + b.ln_rebalances,
        ln_payouts=a.ln_payouts + b.ln_payouts,
        ln_fee_payments=a.ln_fee_payments + b.ln_fee_payments,
        ln_referral_payments=a.ln_referral_payments + b.ln_referral_payments,
        ln_misc=a.ln_misc + b.ln_misc,
    )


async def _count_paid_invoices(api: Any, store_id: str, since_date: Optional[datetime.datetime]) -> int:
    """Count of paid invoices for the store. Issues the same
    `get_invoices(store_id=…)` call new_calc_invoice_stats issues
    (independently — there's no caching/sharing between them), but
    only counts the paid ones, which is cheaper than re-running the
    fee math. Applies the same since_date filter."""
    try:
        invoices = await api.get_invoices(store_id=store_id)
    except Exception as e:
        logger.warning(f"get_invoices failed for store {store_id}: {e} {traceback.format_exc()}")
        return 0
    if not invoices:
        return 0
    import dateutil.parser
    count = 0
    for inv in invoices:
        # Reuse BitcartInvoice.is_paid() logic without instantiating the
        # full dataclass: an invoice is paid iff it has a `paid_date`.
        if not inv.get("paid_date"):
            continue
        if since_date is not None:
            # Use the paid_date timestamp; that's when revenue was recognized.
            try:
                paid_at = dateutil.parser.parse(inv["paid_date"])
            except Exception as e:
                logger.warning(
                    f"compute_dashboard: malformed paid_date on invoice "
                    f"{inv.get('id', '?')}: {inv.get('paid_date')!r}: {e}"
                )
                continue
            if paid_at < since_date:
                continue
        count += 1
    return count


async def compute_dashboard(api: Any, range_key: str) -> DashboardResponse:
    """Build the full dashboard payload. Pure function of `api` state
    at call time — caller is responsible for caching.

    Walks stores via `api.get_stores()`, filters to those whose best
    LN wallet is named "liquidityhelper", and computes per-store +
    summary stats.
    """
    # Validate range
    if range_key not in _RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid range {range_key!r}; expected one of {list(_RANGE_DAYS)}",
        )
    days = _RANGE_DAYS[range_key]
    since_date: Optional[datetime.datetime] = (
        datetime.datetime.now() - datetime.timedelta(days=days)
        if days is not None else None
    )

    # Imported lazily so this module is importable in environments
    # where the engine isn't on sys.path (e.g. focused unit tests).
    from liquidityhelper import new_calc_invoice_stats

    # USD rate — None on failure; downstream renders as '—'.
    btc_usd_rate = await api.get_btc_usd_rate()

    # LND readiness gate. new_calc_invoice_stats walks
    # list_onchain_history which calls into LND via lnd_rpc; if any
    # btclnd wallet's LND isn't responding yet (typical for the first
    # ~5-10s after a bitcart container restart), that path raises
    # RuntimeError("Could not fetch LND info ...") and the whole
    # dashboard endpoint 500s. Probe up front and return a "not ready"
    # skeleton instead — the UI shows a banner and auto-refreshes
    # every 5s until ready. Cheap because get_lnd_info is per-instance
    # cached; subsequent calls in this same compute_dashboard hit the
    # cache.
    lnd_ready, lnd_not_ready_wallets = await _check_lnd_ready(api)
    if not lnd_ready:
        return DashboardResponse(
            range=range_key,
            btc_usd_rate=btc_usd_rate,
            cc_baseline_pct=CREDIT_CARD_BASELINE_PCT,
            bitcoin_network="",
            lnd_ready=False,
            lnd_not_ready_wallets=lnd_not_ready_wallets,
            # The cashout summary is config-only and the engine module
            # is already loaded by now (we got here from the bitcart
            # plugin loader), so it's safe to populate even when LND
            # is still warming up. Lets the dashboard header render
            # the operator's configured mode/cashout immediately
            # instead of jumping in once LND comes up.
            debug_mode=_engine_debug_mode(),
            cashout_summary=_preferred_cashout_summary(),
            topup_warning=TopupWarning(rows=[]),
            stores=[],
            summary=None,
            shared_wallet_warning=False,
            health_warnings=[],
            liquidity_stats=LiquidityStats(
                mode=_liquidity_mode_label(),
                wallets=[],
                total_inbound=_money(0, btc_usd_rate),
                total_outbound=_money(0, btc_usd_rate),
                total_channel_count=0,
            ),
            recent_fee_payments=[],
            recent_cashouts=[],
            recent_channel_closures=[],
            recent_lsp_orders=[],
            recent_network_fees=[],
        )

    # Walk every store + classify by wallet name. We can't filter at
    # the stats step because new_calc_invoice_stats processes ALL
    # stores; we filter the OUTPUT.
    store_list = await api.get_stores() or []
    stats_by_store_id = await new_calc_invoice_stats(api, since_date=since_date)

    # Wallet→stores map: used for the "shared wallet" warning. We build
    # this before filtering by wallet name so we catch the case where
    # store A uses the wallet as its best-LN-wallet and store B is also
    # configured to use it.
    wallet_to_stores: Dict[str, List[str]] = {}
    wallet_by_store: Dict[str, Dict[str, Any]] = {}
    for store in store_list:
        try:
            full_wallet = await api.get_best_ln_wallet_for_store(store)
        except Exception as e:
            logger.warning(f"get_best_ln_wallet_for_store failed for store {store.get('id')}: {e} {traceback.format_exc()}")
            continue
        if not full_wallet:
            continue
        wallet_by_store[store["id"]] = full_wallet
        wid = full_wallet.get("id")
        if wid:
            wallet_to_stores.setdefault(wid, []).append(store["id"])

    shared_wallet_warning = any(len(ss) > 1 for ss in wallet_to_stores.values())

    # Same map but valued as store NAMES, used by build_liquidity_stats
    # to label which stores each wallet is associated with. Stores that
    # have no name (defensive — Bitcart allows empty names through the
    # API) fall back to their id-prefix.
    store_id_to_name: Dict[str, str] = {
        (s.get("id") or ""): (s.get("name") or (s.get("id") or "")[:8])
        for s in store_list
    }
    wallet_to_store_names: Dict[str, List[str]] = {
        wid: [store_id_to_name.get(sid, sid[:8]) for sid in sids]
        for wid, sids in wallet_to_stores.items()
    }

    # Build per-store payloads — only for stores using a wallet named
    # "liquidityhelper". This is the SAME wallet-name attribution
    # new_calc_invoice_stats uses to mark non-`liquidityhelper`
    # revenue as ineligible-for-fees, applied here as a hard filter
    # so the dashboard only surfaces stores the engine treats as
    # billable.
    stores_out: List[StoreDashboard] = []
    summary_breakdown = FeeBreakdown(
        onchain_payouts=0, onchain_fee_payments=0, onchain_referral_payments=0,
        onchain_topup_returns=0, onchain_channel_opens=0, onchain_channel_closes=0,
        onchain_swaps=0, onchain_lsp_orders=0, lsp_service_fees=0,
        onchain_external=0,
        ln_payouts=0, ln_fee_payments=0, ln_referral_payments=0,
        ln_rebalances=0, ln_misc=0,
    )
    sum_revenue_sats = 0
    sum_dev_sats = 0
    sum_dev_due_sats = 0
    sum_hosting_sats = 0
    sum_hosting_due_sats = 0
    sum_invoice_count = 0

    # Configured rates. Read once — same values flow into every
    # per-store `*_fees_due` computation. Defaults match the engine
    # in config.py: FEE_AMOUNT=0.02 (2%), REFERRAL_FEE_AMOUNT=0
    # (no referral collection unless the operator opted in).
    try:
        import config as _cfg
        _fee_rate = float(getattr(_cfg, "FEE_AMOUNT", 0.02) or 0)
        _referral_rate = float(getattr(_cfg, "REFERRAL_FEE_AMOUNT", 0) or 0)
    except Exception as e:
        logger.warning(f"compute_dashboard: could not read fee config: {e} {traceback.format_exc()}")
        _fee_rate = 0.0
        _referral_rate = 0.0

    for store in store_list:
        store_id = store["id"]
        full_wallet = wallet_by_store.get(store_id)
        if not full_wallet or full_wallet.get("name") != "liquidityhelper":
            # Spec: ignore wallets not named liquidityhelper.
            continue
        stats = stats_by_store_id.get(store_id)
        if stats is None:
            # new_calc_invoice_stats didn't produce a record — happens
            # if get_stores returned more rows than the stats walk.
            # Synthesize an all-zero StoreStats so the row still renders.
            from classes import StoreStats
            stats = StoreStats(
                store_id=store_id, ln_total_revenue_in_sats=0,
                onchain_total_revenue_in_sats=0, total_bb_fees_paid_in_sats=0,
                revenue_eligible_for_fee=0,
                ineligible_revenue_because_not_liquidityhelper_wallet_in_sats=0,
                ineligible_revenue_because_not_ln_transaction_in_sats=0,
                ineligible_revenue_because_of_promo_in_sats=0,
                ineligible_revenue_because_of_topups_in_sats=0,
                ineligible_revenue_because_of_bb_topups_in_sats=0,
                ln_network_fees_paid_for_bb_topup_returns_in_sats=0,
                onchain_network_fees_paid_for_bb_topup_returns_in_sats=0,
                ln_network_fees_paid_for_fee_payments_in_sats=0,
                onchain_network_fees_paid_for_fee_payments_in_sats=0,
                ln_network_fees_paid_for_payouts_in_sats=0,
                misc_ln_network_fees_in_sats=0,
                onchain_network_fees_paid_for_payouts_in_sats=0,
                onchain_network_fees_paid_for_channel_opens_in_sats=0,
                onchain_network_fees_paid_for_channel_closes_in_sats=0,
                onchain_network_fees_paid_for_swaps_in_sats=0,
                onchain_network_fees_paid_for_lsp_orders_in_sats=0,
                onchain_lsp_service_fees_paid_in_sats=0,
                total_referral_fees_paid_in_sats=0,
                ln_network_fees_paid_for_referral_payments_in_sats=0,
                onchain_network_fees_paid_for_referral_payments_in_sats=0,
            )
        # Revenue is ALL revenue from this wallet (not the
        # "eligible-for-fee" subset) — the dashboard shows what the
        # store actually brought in, not the after-promo number.
        revenue_sats = int((stats.ln_total_revenue_in_sats or 0)
                            + (stats.onchain_total_revenue_in_sats or 0))
        dev_sats = int(stats.total_bb_fees_paid_in_sats or 0)
        hosting_sats = int(stats.total_referral_fees_paid_in_sats or 0)

        # Gross "due" totals based on lifetime eligible revenue
        # (matches the calculation in calculate_fees at
        # liquidityhelper.py:3321). `revenue_eligible_for_fee` excludes
        # promo, topup, bb-topup, and non-LH-wallet revenue; that's the
        # same base the engine multiplies by FEE_AMOUNT to decide what
        # to charge.
        eligible_sats = int(stats.revenue_eligible_for_fee or 0)
        dev_due_sats = int(eligible_sats * _fee_rate)
        hosting_due_sats = int(eligible_sats * _referral_rate)

        breakdown = _network_breakdown_from_stats(stats)
        network_total_sats = _sum_breakdown(breakdown)

        net_fees_sats = dev_sats + hosting_sats + network_total_sats
        cc_baseline_sats = int(revenue_sats * CREDIT_CARD_BASELINE_PCT)
        # "Saved" can be negative if fees somehow exceed the baseline —
        # _money() clamps to 0 so the UI never shows "you saved -X".
        # Operators who want the raw delta can compute it from the
        # other fields; clamping to 0 is the conservative claim.
        amount_saved_sats = max(0, cc_baseline_sats - net_fees_sats)

        paid_invoice_count = await _count_paid_invoices(api, store_id, since_date)
        inbound_sats, channel_count = await _get_inbound_liquidity(full_wallet, api)

        stores_out.append(StoreDashboard(
            store_id=store_id,
            store_name=store.get("name") or "(unnamed store)",
            wallet_id=full_wallet["id"],
            wallet_name=full_wallet.get("name") or "",
            revenue=_money(revenue_sats, btc_usd_rate),
            paid_invoice_count=paid_invoice_count,
            developer_fees_paid=_money(dev_sats, btc_usd_rate),
            developer_fees_due=_money(dev_due_sats, btc_usd_rate),
            developer_fee_pct=_safe_pct(dev_sats, revenue_sats),
            hosting_fees_paid=_money(hosting_sats, btc_usd_rate),
            hosting_fees_due=_money(hosting_due_sats, btc_usd_rate),
            hosting_fee_pct=_safe_pct(hosting_sats, revenue_sats),
            network_fees_total=_money(network_total_sats, btc_usd_rate),
            network_fee_breakdown=breakdown,
            amount_saved_vs_cc=_money(amount_saved_sats, btc_usd_rate),
            net_fees_paid=_money(net_fees_sats, btc_usd_rate),
            net_fees_pct=_safe_pct(net_fees_sats, revenue_sats),
            pie_slices={
                "developer": dev_sats,
                "hosting": hosting_sats,
                "network": network_total_sats,
            },
            inbound_liquidity=_money(inbound_sats, btc_usd_rate),
            active_channel_count=channel_count,
        ))

        # Accumulate summary
        summary_breakdown = _add_breakdowns(summary_breakdown, breakdown)
        sum_revenue_sats += revenue_sats
        sum_dev_sats += dev_sats
        sum_dev_due_sats += dev_due_sats
        sum_hosting_sats += hosting_sats
        sum_hosting_due_sats += hosting_due_sats
        sum_invoice_count += paid_invoice_count

    # Summary only when there's more than one store
    summary: Optional[SummaryDashboard] = None
    if len(stores_out) > 1:
        summary_network_total = _sum_breakdown(summary_breakdown)
        summary_net = sum_dev_sats + sum_hosting_sats + summary_network_total
        summary_cc_baseline = int(sum_revenue_sats * CREDIT_CARD_BASELINE_PCT)
        summary_saved = max(0, summary_cc_baseline - summary_net)
        summary = SummaryDashboard(
            revenue=_money(sum_revenue_sats, btc_usd_rate),
            paid_invoice_count=sum_invoice_count,
            developer_fees_paid=_money(sum_dev_sats, btc_usd_rate),
            developer_fees_due=_money(sum_dev_due_sats, btc_usd_rate),
            hosting_fees_paid=_money(sum_hosting_sats, btc_usd_rate),
            hosting_fees_due=_money(sum_hosting_due_sats, btc_usd_rate),
            network_fees_total=_money(summary_network_total, btc_usd_rate),
            network_fee_breakdown=summary_breakdown,
            amount_saved_vs_cc=_money(summary_saved, btc_usd_rate),
            net_fees_paid=_money(summary_net, btc_usd_rate),
            net_fees_pct=_safe_pct(summary_net, sum_revenue_sats),
            pie_slices={
                "developer": sum_dev_sats,
                "hosting": sum_hosting_sats,
                "network": summary_network_total,
            },
        )

    # Recent-activity tables. Computed AFTER per-store stats so we
    # can reuse the same api/usd_rate; the wallet-name filter applies
    # equivalently inside _gather_payment_rows.
    recent_fee_payments = await list_recent_fee_payments(api, btc_usd_rate)
    recent_cashouts = await list_recent_cashouts(api, btc_usd_rate)
    try:
        closure_peer_map = await _resolve_closed_channel_peers(api)
    except Exception as e:
        logger.warning(f"_resolve_closed_channel_peers raised: {e} {traceback.format_exc()}")
        closure_peer_map = {}
    recent_channel_closures = list_recent_channel_closures(closure_peer_map)
    recent_lsp_orders = list_recent_lsp_orders(btc_usd_rate)
    recent_network_fees = await list_recent_network_fees(api, btc_usd_rate)
    bitcoin_network = await _detect_bitcoin_network(api)
    liquidity_stats = await build_liquidity_stats(
        api, btc_usd_rate, wallet_to_store_names=wallet_to_store_names,
        bitcoin_network=bitcoin_network,
    )

    # Health audit: pure-config checks (cashout/channel/reserve/loop
    # config sanity) + one dynamic check (LN cashouts stale while LN
    # balance exists). Emitted to decisions.log via log_decision inside
    # the audit fn, so the dashboard and the log stream agree.
    # Use the PURE compute_health_warnings — the dashboard endpoint
    # may run in any of N gunicorn workers, and emitting log_decision
    # transitions from a worker without persistent dedupe state was
    # the dominant source of decisions.log spam (one cleared line per
    # warning ID × per worker × per cache-miss). The tick loop runs
    # in a single process and is the sole authoritative source for
    # transition emissions; the dashboard just reads the state.
    from liquidityhelper import compute_health_warnings
    try:
        health_warnings_raw = await compute_health_warnings(api)
    except Exception as e:
        logger.warning(f"compute_health_warnings raised: {e} {traceback.format_exc()}")
        health_warnings_raw = []
    health_warnings = [HealthWarning(**w) for w in health_warnings_raw]

    cashout_summary = _preferred_cashout_summary()
    debug_mode = _engine_debug_mode()
    try:
        topup_warning = await _compute_topup_warning(api)
    except Exception as e:
        logger.warning(f"_compute_topup_warning raised: {e} {traceback.format_exc()}")
        topup_warning = TopupWarning(rows=[])

    return DashboardResponse(
        range=range_key,
        btc_usd_rate=btc_usd_rate,
        cc_baseline_pct=CREDIT_CARD_BASELINE_PCT,
        bitcoin_network=bitcoin_network,
        lnd_ready=True,
        lnd_not_ready_wallets=[],
        debug_mode=debug_mode,
        cashout_summary=cashout_summary,
        topup_warning=topup_warning,
        stores=stores_out,
        summary=summary,
        shared_wallet_warning=shared_wallet_warning,
        health_warnings=health_warnings,
        liquidity_stats=liquidity_stats,
        recent_fee_payments=recent_fee_payments,
        recent_cashouts=recent_cashouts,
        recent_channel_closures=recent_channel_closures,
        recent_lsp_orders=recent_lsp_orders,
        recent_network_fees=recent_network_fees,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def build_router(auth_dependency: Any | None = None) -> APIRouter:
    """Build the dashboard router. Same factory pattern as
    log_endpoints.build_router — tests mount with auth_dependency=None,
    production wires Bitcart's AuthDependency with server_management
    scope."""
    # prefix is "/plugins/...", not "/api/plugins/..." — bitcart's
    # FastAPI app has root_path="/api"; a leading /api/ here mounts at
    # /api/api/plugins/... and the proxy returns 404.
    router = APIRouter(prefix="/plugins/liquidityhelper/dashboard")

    deps = (
        [Security(auth_dependency, scopes=["server_management"])]
        if auth_dependency is not None else []
    )

    @router.get("", response_model=DashboardResponse, dependencies=deps)
    async def dashboard(
        range: str = Query(
            "all",
            description=(
                "Time window for the data. 'all' = all-time. "
                "30/90/365 = last N days (filters revenue; fee figures "
                "always show all-time, since on-chain/LN history entries "
                "don't carry reliable timestamps through the normalized "
                "RPC layer)."
            ),
        ),
        force_refresh: bool = Query(
            False,
            description="If true, skip the 60s cache and recompute now.",
        ),
    ) -> DashboardResponse:
        if not force_refresh:
            cached = _cache_get(range)
            if cached is not None:
                return cached
        # Resolve the Bitcart API — same lazy import the rest of the
        # plugin uses to avoid a hard module-load dependency.
        from liquidityhelper import _get_dashboard_api
        api = await _get_dashboard_api()
        try:
            payload = await compute_dashboard(api, range)
        finally:
            # Close the API client we opened. The engine has its own
            # long-lived client; a per-request one is fine here since
            # the dashboard fires at most once per ~60s.
            try:
                await api.close()
            except Exception as e:
                logger.debug(f"dashboard endpoint: api.close() best-effort cleanup failed: {e}")
        # Don't cache a "not ready" skeleton — LND comes online within
        # seconds and the operator's auto-refresh poll would keep
        # serving the cached not-ready response for 60s otherwise.
        if payload.lnd_ready:
            _cache_set(range, payload)
        return payload

    return router
