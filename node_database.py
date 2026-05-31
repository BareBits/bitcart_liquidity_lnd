import datetime,os
from datetime import datetime, timedelta
from typing import List,Dict,Set,Union,Tuple,Any,Optional
from peewee import Model, CharField, BigIntegerField, DateTimeField, IntegerField, SqliteDatabase,FloatField,BooleanField
from common_functions import utcnow_naive

from config import (
    NODE_CRITERIA_MINIMUM_CHANNELCOUNT, NODE_CRITERIA_MINIMUM_AGE,
    NODE_CRITERIA_MINIMUM_CAPACITY, NODE_CRITERIA_MAX_FEE_RATE_PPM,
    NODE_CRITERIA_OUTBOUND_CAPACITY_MULTIPLIER, LSP_CHANNEL_SIZE_SAT,
    NODE_CRITERIA_MIN_EFFECTIVE_DEGREE, NODE_CRITERIA_MIN_TWO_HOP_REACH,
    UPTIME_MIN_OBSERVATION_DAYS, UPTIME_MAX_FAILURE_RATIO,
    UPTIME_LONG_OUTAGE_DAYS,
    NODE_CRITERIA_MAX_MIN_HTLC_MSAT, NODE_CRITERIA_MIN_MAX_HTLC_FRACTION,
)
import json

import os as _os
def _resolve_node_db_path() -> str:
    """Same resolution as ``database._resolve_db_path``: env override
    first (LIQUIDITYHELPER_NODE_DB_PATH), then bitcart's plugin_data
    dir (BITCART_DATADIR/plugin_data/liquidityhelper or /datadir/...),
    fall back to a location next to this file. See database.py for
    why CWD-relative fails as a plugin."""
    override = _os.environ.get("LIQUIDITYHELPER_NODE_DB_PATH")
    if override:
        return override
    for candidate in (_os.environ.get("BITCART_DATADIR"), "/datadir"):
        if candidate and _os.path.isdir(candidate):
            plugin_data = _os.path.join(candidate, "plugin_data", "liquidityhelper")
            try:
                _os.makedirs(plugin_data, exist_ok=True)
            except PermissionError:
                continue
            if _os.access(plugin_data, _os.W_OK):
                return _os.path.join(plugin_data, "known_ln_nodes.db")
    return _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "known_ln_nodes.db")

node_db = SqliteDatabase(_resolve_node_db_path())

class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, LightningNode):
            #return obj.to_json()
            return obj.__dict__['__data__']
        elif isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

class BaseModel(Model):
    class Meta:
        database = node_db

    # Per-subclass opt-in list of DateTimeField names that should be
    # backfilled with ``datetime.now()`` at save-time if still None.
    # Subclasses override. Empty default = no auto-stamping.
    #
    # Why an explicit list and not auto-detection: many DateTimeField
    # columns on LightningNode (last_audit_failure_at,
    # audit_close_blacklist_until, force_close_blacklist_until,
    # current_window_started_at, ...) are legitimately nullable and
    # MUST stay None until something explicitly sets them. Peewee
    # gives every field a default of None when no default= is passed,
    # so inspecting ``field.default`` can't distinguish "opt-in
    # sentinel" from "naturally nullable column". Hence the explicit
    # allow-list per model.
    _auto_now_on_insert_fields: tuple = ()

    def save(self, *args, **kwargs):
        """Backfill the per-model ``_auto_now_on_insert_fields`` with
        ``datetime.now()`` before delegating to peewee's save().

        Necessary because the legacy schema used
        ``DateTimeField(default=datetime.now())`` (parenthesized),
        which evaluates ONCE at module import — every row would share
        the same timestamp. Switching the field declaration to
        ``default=None`` removes the import-time freeze; this hook
        restores the intended "stamp on write" semantics without
        forcing every call site to pass the value explicitly.

        Only fills fields currently None — explicitly-set values
        (including timestamps the caller chose, e.g. backdated
        timestamps in tests) are preserved.
        """
        if self._auto_now_on_insert_fields:
            now = utcnow_naive()
            for field_name in self._auto_now_on_insert_fields:
                if getattr(self, field_name, None) is None:
                    setattr(self, field_name, now)
        return super().save(*args, **kwargs)


class LightningNode(BaseModel):
    """Discovered Lightning node, sourced from LND gossip.

    All gossip data is treated as untrusted: every string field in here
    has been validated by `lnd_graph_pull.parse_*` before insertion.
    Numeric fields are bounds-checked. There is no field on this model
    that contains operator-controlled HTML, shell content, or anything
    a node operator can use to exfil from this script's process.

    Renamed from the legacy schema: `last_magma_query` →
    `last_lnd_query`, `magma_queries` → `lnd_queries`. Dropped fields:
    `min_channel_size` (not in gossip), `country` (not in gossip and
    unused). Existing alpha-stage DBs need a fresh `known_ln_nodes.db`
    after this change.

    All units in sats; durations in seconds. Call
    `self.set_oldest_known_date()` after every mutation so
    `oldest_known_date` stays sortable.
    """
    # See BaseModel.save — these DateTimeFields use default=None as a
    # sentinel and get backfilled with datetime.now() at write time.
    # Replaces the broken ``default=datetime.now()`` (parenthesized)
    # pattern that evaluated once at import.
    _auto_now_on_insert_fields = ("oldest_known_date", "last_lnd_query")
    node_address:str = CharField(unique=True, primary_key=True) # pubkey, LOWERCASE 66-hex
    oldest_channel:Optional[DateTimeField] = DateTimeField(null=True) # block-time of node's earliest channel (approx via height * ~575s — see lnd_graph_pull._AVG_BLOCK_INTERVAL_SEC)
    number_of_channels:Optional[int]=IntegerField(null=True) # GetNodeInfo.num_channels
    total_capacity:Optional[int] = BigIntegerField(null=True) # GetNodeInfo.total_capacity (sat)
    smallest_channel_size:Optional[int]=BigIntegerField(null=True) # min(GetNodeInfo.channels[].capacity)
    oldest_known_date:datetime=DateTimeField(default=None, null=True) # max of oldest_channel and oldest_known_date — sortable lower bound. Default=None (was datetime.now() which evaluates once at import); BaseModel.save() backfills to datetime.now() before INSERT.
    tor_address:Optional[str] = CharField(null=True)  # validated v3 onion + port, e.g. xxx...xxx.onion:9735
    ipv4_address:Optional[str] = CharField(null=True)  # validated dotted-quad + port, e.g. 1.2.3.4:9735
    ipv6_address:Optional[str] = CharField(null=True)  # validated bracketed + port, e.g. [2001:db8::1]:9735
    last_lnd_query:datetime=DateTimeField(default=None, null=True)  # last time we hit DescribeGraph/GetNodeInfo for this node. Default=None (was datetime.now() which evaluates once at import); BaseModel.save() backfills to datetime.now() before INSERT.
    lnd_queries: int = IntegerField(default=0)                      # cumulative count of refreshes
    # Median of fee_rate_milli_msat across the node's announced
    # outbound channel policies. NULL = we haven't computed it yet
    # (new row, or LND graph pull hasn't run since the column was
    # added). is_node_blacklisted rejects NULL as UNKNOWN_FEE_RATE,
    # so a node only enters the candidate pool once we've seen its
    # policies via gossip.
    median_outbound_fee_rate_ppm:Optional[int] = IntegerField(null=True)
    # Median of min_htlc_msat across the peer's outbound channel
    # policies. Peers with high values refuse to forward small
    # payments — which would block small customer payments through
    # any channel we open to them. NULL = not yet computed.
    median_min_htlc_msat:Optional[int] = BigIntegerField(null=True)
    # Median of max_htlc_msat across the peer's outbound channel
    # policies. Low values mean the peer self-throttles single-HTLC
    # sizes — large customer payments would have to be MPP-split or
    # would fail at their hop. NULL = not yet computed. We use
    # BigIntegerField because max_htlc_msat can legitimately reach
    # the channel-capacity scale (millisats × millions).
    median_max_htlc_msat:Optional[int] = BigIntegerField(null=True)
    # Number of channels where this node's outbound is enabled AND
    # the policy was gossip-updated within the last 14 days. A
    # stricter version of number_of_channels — counts only channels
    # that can actually route. NULL = not yet computed (gossip pull
    # hasn't run since this column was added).
    effective_channel_count:Optional[int] = IntegerField(null=True)
    # Distinct nodes reachable from this node in 1 or 2 hops via
    # effective edges. The pre-open proxy for "how much of the network
    # can a payment originating here reach without long routes." NULL
    # = not yet computed.
    two_hop_reach:Optional[int] = IntegerField(null=True)
    # Audit hysteresis: count of consecutive daily audits this peer
    # has failed without an intervening pass. Reset to 0 on any pass.
    # When this hits CHANNEL_AUDIT_CONSECUTIVE_FAILURES_TO_CLOSE the
    # audit pipeline coop-closes the channel and sets the blacklist
    # timestamp below.
    consecutive_failed_audits:int = IntegerField(default=0)
    last_audit_failure_at:Optional[datetime] = DateTimeField(null=True)
    # When set and in the future, is_node_blacklisted rejects the
    # node as AUDIT_BLACKLISTED regardless of any other criterion.
    # Set when audit_existing_channels closes a channel; expires
    # silently after the configured CHANNEL_AUDIT_BLACKLIST_DAYS
    # window (no field clearing needed — comparison handles it).
    audit_close_blacklist_until:Optional[datetime] = DateTimeField(null=True)
    # Same idea as the audit blacklist but for force closes — set
    # whenever the retry loop escalates a stuck coop close to a
    # unilateral force close. Lasts longer (365 vs 180 days) because
    # force close is a stronger negative signal (peer was unreachable
    # for the full CHANNEL_COOP_CLOSE_TIMEOUT_DAYS window). The two
    # blacklist fields are independent; is_node_blacklisted rejects
    # if EITHER is in the future. A peer with both set is blocked
    # until the later of the two timestamps expires.
    force_close_blacklist_until:Optional[datetime] = DateTimeField(null=True)
    last_channel_creation_attempt:Optional[datetime] = DateTimeField(null=True)  # last attempt to create a channel with this node
    remote_close_count:int=IntegerField(default=0) # how many times this LN node has closed a channel we have open with it
    # Lifetime counters (never reset) — kept for diagnostics so an
    # operator can see "how many checks has this peer been observed
    # for, ever" even if the rolling window has rotated.
    failed_uptime_checks:int=IntegerField(default=0)
    total_uptime_checks:int=IntegerField(default=0)
    # Rolling-window counters — reset to zero every
    # UPTIME_ROLLING_WINDOW_DAYS. These are what the failure-ratio
    # gate (HIGH_FAILURE_RATIO) reads. Separating from the lifetime
    # counters lets a previously-flaky peer earn forgiveness once
    # they've been reliable for the whole new window.
    recent_uptime_checks:int = IntegerField(default=0)
    recent_failed_uptime_checks:int = IntegerField(default=0)
    # Anchor for the rolling window. NULL means "no window started
    # yet"; the next uptime check will set it to now. When the elapsed
    # time exceeds UPTIME_ROLLING_WINDOW_DAYS the counters reset and
    # this advances to a fresh now.
    current_window_started_at:Optional[datetime] = DateTimeField(null=True)
    last_seen_online: Optional[datetime] = DateTimeField(null=True)

    def needs_lnd_update(self,update_frequency_in_days:int)->bool:
        """
        Returns True if we should re-query LND for this node's details.
        """
        # last_lnd_query is nullable since the schema default changed to
        # None (BaseModel.save backfills on insert, but an in-memory
        # row that hasn't been saved yet — or an older row where the
        # column happens to be NULL — needs to be treated as "stale,
        # please refresh").
        if self.last_lnd_query is None:
            return True
        time_between_insertion = utcnow_naive() - self.last_lnd_query

        if time_between_insertion.days > update_frequency_in_days:
            return True
        else:
            return False
    def get_oldest_known_date(self)->Optional[DateTimeField]:
        # Either field can be None for newly-discovered nodes whose
        # channels haven't been enumerated yet; max() chokes on None.
        candidates = [d for d in (self.oldest_known_date, self.oldest_channel) if d is not None]
        return max(candidates) if candidates else None
    def set_oldest_known_date(self)->Optional[DateTimeField]:
        candidates = [d for d in (self.oldest_known_date, self.oldest_channel) if d is not None]
        if candidates:
            self.oldest_known_date = max(candidates)
    def get_ipv4_uri(self)->str:
        """
        Returns IPv4 URI if possible, otherwise None.
        """
        if not self.ipv4_address:
            return None
        return f"{self.node_address}@{self.ipv4_address}"

    def to_json(self,)->str:
        return json.dumps(self.__dict__['__data__'], cls=CustomEncoder)

    @classmethod
    def from_json(cls, data:dict):
        # Convert all datetime fields
        for field in {'oldest_channel','oldest_known_date','last_lnd_query','last_channel_creation_attempt','last_seen_online'}:
            if field in data and data[field] is not None:
                data[field] = datetime.fromisoformat(data[field])
        return cls(**data)
class LightningChannel(BaseModel):
    """Per-channel state tracked by the script. Most fields exist to
    support the coop-close retry loop (process_pending_closes):
    `cooperative_close_requested` marks when we first asked to close
    the channel, `last_close_attempt_at` is the most-recent retry
    timestamp, `cooperative_close_attempts` counts how many tries
    we've made, and `force_close_initiated_at` is set when we gave
    up on coop and escalated to `force=True`.
    """
    channel_point:str = CharField(unique=True, primary_key=True) # channel point like 658c3cf4e8b798bd1f1d805c2de37fffsfsffba644af193c9e87d5e2adb:0 LOWERCASE
    last_seen_online: Optional[datetime] = DateTimeField(null=True)
    # Set on the FIRST coop close request. Never updated thereafter
    # so the retry loop can compute "how long has this close been
    # stuck?" against a stable reference. Cleared when the channel
    # disappears (close confirmed on-chain).
    cooperative_close_requested: Optional[datetime] = DateTimeField(null=True)
    # Most-recent close attempt timestamp. Updated on EVERY retry.
    # The retry loop uses this plus CHANNEL_COOP_CLOSE_RETRY_INTERVAL_HOURS
    # to decide whether enough time has elapsed for another attempt.
    last_close_attempt_at: Optional[datetime] = DateTimeField(null=True)
    # Cumulative count of coop close attempts. Diagnostic; not used
    # for any control decision (the day-of-first-attempt timestamp
    # drives escalation, not the attempt count).
    cooperative_close_attempts:int = IntegerField(default=0)
    # Set when we gave up on coop close and called CloseChannel with
    # force=True. Used to dedupe: once a force close is in flight,
    # the retry loop must NOT re-issue another force close on the
    # same channel.
    force_close_initiated_at: Optional[datetime] = DateTimeField(null=True)
    # Human-readable explanation for WHY this channel was closed. Set
    # by every close-initiation path (audit failure → "AUDIT_FAILURE:
    # HIGH_FEE_RATE,LOW_LIQUIDITY"; force-close escalation →
    # "FORCE_CLOSE_AFTER_COOP_TIMEOUT: AUDIT_FAILURE: HIGH_FEE_RATE").
    # Surfaced by the dashboard's "Recent
    # channel closures" table — operators expect to see why each
    # closure happened. Nullable because the column was added in a
    # later migration; rows from before then will have it unset.
    close_reason: Optional[str] = CharField(null=True, max_length=500)
    # When the channel was actually CLOSED on-chain (per LND's
    # ClosedChannels enumeration). Distinct from `last_close_attempt_at`
    # (when we asked to close) and `force_close_initiated_at` (when we
    # escalated to a unilateral close): both can fire on a channel
    # whose close hasn't yet confirmed on-chain. The dashboard sorts
    # closures by this column, with `last_close_attempt_at` as a
    # fallback for rows where the close was initiated but not yet
    # observed-confirmed.
    closed_at: Optional[datetime] = DateTimeField(null=True, index=True)


class SwapPriceQuote(BaseModel):
    """Historical record of submarine-swap price quotes returned by any
    provider. Persisted on every quote we receive (whether or not we end up
    taking that quote) so we have data for: (a) operator visibility into
    pricing trends, (b) auditing the chosen provider against alternatives,
    (c) fallback pricing reference if a provider goes down briefly.

    `cleanup_old_swap_quotes()` purges rows older than 6 months.
    """
    provider: str = CharField(index=True)       # "loop", "boltz", ...
    direction: str = CharField()                # "out" (LN -> on-chain). "in" reserved.
    amount_sat: int = BigIntegerField()         # swap notional
    total_fee_sat: int = BigIntegerField()      # provider + miner fees combined
    fee_percent: float = FloatField()           # total_fee_sat / amount_sat
    fetched_at: datetime = DateTimeField(default=datetime.now, index=True)


class LspPriceQuote(BaseModel):
    """Historical record of every LSPS1 `create_order` response we
    receive (whether or not we end up paying that order). Two consumers:

    1. `request_inbound_liquidity_from_lsp` uses the recent rows to
       enforce the once-per-day-per-LSP throttle.
    2. `max_lsp_quote_in_last_6_months_sat()` reads the
       `fee_total_sat` column to compute the dynamic floor for the
       on-chain reserve (so the wallet always has enough headroom to
       pay the LSP's recent peak price).

    Rows older than 6 months are purged daily.
    """
    provider: str = CharField(index=True)        # "zeus" | "megalithic" | ...
    network: str = CharField()                   # "mainnet" | "testnet" | ...
    wallet_id: str = CharField(index=True)
    order_id: str = CharField()
    lsp_balance_sat: int = BigIntegerField()
    fee_total_sat: int = BigIntegerField()
    order_total_sat: int = BigIntegerField()
    channel_expiry_blocks: int = IntegerField()
    fetched_at: datetime = DateTimeField(default=datetime.now, index=True)


class LspChannelOrder(BaseModel):
    """LSP orders we've committed to (paid or attempted). Used to:

    - Enforce the one-LSP-channel-per-wallet invariant
      (`_wallet_has_open_lsp_order(wallet_id)` checks for non-terminal rows).
    - Identify which on-chain channels were funded by an LSP
      (match `lsp_peer_pubkey` against channel peers).
    - Resume polling order state across script restarts.

    Local state machine (this column is `state`):
      ORDERED   — row created, on-chain payment not yet broadcast
      PAID      — on-chain payment broadcast; awaiting LSP fulfilment
      COMPLETED — poll_lsp_orders observed LSPS1 `COMPLETED`; channel open
      FAILED    — poll_lsp_orders observed LSPS1 `FAILED`; LSP issued (or
                  should have issued) a refund to refund_onchain_address
      EXPIRED   — order TTL'd out locally before reaching any terminal
                  remote state (e.g., we never managed to poll, or the
                  LSP became unreachable)
    Terminal states are COMPLETED, FAILED, EXPIRED. Non-terminal states
    are tracked in liquidityhelper._NON_TERMINAL_LSP_STATES — that set
    plus a TTL gates whether new orders are allowed for the wallet.

    Refund tracking fields (added later, after we realised the LSPS1
    spec allows the LSP to refund a paid order when channel-open fails;
    without these we silently lost the visibility on those events):
      refund_onchain_address — what we asked the LSP to refund to,
        passed in the create_order request. Pulled fresh from the
        wallet at order time.
      refund_amount_sat      — amount refunded per the LSP's
        get_order response (if reported); 0 if no refund.
      refund_txid            — refund tx id per the LSP's get_order
        response (if reported); empty string if not surfaced.
      lsps1_state            — last observed remote order_state
        ("CREATED", "COMPLETED", "FAILED", etc.). Distinct from
        local `state` because the local machine has more granularity
        (e.g., distinct ORDERED vs PAID before LSP knows it yet).
    """
    provider: str = CharField(index=True)
    network: str = CharField()
    wallet_id: str = CharField(index=True)
    order_id: str = CharField(unique=True)
    lsp_peer_pubkey: str = CharField(index=True)  # hex, lowercase
    lsp_balance_sat: int = BigIntegerField()
    fee_total_sat: int = BigIntegerField()
    onchain_payment_txid: Optional[str] = CharField(null=True)
    channel_point: Optional[str] = CharField(null=True)   # set on COMPLETED
    state: str = CharField(default="ORDERED", index=True)  # see docstring
    created: datetime = DateTimeField(default=datetime.now)
    last_polled: datetime = DateTimeField(default=datetime.now)
    refund_onchain_address: Optional[str] = CharField(null=True, default=None)
    refund_amount_sat: int = BigIntegerField(default=0)
    refund_txid: Optional[str] = CharField(null=True, default=None)
    lsps1_state: Optional[str] = CharField(null=True, default=None)
    # Whether the claimed refund tx has been verified to exist on-chain
    # as an incoming credit to this wallet. Fee accounting only
    # subtracts refunds we've actually observed; an LSP that claims a
    # refund in its get_order response but never broadcasts the tx
    # would otherwise let us under-count cost in our favor. Set by
    # `reconcile_lsp_refunds` once the refund_txid appears in the
    # wallet's on-chain history.
    refund_observed_onchain: bool = BooleanField(default=False)


class LndPaymentLabel(BaseModel):
    """Sender-side annotation for outgoing LND LN payments.

    LND has no first-class concept of an outgoing-payment label (Electrum's
    `setlabel` has no equivalent on the SendPaymentSync side), so we keep
    our own mapping. Every successful `_lnd_pay_ln_invoice` writes one
    row keyed by payment_hash; downstream code that lists LND payments can
    join against this table to recover the business reason for each send
    (`fee_payout`, `cashout`, etc).
    """
    payment_hash: str = CharField(unique=True, primary_key=True)  # hex, LOWERCASE
    wallet_id: str = CharField(index=True)
    label: str = CharField()
    created: datetime = DateTimeField(default=datetime.now)


class DerivedAddressIndex(BaseModel):
    """Per-xpub counter for the next-unused BIP32 receive-chain index.

    The on-chain payment paths (cashout, dev fee, referral) derive a
    fresh address per send from a configured xpub, using path
    `<xpub>/0/<next_index>`. After a successful send the counter
    increments and persists, so the next call to the same xpub gets
    a different address. Same store-of-state pattern as LspChannelOrder
    et al. — outlives engine restarts; readers and writers go through
    address_derivation.

    Why xpub is the primary key (not a composite with purpose):
      The xpub identifies a logical "destination wallet" managed by the
      recipient. Each xpub has one canonical receive chain — if a single
      wallet is used for multiple purposes (e.g. operator points both
      CASHOUT and REFERRAL at the same wallet, even though that's
      unusual), addresses across purposes should stay monotonically
      ordered for the recipient's wallet-scanning to see them all
      naturally. Bookkeeping the `last_purpose` for diagnostics is
      cheap, but the index counter is per-xpub.

    Starting value:
      next_index defaults to 0. For the BareBits mainnet fee xpub
      specifically, index 0 corresponds to the legacy hardcoded
      `bc1q586um24k7zr6swxqny5qqgqn8xt43pk4xeeg9g` address — so the
      first fee payment under the new code naturally continues
      sending to that same address, with subsequent payments rotating.
    """
    xpub: str = CharField(unique=True, primary_key=True)
    # last_purpose is "cashout" | "fee" | "referral". Free-form text
    # rather than an enum so future purposes can be added without a
    # schema change.
    last_purpose: str = CharField()
    next_index: int = IntegerField(default=0)
    updated: datetime = DateTimeField(default=datetime.now)


class Rebalance(BaseModel):
    """One row per SUCCESSFUL circular rebalance. The engine fires
    periodic self-payments to keep channel balances in motion (which
    discourages peers from closing channels); this table tracks what
    moved and what fee was paid.

    Only successes land here — failed attempts are logged via
    decisions.log + the operational log but not persisted. The
    rebalance budget gate reads `sum(fee_sat)` per wallet to decide
    whether today's allowance is exhausted, so persisting failures
    would just dilute that math.

    `payment_hash` is also written into LndPaymentLabel with
    REBALANCE_REASON so the existing on-chain/LN history walkers
    (new_calc_invoice_stats) can attribute the fees correctly into
    `ln_network_fees_paid_for_rebalances_in_sats`.
    """
    payment_hash: str = CharField(unique=True, primary_key=True)  # hex, LOWERCASE
    wallet_id: str = CharField(index=True)
    date: datetime = DateTimeField(default=datetime.now, index=True)
    amount_sat: int = BigIntegerField()
    fee_sat: int = BigIntegerField()
    out_channel_point: str = CharField()   # "txid:vout"
    in_channel_point: str = CharField()    # "txid:vout"

def dict_to_node(mydict:Dict[str,str])->Optional[LightningNode]:
    """
    Given a dict, make a lightning node. Does not save it, just creates it.
    Assumes you have already verified this node does NOT exist
    """
    if 'node_address' not in mydict:
        return None
    new_object = LightningNode(node_address=mydict['node_address'])
    for k, v, in mydict.items():
        if k.startswith('_'):
            continue
        if not v:
            continue
        field = LightningNode._meta.fields[k]
        if isinstance(field, CharField):
            setattr(new_object, k, v)
        elif isinstance(field, IntegerField):
            setattr(new_object, k, int(v))
        elif isinstance(field, FloatField):
            setattr(new_object, k, float(v))
        elif isinstance(field, BooleanField):
            setattr(new_object, k, bool(v))
        elif isinstance(field, DateTimeField):
            setattr(new_object, k, datetime.fromisoformat(v))
        else:
            print(f'Unknown field type: {field}')
    return new_object
def _evaluate_uptime_signals(node:LightningNode)->List[str]:
    """Return the list of uptime-related rejection reasons applicable
    to `node`. Empty list when the peer's uptime signals pass.

    Two gates:
      HIGH_FAILURE_RATIO — recent_failed / recent_total > 5% AND we've
                           observed the peer for >= 90 days in the
                           current window. The observation-period
                           guard is what prevents a 1-2 day outage
                           from false-positiving on an otherwise
                           healthy peer: 2 days of failures over only
                           a 30-day window is 6.7% (above 5%), but
                           over the required 90+ days is ~2.2% (under).
      LONG_OUTAGE        — last_seen_online > 14 days ago. Replaces
                           the old 48-hour OFFLINE_RECENTLY check
                           which would false-positive on routine peer
                           maintenance windows.

    Used by both is_node_blacklisted (selection gate) and the post-
    open audit (audit_existing_peer). Same math, two call sites.
    """
    reasons:List[str] = []

    # LONG_OUTAGE check first (cheaper, no division)
    if node.last_seen_online is not None:
        outage_threshold = utcnow_naive() - timedelta(days=UPTIME_LONG_OUTAGE_DAYS)
        if node.last_seen_online < outage_threshold:
            reasons.append('LONG_OUTAGE')

    # HIGH_FAILURE_RATIO check — requires both:
    #   - enough observation time has elapsed (warm-up guard),
    #   - failure ratio above the threshold.
    if (node.current_window_started_at is not None
            and node.recent_uptime_checks > 0):
        observation_age = utcnow_naive() - node.current_window_started_at
        if observation_age >= timedelta(days=UPTIME_MIN_OBSERVATION_DAYS):
            ratio = (
                (node.recent_failed_uptime_checks or 0)
                / node.recent_uptime_checks
            )
            if ratio > UPTIME_MAX_FAILURE_RATIO:
                reasons.append('HIGH_FAILURE_RATIO')

    return reasons


def is_node_blacklisted(node:LightningNode)->Tuple[bool,str]:
    """
    Returns True,reason if we should NOT open a channel to this node;
    False,'False' if the node passes every check.

    Reasons (and the intuition for each):
      NO_IPV4                  – we don't dial Tor for channel opens
      REMOTE_CLOSE_COUNT       – this peer has unilaterally closed our
                                 channels >2 times; assume hostile
      UNKNOWN_CHANNEL_COUNT    – no channel-count data yet
      MIN_CHANNEL_COUNT        – fewer raw channels than min
      UNKNOWN_CAPACITY         – no capacity data yet
      LOW_CAPACITY             – below absolute floor (default 1M sat)
      LOW_OUTBOUND_CAPACITY    – capacity below N×LSP_CHANNEL_SIZE_SAT
      NO_OLDEST_KNOWN_DATE     – no birth-date estimate
      NOT_OLD_ENOUGH           – below NODE_CRITERIA_MINIMUM_AGE
      UNKNOWN_FEE_RATE         – median outbound fee not yet computed
      HIGH_FEE_RATE            – median outbound fee above ceiling
      UNKNOWN_CONNECTEDNESS    – effective_channel_count or
                                 two_hop_reach not yet computed; the
                                 graph-based metrics require the
                                 daily LND gossip pull to populate
      LOW_EFFECTIVE_DEGREE     – fewer enabled+fresh channels than
                                 min (the real "can it route" gate;
                                 stricter than MIN_CHANNEL_COUNT)
      LOW_TWO_HOP_REACH        – reaches fewer than min distinct
                                 nodes within 2 hops via effective
                                 edges — isolated sub-cluster
      AUDIT_BLACKLISTED        – we previously closed a channel with
                                 this peer for failing the daily
                                 quality audit; serving a temporary
                                 (default 180-day) blacklist
      FORCE_CLOSE_BLACKLISTED  – we previously force-closed a channel
                                 with this peer (coop close failed
                                 for 10+ days); serving a 1-year
                                 blacklist. Stronger signal than
                                 AUDIT_BLACKLISTED — peer didn't
                                 just operate a low-quality node,
                                 they were entirely unreachable.
      LONG_OUTAGE              – last_seen_online > 14 days ago.
                                 Short blips (1-2 days) DON'T fire
                                 this gate — only sustained absence.
      HIGH_FAILURE_RATIO       – more than 5% of recent uptime checks
                                 failed AND we've observed for >=
                                 90 days. The observation-period guard
                                 keeps a single 2-day outage from
                                 false-positiving on an otherwise
                                 healthy peer.
      UNKNOWN_HTLC_LIMITS      – peer's min_htlc / max_htlc_msat
                                 medians not yet computed
      HIGH_MIN_HTLC            – peer's median min_htlc_msat is above
                                 our ceiling — they'd reject small
                                 customer payments through any
                                 channel we open to them
      LOW_MAX_HTLC             – peer's median max_htlc_msat is below
                                 our floor (a fraction of channel
                                 size) — large customer payments
                                 would fail at their hop without
                                 MPP-splitting
    """
    # Blacklist checks run first — they represent explicit operator-
    # policy decisions (we closed channels with this peer for cause).
    # Even if the peer's metrics have since improved, we don't
    # re-engage until the blacklist window has expired. Force-close
    # check is ordered before audit because it's the stronger signal —
    # if both are active, the more accurate reason gets reported.
    if (node.force_close_blacklist_until is not None
            and node.force_close_blacklist_until > utcnow_naive()):
        return True, 'FORCE_CLOSE_BLACKLISTED'
    if (node.audit_close_blacklist_until is not None
            and node.audit_close_blacklist_until > utcnow_naive()):
        return True, 'AUDIT_BLACKLISTED'
    if not node.ipv4_address:
        return True, 'NO_IPV4'
    if node.remote_close_count>2:
        return True,'REMOTE_CLOSE_COUNT'
    if not node.number_of_channels:
        return True, 'UNKNOWN_CHANNEL_COUNT'
    if node.number_of_channels<NODE_CRITERIA_MINIMUM_CHANNELCOUNT:
        return True, 'MIN_CHANNEL_COUNT'
    if not node.total_capacity:
        return True, 'UNKNOWN_CAPACITY'
    if node.total_capacity<NODE_CRITERIA_MINIMUM_CAPACITY:
        return True, 'LOW_CAPACITY'
    # Outbound-capacity proxy: total capacity must be ≥ N × LSP
    # channel size, so the node likely has enough OUTBOUND liquidity
    # to drain a channel we open. Crude — balances are private — but
    # eliminates obviously-too-small candidates.
    required_capacity = (
        NODE_CRITERIA_OUTBOUND_CAPACITY_MULTIPLIER * LSP_CHANNEL_SIZE_SAT
    )
    if node.total_capacity < required_capacity:
        return True, 'LOW_OUTBOUND_CAPACITY'
    oldest_known_date=node.get_oldest_known_date()
    if not oldest_known_date:
        return True, 'NO_OLDEST_KNOWN_DATE'
    elapsed_time=utcnow_naive()-oldest_known_date
    elapsed_time_in_days=elapsed_time.days
    if elapsed_time_in_days<NODE_CRITERIA_MINIMUM_AGE:
        return True, 'NOT_OLD_ENOUGH'
    # Fee-rate gate: reject nodes without computed median fee data
    # (per operator decision: don't try unknown nodes before known-
    # cheap ones), and reject anything above the configured ceiling.
    if node.median_outbound_fee_rate_ppm is None:
        return True, 'UNKNOWN_FEE_RATE'
    if node.median_outbound_fee_rate_ppm > NODE_CRITERIA_MAX_FEE_RATE_PPM:
        return True, 'HIGH_FEE_RATE'
    # HTLC-limit gates. Treat missing data (NULL) as UNKNOWN — the
    # gossip pull populates both fields together, so either being
    # NULL means we haven't profiled this peer yet.
    if (node.median_min_htlc_msat is None
            or node.median_max_htlc_msat is None):
        return True, 'UNKNOWN_HTLC_LIMITS'
    if node.median_min_htlc_msat > NODE_CRITERIA_MAX_MIN_HTLC_MSAT:
        return True, 'HIGH_MIN_HTLC'
    # max_htlc floor = NODE_CRITERIA_MIN_MAX_HTLC_FRACTION × channel
    # size in msat. Setting the fraction to 0 disables this check.
    if NODE_CRITERIA_MIN_MAX_HTLC_FRACTION > 0:
        required_max_htlc_msat = int(
            NODE_CRITERIA_MIN_MAX_HTLC_FRACTION
            * LSP_CHANNEL_SIZE_SAT * 1000
        )
        if node.median_max_htlc_msat < required_max_htlc_msat:
            return True, 'LOW_MAX_HTLC'
    # Connectedness gates. NULL on either metric means the daily LND
    # gossip pull hasn't populated this row yet — treat as unknown
    # and reject (consistent with UNKNOWN_FEE_RATE policy).
    if node.effective_channel_count is None or node.two_hop_reach is None:
        return True, 'UNKNOWN_CONNECTEDNESS'
    if node.effective_channel_count < NODE_CRITERIA_MIN_EFFECTIVE_DEGREE:
        return True, 'LOW_EFFECTIVE_DEGREE'
    if node.two_hop_reach < NODE_CRITERIA_MIN_TWO_HOP_REACH:
        return True, 'LOW_TWO_HOP_REACH'
    uptime_reasons = _evaluate_uptime_signals(node)
    if uptime_reasons:
        return True, uptime_reasons[0]
    return False,'False'


def audit_existing_peer(node:LightningNode)->Tuple[bool,List[str]]:
    """Re-evaluate a peer we already have an open channel with against
    the degradation criteria. Returns (failed, reasons_list).

    Differs from is_node_blacklisted (the PRE-open gate):
      - Only checks the 5 criteria where degradation justifies a
        channel close: HIGH_FEE_RATE, LOW_EFFECTIVE_DEGREE,
        LOW_TWO_HOP_REACH, LOW_CAPACITY, LOW_OUTBOUND_CAPACITY.
      - Drops pre-open-only reasons: NO_IPV4 (channel already
        established, peer's address irrelevant for routing through
        existing channel), MIN_CHANNEL_COUNT (raw, superseded by
        effective degree), NOT_OLD_ENOUGH (age can't regress),
        REMOTE_CLOSE_COUNT (acts pre-open; once a peer closes our
        channel, the channel is gone before audit could act).
      - "UNKNOWN_*" reasons are treated as "skip audit this tick"
        (return passed=True) rather than as a fault — missing data
        means the graph pull hasn't run yet, not that the peer is bad.
      - AUDIT_BLACKLISTED is NOT checked here — that field is set BY
        this function, so re-checking it would be circular.

    Returns ALL matching reasons (not short-circuited at the first)
    so the caller can name every degradation in the close log.
    """
    # Handle missing-data case: peer is graph-pull-stale, not degraded.
    # Same data fields the pre-open blacklist treats as "UNKNOWN" we
    # treat here as "skip"; gives the next daily pull a chance to fill
    # them in before any close action is taken.
    if (node.total_capacity is None
            or node.median_outbound_fee_rate_ppm is None
            or node.effective_channel_count is None
            or node.two_hop_reach is None
            or node.median_min_htlc_msat is None
            or node.median_max_htlc_msat is None):
        return False, []

    reasons:List[str] = []
    if node.median_outbound_fee_rate_ppm > NODE_CRITERIA_MAX_FEE_RATE_PPM:
        reasons.append('HIGH_FEE_RATE')
    if node.effective_channel_count < NODE_CRITERIA_MIN_EFFECTIVE_DEGREE:
        reasons.append('LOW_EFFECTIVE_DEGREE')
    if node.two_hop_reach < NODE_CRITERIA_MIN_TWO_HOP_REACH:
        reasons.append('LOW_TWO_HOP_REACH')
    if node.total_capacity < NODE_CRITERIA_MINIMUM_CAPACITY:
        reasons.append('LOW_CAPACITY')
    required_outbound = (
        NODE_CRITERIA_OUTBOUND_CAPACITY_MULTIPLIER * LSP_CHANNEL_SIZE_SAT
    )
    if node.total_capacity < required_outbound:
        reasons.append('LOW_OUTBOUND_CAPACITY')
    # HTLC-limit gates. The None-guard above already filtered missing
    # data, so by here both medians are populated. If the peer has
    # raised their min_htlc ceiling above what we tolerate or dropped
    # their max_htlc below the floor since channel-open, audit closes.
    if node.median_min_htlc_msat > NODE_CRITERIA_MAX_MIN_HTLC_MSAT:
        reasons.append('HIGH_MIN_HTLC')
    if NODE_CRITERIA_MIN_MAX_HTLC_FRACTION > 0:
        required_max_htlc_msat = int(
            NODE_CRITERIA_MIN_MAX_HTLC_FRACTION
            * LSP_CHANNEL_SIZE_SAT * 1000
        )
        if node.median_max_htlc_msat < required_max_htlc_msat:
            reasons.append('LOW_MAX_HTLC')
    # Uptime gates — collected by the shared helper so this list stays
    # in sync with the selection-side blacklist gate. Both fail audit;
    # the audit pipeline will coop-close after 3 consecutive failure
    # days regardless of which uptime signal fired.
    reasons.extend(_evaluate_uptime_signals(node))

    return (len(reasons) > 0), reasons


node_db.connect()
node_db.create_tables([
    LightningNode, LightningChannel, LndPaymentLabel,
    SwapPriceQuote, LspPriceQuote, LspChannelOrder, Rebalance,
    DerivedAddressIndex,
])


def _migrate_lightning_channel_close_columns(_db=node_db) -> None:
    """Add `close_reason` and `closed_at` to LightningChannel for
    existing DBs. peewee's `create_tables(safe=True)` only creates
    missing tables — it never adds columns to a table that already
    exists. We have to ALTER TABLE explicitly.

    Idempotent: introspects PRAGMA table_info first and only issues
    ADD COLUMN when the column is genuinely missing. The CREATE INDEX
    statement uses IF NOT EXISTS so a re-run is harmless.

    Run unconditionally at module import (right after create_tables)
    so the next startup of an old installation upgrades cleanly without
    operator intervention.
    """
    cursor = _db.execute_sql("PRAGMA table_info('lightningchannel')")
    existing = {row[1] for row in cursor.fetchall()}
    if "close_reason" not in existing:
        _db.execute_sql(
            "ALTER TABLE lightningchannel ADD COLUMN close_reason VARCHAR(500) NULL"
        )
    if "closed_at" not in existing:
        _db.execute_sql(
            "ALTER TABLE lightningchannel ADD COLUMN closed_at DATETIME NULL"
        )
    _db.execute_sql(
        "CREATE INDEX IF NOT EXISTS lightningchannel_closed_at "
        "ON lightningchannel (closed_at)"
    )


_migrate_lightning_channel_close_columns()


def _migrate_lsp_channel_order_refund_columns(_db=node_db) -> None:
    """Add refund-tracking columns to LspChannelOrder for existing DBs.

    Refund tracking was added after we realised the LSPS1 spec allows
    the LSP to refund a paid order when channel-open fails — until
    then we silently lost visibility on FAILED orders and didn't
    reconcile refund txs in fee accounting. New deployments get the
    columns via create_tables; existing deployments need ALTER TABLE.

    Same pattern as _migrate_lightning_channel_close_columns:
    introspect PRAGMA, only ADD COLUMN when missing, idempotent.
    """
    cursor = _db.execute_sql("PRAGMA table_info('lspchannelorder')")
    existing = {row[1] for row in cursor.fetchall()}
    if "refund_onchain_address" not in existing:
        _db.execute_sql(
            "ALTER TABLE lspchannelorder ADD COLUMN refund_onchain_address VARCHAR(255) NULL"
        )
    if "refund_amount_sat" not in existing:
        _db.execute_sql(
            "ALTER TABLE lspchannelorder ADD COLUMN refund_amount_sat BIGINT NOT NULL DEFAULT 0"
        )
    if "refund_txid" not in existing:
        _db.execute_sql(
            "ALTER TABLE lspchannelorder ADD COLUMN refund_txid VARCHAR(64) NULL"
        )
    if "lsps1_state" not in existing:
        _db.execute_sql(
            "ALTER TABLE lspchannelorder ADD COLUMN lsps1_state VARCHAR(64) NULL"
        )
    if "refund_observed_onchain" not in existing:
        _db.execute_sql(
            "ALTER TABLE lspchannelorder ADD COLUMN refund_observed_onchain "
            "INTEGER NOT NULL DEFAULT 0"
        )


_migrate_lsp_channel_order_refund_columns()


print("LN Node database initialized successfully")