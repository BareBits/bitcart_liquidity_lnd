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
    ln_payouts: int
    ln_fee_payments: int
    ln_referral_payments: int
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


class ChannelClosureRow(BaseModel):
    """One row in the Recent Channel Closures table."""
    timestamp: int             # unix seconds of last_close_attempt_at or closed_at
    iso_date: str
    channel_point: str
    close_reason: str
    cooperative_close_attempts: int
    force_close_initiated: bool      # True if force_close_initiated_at is set


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


class DashboardResponse(BaseModel):
    """Top-level shape returned by GET /dashboard."""
    range: str
    btc_usd_rate: Optional[float]
    cc_baseline_pct: float
    stores: List[StoreDashboard]
    summary: Optional[SummaryDashboard]    # None when only one store
    shared_wallet_warning: bool            # True if ≥2 stores share a wallet
    health_warnings: List[HealthWarning]   # config-sanity + runtime checks
    # Recent activity tables — capped at 100 rows so the response stays
    # small. The UI paginates 10/page client-side. Sorted newest-first.
    recent_fee_payments: List[PaymentRow]
    recent_cashouts: List[PaymentRow]
    recent_channel_closures: List[ChannelClosureRow]
    recent_lsp_orders: List[LspOrderRow]


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

async def _get_inbound_liquidity(wallet: Dict[str, Any], api: Any) -> Tuple[int, int]:
    """Return (inbound_sats, active_channel_count) for `wallet`.

    For btclnd wallets we go straight to LND's `Lightning.ListChannels`
    via `lnd_rpc` — Bitcart's btclnd channel proxy doesn't always pass
    through `remote_balance`, so the direct path is the source of truth.
    For Electrum wallets we have no alternative and use Bitcart's
    `get_wallet_ln_channels`.

    Failure is silent: returns (0, 0) on any error. The UI shows a 0
    rather than an error banner — the dashboard should still render
    if one wallet's LND is briefly unreachable.
    """
    wallet_id = wallet.get("id")
    currency = wallet.get("currency")
    try:
        if currency == "btclnd":
            from liquidityhelper import lnd_rpc
            resp = await lnd_rpc(api, wallet_id, "ListChannels", {}, "Lightning")
            if not isinstance(resp, dict):
                return (0, 0)
            inbound = 0
            count = 0
            for c in (resp.get("channels") or []):
                # `active` is a bool from LND. Treat missing as inactive
                # (defensive — older LND builds may have omitted it).
                if not c.get("active"):
                    continue
                inbound += int(c.get("remote_balance") or 0)
                count += 1
            return (inbound, count)
        # Electrum / btc path — Bitcart's API is the only source.
        channels = await api.get_wallet_ln_channels(
            wallet_id, active_only=True, online_only=False,
        )
        if not channels:
            return (0, 0)
        inbound = 0
        for c in channels:
            inbound += int(float(c.get("remote_balance") or 0))
        return (inbound, len(channels))
    except Exception as e:
        logger.warning(
            f"inbound-liquidity fetch failed for wallet {wallet_id} "
            f"(currency={currency}): {e}"
        )
        return (0, 0)


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


def list_recent_channel_closures() -> List[ChannelClosureRow]:
    """Recent channel closures the script initiated.

    Reads from LightningChannel rows with a non-null `close_reason`
    (we set this in every close-initiation path). Sorts by
    `closed_at` if set, otherwise `last_close_attempt_at`. Returns
    up to 100 rows. Pure DB query — no network calls.

    Channels closed PEER-initiated where the script never decided to
    close them won't appear here because we don't have a row for them.
    A future iteration could backfill from LND's ClosedChannels.
    """
    # Lazy import so the module is importable without the engine.
    from node_database import LightningChannel
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
            rows.append(ChannelClosureRow(
                timestamp=ts,
                iso_date=_iso(ts),
                channel_point=r.channel_point,
                close_reason=r.close_reason or "(no reason recorded)",
                cooperative_close_attempts=int(r.cooperative_close_attempts or 0),
                force_close_initiated=(r.force_close_initiated_at is not None),
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
        ln_payouts=g("ln_network_fees_paid_for_payouts_in_sats"),
        ln_fee_payments=g("ln_network_fees_paid_for_fee_payments_in_sats"),
        ln_referral_payments=g("ln_network_fees_paid_for_referral_payments_in_sats"),
        ln_misc=g("misc_ln_network_fees_in_sats"),
    )


def _sum_breakdown(b: FeeBreakdown) -> int:
    return (
        b.onchain_payouts + b.onchain_fee_payments + b.onchain_referral_payments
        + b.onchain_topup_returns + b.onchain_channel_opens + b.onchain_channel_closes
        + b.onchain_swaps + b.onchain_lsp_orders + b.lsp_service_fees
        + b.ln_payouts + b.ln_fee_payments + b.ln_referral_payments + b.ln_misc
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
        ln_payouts=0, ln_fee_payments=0, ln_referral_payments=0, ln_misc=0,
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
    recent_channel_closures = list_recent_channel_closures()
    recent_lsp_orders = list_recent_lsp_orders(btc_usd_rate)

    # Health audit: pure-config checks (cashout/channel/reserve/loop
    # config sanity) + one dynamic check (LN cashouts stale while LN
    # balance exists). Emitted to decisions.log via log_decision inside
    # the audit fn, so the dashboard and the log stream agree.
    from liquidityhelper import collect_health_warnings
    try:
        health_warnings_raw = await collect_health_warnings(api)
    except Exception as e:
        logger.warning(f"collect_health_warnings raised: {e} {traceback.format_exc()}")
        health_warnings_raw = []
    health_warnings = [HealthWarning(**w) for w in health_warnings_raw]

    return DashboardResponse(
        range=range_key,
        btc_usd_rate=btc_usd_rate,
        cc_baseline_pct=CREDIT_CARD_BASELINE_PCT,
        stores=stores_out,
        summary=summary,
        shared_wallet_warning=shared_wallet_warning,
        health_warnings=health_warnings,
        recent_fee_payments=recent_fee_payments,
        recent_cashouts=recent_cashouts,
        recent_channel_closures=recent_channel_closures,
        recent_lsp_orders=recent_lsp_orders,
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
        _cache_set(range, payload)
        return payload

    return router
