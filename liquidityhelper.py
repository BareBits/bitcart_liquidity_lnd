# Make our absolute imports of plugin-local modules (`database`,
# `classes`, `config`, `notifications`, `lnd_graph_pull`, etc.) work
# regardless of how this file is loaded:
#   - Standalone: invoked as a script from the plugin root, so the dir
#     is already first on sys.path. The insert below is a no-op.
#   - Plugin: bitcart loads us as
#     `modules.@barebits.liquidityhelper.liquidityhelper`, and the
#     plugin dir is NOT on sys.path. Without this, `import database`
#     would raise ModuleNotFoundError on the very next line. Putting
#     this at the top of the file makes the file self-bootstrapping so
#     every downstream import resolves before anyone reaches for it.
import os as _os, sys as _sys, os, sys
_PLUGIN_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _PLUGIN_DIR not in _sys.path:
    _sys.path.insert(0, _PLUGIN_DIR)

import json, dataclasses,math
from dataclasses import dataclass
from typing import Tuple, Union, Callable,Iterable,Set,Optional,Dict,List
import asyncio, database, inspect
import lnd_graph_pull
from peewee import DoesNotExist
import requests
import time

import notifications
from notifications import EmailNotificationProvider,NotificationProvider
from typing import Dict, Any, Optional


import common_functions
import config
from config import AUTH_TOKEN
import node_database
import traceback
from database import (
    SimpleDateTimeField,
    SimpleCacheField,
    LOrder,
    LastRunTracker,
    SimpleVariable,
    Notification,
)

from classes import (
    get_lightning_invoice,
    StoreStats,
    BitcartInvoice,
)
from common_functions import sats_to_btc, btc_to_sats
import datetime, sys

import logging
from logging.handlers import RotatingFileHandler
import queue

from classes import BitcartAPI
import dateutil.parser
from config import *
from copy import deepcopy
import hashlib

# ---------------------------------------------------------------------------
# Logging setup
#
# Three sinks:
#   - liquidityhelper.log : the operational firehose. Everything DEBUG and
#                           above. Rotated at 10 MB × 5. Read this when
#                           diagnosing an incident.
#   - decisions.log       : a higher-level audit of what the script actually
#                           DID and DECIDED. Routed via the
#                           `liquidityhelper.decisions` child logger (see
#                           log_event / log_decision below). Rotated at
#                           10 MB × 10 so you can usually find any decision
#                           from the last several months. Read this when
#                           asking "what happened?", not "why did X fail?".
#   - stdout              : live tail. INFO and above; never DEBUG. Per the
#                           operational rule that debug detail lives only
#                           in the log files.
#
# All three sinks are dispatched from a single QueueListener running on a
# background thread. Loggers' only attached handler is a QueueHandler that
# enqueues records; the disk writes and stdout writes happen off the event
# loop, so a slow disk or rotating-handler rename never freezes a tick.
# Add additional async-safe handlers at runtime via `add_async_log_handler`.
#
# The config knob `LOG_LEVEL` is preserved for backward compatibility but is
# now a no-op: the new defaults are fixed (see above). Operators wanting to
# trim file size further can edit this block directly.
# ---------------------------------------------------------------------------

main_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)


class _DecisionsOnlyFilter(logging.Filter):
    """Accept only records emitted on the decisions logger (or its
    children). Attached to handlers that should write *only* the
    decisions stream."""
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("liquidityhelper.decisions")


class _NotDecisionsFilter(logging.Filter):
    """Reject records emitted on the decisions logger. Attached to
    the operational file/console handlers so they don't double-log
    decision records (decisions have their own dedicated file sink and
    we use propagate=False to keep them out of the main file)."""
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("liquidityhelper.decisions")


logger = logging.getLogger("liquidityhelper")
# All messages reach the handlers; per-handler levels do the filtering.
logger.setLevel(logging.DEBUG)

# Operational + decisions log paths. CWD-relative (the historical
# default for standalone runs from the plugin root) breaks under
# bitcart, where CWD=/app and the electrum user can't write there —
# emitting any log record turns into a PermissionError that bubbles
# all the way up to a 500. Use the same plugin_data_dir resolution
# the SQLite files use (see database.py); add a try/except around the
# handler construction so a non-writable target degrades to console
# logging instead of crashing module load.
def _resolve_engine_log_dir() -> str:
    override = os.environ.get("LIQUIDITYHELPER_LOG_DIR")
    if override:
        try:
            os.makedirs(override, exist_ok=True)
            if os.access(override, os.W_OK):
                return override
        except OSError:
            pass
    for candidate in (os.environ.get("BITCART_DATADIR"), "/datadir"):
        if candidate and os.path.isdir(candidate):
            plugin_data = os.path.join(candidate, "plugin_data", "liquidityhelper")
            try:
                os.makedirs(plugin_data, exist_ok=True)
            except OSError:
                continue
            if os.access(plugin_data, os.W_OK):
                return plugin_data
    # Standalone fallback: the dir next to this file.
    here = os.path.dirname(os.path.abspath(__file__))
    return here if os.access(here, os.W_OK) else "."

_ENGINE_LOG_DIR = _resolve_engine_log_dir()

# Operational file: DEBUG and above so post-mortems have full detail.
# Filter out decisions records — they have their own file sink.
file_handler = RotatingFileHandler(
    os.path.join(_ENGINE_LOG_DIR, "liquidityhelper.log"),
    maxBytes=10_000_000, backupCount=5,
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(main_formatter)
file_handler.addFilter(_NotDecisionsFilter())

# Decisions file: INFO and above; accepts only decisions records.
_decisions_file_handler = RotatingFileHandler(
    os.path.join(_ENGINE_LOG_DIR, "decisions.log"),
    maxBytes=10_000_000, backupCount=10,
)
_decisions_file_handler.setLevel(logging.INFO)
_decisions_file_handler.setFormatter(main_formatter)
_decisions_file_handler.addFilter(_DecisionsOnlyFilter())

# Console (stdout): INFO and above only. Never DEBUG — debug detail is
# captured in the log file.
console_handler = logging.StreamHandler(stream=sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(main_formatter)

# One shared queue, one listener. The listener fans records out to all
# attached handlers on a background thread. respect_handler_level=True
# so each handler still applies its own level filter at dispatch time.
log_queue: "queue.Queue[Any]" = queue.Queue(2000)
listener = logging.handlers.QueueListener(
    log_queue,
    file_handler,
    _decisions_file_handler,
    console_handler,
    respect_handler_level=True,
)
listener.start()

queue_handler = logging.handlers.QueueHandler(log_queue)
logger.addHandler(queue_handler)

# Decisions logger — separate sink, separate retention. propagate=False
# so decision lines do NOT also appear in liquidityhelper.log; we attach
# the queue handler directly here so the listener's _decisions_file_handler
# (gated by the decisions-only filter) is the one that picks them up.
decisions_logger = logging.getLogger("liquidityhelper.decisions")
decisions_logger.setLevel(logging.INFO)
decisions_logger.propagate = False
decisions_logger.addHandler(queue_handler)


def add_async_log_handler(handler: logging.Handler) -> None:
    """Attach a handler to the background QueueListener so its emit()
    runs off the event loop. Use this instead of `logger.addHandler`
    for any new file/network sink — direct addHandler attaches the
    handler to the logger, which makes its writes synchronous and
    blocks the event loop on every log call.

    Idempotent: the same handler instance is added at most once.
    """
    if handler in listener.handlers:
        return
    handlers = list(listener.handlers)
    handlers.append(handler)
    listener.stop()
    listener.handlers = tuple(handlers)
    listener.start()


def stop_log_listener() -> None:
    """Flush the queue and stop the listener thread. Called at engine
    shutdown so the process can exit cleanly without losing buffered
    records."""
    try:
        listener.stop()
    except Exception:
        pass


import atexit
# Standalone runs end via SIGINT or natural completion. Without atexit
# the listener's daemon thread is killed mid-flight and the last few
# enqueued records vanish. Plugin mode calls stop_log_listener()
# explicitly in shutdown(), so atexit there is just a redundant
# safety net (idempotent).
atexit.register(stop_log_listener)


# In-memory dedup state for log_decision(). Cleared on process restart;
# the first decision after restart always logs (even if it matches what
# the last process recorded), which is the correct behavior for a
# restart-aware audit log.
_last_decision_state: Dict[Any, Any] = {}


def log_event(message: str, *args, **kwargs) -> None:
    """Record a discrete event in decisions.log. Use for things that
    should appear *every* time they happen — channel opened, cashout
    dispatched, fee payment sent, swap initiated.

    Equivalent to decisions_logger.info() with a convention.
    """
    decisions_logger.info(message, *args, **kwargs)


def log_decision(
    key: Any, value: Any, message: str, *args,
    level: int = logging.INFO, **kwargs,
) -> None:
    """Record a re-evaluated state in decisions.log, but only when (key,
    value) differs from the previous call's value for the same key.

    Use for tick-frequency status: rail choice, "liquidity is fine /
    needs more", per-store topup amount. Naive logging of these would
    produce one identical line per tick; this helper logs only on the
    transitions you actually care about.

    `level` lets callers emit transitions at WARNING/ERROR instead of
    the default INFO when the state change is operationally serious
    (e.g. "your funds are stranded"). The dedupe behavior is the same
    either way — only transitions log, not every tick.

    First call for a given key always logs (no prior value to compare).
    """
    if _last_decision_state.get(key) == value:
        return
    _last_decision_state[key] = value
    decisions_logger.log(level, message, *args, **kwargs)


_HEARTBEAT_EVERY_N_TICKS = 100   # Tick rate is work-dominated (no fixed sleep); typically tens of seconds per tick, so ~30-90 min between heartbeats in steady state.
_tick_counter = 0


def maybe_emit_heartbeat() -> None:
    """Emit a 'still alive' line to decisions.log every N ticks. Long
    gaps between heartbeats in decisions.log indicate the script
    stopped — useful for post-hoc 'when did it crash?' triage."""
    global _tick_counter
    _tick_counter += 1
    if _tick_counter % _HEARTBEAT_EVERY_N_TICKS == 0:
        log_event("heartbeat: tick %d (script alive)", _tick_counter)


def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    logger.critical(
        "uncaught exception, application will terminate.",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


sys.excepthook = handle_uncaught_exception

from node_database import LightningNode, LightningChannel,is_node_blacklisted, audit_existing_peer

LAST_FEE_CHECK = datetime.datetime.now()
START_TIME = datetime.datetime.now()
NOTIFICATION_PROVIDERS:List[NotificationProvider]=[]
def hash_string(mystring: str) -> str:
    # Encode the string to bytes
    encoded_string = mystring.encode("utf-8")
    # Create a new hash object and update it with the encoded string
    hash_object = hashlib.sha256(encoded_string)
    # Get the hexadecimal representation of the hash
    hex_digest = hash_object.hexdigest()

    return hex_digest


async def run_every_x_seconds(
    *args, my_func: Callable, hash_arguments: bool = False, seconds: int, **kwargs
):
    """
    Run a given function my_func but only if it hasn't been run in x seconds.
    If hash_arguments is false, every run of the function counts as a recent run, otherwise, a recent run only counts if the arguments match exactly (including order)
    Haven't thoroughly tested the hash_arguments function or made it order-agnostic
    """
    if hash_arguments:
        hash_source = str(my_func.__name__) + str(args) + str(kwargs)
    else:
        hash_source = str(my_func.__name__)
    object_hash = hash_string(hash_source)
    db_object: Optional[LastRunTracker] = LastRunTracker.get_or_none(name=object_hash)
    if not db_object:
        my_tracker = LastRunTracker(name=object_hash)
        my_tracker.save()
        logger.debug(f"Running :{my_func.__name__} bc never run before.")
        if inspect.iscoroutinefunction(my_func):
            return await my_func(*args, **kwargs)
        else:
            return my_func(*args, **kwargs)
    else:
        last_run = db_object.last_run
        time_difference = datetime.datetime.now() - last_run
        seconds_ago = time_difference.total_seconds()
        if seconds_ago > seconds:
            logger.debug(
                f"Running {my_func.__name__} bc hasnt been run in {seconds} seconds"
            )
            db_object.last_run = datetime.datetime.now()
            db_object.save()
            if inspect.iscoroutinefunction(my_func):
                return await my_func(*args, **kwargs)
            else:
                return my_func(*args, **kwargs)
        logger.debug(
            f"Not running {my_func.__name__} bc has been run {seconds_ago} ago which is less than target {seconds} seconds"
        )
        return None


async def run_every_x_minutes(*args, my_func: Callable, minutes: int, **kwargs):
    return await run_every_x_seconds(seconds=minutes * 60, my_func=my_func, *args, **kwargs)


async def run_every_x_hours(*args, my_func: Callable, hours: int, **kwargs):
    return await run_every_x_seconds(seconds=hours * 60 * 60, my_func=my_func, *args, **kwargs)


async def run_every_x_days(*args, my_func: Callable, days: int, **kwargs):
    return await run_every_x_seconds(seconds=days * 24 * 60 * 60, my_func=my_func, *args, **kwargs)


async def _lnd_list_channels(api: "BitcartAPI", wallet_id: str) -> List[Dict[str, Any]]:
    """Normalize LND's Lightning.ListChannels output into the same shape
    `find_offline_channels` already consumes from Electrum's list_channels.
    """
    resp = await lnd_rpc(api, wallet_id, "ListChannels", {}, "Lightning")
    if not isinstance(resp, dict):
        return []
    out: List[Dict[str, Any]] = []
    for c in resp.get("channels", []) or []:
        out.append({
            "remote_pubkey": (c.get("remote_pubkey") or "").lower(),
            "channel_point": c.get("channel_point") or "",
            "short_channel_id": str(c.get("chan_id") or c.get("channel_point") or ""),
            # Lightning.ListChannels only returns channels in OPEN state;
            # pending/closing/closed live in PendingChannels/ClosedChannels.
            "state": "OPEN",
            # Electrum's peer_state vocabulary is "CONNECTED"/"GOOD"/"DISCONNECTED";
            # LND just gives us a boolean.
            "peer_state": "GOOD" if c.get("active") else "DISCONNECTED",
        })
    return out


async def _lnd_channel_tx_hashes(api: "BitcartAPI", wallet_id: str) -> Tuple[set, set]:
    """Build (funding_tx_hashes, closing_tx_hashes) sets for the wallet's LND.
    Used by list_onchain_history to label channel-open/close txs structurally
    rather than relying on a `label` string LND wouldn't have written."""
    funding: set = set()
    closing: set = set()
    # Currently-open channels.
    list_resp = await lnd_rpc(api, wallet_id, "ListChannels", {}, "Lightning") or {}
    for c in list_resp.get("channels") or []:
        cp = (c.get("channel_point") or "").split(":")[0]
        if cp:
            funding.add(cp.lower())
    # Pending opens (funding tx broadcast, < 6 confs).
    pending_resp = await lnd_rpc(api, wallet_id, "PendingChannels", {}, "Lightning") or {}
    for c in pending_resp.get("pending_open_channels") or []:
        cp = (c.get("channel", {}).get("channel_point") or "").split(":")[0]
        if cp:
            funding.add(cp.lower())
    # Pending closes — funding tx still relevant, closing tx newly observable.
    for key in ("waiting_close_channels", "pending_closing_channels", "pending_force_closing_channels"):
        for c in pending_resp.get(key) or []:
            cp = (c.get("channel", {}).get("channel_point") or "").split(":")[0]
            if cp:
                funding.add(cp.lower())
            close_tx = c.get("closing_txid") or c.get("closing_tx_hash") or ""
            if close_tx:
                closing.add(close_tx.lower())
    # Fully closed channels.
    closed_resp = await lnd_rpc(api, wallet_id, "ClosedChannels", {}, "Lightning") or {}
    for c in closed_resp.get("channels") or []:
        cp = (c.get("channel_point") or "").split(":")[0]
        if cp:
            funding.add(cp.lower())
        ct = c.get("closing_tx_hash") or c.get("closing_txid") or ""
        if ct:
            closing.add(ct.lower())
    return funding, closing


async def _lnd_list_onchain_history(api: "BitcartAPI", wallet_id: str) -> List[Dict[str, Any]]:
    """Normalize Lightning.GetTransactions into Electrum's onchain_history shape.

    Each row matches what is_ln_open_transaction / is_ln_close_transaction
    and new_calc_invoice_stats already consume from Electrum:
      - txid, incoming, fee_sat, label

    For channel-funding txs we inject label="OPEN CHANNEL"; for closing txs
    label="CLOSE CHANNEL". This keeps the existing label-string-based logic
    in is_ln_open/close_transaction working unchanged for LND wallets.
    """
    funding, closing = await _lnd_channel_tx_hashes(api, wallet_id)
    resp = await lnd_rpc(api, wallet_id, "GetTransactions", {}, "Lightning") or {}
    out: List[Dict[str, Any]] = []
    for t in resp.get("transactions") or []:
        tx_hash = (t.get("tx_hash") or "").lower()
        amount_sat = int(t.get("amount") or 0)
        if tx_hash in funding:
            label = "OPEN CHANNEL"
        elif tx_hash in closing:
            label = "CLOSE CHANNEL"
        else:
            label = t.get("label") or ""
        # `time_stamp` is LND's unix-seconds (string or int depending
        # on version). Used by the dashboard's "Recent fee payments" /
        # "Recent cashouts" tables to sort and display dates.
        # `dest_addresses` is LND's list of output addresses, useful
        # for surfacing where a payment went; we pass the first one.
        dest_addresses = t.get("dest_addresses") or []
        out.append({
            "txid": tx_hash,
            "incoming": amount_sat > 0,
            "fee_sat": int(t.get("total_fees") or 0),
            "label": label,
            "amount_sat": amount_sat,
            "block_height": int(t.get("block_height") or 0),
            "num_confirmations": int(t.get("num_confirmations") or 0),
            "timestamp": int(t.get("time_stamp") or 0),
            "dest_address": (dest_addresses[0] if dest_addresses else ""),
        })
    return out


async def list_onchain_history(
    *,
    wallet: Dict[str, Any],
    api: Optional["BitcartAPI"] = None,
) -> List[Dict[str, Any]]:
    """Wallet-aware on-chain history. Dispatcher counterpart to
    `electrum_rpc("onchain_history", ...)` that knows how to ask LND wallets
    via `Lightning.GetTransactions` and inject OPEN CHANNEL / CLOSE CHANNEL
    labels by tx_hash matching."""
    if wallet.get("currency") == "btclnd":
        if api is None and wallet["id"] not in _LND_CONNECTIONS:
            raise ValueError(
                "list_onchain_history: LND path needs either `api` or a "
                "pre-populated _LND_CONNECTIONS[wallet['id']] entry"
            )
        return await _lnd_list_onchain_history(api, wallet["id"])
    resp = await electrum_rpc("onchain_history", wallet.get("xpub"))
    result = resp.get("result")
    # Electrum's `onchain_history` wraps the tx list inside a summary dict:
    # {"summary": {...}, "transactions": [...]}. Some Bitcart server flavors
    # unwrap that to a bare list before returning. Accept both shapes.
    if isinstance(result, dict):
        return result.get("transactions") or []
    return result or []


async def _lnd_list_ln_payments(api: "BitcartAPI", wallet_id: str) -> List[Dict[str, Any]]:
    """Normalize Lightning.ListPayments into Electrum's lightning_history shape.

    Each row matches what new_calc_invoice_stats consumes from Electrum:
      - type='payment', amount_msat<0 for outgoing, fee_msat, label.

    The `label` field is joined from our LndPaymentLabel side-table keyed by
    payment_hash (written on successful sends by _lnd_pay_ln_invoice). LND
    has no native equivalent for outgoing-payment labels.
    """
    resp = await lnd_rpc(api, wallet_id, "ListPayments", {}, "Lightning") or {}
    payments = resp.get("payments") or []
    if not payments:
        return []
    # One DB lookup keyed by all observed payment_hashes; cheaper than per-row.
    from node_database import LndPaymentLabel
    hashes = [str(p.get("payment_hash") or "").lower() for p in payments]
    labels_by_hash: Dict[str, str] = {}
    try:
        rows = LndPaymentLabel.select().where(LndPaymentLabel.payment_hash.in_(hashes))
        labels_by_hash = {r.payment_hash: r.label for r in rows}
    except Exception as e:
        logger.warning(f"LndPaymentLabel lookup failed: {e}")
    out: List[Dict[str, Any]] = []
    for p in payments:
        # Skip in-flight / failed; only SUCCEEDED corresponds to a settled
        # outgoing payment Electrum would have shown in lightning_history.
        status = p.get("status") or ""
        if isinstance(status, str) and status.upper() not in ("SUCCEEDED", "2"):
            continue
        payment_hash = str(p.get("payment_hash") or "").lower()
        value_msat = int(p.get("value_msat") or 0)
        fee_msat = int(p.get("fee_msat") or 0)
        # creation_time_ns is LND's nanosecond unix timestamp. The
        # dashboard expects seconds-precision; divide. Falls back to
        # 0 if missing (very old LND or stub responses).
        creation_ns = int(p.get("creation_time_ns") or 0)
        out.append({
            "type": "payment",
            "amount_msat": -abs(value_msat),  # outgoing -> negative, matches Electrum
            "fee_msat": fee_msat,
            "label": labels_by_hash.get(payment_hash, ""),
            "payment_hash": payment_hash,
            "payment_request": p.get("payment_request") or "",
            "timestamp": creation_ns // 1_000_000_000 if creation_ns else 0,
        })
    return out


async def list_ln_payments_with_labels(
    *,
    wallet: Dict[str, Any],
    api: Optional["BitcartAPI"] = None,
) -> List[Dict[str, Any]]:
    """Wallet-aware LN payment history. Dispatcher counterpart to
    `electrum_rpc("lightning_history", ...)` that pulls from LND's
    ListPayments and joins the per-payment label from our LndPaymentLabel
    side-table."""
    if wallet.get("currency") == "btclnd":
        if api is None and wallet["id"] not in _LND_CONNECTIONS:
            raise ValueError(
                "list_ln_payments_with_labels: LND path needs either `api` or a "
                "pre-populated _LND_CONNECTIONS[wallet['id']] entry"
            )
        return await _lnd_list_ln_payments(api, wallet["id"])
    resp = await electrum_rpc("lightning_history", wallet.get("xpub"))
    return resp.get("result") or []


async def find_offline_channels(
    *,
    wallet: Dict[str, Any],
    api: Optional["BitcartAPI"] = None,
):
    """Record per-peer uptime samples for every OPEN channel on this
    wallet. Does NOT close channels — that decision lives in the
    daily `audit_existing_peer` pipeline now, which reads the same
    counters this function maintains.

    Per peer (deduped within a single call), this:
      - Ensures a LightningNode row exists (defensive — peers we have
        a channel open with may not be in our local DB yet if the
        daily LND gossip pull hasn't gotten to them).
      - Rolls the 6-month observation window if it's expired:
        recent_* counters reset to zero and current_window_started_at
        advances to now. The lifetime total_* counters are never reset.
      - Increments recent_uptime_checks AND total_uptime_checks.
      - On peer_state == CONNECTED/GOOD: sets last_seen_online = now.
      - On peer_state == DISCONNECTED: increments recent_failed_uptime_checks
        AND failed_uptime_checks.

    The HIGH_FAILURE_RATIO and LONG_OUTAGE gates in is_node_blacklisted +
    audit_existing_peer read these fields. find_offline_channels itself
    never decides to close a channel; it just records the data.

    Cadence: this function is throttled to UPTIME_CHECK_INTERVAL_MINUTES
    by its caller (the per-tick `await run_every_x_minutes(...)`
    wrapper in liquidity_check). At default 10-min cadence, a 6-month
    rolling window holds ~26k samples — enough for the failure-ratio
    gate to be statistically meaningful AND for a 2-day outage to
    represent a small fraction of the window.

    Dispatch is keyed off `wallet["currency"]`:
      - "btclnd"  -> Lightning.ListChannels gRPC (normalized to electrum shape)
      - anything  -> Electrum's list_channels JSON-RPC.
    """
    if wallet.get("currency") == "btclnd":
        if api is None and wallet["id"] not in _LND_CONNECTIONS:
            raise ValueError(
                "find_offline_channels: LND path needs either `api` or a "
                "pre-populated _LND_CONNECTIONS[wallet['id']] entry"
            )
        channels = await _lnd_list_channels(api, wallet["id"])
    else:
        found_channels = await electrum_rpc(
            "list_channels", myxpub=wallet.get("xpub"),
        )
        channels = found_channels["result"]
    now = datetime.datetime.now()
    window = datetime.timedelta(days=UPTIME_ROLLING_WINDOW_DAYS)
    checked_peers = set()
    for channel in channels:
        peer_address = channel["remote_pubkey"].lower()
        peer_state = channel["peer_state"]
        channel_state = channel["state"]
        channel_id = channel["short_channel_id"]
        if peer_address in checked_peers:
            continue
        checked_peers.add(peer_address)
        node_object: Optional[LightningNode] = LightningNode.get_or_none(
            LightningNode.node_address == peer_address
        )
        if not node_object:
            logger.warning(
                f"find_offline_channels: peer with no LightningNode row: "
                f"{peer_address} for channel id {channel_id}; creating one."
            )
            node_object = LightningNode(
                node_address=peer_address,
                last_lnd_query=datetime.datetime(1990, 12, 12, 12, 12, 12),
            )
            node_object.save(force_insert=True)
        if channel_state in {"REDEEMED", "CLOSED", "OPENING"}:
            continue
        if channel_state != "OPEN":
            logger.warning(
                f"find_offline_channels: unknown channel state {channel_state} "
                f"for peer {peer_address} channel {channel_id}; skipping."
            )
            continue

        # Roll the window if expired. First-ever check starts a new
        # window. Reset semantics: the rolling counters return to zero
        # so a peer that's been bad over the LAST window doesn't carry
        # that history forward forever.
        if node_object.current_window_started_at is None:
            node_object.current_window_started_at = now
            node_object.recent_uptime_checks = 0
            node_object.recent_failed_uptime_checks = 0
        elif now - node_object.current_window_started_at > window:
            node_object.current_window_started_at = now
            node_object.recent_uptime_checks = 0
            node_object.recent_failed_uptime_checks = 0

        node_object.total_uptime_checks += 1
        node_object.recent_uptime_checks += 1
        if peer_state in {"CONNECTED", "GOOD"}:
            node_object.last_seen_online = now
        elif peer_state == "DISCONNECTED":
            node_object.failed_uptime_checks += 1
            node_object.recent_failed_uptime_checks += 1
        else:
            logger.warning(
                f"find_offline_channels: unknown peer_state {peer_state} "
                f"for {peer_address} channel {channel_id}; not counted as "
                f"either success or failure."
            )
        node_object.save()


async def get_channel_partners(
    url: str,
    max_retries: int = 5,
    initial_backoff: float = 1.0,
    backoff_multiplier: float = 2.0,
    timeout: int = 10,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[List[Dict[str, str]]]:
    """Fetch a JSON file from a URL with exponential-backoff retries.

    Async to avoid blocking the event loop — previously this used
    `requests.get` (sync) and `time.sleep(backoff)` between retries,
    which in plugin mode would freeze the entire Bitcart worker for
    up to ~31s on a flaky network.

    Args:
        url: The URL to fetch the JSON from
        max_retries: Maximum number of retry attempts (default: 5)
        initial_backoff: Initial backoff time in seconds (default: 1.0)
        backoff_multiplier: Multiplier for exponential backoff (default: 2.0)
        timeout: Request timeout in seconds (default: 10)
        headers: Optional headers to include in the request

    Returns:
        Parsed JSON data.

    Raises:
        httpx.HTTPError: If all retry attempts fail.
        ValueError: If the response is not valid JSON.
    """
    import httpx
    backoff = initial_backoff
    last_exception: Optional[Exception] = None

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for attempt in range(max_retries + 1):
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                last_exception = e
                status_code = e.response.status_code
                # Don't retry on client errors (4xx except 429)
                if 400 <= status_code < 500 and status_code != 429:
                    raise
                if attempt == max_retries:
                    raise
                logger.warning(
                    f"Request failed ({status_code}, attempt "
                    f"{attempt + 1}/{max_retries + 1}); retrying in "
                    f"{backoff:.2f}s"
                )
                await asyncio.sleep(backoff)
                backoff *= backoff_multiplier
            except httpx.HTTPError as e:
                last_exception = e
                if attempt == max_retries:
                    raise
                logger.warning(
                    f"Request failed ({type(e).__name__}: {e}, attempt "
                    f"{attempt + 1}/{max_retries + 1}); retrying in "
                    f"{backoff:.2f}s"
                )
                await asyncio.sleep(backoff)
                backoff *= backoff_multiplier
            except ValueError as e:
                raise ValueError(f"Invalid JSON response from {url}: {e}")

    # Defensive — the loop always either returns or raises before here.
    if last_exception:
        raise last_exception
    return None


def payment_made(payment: dict) -> bool:
    """
    Helper function to determine if a payment has been made or not
    """
    if payment["is_used"]:
        return True
    return False

def is_ln_open_transaction(transaction: Dict[str, Union[str, float]]) -> bool:
    """
    Given transaction from electrum, return True if is a transaction to open a LN channel
    """
    if not transaction["label"]:
        return False
    if "OPEN CHANNEL" in transaction["label"].upper():
        return True
    return False


def is_ln_close_transaction(transaction: Dict[str, Union[str, float]]) -> bool:
    """
    Given transaction from electrum, return True if is a transaction to open a LN channel
    """
    if not transaction["label"]:
        return False
    if "CLOSE CHANNEL" in transaction["label"].upper():
        return True
    return False


def is_swap_transaction(transaction: Dict[str, Union[str, float]]) -> bool:
    """Return True if `transaction` is a submarine-swap HTLC/sweep tx.

    Detection is purely label-based: loopd auto-labels its on-chain txs via
    LND's LabelTransaction with strings like `loop-out: <swap_id>` /
    `loop-in: <swap_id>` / `loop-out htlc: <swap_id>` etc. Any label
    starting with `loop-out` or `loop-in` (case-insensitive) is considered
    a swap tx. Future providers that use the same labeling pattern would
    Just Work; providers that don't would need a parallel helper.
    """
    label = transaction.get("label") or ""
    if not label:
        return False
    norm = label.upper()
    return norm.startswith("LOOP-OUT") or norm.startswith("LOOP-IN") \
        or "SWAP" in norm and ("LOOP" in norm or "HTLC" in norm)


def is_lsp_channel_order_transaction(
    transaction: Dict[str, Union[str, float]],
) -> bool:
    """True if `transaction` is the on-chain payment we sent to an LSP
    to fund a channel order.

    Detection: `electrum_pay_onchain` writes the label
    `lsp_channel_order:<order_id>` for both LND (via
    SendCoinsRequest.label) and Electrum (via setlabel). The label is
    case-preserved by both wallet types; we match case-insensitively.

    The `fee_sat` on this tx is the miner fee; `amount_sat` is the
    principal we paid the LSP, which is also the LSP's service fee
    (because LSPS1 client_balance_sat=0 in our requests means the
    entire payment goes to the LSP as their channel-open fee).
    """
    label = (transaction.get("label") or "").lower()
    return label.startswith("lsp_channel_order:")


async def electrum_rpc(method, myxpub: str, params: Dict[str, str] = None):
    """JSON-RPC bridge to the local Electrum daemon. Async via httpx so
    we don't freeze the event loop for the duration of the call —
    `lnpay` in particular can legitimately take ~2 minutes waiting for
    route+HTLC settlement, and in plugin mode a sync `requests.post`
    here would hang every other Bitcart HTTP request served by the
    same worker for the duration.
    """
    import httpx
    if not params:
        params = {}
    params["xpub"] = myxpub
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 0}
    timeout_s = 129 if method == "lnpay" else 60
    async with httpx.AsyncClient(
        timeout=timeout_s, auth=("electrum", "electrumz"),
    ) as client:
        response = await client.post(
            "http://localhost:5000", json=payload,
        )
        return response.json()


# ----------------------------------------------------------------------------
# LND gRPC bridge — counterpart to electrum_rpc for the BareBits LND fork.
# ----------------------------------------------------------------------------
import base64 as _base64
import codecs as _codecs

import grpc as _grpc
from google.protobuf.json_format import MessageToDict as _MessageToDict
from google.protobuf.json_format import ParseDict as _ParseDict

from lnd_proto import (
    chainnotifier_pb2 as _chainnotifier_pb2,
    chainnotifier_pb2_grpc as _chainnotifier_pb2_grpc,
    invoices_pb2 as _invoices_pb2,
    invoices_pb2_grpc as _invoices_pb2_grpc,
    lightning_pb2 as _lightning_pb2,
    lightning_pb2_grpc as _lightning_pb2_grpc,
    router_pb2 as _router_pb2,
    router_pb2_grpc as _router_pb2_grpc,
    signer_pb2 as _signer_pb2,
    signer_pb2_grpc as _signer_pb2_grpc,
    walletkit_pb2 as _walletkit_pb2,
    walletkit_pb2_grpc as _walletkit_pb2_grpc,
)

# service_name -> (stub_class, pb2_module)
_LND_SERVICES = {
    "Lightning": (_lightning_pb2_grpc.LightningStub, _lightning_pb2),
    "Router": (_router_pb2_grpc.RouterStub, _router_pb2),
    "WalletKit": (_walletkit_pb2_grpc.WalletKitStub, _walletkit_pb2),
    "Invoices": (_invoices_pb2_grpc.InvoicesStub, _invoices_pb2),
    "ChainNotifier": (_chainnotifier_pb2_grpc.ChainNotifierStub, _chainnotifier_pb2),
    "Signer": (_signer_pb2_grpc.SignerStub, _signer_pb2),
}

# wallet_id -> {"channel": grpc.aio.Channel, "stubs": {service_name: stub}}
_LND_CONNECTIONS: Dict[str, Dict[str, Any]] = {}

# Per-wallet locks serializing the cache-or-build path. Without
# these, two concurrent _get_lnd_connection(api, "w1") calls would
# both see "not in cache", both fetch info + build a gRPC channel,
# the second writes its conn dict over the first, and the first
# channel object is leaked (no `await channel.close()`). gRPC
# channels hold real OS resources (sockets, threads); leaking them
# over the lifetime of a long-running plugin process accumulates.
_LND_CONNECTION_LOCKS: Dict[str, asyncio.Lock] = {}
_LND_CONNECTION_LOCKS_GUARD = asyncio.Lock()

# Match the daemon's MAX_MSG_SIZE so large responses don't get truncated.
_LND_MAX_MSG_SIZE = 50 * 1024 * 1024


async def _get_lnd_connection_lock(wallet_id: str) -> asyncio.Lock:
    """Return (or lazily create) the asyncio.Lock for `wallet_id`.

    Lock creation itself is serialized by _LND_CONNECTION_LOCKS_GUARD
    so two coroutines trying to lock the same brand-new wallet_id at
    the same time can't both create their own lock objects (which
    would defeat the per-wallet locking).
    """
    if wallet_id in _LND_CONNECTION_LOCKS:
        return _LND_CONNECTION_LOCKS[wallet_id]
    async with _LND_CONNECTION_LOCKS_GUARD:
        if wallet_id not in _LND_CONNECTION_LOCKS:
            _LND_CONNECTION_LOCKS[wallet_id] = asyncio.Lock()
        return _LND_CONNECTION_LOCKS[wallet_id]


async def _get_lnd_connection(api: BitcartAPI, wallet_id: str) -> Dict[str, Any]:
    """Build (and cache) the gRPC channel + stubs for a wallet.

    Concurrent calls for the same wallet_id are serialized so the
    cache-or-build decision is atomic — only one channel ever gets
    built per wallet, even when many coroutines race to call this.
    """
    # Fast path: already cached. No lock needed for the read because
    # dict reads of an existing key are atomic under the GIL, and
    # entries never get removed once added.
    if wallet_id in _LND_CONNECTIONS:
        return _LND_CONNECTIONS[wallet_id]
    lock = await _get_lnd_connection_lock(wallet_id)
    async with lock:
        # Re-check under the lock — a coroutine that was waiting on
        # the lock may find the cache populated by the one that got
        # it first.
        if wallet_id in _LND_CONNECTIONS:
            return _LND_CONNECTIONS[wallet_id]
        info = await api.get_lnd_info(wallet_id)
        if not info:
            raise RuntimeError(f"Could not fetch LND info for wallet {wallet_id}")
        # Plugin-mode host override. bitcart's get_lnd_info() returns
        # the host LND advertises to itself ("127.0.0.1") — works on a
        # laptop with an SSH-tunneled gRPC port but fails inside the
        # backend container, where 127.0.0.1 isn't where LND lives.
        # When the bitcart container env var BITCART_BACKEND_ROOTPATH is
        # set we know we are running inside a bitcart backend; in that
        # case dial the docker DNS name of the LND wallet's daemon
        # container instead. The port stays whatever bitcart reports for
        # this particular wallet so multi-wallet installs (one LND
        # process per wallet, each on a distinct port in btclnd's range)
        # all reach their own LND. LIQUIDITYHELPER_LND_HOST is an
        # explicit override for tests and unusual topologies.
        host = info["host"]
        host_override = _os.environ.get("LIQUIDITYHELPER_LND_HOST")
        if host_override:
            host = host_override
        elif _os.environ.get("BITCART_BACKEND_ROOTPATH") and host in ("127.0.0.1", "localhost"):
            host = "btclnd"
        cert = _base64.b64decode(info["tls_cert"])
        macaroon_hex = _codecs.encode(_base64.b64decode(info["macaroon"]), "hex").decode()
        ssl_creds = _grpc.ssl_channel_credentials(root_certificates=cert)

        def _macaroon_callback(_context, callback):
            callback([("macaroon", macaroon_hex)], None)

        creds = _grpc.composite_channel_credentials(
            ssl_creds, _grpc.metadata_call_credentials(_macaroon_callback)
        )
        # When dialing via a host that doesn't match the cert's SANs
        # (LND only signs for localhost + 127.0.0.1 by default), tell
        # gRPC to validate the cert against `localhost` instead of the
        # dial hostname. The cert authority + key material still get
        # validated normally — this only relaxes the hostname check,
        # which is meaningful for public-internet TLS but redundant on
        # a private docker network where the network identity IS the
        # security boundary. Only do this when the host actually
        # differs from the cert's expected name; standalone-mode
        # callers dialing 127.0.0.1 don't need this override and get
        # full verification.
        channel_options = [
            ("grpc.max_receive_message_length", _LND_MAX_MSG_SIZE),
            ("grpc.max_send_message_length", _LND_MAX_MSG_SIZE),
        ]
        if host not in ("127.0.0.1", "localhost"):
            channel_options.append(("grpc.ssl_target_name_override", "localhost"))
        channel = _grpc.aio.secure_channel(
            f"{host}:{info['grpc_port']}",
            creds,
            options=channel_options,
        )
        stubs = {name: stub_cls(channel) for name, (stub_cls, _) in _LND_SERVICES.items()}
        conn = {"channel": channel, "stubs": stubs, "info": info}
        _LND_CONNECTIONS[wallet_id] = conn
        return conn


async def lnd_rpc(
    api: BitcartAPI,
    wallet_id: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    service: str = "Lightning",
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """Bare LND gRPC dispatcher — counterpart to electrum_rpc.

    Resolves connection details for the wallet via api.get_lnd_info (cached),
    opens a TLS+macaroon gRPC channel (cached), and dispatches `method` on
    the chosen `service` stub. Request fields come from `params` (dict ->
    proto via ParseDict). Responses are converted back to plain dicts via
    MessageToDict with snake_case field names. Server-streaming responses
    are collected into a list[dict].

    Args:
        api: BitcartAPI instance for the bitcart server hosting this wallet.
        wallet_id: bitcart wallet id.
        method: gRPC method name on `service`, e.g. "ListChannels".
        params: dict of request fields (snake_case, matching the proto).
        service: which gRPC service hosts the method. One of:
                 Lightning, Router, WalletKit, Invoices, ChainNotifier, Signer.
                 Defaults to Lightning.

    Returns:
        dict for unary methods, list[dict] for server-streaming methods.
    """
    if service not in _LND_SERVICES:
        raise ValueError(
            f"Unknown LND gRPC service: {service}. "
            f"One of: {sorted(_LND_SERVICES)}"
        )
    conn = await _get_lnd_connection(api, wallet_id)
    stub = conn["stubs"][service]
    _, pb2_module = _LND_SERVICES[service]
    service_desc = pb2_module.DESCRIPTOR.services_by_name[service]
    if method not in service_desc.methods_by_name:
        raise ValueError(f"Unknown gRPC method: {service}.{method}")
    method_desc = service_desc.methods_by_name[method]
    if method_desc.client_streaming:
        raise NotImplementedError(
            f"{service}.{method} is client-streaming; not supported by lnd_rpc"
        )
    request_cls = getattr(pb2_module, method_desc.input_type.name)
    request = request_cls()
    if params:
        _ParseDict(params, request, ignore_unknown_fields=False)
    rpc_callable = getattr(stub, method)
    call = rpc_callable(request)
    if method_desc.server_streaming:
        return [
            _MessageToDict(msg, preserving_proto_field_name=True)
            async for msg in call
        ]
    return _MessageToDict(await call, preserving_proto_field_name=True)

async def electrum_pay_onchain(
    dest_addr: str,
    amount: float,
    label: str = "",
    *,
    wallet: Dict[str, Any],
    api: Optional["BitcartAPI"] = None,
) -> bool:
    """
    Send an on-chain payment. AMOUNT IS IN BTC, NOT SATS.

    Dispatch is keyed off `wallet["currency"]`:
      - "btclnd"  -> Lightning.SendCoins gRPC. LND has a native `label` slot
                     on each transaction, so the label is stored on the LND
                     side and surfaces in GetTransactions output.
      - anything  -> Electrum's payto + broadcast + setlabel.

    The Electrum xpub is read from `wallet["xpub"]`.
    """
    if wallet.get("currency") == "btclnd":
        if api is None and wallet["id"] not in _LND_CONNECTIONS:
            raise ValueError(
                "electrum_pay_onchain: LND path needs either `api` or a "
                "pre-populated _LND_CONNECTIONS[wallet['id']] entry"
            )
        return await _lnd_pay_onchain(api, wallet["id"], dest_addr, amount, label)

    xpub = wallet.get("xpub")
    # Make transaction
    pay_response=await electrum_rpc(
        "payto",
        xpub,params={'destination':dest_addr,
                     'amount':str(amount),
                     'feerate':1,
                     }
    )
    if not pay_response['result']:
        logger.warning(f'Error making payment: {pay_response}')
        return False
    transaction=pay_response['result']
    # Broadcast transaction
    broadcast_response = await electrum_rpc(
        "broadcast",
        xpub, params={'tx': transaction}
    )
    if not broadcast_response['result']:
        logger.warning(f'Error making payment broadcast: {broadcast_response}')
        return False
    # set label
    mykey=broadcast_response['result']
    label_response = await electrum_rpc(
        "setlabel",
        xpub,params={'key':mykey,'label':label}
    )
    return True


async def _lnd_pay_onchain(
    api: "BitcartAPI", wallet_id: str, dest_addr: str, amount_btc: float, label: str,
) -> bool:
    """Send an on-chain payment from LND with a native transaction label."""
    if not dest_addr:
        logger.warning("_lnd_pay_onchain called without dest_addr")
        return False
    amount_sat = int(round(float(amount_btc) * 100_000_000))
    if amount_sat <= 0:
        logger.warning(f"_lnd_pay_onchain: non-positive amount {amount_btc} BTC")
        return False
    conn = await _get_lnd_connection(api, wallet_id)
    stub = conn["stubs"]["Lightning"]
    request = _lightning_pb2.SendCoinsRequest(
        addr=dest_addr,
        amount=amount_sat,
        sat_per_vbyte=1,
        label=label or "",
    )
    try:
        await stub.SendCoins(request)
    except _grpc.aio.AioRpcError as e:
        logger.warning(
            f"LND SendCoins to {dest_addr} for {amount_sat} sat failed: {e.details()}"
        )
        return False
    return True
async def electrum_pay_ln_invoice(
    invoice: str,
    label: str = "",
    *,
    wallet: Dict[str, Any],
    api: Optional["BitcartAPI"] = None,
) -> bool:
    """
    Pay an LN invoice, return True if successful, False otherwise.

    Dispatch is keyed off `wallet["currency"]`:
      - "btclnd"  -> Lightning.SendPaymentSync gRPC. LND has no equivalent
                     of Electrum's per-payment label, so on success the
                     label is persisted in our `LndPaymentLabel` side-table.
      - anything  -> Electrum's lnpay + setlabel JSON-RPC.

    The Electrum xpub is read from `wallet["xpub"]`. `api` is required for
    LND wallets unless the gRPC connection is already cached in
    `_LND_CONNECTIONS[wallet["id"]]`.
    """
    if wallet.get("currency") == "btclnd":
        if api is None and wallet["id"] not in _LND_CONNECTIONS:
            raise ValueError(
                "electrum_pay_ln_invoice: LND path needs either `api` or a "
                "pre-populated _LND_CONNECTIONS[wallet['id']] entry"
            )
        return await _lnd_pay_ln_invoice(api, wallet["id"], invoice, label)

    xpub = wallet.get("xpub")
    pay_response = await electrum_rpc(
        "lnpay", xpub, params={'invoice': invoice}
    )
    if not pay_response['result']['success']:
        logger.warning(f'Error making payment: {pay_response}')
        return False
    mykey = pay_response['result']['payment_hash']
    label_response = await electrum_rpc(
        "setlabel", xpub, params={'key': mykey, 'label': label}
    )
    return True


async def _lnd_pay_ln_invoice(
    api: "BitcartAPI", wallet_id: str, invoice: str, label: str
) -> bool:
    """Pay a BOLT11 invoice via Lightning.SendPaymentSync against the wallet's
    LND. Returns True on success, False on payment_error.

    LND has no outgoing-payment label slot, so on success we persist the
    label in our own `LndPaymentLabel` table keyed by payment_hash for
    later lookup against ListPayments output.
    """
    conn = await _get_lnd_connection(api, wallet_id)
    stub = conn["stubs"]["Lightning"]
    request = _lightning_pb2.SendRequest(payment_request=invoice)
    response = await stub.SendPaymentSync(request)
    if response.payment_error:
        logger.warning(
            f"LND lnpay failed for {invoice[:30]}…: {response.payment_error}"
        )
        return False
    if label:
        try:
            from node_database import LndPaymentLabel
            payment_hash_hex = bytes(response.payment_hash).hex().lower()
            LndPaymentLabel.replace(
                payment_hash=payment_hash_hex,
                wallet_id=wallet_id,
                label=label,
            ).execute()
        except Exception as e:
            # Label persistence is best-effort — the payment itself succeeded.
            logger.warning(f"failed to persist LndPaymentLabel: {e}")
    return True

async def new_calc_invoice_stats(
    api: BitcartAPI,
    since_date: Optional[datetime.datetime] = None,
) -> Dict[str, StoreStats]:
    """
    Remember all values should have abs() applied so they don't accidentally cancel out.

    `since_date`: when set, invoice payments dated before this are skipped — used
    by the dashboard's time-range selector. Note this filters REVENUE only;
    on-chain and LN fee/payment histories don't carry reliable timestamps
    through our normalized helpers (the LND path strips them and Electrum's
    shape differs), so fee figures still reflect all-time activity. Callers
    needing per-window fee math should compute that downstream from per-tx
    timestamps in the raw RPCs.
    """
    store_list = await api.get_stores()
    payout_list = await api.get_payouts()
    fee_start_datetime = None
    if FEE_START_DATE:
        format_string = "%Y/%m/%d"
        fee_start_datetime = datetime.datetime.strptime(FEE_START_DATE, format_string)
    auth_store_dict: Dict[str, StoreStats] = {}
    reviewed_wallets = set()
    # Get data from invoices
    sorted_store_list = sorted(
        store_list, key=lambda x: x["created"]
    )  # sort is important to make sure data from any given wallet gets assigned to FIRST relevant store consistently
    for store in sorted_store_list:
        store_id = store["id"]
        auth_store_dict[store_id] = StoreStats(
            store_id=store_id,
            ln_total_revenue_in_sats=0,
            onchain_total_revenue_in_sats=0,
            total_bb_fees_paid_in_sats=0,
            ineligible_revenue_because_of_promo_in_sats=0,
            ineligible_revenue_because_of_topups_in_sats=0,
            ineligible_revenue_because_of_bb_topups_in_sats=0,
            ln_network_fees_paid_for_bb_topup_returns_in_sats=0,
            onchain_network_fees_paid_for_bb_topup_returns_in_sats=0,
            ln_network_fees_paid_for_fee_payments_in_sats=0,
            onchain_network_fees_paid_for_fee_payments_in_sats=0,
            ln_network_fees_paid_for_payouts_in_sats=0,
            onchain_network_fees_paid_for_payouts_in_sats=0,
            ineligible_revenue_because_not_liquidityhelper_wallet_in_sats=0,
            revenue_eligible_for_fee=0,
            ineligible_revenue_because_not_ln_transaction_in_sats=0,
            onchain_network_fees_paid_for_channel_opens_in_sats=0,
            onchain_network_fees_paid_for_channel_closes_in_sats=0,
            onchain_network_fees_paid_for_swaps_in_sats=0,
            onchain_network_fees_paid_for_lsp_orders_in_sats=0,
            onchain_lsp_service_fees_paid_in_sats=0,
            total_referral_fees_paid_in_sats=0,
            ln_network_fees_paid_for_referral_payments_in_sats=0,
            onchain_network_fees_paid_for_referral_payments_in_sats=0,
            misc_ln_network_fees_in_sats=0,
        )
        full_wallet=await api.get_best_ln_wallet_for_store(store)
        wallet_id=full_wallet['id']
        store_stats = auth_store_dict[store_id]

        # Process data from invoices. Invoices must go first because they get us a little database to match onchain and LN payments to
        all_invoices = await api.get_invoices(store_id=store_id)
        for invoice in all_invoices:
            ineligible = False
            field_names = set(f.name for f in dataclasses.fields(BitcartInvoice))
            classified_invoice = BitcartInvoice(
                **{k: v for k, v in invoice.items() if k in field_names}
            )
            try:
                if not classified_invoice.is_paid():
                    continue # TODO make sure this works on multipayment invoices like our topup invoices
                if classified_invoice.is_refunded():
                    continue # we shouldnt have any of these
                for payment in invoice.get("payments", []):
                    if not payment_made(payment):
                        continue
                    # Filter out non-Bitcoin payments (LTC, ETH, etc.).
                    # Use `symbol == "BTC"` rather than `currency == "btc"`
                    # because Bitcart has multiple per-wallet-backend BTC
                    # variants — `btc` (Electrum), `btclnd` (LND Lightning
                    # daemon), and likely more (`btccln` etc.) over time —
                    # and ALL of them carry `symbol == "BTC"`. The previous
                    # filter only accepted the literal string "btc", so
                    # any deployment whose primary wallet was an LND
                    # wallet (currency="btclnd") had every payment
                    # silently dropped, zeroing out the dashboard's
                    # revenue and fee figures.
                    if (payment.get("symbol") or "").upper() != "BTC":
                        logger.warning(
                            f"Warning: found payment in non-BTC currency: {payment}"
                        )
                        continue
                    wallet_id = payment["wallet_id"]
                    if not wallet_id:
                        # sometimes no wallet id if wallet was deleted
                        logger.error(
                            f"Error: no wallet id found for invoice {invoice['id']} payment {payment}"
                        )
                    amount_in_sats = btc_to_sats(abs(float(payment["amount"])))
                    if amount_in_sats==0:
                        continue
                    payment_date = dateutil.parser.parse(payment["created"])
                    if since_date is not None and payment_date < since_date:
                        continue
                    if payment["lightning"]:
                        store_stats.ln_total_revenue_in_sats += amount_in_sats
                    else:
                        store_stats.onchain_total_revenue_in_sats += amount_in_sats
                    # figure out if transaction is eligible for fee
                    if classified_invoice.is_self_topup_invoice():
                        store_stats.ineligible_revenue_because_of_topups_in_sats += (
                            amount_in_sats
                        )
                        ineligible = True
                    elif classified_invoice.is_bb_topup_invoice():
                        store_stats.ineligible_revenue_because_of_bb_topups_in_sats += (
                            amount_in_sats
                        )
                        ineligible = True
                    elif full_wallet['name'] != "liquidityhelper":
                        store_stats.ineligible_revenue_because_not_liquidityhelper_wallet_in_sats += (
                            amount_in_sats
                        )
                        ineligible = True
                    elif (
                        payment["lightning"] and not CHARGE_FEE_FOR_ONCHAIN_TRANSACTIONS
                    ):
                        store_stats.ineligible_revenue_because_not_ln_transaction_in_sats += (
                            amount_in_sats
                        )
                        ineligible = True
                    elif fee_start_datetime:
                        if payment_date.date() < fee_start_datetime.date():
                            store_stats.ineligible_revenue_because_of_promo_in_sats += (
                                amount_in_sats
                            )
                            ineligible = True
                    elif FEE_START_REVENUE:
                        if store_stats.calc_total_revenue()<FEE_START_REVENUE:
                            store_stats.ineligible_revenue_because_of_promo_in_sats += (
                                amount_in_sats
                            )
                            ineligible = True
                    if not ineligible:
                        store_stats.revenue_eligible_for_fee += amount_in_sats
            except Exception as e:
                logger.error(f"Error processing invoice in calc_fees: {e}:{invoice}")

        # Process data from wallet
        if wallet_id in reviewed_wallets:
            logger.debug("Skipping already reviewed wallet:, this shouldnt happen unless you have multiple stores sharing the same wallet {}".format(wallet_id))
            continue
        else:
            reviewed_wallets.add(wallet_id)
        # Get onchain history, channel opens/closes
        onchain_history_rows = await list_onchain_history(
            wallet=full_wallet, api=api,
        )
        for transaction in onchain_history_rows:
            # Label-based dev-fee / referral on-chain detection. Comes
            # before LSP/swap so a transaction labeled FEE_PAYOUT_REASON
            # always counts toward `total_bb_fees_paid_in_sats`, even
            # if it happens to also match looser heuristics.
            tx_label = (transaction.get("label") or "").strip()
            if tx_label == FEE_PAYOUT_REASON:
                if transaction.get("incoming"):
                    continue
                store_stats.onchain_network_fees_paid_for_fee_payments_in_sats += (
                    abs(float(transaction.get("fee_sat") or 0))
                )
                store_stats.total_bb_fees_paid_in_sats += (
                    abs(float(transaction.get("amount_sat") or 0))
                )
                continue
            if tx_label == REFERRAL_PAYOUT_REASON:
                if transaction.get("incoming"):
                    continue
                store_stats.onchain_network_fees_paid_for_referral_payments_in_sats += (
                    abs(float(transaction.get("fee_sat") or 0))
                )
                store_stats.total_referral_fees_paid_in_sats += (
                    abs(float(transaction.get("amount_sat") or 0))
                )
                continue
            if is_lsp_channel_order_transaction(transaction):
                if transaction.get("incoming"):
                    continue
                # Miner fee: the chain cost of paying the LSP.
                store_stats.onchain_network_fees_paid_for_lsp_orders_in_sats += (
                    abs(float(transaction.get("fee_sat") or 0))
                )
                # Service fee: the principal we sent to the LSP.
                # With client_balance_sat=0 (our standard request), the
                # entire amount we sent IS the LSP's service fee.
                store_stats.onchain_lsp_service_fees_paid_in_sats += (
                    abs(float(transaction.get("amount_sat") or 0))
                )
            elif is_swap_transaction(transaction):
                if transaction.get("incoming"):
                    continue
                store_stats.onchain_network_fees_paid_for_swaps_in_sats += (
                    abs(float(transaction["fee_sat"]))
                )
            elif is_ln_open_transaction(transaction):
                if transaction.get("incoming"):
                    continue
                store_stats.onchain_network_fees_paid_for_channel_opens_in_sats += (
                    abs(float(transaction["fee_sat"]))
                )
            elif is_ln_close_transaction(transaction):
                store_stats.onchain_network_fees_paid_for_channel_closes_in_sats += abs(float(transaction['fee_sat']))
            elif transaction['incoming']==True:
                continue
            else:
                store_stats.onchain_network_fees_paid_for_channel_opens_in_sats+=abs(float(transaction['fee_sat']))
                logger.warning(f'Unhandled transaction: {transaction}') # TODO figure out how to handle the sweep local anchor transaction, not sure what it does
        # Get LN history + fees
        ln_history_rows = await list_ln_payments_with_labels(
            wallet=full_wallet, api=api,
        )
        for transaction in ln_history_rows:
            if is_ln_open_transaction(transaction):
                continue #these are already counted in on-chain section
            if is_ln_close_transaction(transaction):
                continue  # these are already counted in on-chain section
            if transaction['amount_msat']>0:
                continue # ignore incoming transactions
            if transaction['type']=='payment' and transaction['amount_msat']<0: #outgoing
                if transaction['label'] == CASHOUT_REASON:
                    store_stats.ln_network_fees_paid_for_payouts_in_sats += abs(transaction['amount_msat']/1000)
                    continue
                if transaction['label'] == FEE_PAYOUT_REASON:
                    store_stats.ln_network_fees_paid_for_fee_payments_in_sats += abs(transaction['fee_msat']/1000)
                    store_stats.total_bb_fees_paid_in_sats += abs(transaction['amount_msat']/1000)
                    continue
                if transaction['label'] == REFERRAL_PAYOUT_REASON:
                    # Mirror of the FEE_PAYOUT_REASON branch but routed
                    # into the referral-specific buckets.
                    store_stats.ln_network_fees_paid_for_referral_payments_in_sats += abs(transaction['fee_msat']/1000)
                    store_stats.total_referral_fees_paid_in_sats += abs(transaction['amount_msat']/1000)
                    continue
                else:
                    store_stats.misc_ln_network_fees_in_sats+=abs(transaction['fee_msat']/1000)
                    logger.warning(f'Unhandled tx: {transaction}')
            else:
                logger.warning(f'Unhandled tx: {transaction}')

    return auth_store_dict


# COMMENTED OUT DEC 2025, DELETE IN 1 month if it doesn't break anything
# async def update_ln_payout_fees(api:BitcartAPI):
#     payout_list = await api.get_payouts()
#     for payout in payout_list:
#         if payout['currency']!='BTC':
#             continue
#         if payout['status'].lower()!='complete':
#             continue
#         if payout['metadata'].get('lnfees'):
#             continue
#         store_id=payout['store_id']
#         if store_id not in auth_store_dict:
#             logger.warning(f'in new_calc_invoice: store_id not in auth_store_dict {store_id}, skipping...')
#             continue
#         store_stats=auth_store_dict[store_id]
#         if 'payment_hash' in payout.get('metadata',{}):
#             # Is "fake" LN payout, OLDTODO add any ln fees found in the payout. We currently don't add this information in
#             amount_in_sats = btc_to_sats(float(payout['amount']))
#             network_fees_ln = btc_to_sats(float(payout['metadata'].get('lnfees',0)))
#         else:
#             amount_in_sats=btc_to_sats(float(payout['amount']))
#             #OLDTODO not sure how this looks on a real payout, update the below two lines when we have one
#             pass
#             #network_fees_chain+=btc_to_sats(float(payout['used_fee']))
#
#         if payout.get('metadata',{}).get('reason','')==FEE_PAYOUT_REASON:
#             pass
#             # OLDTODO re-enable etc store_dict['bb_fees_paid_in_sats'] += amount_in_sats_ln
#             continue
# COMMENTED OUT DEC 2025 DELETE IN a few months if nothing broke
# async def calc_invoice_stats(api:BitcartAPI)->Dict[str,InvoiceStats]:
#     store_list=await api.get_stores()
#     return_dict={}
#     fee_start_datetime = None
#     all_existing_payouts=await api.get_payouts()
#     fees_paid_dict=count_all_fees_paid_from_payouts(all_existing_payouts)
#     if FEE_START_DATE:
#         format_string = "%Y/%m/%d"
#         fee_start_datetime = datetime.datetime.strptime(FEE_START_DATE, format_string)
#     for store in store_list:
#         total_revenue_in_sats = 0
#         ineligible_revenue_promo=0 # amount of fee skipped due to promo period
#         topup_revenue = 0  # amount of fee skipped due to topups
#         bb_topup_revenue = 0  # amount of fee skipped due to topups
#         network_fees_in_sats=0
#         payoutinfo = fees_paid_dict.get(store['id'])
#
#
#         store_wallets={}
#         # find all wallets
#         for wallet_id in store['wallets']:
#             full_wallet=await api.get_wallet(wallet_id)
#             full_wallet_name=full_wallet['name']
#             store_wallets[wallet_id]=full_wallet_name
#         all_invoices = await api.get_invoices(store_id=store)
#         for invoice in all_invoices['result']:
#             try:
#                 if not invoice['paid_date']:
#                     continue
#                 if invoice['refund_id']:
#                     continue
#                 for payment in invoice.get('payments',[]):
#                     if payment['status'] == 'pending':
#                         continue
#                     if payment['currency']!='btc':
#                         logger.warning(f'Warning: found payment in non-btc currency: {payment}')
#                         continue
#                     wallet_id=payment['wallet_id']
#                     if not wallet_id:
#                         # sometimes no wallet id if wallet was deleted
#                         logger.error(f"Error: no wallet id found for invoice {invoice['id']} payment {payment}")
#                     if wallet_id not in store_wallets:
#                         # This can happen if a wallet was deleted, but is still associated with a past tx
#                         logger.error(f"Warning in calc_fees: wallet id {wallet_id} not found in store wallets: {store_wallets}")
#                     if not payment_made(payment):
#                         continue
#                     wallet_name = store_wallets.get(wallet_id, 'UNKNOWN')
#                     amount_in_sats= btc_to_sats(float(payment['amount']))
#                     payment_date=dateutil.parser.parse(payment['created'])
#                     if wallet_name == 'liquidityhelper' or ALL_TRANSACTIONS_ELIGIBLE_FOR_FEE:
#                         total_revenue_in_sats+=amount_in_sats
#                         if is_topup_invoice(invoice):
#                             topup_revenue+=amount_in_sats
#                         if is_bb_topup_invoice(invoice):
#                             bb_topup_revenue+=amount_in_sats
#                         if fee_start_datetime:
#                             if payment_date.date() < fee_start_datetime.date():
#                                 ineligible_revenue_promo+=amount_in_sats
#             except Exception as e:
#                 logger.error(f"Error processing invoice in calc_fees: {e}:{invoice}")
#         if store['id'] in fees_paid_dict:
#             total_fees_paid_in_sats = payoutinfo.total_paid_in_sats_ln + payoutinfo.total_paid_in_sats_onchain
#             network_fees_in_sats += payoutinfo.total_network_fees_paid_ln + payoutinfo.total_network_fees_paid_onchain
#         else:
#             total_fees_paid_in_sats =0
#             network_fees_in_sats=0
#         my_calc_fees=InvoiceStats(store['id'],
#                                   total_revenue_in_sats,
#                                   total_fees_paid_in_sats=total_fees_paid_in_sats,
#                                   ineligible_revenue_because_of_promo=ineligible_revenue_promo,
#                                   ineligible_revenue_because_of_topups=topup_revenue,
#                                   ineligible_revenue_because_of_bb_topups=bb_topup_revenue,
#                                   network_fees_paid_in_payouts=network_fees_in_sats,
#                                   )
#         return_dict[store['id']]=my_calc_fees
#     return return_dict

def max_channel_size_from_sats(sats_we_have:int)->int:
    """"
    Given an amount of sats, find the biggest channel we can open, starting with the total number of sats and working our way down to 1
    """
    for i in reversed(range(1,sats_we_have)):
        own_reserve = min(i * 0.015, 500)

        dust_limit_sat = 20001  # don't know what this actually is
        electrum_reserve = max(i // 100, dust_limit_sat)

        reserve = max(own_reserve, electrum_reserve)
        final_cost=i + reserve
        if final_cost<=sats_we_have:
            return i
    return 0
def channel_size_from_intended_sats(intended_sats: int) -> int:
    """
    Given an intended amount of liquidity, return the amount of sats it will take to open.
    If you instead have an amount of sats you have and want to know the highest channnel you can open
    with it, use max_channel_size_from_sats
    """
    own_reserve = min(intended_sats * 0.015, 500)

    dust_limit_sat=20001 # don't know what this actually is
    electrum_reserve=max(intended_sats // 100, dust_limit_sat)

    reserve=max(own_reserve,electrum_reserve)
    return int(intended_sats + reserve)


def remove_existing_channel_partners(
    partner_list: List[str], current_channels: List[dict]
) -> List[str]:
    """ "
    Given list of current channels, remove existing partners from partner_list
    """
    found_channel_pubkeys = set()
    for channel in current_channels:
        if channel["state"] != "REDEEMED":
            found_channel_pubkeys.add(channel["remote_pubkey"].lower())
    return_list = deepcopy(partner_list)
    for partner in partner_list:
        for found_channel in found_channel_pubkeys:
            if found_channel in partner:
                return_list.remove(partner)
    return return_list


async def pick_best_channel_partners(ln_cashout_address: Optional[str] = None) -> List[str]:
    """
    Pick best channel partners to try.

    Gossip-staleness gate: the connectivity metrics on every
    LightningNode row (`effective_degree`, `two_hop_reach`, median
    fees) come from the LAST successful gossip pull. If no successful
    pull has ever happened, OR the last one is older than
    GOSSIP_MAX_STALENESS_DAYS days, we refuse to return any candidates
    — picking from metrics that are too old (or never computed) is
    no better than picking from an artificially short list. Returning
    an empty list propagates to attempt_create_channels which already
    handles "no partners found" by not opening a channel this tick.
    """
    last_pull = _get_last_gossip_pull_datetime()
    if last_pull is None:
        log_decision(
            ("channel_partner_pick_gated",), "no_gossip_pull",
            "pick_best_channel_partners: no successful gossip pull on "
            "record yet — refusing to return candidates. The next "
            "successful daily gossip pull will unblock this.",
        )
        return []
    age = datetime.datetime.now() - last_pull
    if age.days >= GOSSIP_MAX_STALENESS_DAYS:
        log_decision(
            ("channel_partner_pick_gated",), "stale_gossip",
            "pick_best_channel_partners: last successful gossip pull "
            "was %s ago (>= GOSSIP_MAX_STALENESS_DAYS=%d days). "
            "Refusing to return candidates; connectivity metrics may "
            "no longer reflect reality. Check that the daily pull is "
            "succeeding — it's likely being skipped by the readiness "
            "gate.",
            age, GOSSIP_MAX_STALENESS_DAYS,
            level=logging.WARNING,
        )
        return []

    coinos_uri = "021294fff596e497ad2902cd5f19673e9020953d90625d68c22e91b51a45c032d3@51.79.52.200:9736"
    strike_uri = "03c8e5f583585cac1de2b7503a6ccd3c12ba477cfd139cd4905be504c2f48e86bd@34.73.189.183:9735"
    boltz_node = "026165850492521f4ac8abd9bd8088123446d126f648ca35e60f88177dc149ceb2@143.202.162.204:9735"
    return_list = []
    partner_list = []
    try:
        bb_partners = await run_every_x_seconds(
            my_func=get_channel_partners,
            seconds=1,
            url="https://getbarebits.com/default_channel_partners.json",
        )
    except Exception as e:
        logger.error(f"Error fetching channel partners from bb: {e}")
    else:
        # add found nodes to database if we don't already have them
        for found_partner in bb_partners:
            try:
                pubkey = found_partner["node_address"]
                existing_db_object = LightningNode.get_or_none(
                    LightningNode.node_address == pubkey
                )
                try:
                    new_object = node_database.dict_to_node(found_partner)
                except Exception as e:
                    logger.error(f"Error turning dict tonode: {e}:{found_partner}")
                    continue

                if existing_db_object:
                    lnd_graph_pull.merge_lightning_node(
                        existing_db_object, new_object
                    )
                else:
                    logger.debug("Adding new node from json, not in existing DB")
                    new_object.save(force_insert=True)
            except Exception as e:
                logger.error(
                    f"Error processing partner in partner list: {e}:{found_partner}"
                )

    # add known nodes for strike, coinos, if not blacklisted
    if ln_cashout_address:
        if "strike.me" in ln_cashout_address:
            strike_db_object = LightningNode.get_or_none(
                LightningNode.node_address == strike_uri.split("@")[0]
            )
            if strike_db_object:
                blacklist_response, reason = is_node_blacklisted(strike_db_object)
                if not blacklist_response:
                    return_list.insert(0, strike_uri)
        elif "coin.os" in ln_cashout_address:
            coinos_db_object = LightningNode.get_or_none(
                LightningNode.node_address == coinos_uri.split("@")[0]
            )
            if coinos_db_object:
                blacklist_response, reason = is_node_blacklisted(coinos_db_object)
                if not blacklist_response:
                    return_list.insert(0, coinos_uri)

    # Pull all candidates from the local DB. Filter via the blacklist
    # (which now enforces 2-year min age + effective-degree floor +
    # 2-hop reach floor), then order by:
    #   1. Fee bucket (cheaper bucket first). bucket = fee_ppm //
    #      NODE_FEE_BUCKET_PPM (default 1000 = 0.10% granularity).
    #      Nodes at 750 ppm and 790 ppm share a bucket; 700 ppm and
    #      800 ppm don't.
    #   2. Within a bucket, higher 2-hop reach wins. The fee bucketing
    #      treats "similar fee rates" as equivalent, so connectedness
    #      becomes the tie-breaker — more options to route to the
    #      wider network.
    # Survivors of the blacklist always have non-None values for both
    # median_outbound_fee_rate_ppm and two_hop_reach, by construction.
    ln_node_list: List[LightningNode] = list(LightningNode.select())
    candidates: List[Tuple[int, int, str]] = []   # (bucket, -reach, uri)
    for node in ln_node_list:
        blacklisted, _ = is_node_blacklisted(node)
        if blacklisted:
            continue
        uri = node.get_ipv4_uri()
        if not uri:
            continue
        fee_ppm = int(node.median_outbound_fee_rate_ppm or 0)
        bucket = fee_ppm // NODE_FEE_BUCKET_PPM
        # Negate reach so the default ASC sort puts higher-reach first.
        reach_sort_key = -int(node.two_hop_reach or 0)
        candidates.append((bucket, reach_sort_key, uri))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return_list.extend(uri for _, _, uri in candidates)

    return return_list


async def move_onchain_to_ln(
    wallet_id: str, amount_in_btc: float, api: BitcartAPI
) -> bool:
    """
    Open channels.
    pubkey: open channel to specified node
    Returns True if successful, false otherwise
    """
    if not MANUAL_CHANNEL_CREATION_ENABLED:
        # Defensive guard. The callers (decide_onchain_to_ln,
        # attempt_create_channels from liquidity_check) already gate on
        # this flag, but this catches any future call site that forgets.
        # Returning False signals "no channel opened" to the caller.
        logger.debug(
            "move_onchain_to_ln called with MANUAL_CHANNEL_CREATION_ENABLED=False; "
            "skipping channel open for wallet %s", wallet_id,
        )
        return False
    # Electrum guard. Manual channel creation depends on the
    # LightningNode candidate DB which is populated from LND gossip
    # (effective_degree, two_hop_reach, median outbound fee rate —
    # none of which Electrum can supply or audit). Electrum's own
    # open_channel still works in principle, but selecting peers from
    # an LND-derived graph for an Electrum wallet conflates two
    # routing models. Skip with an explicit log so the operator sees
    # the constraint and decides how to handle inbound on Electrum
    # (typically: open channels manually outside the script).
    full_wallet = await api.get_wallet(wallet_id)
    if not full_wallet or full_wallet.get("currency") != "btclnd":
        log_decision(
            ("manual_channel_create_skipped_non_lnd", wallet_id), True,
            "move_onchain_to_ln: wallet %s is currency=%s, not btclnd; "
            "manual channel creation is LND-only (depends on gossip-"
            "derived candidate metrics). Operator must open channels "
            "directly via Electrum if needed.",
            wallet_id,
            (full_wallet or {}).get("currency", "<unknown>"),
        )
        return False
    channel_partners = await pick_best_channel_partners(CASHOUT_LIGHTNING_ADDRESS)
    current_channels = await api.get_wallet_ln_channels(wallet_id)
    # don't open more than one channel to existing partners
    paired_list = remove_existing_channel_partners(channel_partners, current_channels)
    # keep trying until we successfully open a channel with somebody
    if len(paired_list) == 0:
        logger.error(
            "In move_onchain_to_ln No potential partners found for creating channels."
        )
        return False
    for partner in paired_list:
        try:
            partner_pubkey = partner.lower().split("@")[0]
        except Exception as e:
            logger.error(f"Error getting pubkey from partner: {e}:{partner}")
            continue
        if DRY_RUN_FUNDS:
            logger.info(
                f"DRY RUN: Skipping LN channel open {amount_in_btc}BTC to {partner} from wallet {wallet_id}"
            )
        else:
            ln_node: Optional[LightningNode] = LightningNode.get_or_none(
                LightningNode.node_address == partner_pubkey
            )
            if not ln_node:
                logger.warning(
                    "In move_onchain_to_ln, no ln_node found"
                )  # TODO this shouldn't be happening
                continue
            blacklist_result, blacklist_reason = is_node_blacklisted(ln_node)
            if blacklist_reason == "REMOTE_CLOSE_COUNT":
                continue
            if not ln_node.ipv4_address and ln_node.lnd_queries >= 2:
                continue
            if ln_node.needs_lnd_update(30):
                # In-process gRPC stub for the LSP-targeted refresh.
                # Skipped here pending a top-level LND-stub plumb-through;
                # the daily pull_and_upsert covers the common case.
                pass
            blacklist_result, blacklist_reason = is_node_blacklisted(ln_node)
            if blacklist_result:
                logger.debug(
                    f"In move_onchain_to_ln after LND graph refresh, node is {ln_node.node_address} blacklisted for reason {blacklist_reason}"
                )
                continue
            log_event("Attempting channel open to %s for %s BTC (wallet %s)",
                      partner, amount_in_btc, wallet_id)
            move_response = await api.open_ln_channel(wallet_id, partner, amount_in_btc)
            if move_response:  # channel opened successfully
                log_event("Channel opened: %s", move_response)
                return True
    return False



async def attempt_create_channels(
    wallet_id: str,
    api: BitcartAPI,
    target_channel_sizes: List[int],
) -> bool:
    """
    Function to create new channels. Returns True if ANY channels
    created successfully
    """
    return_value = False
    for i in target_channel_sizes:
        if DRY_RUN_FUNDS:
            logger.info(f"DRY RUN: Would have opened channel w {i} sats")
            continue
        result = await move_onchain_to_ln(wallet_id, sats_to_btc(i), api)
        if result:
            return_value = True
    return return_value


async def our_wallet_exists(api: BitcartAPI, store: dict) -> Optional[dict]:
    """
    Get liquidityhelper wallet for store, return None if not found
    """
    found_wallet = None
    try:
        found_wallet = await api.get_best_ln_wallet_for_store(store)
    except Exception as e:
        logger.error(f"Error in our_wallet_exists: {e}:{found_wallet}")
        return None
    else:
        return found_wallet


async def first_wallet_check_create(api: BitcartAPI) -> bool:
    """
    Check for first wallet, create it if it doesn't exist. Return True if went well, None if error
    """
    wallet_list = await api.get_wallets()
    if wallet_list:
        if len(wallet_list) > 0:
            return True
    logger.info("No wallets found, creating first wallet..")
    mywallet_seed_response = await api.create_wallet_seed()
    if not isinstance(mywallet_seed_response, dict):
        logger.error(f"Err making wallet seed response: {mywallet_seed_response}")
        return False
    if "seed" not in mywallet_seed_response:
        logger.error(f"2Err making wallet seed response: {mywallet_seed_response}")
        return False
    mywallet_seed = mywallet_seed_response["seed"]
    if isinstance(mywallet_seed, str):
        print("=================================")
        print(
            "A new wallet has been created, your seed phrase is below. Store this seed phrase somewhere securely"
        )
        print(
            "If you lose your seed phrase, you will lose access to any funds stored in Bitcart!"
        )
        print(mywallet_seed)
    else:
        logger.error("Err generating wallet seed, will try again later")
        return False
    mywallet = await api.create_wallet(seed=mywallet_seed)
    if not mywallet:
        logger.error("Err generating wallet, will try again later")
        return False
    return True


def should_close_channel(
    failed_checks: int,
    total_checks: int,
    last_online: datetime.datetime,
    check_interval_in_seconds: int,
    *,
    now: Optional[datetime.datetime] = None,
) -> Tuple[str, bool]:
    """LEGACY — superseded by audit_existing_peer + the 14-day
    LONG_OUTAGE / 5% HIGH_FAILURE_RATIO gates in node_database.py.
    Kept only because tests/code_only_tests.py still pins its
    behavior; no production code path calls it anymore.

    Pure decision for whether a peer's flakiness warrants closing
    the channel. `now` defaults to `datetime.datetime.now()` for
    callers; tests inject a fixed timestamp to exercise the
    OFFLINE_RECENTLY 48-hour edge deterministically.
    """
    _now = now if now is not None else datetime.datetime.now()
    if failed_checks < 5:
        return "", False
    check_period_duration = check_interval_in_seconds * total_checks
    failed_check_duration = check_interval_in_seconds * failed_checks
    failed_check_ratio = failed_checks / total_checks
    # monitoring less than one hour
    if check_period_duration < 3600:
        return "", False
    # down longer than 5 hours per month over a > 2 month period
    if failed_check_duration / (86400 * 30) > 18000 and check_period_duration > (
        86400 * 60
    ):
        return "LONG_TERM_UNRELIABLE", True
    # down > 10% of the time over a > 1 week period
    if failed_check_ratio > 0.10 and check_period_duration > (86400 * 7):
        return "SHORT_TERM_UNRELIABLE", True
    # dropped offline and has been down for 48 hours
    hours_ago_48 = datetime.timedelta(hours=-48)
    if last_online < _now + hours_ago_48:
        return "OFFLINE_RECENTLY", True
    return "", False


async def attempt_cooperative_close(
    channel_point: str,
    *,
    wallet: Dict[str, Any],
    api: Optional[BitcartAPI] = None,
    reason: Optional[str] = None,
) -> Optional[dict]:
    """Cooperatively close a Lightning channel via Electrum or LND, and
    record the attempt on the LightningChannel row.

    Dispatch is keyed off `wallet["currency"]`:
      - "btclnd"  -> Lightning.CloseChannel(force=False) gRPC (returns
                     first close-pending update)
      - anything  -> Electrum's close_channel JSON-RPC.

    Side effect: writes/updates the LightningChannel row keyed on
    channel_point so the retry loop (process_pending_closes) can find
    this attempt later. Tracks `cooperative_close_requested` (first
    attempt only — never updated), `last_close_attempt_at` (every
    attempt), and `cooperative_close_attempts` (incrementing counter).
    Centralised here so all 3 callers automatically get retry
    bookkeeping with no duplicate code paths.
    """
    _record_close_attempt(channel_point, force=False, reason=reason)
    if wallet.get("currency") == "btclnd":
        if api is None and wallet["id"] not in _LND_CONNECTIONS:
            raise ValueError(
                "attempt_cooperative_close: LND path needs either `api` or "
                "a pre-populated _LND_CONNECTIONS[wallet['id']] entry"
            )
        return await _lnd_close_channel(
            api, wallet["id"], channel_point, force=False,
        )
    return await electrum_rpc(
        "close_channel", myxpub=wallet.get("xpub"),
        params={"channel_point": channel_point},
    )


async def attempt_force_close(
    channel_point: str,
    *,
    wallet: Dict[str, Any],
    api: Optional[BitcartAPI] = None,
    reason: Optional[str] = None,
) -> Optional[dict]:
    """Force-close a Lightning channel — call when coop close has
    failed repeatedly for CHANNEL_COOP_CLOSE_TIMEOUT_DAYS and the
    peer is clearly unreachable.

    Force closes have a CSV timelock (typically days-to-weeks per
    peer-set policy) before our funds are spendable on-chain. The
    caller is responsible for the rate-limit decision; this function
    just submits the close.

    Side effect: writes `force_close_initiated_at = now` on the
    LightningChannel row so the retry loop knows not to repeat the
    escalation.
    """
    _record_close_attempt(channel_point, force=True, reason=reason)
    if wallet.get("currency") == "btclnd":
        if api is None and wallet["id"] not in _LND_CONNECTIONS:
            raise ValueError(
                "attempt_force_close: LND path needs either `api` or "
                "a pre-populated _LND_CONNECTIONS[wallet['id']] entry"
            )
        return await _lnd_close_channel(
            api, wallet["id"], channel_point, force=True,
        )
    # Electrum: force_close_channel is the canonical RPC. Some
    # electrum-server versions don't expose it; the caller treats a
    # None/error result as "stuck, log and move on".
    return await electrum_rpc(
        "force_close_channel", myxpub=wallet.get("xpub"),
        params={"channel_point": channel_point},
    )


def _record_close_attempt(
    channel_point: str, *, force: bool, reason: Optional[str] = None,
) -> None:
    """Upsert the LightningChannel row for `channel_point` with the
    current attempt's state. Called from both attempt_cooperative_close
    and attempt_force_close BEFORE the underlying RPC, so retries are
    recorded even if the RPC itself raises.

    `reason`: optional free-text close reason (e.g. "AUDIT_FAILURE:
    HIGH_FEE_RATE", "OFFLINE_BEYOND_THRESHOLD"). Stored verbatim on
    the row so the dashboard's "Recent channel closures" table can
    surface it. We OVERWRITE the reason on every call so a force-close
    escalation can update the original coop-close reason with the
    escalation context (callers concatenate the original into the new
    reason text — see `process_pending_closes`).
    """
    now = datetime.datetime.now()
    row: Optional[LightningChannel] = LightningChannel.get_or_none(
        LightningChannel.channel_point == channel_point
    )
    if row is None:
        row = LightningChannel(
            channel_point=channel_point,
            cooperative_close_requested=now,
            last_close_attempt_at=now,
            cooperative_close_attempts=1,
            force_close_initiated_at=now if force else None,
            close_reason=reason,
        )
        row.save(force_insert=True)
        return
    # Existing row: preserve the FIRST-attempt timestamp, bump the
    # rest. Force close sets its own timestamp once (don't overwrite
    # if already set — pin against double-issuing).
    if not row.cooperative_close_requested:
        row.cooperative_close_requested = now
    row.last_close_attempt_at = now
    row.cooperative_close_attempts = (row.cooperative_close_attempts or 0) + 1
    if force and not row.force_close_initiated_at:
        row.force_close_initiated_at = now
    if reason is not None:
        row.close_reason = reason
    row.save()


async def _lnd_close_channel(
    api: "BitcartAPI", wallet_id: str, channel_point: str,
    *, force: bool,
) -> Optional[dict]:
    """Submit a CloseChannel RPC to the wallet's LND. `force=False` for
    cooperative, `force=True` for unilateral.

    LND's Lightning.CloseChannel is server-streaming; the first update is
    typically the close_pending message containing the closing txid. We
    read that one update and cancel the stream so we don't block waiting
    for on-chain confirmation (which the caller can poll separately)."""
    txid_str, _, vout = channel_point.rpartition(":")
    if not txid_str or not vout:
        raise ValueError(f"Malformed channel_point '{channel_point}'; want 'txid:vout'")
    conn = await _get_lnd_connection(api, wallet_id)
    stub = conn["stubs"]["Lightning"]
    request = _lightning_pb2.CloseChannelRequest(
        channel_point=_lightning_pb2.ChannelPoint(
            funding_txid_str=txid_str,
            output_index=int(vout),
        ),
        force=force,
    )
    call = stub.CloseChannel(request)
    try:
        async for update in call:
            return _MessageToDict(update, preserving_proto_field_name=True)
    finally:
        call.cancel()
    return None


# Back-compat shim: existing callers reference _lnd_cooperative_close.
# Forward to the unified _lnd_close_channel with force=False.
async def _lnd_cooperative_close(
    api: "BitcartAPI", wallet_id: str, channel_point: str,
) -> Optional[dict]:
    return await _lnd_close_channel(api, wallet_id, channel_point, force=False)


async def wallet_creation(
    api: BitcartAPI,
) -> Optional[bool]:
    store_list=await api.get_stores()
    for store in store_list:
        store_id = store["id"]
        mywallet = None
        # get our best LN wallet
        found_wallet = await our_wallet_exists(api, store)
        if found_wallet:
            # print(f"best wallet info id: {found_wallet['id']} balance: {found_wallet['balance']}")
            continue

        logger.info(
            f"No liquidity helper wallet found for store : {store['name']}, creating.."
        )
        mywallet_seed_response = await api.create_wallet_seed()
        if not isinstance(mywallet_seed_response, dict):
            logger.error(
                f"Err making wallet seed response: {mywallet_seed_response}"
            )
            continue
        if "seed" not in mywallet_seed_response:
            logger.error(
                f"2Err making wallet seed response: {mywallet_seed_response}"
            )
            continue
        mywallet_seed = mywallet_seed_response["seed"]
        if isinstance(mywallet_seed, str):
            print("=================================")
            print(
                "A new wallet has been created, your seed phrase is below. Store this seed phrase somewhere securely"
            )
            print(
                "If you lose your seed phrase, you will lose access to any funds stored in Bitcart!"
            )
            print(mywallet_seed)
        else:
            logger.error("Err generating wallet seed, will try again later")
            continue
        mywallet = await api.create_wallet(seed=mywallet_seed)
        if not mywallet:
            logger.error("Err generating wallet, will try again later")
            continue
        # add wallet to store
        new_wallet_list = store["wallets"]
        new_wallet_list.append(mywallet["id"])
        await api.add_wallet_to_store(new_wallet_list, store_id)
    return True


async def _lnd_find_channel_closings(api: "BitcartAPI", wallet_id: str) -> Dict[str, int]:
    """Per-peer count of remote-INITIATED closes for one LND wallet.

    Filters Lightning.ClosedChannels by:
      1. close_type NOT IN {FUNDING_CANCELED, ABANDONED} — these don't
         correspond to a real on-chain close.
      2. close_initiator == INITIATOR_REMOTE — only the peer-initiated
         closes count toward remote_close_count. This matters because
         the script itself can coop-close channels (via the audit path
         or operator-driven), and those must NOT inflate the peer's
         count or we'd self-trigger the REMOTE_CLOSE_COUNT blacklist
         on peers we ourselves dropped.

    `close_initiator` is serialized as a string enum by Bitcart's
    JSON-mode gRPC wrapper. We accept both the fully-qualified
    "INITIATOR_REMOTE" and the bare "REMOTE" form (LND's gRPC has been
    inconsistent across versions).
    """
    resp = await lnd_rpc(api, wallet_id, "ClosedChannels", {}, "Lightning") or {}
    counts: Dict[str, int] = {}
    excluded_close_types = {"FUNDING_CANCELED", "ABANDONED"}
    remote_initiator_values = {"INITIATOR_REMOTE", "REMOTE"}
    for c in resp.get("channels") or []:
        close_type = c.get("close_type") or ""
        if isinstance(close_type, str) and close_type.upper() in excluded_close_types:
            continue
        close_initiator = c.get("close_initiator") or ""
        if not isinstance(close_initiator, str):
            continue
        if close_initiator.upper() not in remote_initiator_values:
            continue
        pubkey = (c.get("remote_pubkey") or "").lower()
        if not pubkey:
            continue
        counts[pubkey] = counts.get(pubkey, 0) + 1
    return counts


async def find_channel_closings(
    *,
    wallet: Dict[str, Any],
    api: Optional["BitcartAPI"] = None,
) -> Dict[str, int]:
    """Per-peer count of remote-initiated closed channels for this wallet.

    Dispatch is keyed off `wallet["currency"]`:
      - "btclnd"  -> Lightning.ClosedChannels gRPC, filtered to
                     close_initiator == REMOTE.
      - anything else (Electrum) -> empty dict. Electrum's list_channels
                     doesn't expose close_initiator, so counting
                     state=REDEEMED would conflate our own closes with
                     peer-initiated ones. Over-counting the latter would
                     self-trigger the REMOTE_CLOSE_COUNT blacklist after
                     a few coop closes we initiated — exactly what this
                     tracking is supposed to prevent. For Electrum we
                     skip the measure entirely; the peer's row keeps
                     whatever count it had from the LND path (if any).
    """
    if wallet.get("currency") == "btclnd":
        if api is None and wallet["id"] not in _LND_CONNECTIONS:
            raise ValueError(
                "find_channel_closings: LND path needs either `api` or a "
                "pre-populated _LND_CONNECTIONS[wallet['id']] entry"
            )
        return await _lnd_find_channel_closings(api, wallet["id"])
    logger.debug(
        "find_channel_closings: wallet %s is %s (not LND); skipping "
        "remote-close tracking (Electrum doesn't expose close_initiator)",
        wallet.get("id"), wallet.get("currency"),
    )
    return {}
@dataclass
class LiquidityNeed:
    """How short a store is of its Lightning liquidity targets.

    Both values are >= 0; if both are zero the store is fully provisioned and
    `store_needs_liquidity` would have returned None instead of this dataclass.
    """
    liquidity_needed_sat: int
    channels_needed: int


async def store_needs_liquidity(
    store_id: str,
    api: "BitcartAPI",
    min_sats_liquidity: int = MIN_INBOUND_LIQUIDITY,
    min_channel_count: int = MIN_CHANNEL_COUNT,
    assume_zero: bool = False,
) -> Optional[LiquidityNeed]:
    """
    Returns None if the store has enough inbound liquidity and enough channels;
    otherwise a `LiquidityNeed` describing the shortfall.

    Assumes any balance in LN is "inbound" since it will be converted to
    inbound next time cashout runs.

    Args:
        min_channel_count: minimum number of channels this store should have.
        min_sats_liquidity: minimum amount of liquidity we want this store to have.
        assume_zero: if true, skip the wallet/channel lookup and treat the
            store as having 0 channels / 0 liquidity. Used by topup amount/
            reserve calculations.
    """
    found_inbound_liquidity: float = 0.0
    found_channels = 0
    if not assume_zero:
        full_store = await api.get_store_by_id(store_id)
        best_wallet = await api.get_best_ln_wallet_for_store(full_store)
        wallet_id = best_wallet["id"]
        open_channels = await api.get_wallet_ln_channels(
            wallet_id, active_only=True, online_only=True,
        )
        if open_channels:
            found_channels += len(open_channels)
        for channel in open_channels:
            found_inbound_liquidity += float(channel["remote_balance"])
            found_inbound_liquidity += float(channel["local_balance"])
    if (
        found_inbound_liquidity > min_sats_liquidity
        and found_channels > min_channel_count
        and not assume_zero
    ):
        return None
    liquidity_needed = max(min_sats_liquidity - found_inbound_liquidity, 0)
    channels_needed = max(min_channel_count - found_channels, 0)
    # If splitting evenly over `channels_needed` would yield sub-dust slices,
    # bump liquidity_needed up until each slice clears MIN_CHANNEL_SIZE_IN_SATS.
    # No bump needed if no channels would be opened.
    if channels_needed > 0:
        while min(common_functions.distribute_sats_over_channels(
            liquidity_needed, channels_needed,
        )) < MIN_CHANNEL_SIZE_IN_SATS:
            liquidity_needed += 1
    return LiquidityNeed(
        liquidity_needed_sat=int(liquidity_needed),
        channels_needed=int(channels_needed),
    )

async def update_channel_closings(api:BitcartAPI) -> None:
    """Refresh LightningNode.remote_close_count for every peer we've
    ever had a remote-initiated close with, aggregated across all
    LND wallets we have access to.

    Why aggregate: a peer that has been hostile to us across multiple
    wallets should be reflected in ONE per-peer total, not whichever
    wallet's count happened to be written last. The previous
    implementation iterated per-store and overwrote — so the same
    peer's count would flip-flop between wallets each tick.

    Electrum wallets are skipped (find_channel_closings returns
    empty for them — see its docstring).
    """
    try:
        wallets = await api.get_wallets(limit=200)
    except Exception as e:
        logger.warning(
            f"update_channel_closings: get_wallets failed: {e}"
        )
        return
    # Aggregate per-peer counts across every LND wallet in one pass.
    # A peer that's burned us on two different wallets ends up with
    # the sum, not the last-write-wins value.
    aggregate: Dict[str, int] = {}
    seen_any_lnd = False
    for wallet in wallets:
        if wallet.get("currency") != "btclnd":
            continue
        seen_any_lnd = True
        try:
            wallet_counts = await find_channel_closings(
                wallet=wallet, api=api,
            )
        except Exception as e:
            logger.warning(
                f"update_channel_closings: find_channel_closings "
                f"failed for {wallet.get('id')}: {e}"
            )
            continue
        for pubkey, count in wallet_counts.items():
            pk = pubkey.lower()
            aggregate[pk] = aggregate.get(pk, 0) + count
    if not seen_any_lnd:
        # All-Electrum deployment: nothing to update. Don't zero
        # existing rows — they may have residual counts from earlier
        # LND-tracked closes, and zeroing on absence of data would
        # silently clear a real signal.
        return
    # Write the aggregated counts. We deliberately OVERWRITE (not
    # increment) — the LND query is authoritative for that wallet's
    # close history, so the sum across wallets is the current truth.
    for pubkey, count in aggregate.items():
        found_node: LightningNode = LightningNode.get_or_none(
            LightningNode.node_address == pubkey
        )
        if not found_node:
            found_node = LightningNode(node_address=pubkey)
            found_node.remote_close_count = count
            found_node.save(force_insert=True)
        else:
            found_node.remote_close_count = count
            found_node.save()

async def get_most_recent_channel_close(api:BitcartAPI,wallet_id:str)->Optional[datetime.datetime]:
    """
    Returns the most recent channel closing attempt date.
    This is the date of the first attempt to close said channel, subsequent dates are not tracked
    """
    channels=await api.get_wallet_ln_channels(wallet_id)
    found_closes=[]
    for channel in channels:
        state=channel['state']
        if state in ['OPEN','REDEEMED','CLOSED']:
            continue
        elif state in ['CLOSING']:
            channel_point=channel['channel_point']
            channel_object=LightningChannel.get_or_none(LightningChannel.channel_point==channel_point)
            if channel_object:
                if channel_object.cooperative_close_requested:
                    found_closes.append(found_closes)
        else:
            logger.warning(f'In get_most_recent_channel_close, found unknown state {state}')
    if len(found_closes)>0:
        return max(found_closes)
    return None


async def liquidity_check(
    api: BitcartAPI) -> Optional[bool]:
    """
    Find and make inbound liquidity.
    """
    list_of_stores=await api.get_stores()
    for store in list_of_stores:
        store_id=store['id']
        store_name=store['name']
        store_liquidity_result=await store_needs_liquidity(store_id,api,MIN_INBOUND_LIQUIDITY,MIN_CHANNEL_COUNT)
        if not store_liquidity_result:
            logger.debug(f'Store has enough liquidity: {store_id}')
            continue
        liquidity_needed=store_liquidity_result.liquidity_needed_sat
        channels_needed = store_liquidity_result.channels_needed
        store_total_liquidity=await api.get_store_total_liquidity(store_id)
        #current_inbound_liquidity=await api.get_store_inbound_liquidity(store_id)
        best_wallet=await api.get_best_ln_wallet_for_store(store)
        best_wallet_balance_in_sats=btc_to_sats(float(best_wallet['balance']))
        wallet_id=best_wallet['id']
        # Record uptime sample for every open peer. Per-wallet throttle
        # keyed by wallet_id so each wallet gets its own 10-minute
        # cadence (a shared throttle would let the first wallet of
        # the tick starve all subsequent wallets). find_offline_channels
        # no longer closes channels itself — degraded peers flow
        # through audit_existing_peer's 3-day-hysteresis pipeline
        # instead.
        _throttle_name = f"find_offline_channels:{wallet_id}"
        _throttle_now = datetime.datetime.now()
        _throttle_row = LastRunTracker.get_or_none(name=_throttle_name)
        _interval_sec = UPTIME_CHECK_INTERVAL_MINUTES * 60
        if (_throttle_row is None
                or (_throttle_now - _throttle_row.last_run).total_seconds()
                   > _interval_sec):
            if _throttle_row is None:
                _throttle_row = LastRunTracker(
                    name=_throttle_name, last_run=_throttle_now,
                )
            else:
                _throttle_row.last_run = _throttle_now
            _throttle_row.save()
            await find_offline_channels(wallet=best_wallet, api=api)
        # don't continue if there are pending channel opens.
        open_pending_response = await api.is_channel_open_pending(best_wallet["id"])
        if open_pending_response:
            logger.info(
                f"Not opening channel/getting more liquidity due to pending channel open on wallet {best_wallet['id']}"
            )
            continue
        # or recent channel closes
        close_pending_response = await get_most_recent_channel_close(api,best_wallet['id'])
        if close_pending_response:
            two_hours_ago=datetime.datetime.now()-datetime.timedelta(minutes=61)
            if close_pending_response>two_hours_ago:
                logger.info(
                    f"Not opening channel/getting more liquidity due to recent pending channel close on wallet {best_wallet['id']}"
                )
                continue
        # or we're due for a topup
        topup_result = await store_needs_topup(api,store_id)
        if topup_result:
            logger.warning(f'Skipping adding liquidity bc store needs topup before doing so: {store_name}')
            continue
        # or we don't have enough sats to open the channels we need
        needed_channel_liquidity_sizes = common_functions.distribute_sats_over_channels(liquidity_needed,
                                                                                        channels_needed)
        channel_sizes = [common_functions.liquidity_to_channel_size(item) for item in needed_channel_liquidity_sizes]

        amount_needed_including_onchain_reserves = sum(
            [common_functions.onchain_reserves_to_keep_for_channel(item) for item in channel_sizes])
        if amount_needed_including_onchain_reserves>best_wallet_balance_in_sats:
            logger.error(f'in liquidity_check this shouldnt happen (amount_needed_including_onchain_reserves>best_wallet_balance_in_sats)')
            continue

        if min(channel_sizes) < MIN_CHANNEL_SIZE_IN_SATS:
            logger.error(
                f'in liquidity_check this shouldnt happen (min(channel_sizes) < MIN_CHANNEL_SIZE_IN_SATS)')
            continue

        if sum(needed_channel_liquidity_sizes)+store_total_liquidity<MIN_INBOUND_LIQUIDITY:
            logger.error(
                f'in liquidity_check this shouldnt happen sum(needed_channel_liquidity_sizes)+store_total_liquidity<MIN_INBOUND_LIQUIDITY')
            continue
        if not MANUAL_CHANNEL_CREATION_ENABLED:
            # Channel creation has been delegated to an LSP. For LND
            # wallets we fire the LSPS1 request flow (quote both providers,
            # pick via Zeus-preference, pay on-chain). Electrum wallets
            # can't pay LSP invoices reliably in this codebase — they're
            # left to the operator. `calculate_topups` already sent the
            # low-inbound notification.
            log_decision(
                ("liquidity_check_manual_create_disabled", best_wallet["id"]),
                True,
                "liquidity_check: wallet %s needs more inbound liquidity; "
                "MANUAL_CHANNEL_CREATION_ENABLED=False -> delegating to LSP",
                best_wallet["id"],
            )
            if best_wallet.get("currency") == "btclnd":
                try:
                    await request_inbound_liquidity_from_lsp(
                        wallet=best_wallet, api=api,
                    )
                except Exception as e:
                    logger.error(
                        f"request_inbound_liquidity_from_lsp raised for "
                        f"wallet {best_wallet['id']}: {e} "
                        f"{traceback.print_exc()}"
                    )
            else:
                log_decision(
                    ("lsp_skipped_non_lnd_wallet", best_wallet["id"]),
                    True,
                    "Wallet %s needs inbound liquidity but LSPs are "
                    "LND-only and this wallet is currency=%s; operator "
                    "must handle manually",
                    best_wallet["id"], best_wallet.get("currency"),
                )
            continue
        # We're now in the MANUAL_CHANNEL_CREATION_ENABLED=True branch.
        # Same constraint as decide_onchain_to_ln and move_onchain_to_ln:
        # peer selection runs against the LND-gossip-derived candidate
        # DB, so Electrum wallets are skipped. The operator opens
        # channels manually via Electrum if they want inbound there.
        if best_wallet.get("currency") != "btclnd":
            log_decision(
                ("liquidity_check_manual_skipped_non_lnd", best_wallet["id"]),
                True,
                "liquidity_check: wallet %s needs inbound liquidity and "
                "MANUAL_CHANNEL_CREATION_ENABLED=True, but wallet is "
                "currency=%s; manual creation is LND-only. Operator "
                "must open channels manually for this wallet.",
                best_wallet["id"], best_wallet.get("currency"),
            )
            continue
        if DRY_RUN_FUNDS:
            logger.info(
                f"DRY RUN: Would try to open channel due to total_in_ln_channels<MIN_INBOUND_LIQUIDITY on wallet {best_wallet['id']}"
            )
        else:
            logger.info(
                f"Opening channel due to total_in_ln_channels<MIN_INBOUND_LIQUIDITY on wallet {best_wallet['id']}"
            )
        channel_open_successful = await attempt_create_channels(
            best_wallet["id"], api, channel_sizes
        )
        if channel_open_successful:
            continue
        # Get new lightning nodes, try again with all available funds.
        # Note: the per-failure refresh used to scrape Magma; now it's a
        # no-op because the daily lnd_graph_pull schedule covers the
        # same ground at a saner cadence. Keeping the log line so the
        # tick history is self-documenting.
        logger.info(
            "Still failed; next scheduled LND graph pull will refresh "
            "candidates. Skipping inline refresh."
        )
        channel_open_result = await attempt_create_channels(
            best_wallet["id"], api, channel_sizes
        )
        if channel_open_result:
            continue
async def lnurl_to_invoice(
    lnurl: str,
    payment_amount_in_sats: int,
    comment: Optional[str] = None,
) -> Optional[str]:
    """Given a Lightning Address and payment amount, return a BOLT11
    invoice or None on failure.

    `comment` is the optional LUD-12 comment string — used by the
    fee/referral payment paths to thread BB_STOREID through to the
    receiving endpoint, which typically lands as the invoice's
    description (BOLT-11 `d` field). Silently dropped if the
    recipient doesn't advertise `commentAllowed` in their LNURL
    metadata. See classes.get_lightning_invoice for details.
    """
    result = await get_lightning_invoice(
        lnurl, payment_amount_in_sats, comment=comment,
    )
    if result.get("success"):
        # print(f"Got lightning invoice from LNURL")
        # print(f"Amount: {result['amount_sats']} sats")
        # print(f"Invoice: {result['invoice']}")
        invoice = result["invoice"]
        invoice_amount_in_sats = int(result["amount_sats"])
        # Defensive: the LNURL host should return an invoice for the
        # exact amount we asked for. If it doesn't, log and skip
        # rather than asserting (assertions would propagate up and,
        # before the run_tick_loop hardening, could have killed the
        # whole loop on a misbehaving LNURL host).
        if invoice_amount_in_sats != payment_amount_in_sats:
            logger.error(
                "LNURL returned invoice for %d sat but we asked for "
                "%d sat; refusing to use this invoice",
                invoice_amount_in_sats, payment_amount_in_sats,
            )
            return None
        return invoice
    else:
        logger.error(
            f"Error getting LN invoice from LNURL: {result['error']}"
        )
        if "details" in result:
            logger.error(f"Details: {result['details']}")
        return None

async def calculate_fees(api: BitcartAPI) -> Optional[bool]:
    logger.info("Calculating fees...")
    fees_due = await new_calc_invoice_stats(api)
    if not ENABLE_FEE_SENDING:
        logger.info("SKIPPING FEE SENDING DUE TO ENABLE_FEE_SENDING=False")
        return True
    combined_cashout_made = False
    for store_id, calculated_cashout in fees_due.items():
        if combined_cashout_made:
            logger.debug("Skipping fee calc/pay bc combined cashout made")
            continue
        eligible_revenue = calculated_cashout.calc_total_eligible_revenue_in_sats()
        fees_already_paid = (
            calculated_cashout.calc_total_bb_fees_paid_in_sats(include_onchain_network_fees=FEES_PAID_INCLUDES_ONCHAIN_NETWORK_FEES,include_ln_network_fees=FEES_PAID_INCLUDES_LN_NETWORK_FEES)
        )
        total_fees_due = eligible_revenue * FEE_AMOUNT
        remaining_fees_due = total_fees_due - fees_already_paid
        if FORCE_FEE_AMOUNT:
            remaining_fees_due = FORCE_FEE_AMOUNT
        if remaining_fees_due == 0:
            logger.debug(f"no fee due for store {store_id}")
            continue
        if remaining_fees_due < 0:
            logger.warning(
                f"Reported negative fee due for store {store_id}, fee amount {remaining_fees_due}"
            )
            continue

        # Rail decision: per-destination LN-staleness fallback for the
        # dev fee. Returns True when FORCE_FEE_ONCHAIN_INSTEAD_OF_LN
        # is set OR when the LN-fee timestamp is older than threshold.
        # Independent of cashout/referral staleness.
        # Resolve the per-store wallet once; both fee and referral paths
        # use it. (Prior code re-fetched inside each rail.)
        full_store = await api.get_store_by_id(store_id)
        wallet_to_use = await api.get_best_ln_wallet_for_store(full_store)

        # ---------------- developer fee (try-LN-first) ----------------
        # Manual override goes straight to on-chain. Otherwise we try LN
        # first; LN success auto-refreshes LAST_SUCCESSFUL_LN_FEE_PAYMENT
        # and the rail decision recovers. LN failure escalates to
        # on-chain only when LN has been stale beyond threshold.
        if FORCE_FEE_ONCHAIN_INSTEAD_OF_LN:
            log_decision(
                ("fee_rail", store_id), "onchain_forced",
                "Dev fee rail (store %s): on-chain (FORCE_FEE_ONCHAIN_INSTEAD_OF_LN)",
                store_id,
            )
            await _pay_dev_fee_via_onchain(
                api, store_id, wallet_to_use, int(remaining_fees_due),
            )
        elif LN_FEE_DEST:
            log_decision(
                ("fee_rail", store_id), "ln_try",
                "Dev fee rail (store %s): trying LN first", store_id,
            )
            ln_ok = await _pay_dev_fee_via_ln(
                api, store_id, wallet_to_use, int(remaining_fees_due),
            )
            if not ln_ok and _ln_known_stale_for_fee_payment():
                log_decision(
                    ("fee_rail", store_id), "onchain_fallback_after_ln_fail",
                    "Dev fee rail (store %s): LN failed and last success was "
                    ">%d days ago — falling back to on-chain", store_id,
                    FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS,
                )
                await _pay_dev_fee_via_onchain(
                    api, store_id, wallet_to_use, int(remaining_fees_due),
                )
            elif not ln_ok:
                log_decision(
                    ("fee_rail", store_id), "ln_retry_next_tick",
                    "Dev fee rail (store %s): LN failed but not yet stale — "
                    "will retry LN next tick", store_id,
                )
        elif ONCHAIN_FEE_DEST:
            # No LN destination configured at all — go straight to on-chain.
            await _pay_dev_fee_via_onchain(
                api, store_id, wallet_to_use, int(remaining_fees_due),
            )

        # ---------------- referral fee (try-LN-first, flat) ------------
        if REFERRAL_FEE_AMOUNT > 0:
            remaining_referral_due = (
                calculated_cashout.calc_remaining_referral_fee_due_in_sats(
                    REFERRAL_FEE_AMOUNT
                )
            )
            if remaining_referral_due <= 0:
                log_decision(
                    ("referral_status", store_id), "paid_up",
                    "Referral fee for store %s is paid up", store_id,
                )
                continue
            if FORCE_REFERRAL_ONCHAIN_INSTEAD_OF_LN:
                log_decision(
                    ("referral_rail", store_id), "onchain_forced",
                    "Referral rail (store %s): on-chain "
                    "(FORCE_REFERRAL_ONCHAIN_INSTEAD_OF_LN)", store_id,
                )
                await _pay_referral_via_onchain(
                    api, store_id, wallet_to_use, int(remaining_referral_due),
                )
            elif REFERRAL_FEE_DEST and ENABLE_FEE_SENDING_LN:
                log_decision(
                    ("referral_rail", store_id), "ln_try",
                    "Referral rail (store %s): trying LN first", store_id,
                )
                ln_ok = await _pay_referral_via_ln(
                    api, store_id, wallet_to_use, int(remaining_referral_due),
                )
                if not ln_ok and _ln_known_stale_for_referral_payment():
                    log_decision(
                        ("referral_rail", store_id),
                        "onchain_fallback_after_ln_fail",
                        "Referral rail (store %s): LN failed and last success "
                        "was >%d days ago — falling back to on-chain",
                        store_id, REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS,
                    )
                    await _pay_referral_via_onchain(
                        api, store_id, wallet_to_use,
                        int(remaining_referral_due),
                    )
                elif not ln_ok:
                    log_decision(
                        ("referral_rail", store_id), "ln_retry_next_tick",
                        "Referral rail (store %s): LN failed but not yet "
                        "stale — will retry LN next tick", store_id,
                    )
            elif REFERRAL_ONCHAIN_DEST:
                # No LN destination -> go straight to on-chain.
                await _pay_referral_via_onchain(
                    api, store_id, wallet_to_use, int(remaining_referral_due),
                )
            else:
                logger.error(
                    "REFERRAL_FEE_AMOUNT is %s but neither REFERRAL_FEE_DEST "
                    "nor REFERRAL_ONCHAIN_DEST is set for store %s; skipping",
                    REFERRAL_FEE_AMOUNT, store_id,
                )
    return True


# -- per-rail per-destination payment helpers ---------------------------------
# Each returns True iff funds were successfully sent on that rail. Callers
# use the return value to decide whether to fall back to the other rail.

async def _pay_dev_fee_via_ln(
    api: BitcartAPI, store_id: str, wallet_to_use: dict, amount: int,
) -> bool:
    if not ENABLE_FEE_SENDING_LN:
        logger.warning("Skipping LN fee sending due to not ENABLE_FEE_SENDING_LN")
        return False
    try:
        wallet_max_payout = int(await api.get_outbound_liquidity(wallet_to_use["id"]))
    except Exception as e:
        logger.warning(f"Failed to read outbound for LN fee on store {store_id}: {e}")
        return False
    if wallet_max_payout < MIN_FEE_OUT:
        logger.warning(
            f"Unable to send fee due to wallet_max_payout {wallet_max_payout} < "
            f"MIN_FEE_OUT, will try later"
        )
        return False
    if DRY_RUN_FUNDS:
        logger.warning(
            f"Skipping LN fee due to DRY_RUN_FUNDS (would have sent {amount} sat)"
        )
        return False
    SimpleDateTimeField.replace(
        name="LAST_LN_FEE_PAYMENT_ATTEMPT", date=datetime.datetime.now(),
    ).execute()
    if FORCE_FEE_INVOICE:
        invoice = FORCE_FEE_INVOICE
    else:
        # Attach BB_STOREID via the LUD-12 `comment` field so the
        # receiving endpoint (e.g. the dev fee address) can attribute
        # this payment to our deployment. The recipient threads the
        # comment into the BOLT-11 invoice's `d` (description) when
        # they support LUD-12; otherwise the comment is silently
        # dropped without affecting the payment.
        invoice = await lnurl_to_invoice(
            LN_FEE_DEST, amount,
            comment=f"storeid:{BB_STOREID}",
        )
        if not invoice:
            return False
    ok = await electrum_pay_ln_invoice(
        invoice, FEE_PAYOUT_REASON, wallet=wallet_to_use, api=api,
    )
    if ok:
        log_event("Fee payment successful (%d sats, wallet %s)",
                  amount, wallet_to_use.get("id"))
        SimpleDateTimeField.replace(
            name="LAST_SUCCESSFUL_LN_FEE_PAYMENT",
            date=datetime.datetime.now(),
        ).execute()
        return True
    logger.error('Failed to pay fee via LN!')
    return False


async def _pay_dev_fee_via_onchain(
    api: BitcartAPI, store_id: str, wallet_to_use: dict, amount: int,
) -> bool:
    if not ONCHAIN_FEE_DEST:
        logger.error(
            "Dev fee rail is on-chain but ONCHAIN_FEE_DEST is unset; "
            "cannot send. Store %s", store_id,
        )
        return False
    if await has_pending_channel_activity(wallet=wallet_to_use, api=api):
        log_decision(
            ("fee_blocked_pending", store_id), True,
            "Onchain fee payment blocked on store %s: pending channel activity",
            store_id,
        )
        return False
    log_decision(("fee_blocked_pending", store_id), False, "")
    if DRY_RUN_FUNDS:
        logger.warning(
            "DRY RUN: would have sent %d sat onchain fee to %s",
            amount, ONCHAIN_FEE_DEST,
        )
        return False
    SimpleDateTimeField.replace(
        name="LAST_ONCHAIN_FEE_PAYMENT_ATTEMPT",
        date=datetime.datetime.now(),
    ).execute()
    ok = await electrum_pay_onchain(
        ONCHAIN_FEE_DEST, sats_to_btc(amount), label=FEE_PAYOUT_REASON,
        wallet=wallet_to_use, api=api,
    )
    if ok:
        log_event(
            "Onchain dev-fee payment sent: %d sat from wallet %s to %s",
            amount, wallet_to_use.get("id"), ONCHAIN_FEE_DEST,
        )
        SimpleDateTimeField.replace(
            name="LAST_SUCCESSFUL_ONCHAIN_FEE_PAYMENT",
            date=datetime.datetime.now(),
        ).execute()
        return True
    logger.error("Failed to pay dev fee on-chain for store %s", store_id)
    return False


async def _pay_referral_via_ln(
    api: BitcartAPI, store_id: str, wallet_to_use: dict, amount: int,
) -> bool:
    if not REFERRAL_FEE_DEST:
        logger.error(
            "REFERRAL_FEE_AMOUNT > 0 but REFERRAL_FEE_DEST is unset for "
            "store %s; cannot send via LN", store_id,
        )
        return False
    try:
        wallet_max_payout = int(await api.get_outbound_liquidity(wallet_to_use["id"]))
    except Exception as e:
        logger.warning(
            f"Failed to read outbound for referral payment on store {store_id}: {e}"
        )
        return False
    if wallet_max_payout < MIN_FEE_OUT:
        logger.warning(
            f"Unable to send referral due to wallet_max_payout "
            f"{wallet_max_payout} < MIN_FEE_OUT; will try later"
        )
        return False
    if DRY_RUN_FUNDS:
        logger.warning(
            f"Skipping LN referral due to DRY_RUN_FUNDS (would have sent "
            f"{amount} sat to {REFERRAL_FEE_DEST})"
        )
        return False
    SimpleDateTimeField.replace(
        name="LAST_LN_REFERRAL_PAYMENT_ATTEMPT",
        date=datetime.datetime.now(),
    ).execute()
    # Attach BB_STOREID via the LUD-12 `comment` field, same as the
    # dev-fee path. Lands in the BOLT-11 invoice description when the
    # recipient supports LUD-12; silently dropped otherwise.
    invoice = await lnurl_to_invoice(
        REFERRAL_FEE_DEST, amount,
        comment=f"storeid:{BB_STOREID}",
    )
    if not invoice:
        return False
    ok = await electrum_pay_ln_invoice(
        invoice, REFERRAL_PAYOUT_REASON, wallet=wallet_to_use, api=api,
    )
    if ok:
        log_event(
            "Referral payment successful (%d sats, store %s, wallet %s)",
            amount, store_id, wallet_to_use.get("id"),
        )
        SimpleDateTimeField.replace(
            name="LAST_SUCCESSFUL_LN_REFERRAL_PAYMENT",
            date=datetime.datetime.now(),
        ).execute()
        return True
    logger.error("Failed to pay referral fee via LN for store %s", store_id)
    return False


async def _pay_referral_via_onchain(
    api: BitcartAPI, store_id: str, wallet_to_use: dict, amount: int,
) -> bool:
    if not REFERRAL_ONCHAIN_DEST:
        logger.error(
            "Referral rail is on-chain but REFERRAL_ONCHAIN_DEST is unset; "
            "cannot send. Store %s, %d sat due.", store_id, amount,
        )
        return False
    if await has_pending_channel_activity(wallet=wallet_to_use, api=api):
        log_decision(
            ("referral_blocked_pending", store_id), True,
            "Onchain referral blocked on store %s: pending channel activity",
            store_id,
        )
        return False
    log_decision(("referral_blocked_pending", store_id), False, "")
    if DRY_RUN_FUNDS:
        logger.warning(
            "DRY RUN: would have sent %d sat onchain referral to %s",
            amount, REFERRAL_ONCHAIN_DEST,
        )
        return False
    SimpleDateTimeField.replace(
        name="LAST_ONCHAIN_REFERRAL_PAYMENT_ATTEMPT",
        date=datetime.datetime.now(),
    ).execute()
    ok = await electrum_pay_onchain(
        REFERRAL_ONCHAIN_DEST, sats_to_btc(amount), label=REFERRAL_PAYOUT_REASON,
        wallet=wallet_to_use, api=api,
    )
    if ok:
        log_event(
            "Onchain referral payment sent: %d sat from wallet %s to %s",
            amount, wallet_to_use.get("id"), REFERRAL_ONCHAIN_DEST,
        )
        SimpleDateTimeField.replace(
            name="LAST_SUCCESSFUL_ONCHAIN_REFERRAL_PAYMENT",
            date=datetime.datetime.now(),
        ).execute()
        return True
    logger.error("Failed to pay referral fee on-chain for store %s", store_id)
    return False


# ---------------------------------------------------------------------------
# Cashout / fee-payment recency tracking. The do_*_cashouts() functions
# write timestamp rows into SimpleDateTimeField; the helpers below read
# those rows so calling code can ask "how long since LN last succeeded?"
# and switch rails when LN has been quietly failing.
# ---------------------------------------------------------------------------

def get_last_date(name: str) -> Optional[datetime.datetime]:
    """Return the timestamp last written under `name` via
    SimpleDateTimeField, or None if nothing was ever recorded.

    Uses `order_by(date.desc()).first()` rather than `.get()` so it stays
    correct even if a pre-migration DB still has duplicate rows for the
    same name.
    """
    row = (SimpleDateTimeField
           .select()
           .where(SimpleDateTimeField.name == name)
           .order_by(SimpleDateTimeField.date.desc())
           .first())
    return row.date if row else None


def days_since_last_successful_ln_cashout() -> Optional[int]:
    """Number of whole days (floor) since the most recent successful LN
    cashout, or None if no successful LN cashout has ever been recorded.

    Examples (now = 2026-05-18):
      - last recorded 2026-05-10 -> returns 8
      - last recorded 2026-05-18 (today, earlier)  -> returns 0
      - never recorded -> returns None
    """
    last = get_last_date("LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT")
    if last is None:
        return None
    return (datetime.datetime.now() - last).days


def days_since_last_successful_ln_fee_payment() -> Optional[int]:
    """Whole days since LAST_SUCCESSFUL_LN_FEE_PAYMENT, or None if no
    successful LN dev-fee payment has ever been recorded. Independent
    of cashout / referral timestamps — a stale cashout doesn't make
    the dev fee 'stale' too."""
    last = get_last_date("LAST_SUCCESSFUL_LN_FEE_PAYMENT")
    if last is None:
        return None
    return (datetime.datetime.now() - last).days


def days_since_last_successful_ln_referral_payment() -> Optional[int]:
    """Whole days since LAST_SUCCESSFUL_LN_REFERRAL_PAYMENT, or None if
    no successful LN referral payment has ever been recorded. Tracked
    separately from the dev fee timestamp — a stale dev fee doesn't
    force the referral to on-chain (and vice versa)."""
    last = get_last_date("LAST_SUCCESSFUL_LN_REFERRAL_PAYMENT")
    if last is None:
        return None
    return (datetime.datetime.now() - last).days


def should_prefer_onchain_fee_payment() -> bool:
    """Mirror of should_prefer_onchain_cashout but for the dev fee
    destination, reading the dev-fee-specific LN timestamp.

    Returns True when:
      - FORCE_FEE_ONCHAIN_INSTEAD_OF_LN is set (manual operator override), OR
      - FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS is configured AND the last
        successful LN fee payment is older than that threshold.

    Returns False when no LN fee payment has ever been recorded — a
    brand-new install hasn't tried LN yet, so we shouldn't preemptively
    fall back. Same policy as the cashout fallback.
    """
    if FORCE_FEE_ONCHAIN_INSTEAD_OF_LN:
        return True
    if FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS is not None:
        days = days_since_last_successful_ln_fee_payment()
        if days is not None and days > FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS:
            return True
    return False


def should_prefer_onchain_referral_payment() -> bool:
    """Mirror of should_prefer_onchain_fee_payment but for the
    referral destination, reading the referral-specific LN timestamp.

    Returns True when:
      - FORCE_REFERRAL_ONCHAIN_INSTEAD_OF_LN (manual operator override), OR
      - REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS is configured AND the
        last successful LN referral payment is older than that threshold.
    """
    if FORCE_REFERRAL_ONCHAIN_INSTEAD_OF_LN:
        return True
    if REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS is not None:
        days = days_since_last_successful_ln_referral_payment()
        if days is not None and days > REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS:
            return True
    return False


def _ln_known_stale_for_fee_payment() -> bool:
    """True only when LN fee history exists AND is stale beyond
    threshold. Used by the post-LN-failure fallback decision — distinct
    from `should_prefer_onchain_fee_payment` because it ignores the
    `FORCE_FEE_ONCHAIN_INSTEAD_OF_LN` knob (the caller already
    short-circuited that path)."""
    if FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS is None:
        return False
    days = days_since_last_successful_ln_fee_payment()
    return days is not None and days > FEE_SWITCH_TO_ONCHAIN_AFTER_X_DAYS


def _ln_known_stale_for_referral_payment() -> bool:
    """Referral counterpart to _ln_known_stale_for_fee_payment."""
    if REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS is None:
        return False
    days = days_since_last_successful_ln_referral_payment()
    return days is not None and days > REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS


def _ln_known_stale_for_cashout() -> bool:
    """Cashout counterpart."""
    if CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS is None:
        return False
    days = days_since_last_successful_ln_cashout()
    return days is not None and days > CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS


def should_prefer_onchain_cashout() -> bool:
    """Single source of truth for the cashout-rail decision this tick.

    Consulted by both `do_cashouts` and `decide_onchain_to_ln` so they
    can't disagree: previously `decide_onchain_to_ln` read the *global*
    PREFER_CASHOUT_ONCHAIN while `do_cashouts` had its own local
    fallback flip, meaning the same tick could open a channel AND then
    try to cash out the funds that just went into it.

    Returns True if:
      - PREFER_CASHOUT_ONCHAIN is set, OR
      - ENABLE_CASHOUT_ONCHAIN is set, CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS
        is configured, and the last successful LN cashout is older than
        that threshold.

    Note that `None` from `days_since_last_successful_ln_cashout()` (no
    LN cashout ever recorded) deliberately does NOT trigger the
    fallback — a brand-new install hasn't had a chance to try LN yet.
    """
    if PREFER_CASHOUT_ONCHAIN:
        return True
    if (ENABLE_CASHOUT_ONCHAIN
            and CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS is not None):
        days = days_since_last_successful_ln_cashout()
        if days is not None and days > CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS:
            return True
    return False


async def do_onchain_cashouts(api:BitcartAPI,
                              wallet_id: str, cashout_amount_avail_onchain: int
                              ) -> bool:
    """Send `cashout_amount_avail_onchain` sat on-chain to CASHOUT_ONCHAIN.
    Returns True on a successful broadcast, False otherwise (config
    error, amount below MIN_ONCHAIN_CASHOUT, DRY_RUN_FUNDS, or broadcast
    failure). Caller uses the return value to decide whether to mark
    the wallet+destination as cashed-out this tick or to retry next."""
    if not CASHOUT_ONCHAIN:
        logger.error('In do_onchain_cashouts, no CASHOUT_ONCHAIN (address), not cashing out')
        return False
    if FORCE_CASHOUT_AMOUNT_ONCHAIN:
        cashout_amount_avail_onchain = FORCE_CASHOUT_AMOUNT_ONCHAIN
    if cashout_amount_avail_onchain < MIN_ONCHAIN_CASHOUT:
        logger.info(
            f"Unable to run onchain cashout due to MIN_ONCHAIN_CASHOUT {cashout_amount_avail_onchain}<{MIN_ONCHAIN_CASHOUT}"
        )
        return False
    full_wallet = await api.get_wallet(wallet_id)
    if DRY_RUN_FUNDS:
        logger.info(
            f"DRY RUN: For wallet {wallet_id} would attempt to cashout "
            f"via onchain {cashout_amount_avail_onchain}"
        )
        return False
    logger.info(
        f"For wallet {wallet_id} Attempting to cashout via onchain "
        f"{cashout_amount_avail_onchain}"
    )

    transaction_result = await electrum_pay_onchain(
        CASHOUT_ONCHAIN, sats_to_btc(cashout_amount_avail_onchain),
        label=CASHOUT_REASON,
        wallet=full_wallet, api=api,
    )
    if transaction_result:
        log_event("Onchain cashout sent: %d sats from wallet %s to %s",
                  cashout_amount_avail_onchain, wallet_id, CASHOUT_ONCHAIN)
        SimpleDateTimeField.replace(
            name="LAST_SUCCESSFUL_ONCHAIN_CASHOUT_PAYMENT",
            date=datetime.datetime.now(),
        ).execute()
        return True
    return False


async def drain_ln_to_onchain(
    api: "BitcartAPI",
    *,
    wallet: Dict[str, Any],
    dest_addr: str,
) -> bool:
    """When the cashout rail is on-chain, fire a loop-out to move excess
    LN balance to `dest_addr`. Without this, funds in LN channels are
    stranded whenever PREFER_CASHOUT_ONCHAIN or the recency fallback
    chooses the on-chain path — they'd just sit in channels.

    Called from `do_cashouts` once per wallet per tick. Gated by
    LN_DRAIN_MIN_SWAP_SAT so dust-sized excess doesn't waste fees;
    capped per call at LN_DRAIN_MAX_PER_TICK_SAT so a single tick can't
    initiate a swap above loopserver's per-swap max. Multiple ticks
    drain larger amounts incrementally.

    LND-only (loop is LND-only). Electrum wallets short-circuit so the
    caller can treat the function uniformly.

    Reserve: leaves MIN_INBOUND_LIQUIDITY_PER_CHANNEL × channel_count
    behind, so we keep at least some inbound liquidity. Without this,
    repeated draining would close us out of receiving capacity.

    Returns True iff a swap was successfully initiated (the swap itself
    is still in flight on return; this only confirms the server accepted
    the request).
    """
    if wallet.get("currency") != "btclnd":
        return False
    wallet_id = wallet["id"]
    if await has_pending_channel_activity(wallet=wallet, api=api):
        log_decision(
            ("ln_drain_blocked_pending", wallet_id),
            True,
            "LN drain blocked on wallet %s: pending channel activity",
            wallet_id,
        )
        return False
    log_decision(("ln_drain_blocked_pending", wallet_id), False, "")

    try:
        channels = await api.get_wallet_ln_channels(
            wallet_id, active_only=True, online_only=True
        )
    except Exception as e:
        logger.warning(
            f"drain_ln_to_onchain: get_wallet_ln_channels failed for "
            f"wallet {wallet_id}: {e}"
        )
        return False

    if not channels:
        return False

    total_local = sum(int(c.get("local_balance") or 0) for c in channels)
    reserve = MIN_INBOUND_LIQUIDITY_PER_CHANNEL * len(channels)
    excess = total_local - reserve

    log_decision(
        ("ln_drain_excess", wallet_id),
        excess,
        "LN drain math for wallet %s: total_local=%d - reserve=%d "
        "(%d channels × %d) = excess=%d sat",
        wallet_id, total_local, reserve, len(channels),
        MIN_INBOUND_LIQUIDITY_PER_CHANNEL, excess,
    )

    if excess < LN_DRAIN_MIN_SWAP_SAT:
        return False

    amount = min(excess, LN_DRAIN_MAX_PER_TICK_SAT)
    log_event(
        "LN drain: initiating loop-out of %d sat (excess=%d) from wallet %s -> %s",
        amount, excess, wallet_id, dest_addr,
    )
    try:
        result = await initiate_lightning_to_onchain_swap(
            wallet=wallet, api=api,
            amount_sat=amount, dest_addr=dest_addr,
        )
    except Exception as e:
        logger.error(
            f"drain_ln_to_onchain: initiate_lightning_to_onchain_swap "
            f"raised for wallet {wallet_id}: {e}"
        )
        return False
    if result is None:
        return False
    log_event(
        "LN drain accepted by provider: swap_id=%s, htlc_address=%s",
        result.swap_id[:16] + "...", result.htlc_address,
    )
    return True


async def do_ln_cashouts(api:BitcartAPI,
    wallet_id: str, cashout_amount_avail_ln: int
) -> bool:
    """Send `cashout_amount_avail_ln` sat via LN to CASHOUT_LIGHTNING_ADDRESS.
    Returns True on success, False otherwise. Caller treats `False` as
    "LN didn't get the funds out this attempt" and decides whether to
    fall back to on-chain based on per-destination staleness.

    Retry behavior: if the first attempt fails, halve the amount and
    retry, down to MIN_LN_CASHOUT_IN_SATS. Path-finder failures on
    larger amounts often clear when the amount is small enough to find
    a route through.

    Bug fixes vs the prior version:
      - Loop condition was `<= 1000`, never executed for cashouts > 1000 sat.
        Changed to `>= MIN_LN_CASHOUT_IN_SATS` so retry actually runs.
      - LNURL was queried with the original amount instead of the
        current retry amount; fixed.
      - Function returned None on all paths; now returns bool so
        do_cashouts can decide on the fallback.
    """
    if not CASHOUT_LIGHTNING_ADDRESS:
        logger.error('In do_ln_cashouts, no CASHOUT_LIGHTNING_ADDRESS, not cashing out')
        return False
    if FORCE_CASHOUT_AMOUNT_LN:
        cashout_amount_avail_ln = FORCE_CASHOUT_AMOUNT_LN
    if cashout_amount_avail_ln < MIN_LN_CASHOUT_IN_SATS:
        logger.info(
            f"Unable to run LN cashout due to MIN_LN_CASHOUT_IN_SATS "
            f"{cashout_amount_avail_ln} < {MIN_LN_CASHOUT_IN_SATS}"
        )
        return False
    full_wallet = await api.get_wallet(wallet_id)
    if DRY_RUN_FUNDS:
        logger.info(
            f"DRY RUN: For wallet {wallet_id} would attempt to cashout "
            f"via LN {cashout_amount_avail_ln}"
        )
        return False
    logger.info(
        f"For wallet {wallet_id} Attempting to cashout via LN "
        f"{cashout_amount_avail_ln}"
    )

    actual_cashout_amount = cashout_amount_avail_ln
    while actual_cashout_amount >= MIN_LN_CASHOUT_IN_SATS:
        SimpleDateTimeField.replace(
            name="LAST_LN_CASHOUT_ATTEMPT",
            date=datetime.datetime.now(),
        ).execute()
        if FORCE_CASHOUT_INVOICE:
            invoice = FORCE_CASHOUT_INVOICE
        else:
            invoice = await lnurl_to_invoice(
                CASHOUT_LIGHTNING_ADDRESS, actual_cashout_amount,
            )
            if not invoice:
                logger.error('Error turning LNURL to invoice, not making cashout')
                return False
        ln_transaction_result = await electrum_pay_ln_invoice(
            invoice, label=CASHOUT_REASON,
            wallet=full_wallet, api=api,
        )
        if ln_transaction_result:
            log_event(
                "LN cashout sent: %d sats from wallet %s to %s",
                actual_cashout_amount, wallet_id, CASHOUT_LIGHTNING_ADDRESS,
            )
            SimpleDateTimeField.replace(
                name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT",
                date=datetime.datetime.now(),
            ).execute()
            return True
        actual_cashout_amount = int(actual_cashout_amount / 2)
    return False



async def notused_do_onchain_cashouts(
    wallet_id: str, cashout_amount_avail_onchain: int, store: dict, api: BitcartAPI
):
    if not CASHOUT_ONCHAIN:
        return
    if not ENABLE_CASHOUT_ONCHAIN:
        logger.warning(
            f"Skipping actual onchain cashout due to ENABLE_CASHOUT_ONCHAIN. Would have sent {cashout_amount_avail_onchain} from {wallet_id} to {ONCHAIN_FEE_DEST}"
        )
        return
    if cashout_amount_avail_onchain < MIN_ONCHAIN_CASHOUT:
        logger.info("Unable to run cashout due to MIN_ONCHAIN_CASHOUT")
        return
    if FORCE_CASHOUT_AMOUNT_ONCHAIN:
        cashout_amount_avail_onchain = FORCE_CASHOUT_AMOUNT_ONCHAIN
    if (
        cashout_amount_avail_onchain < MIN_ONCHAIN_CASHOUT
        and not FORCE_CASHOUT_AMOUNT_ONCHAIN
    ):
        logger.info(
            f"Skipping bc fee_due_in_sats<MIN_ONCHAIN_CASHOUT {cashout_amount_avail_onchain}:{MIN_ONCHAIN_CASHOUT}"
        )
        return
    if DRY_RUN_FUNDS:
        logger.info(
            f"DRY RUN: Onchain cashout would be created: store {store} wallet {wallet_id} amount {cashout_amount_avail_onchain}"
        )
        return
    # do on-chain cashout
    cashout_result = await api.create_payout_onchain(
        store["id"], wallet_id, cashout_amount_avail_onchain, CASHOUT_ONCHAIN
    )
    cashout_approval_result = None
    cashout_send_result = None
    logger.info(
        f"Onchain cashout created: store {store} wallet {wallet_id} amount {cashout_amount_avail_onchain} payout id {cashout_result.get('id')}"
    )


async def do_cashouts(api: BitcartAPI) -> Optional[bool]:
    """
    Make appropriate LN cashouts, return False/None if some kind of error
    """
    logger.info("Calculating cashouts...")
    if not ENABLE_CASHOUT_ONCHAIN and not ENABLE_CASHOUT_LN:
        logger.info("SKIPPING CASHOUTS DUE TO ENABLE_CASHOUT_*")
        return None

    store_list=await api.get_stores()
    used_wallets=set()
    for store in store_list:
        store_id=store['id']
        best_wallet=await api.get_best_ln_wallet_for_store(store)
        wallet_id=best_wallet['id']
        if wallet_id in used_wallets:
            continue
        used_wallets.add(wallet_id)

        # --- LN cashout leg ---
        # Tries LN first unless PREFER_CASHOUT_ONCHAIN is set. LN success
        # auto-refreshes LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT; LN failure
        # paired with staleness triggers the drain helper to move LN
        # funds out via loop-out (when LOOP_OUT_ENABLED).
        ln_ok = False
        ln_attempted = False
        if not PREFER_CASHOUT_ONCHAIN:
            available_ln = 0
            for channel in await api.get_wallet_ln_channels(
                wallet_id, active_only=True, online_only=True,
            ):
                available_ln += int(channel['local_balance'])
            if FORCE_CASHOUT_AMOUNT_LN:
                # Operator override: send exactly this amount, no LSP
                # shortfall reservation.
                available_ln = FORCE_CASHOUT_AMOUNT_LN
            else:
                # Hold back enough LN balance to cover the on-chain
                # shortfall vs the LSP-purchase reserve floor. If on
                # chain is already healthy, shortfall is 0 and the
                # full LN balance is eligible for cashout. The held-
                # back amount stays in the channel; if the operator
                # later needs on-chain funds to buy an LSP channel,
                # closing a channel returns the local balance on-chain.
                wallet_onchain_sat = btc_to_sats(
                    float(best_wallet.get("balance") or 0)
                )
                reserve_floor = effective_min_reserve_onchain()
                lsp_shortfall = max(0, reserve_floor - wallet_onchain_sat)
                if lsp_shortfall > 0:
                    held_back = min(lsp_shortfall, available_ln)
                    log_decision(
                        ("ln_cashout_holdback", wallet_id), held_back,
                        "LN cashout (wallet %s): holding back %d sat for "
                        "LSP shortfall (on-chain %d < reserve floor %d)",
                        wallet_id, held_back, wallet_onchain_sat, reserve_floor,
                    )
                    available_ln = max(0, available_ln - lsp_shortfall)
                else:
                    # Healthy on-chain — clear any prior holdback state.
                    log_decision(
                        ("ln_cashout_holdback", wallet_id), 0,
                        "LN cashout (wallet %s): no LSP shortfall; "
                        "full LN balance eligible for cashout", wallet_id,
                    )
            if available_ln < 0:
                logger.warning(
                    f"Reported negative LN cashout due for wallet {wallet_id}"
                )
            elif available_ln > 0:
                ln_attempted = True
                log_decision(
                    ("cashout_rail_ln", wallet_id), "ln_try",
                    "Cashout LN leg (wallet %s): trying", wallet_id,
                )
                try:
                    ln_ok = await do_ln_cashouts(api, wallet_id, available_ln)
                except Exception as e:
                    logger.error(
                        f'Exception in do_ln_cashouts: {e} {traceback.print_exc()}'
                    )
                    ln_ok = False
                if ln_ok:
                    log_decision(
                        ("cashout_rail_ln", wallet_id), "ln_success",
                        "Cashout LN leg (wallet %s): succeeded", wallet_id,
                    )
                    # Clear any prior "funds stranded" warning — LN
                    # works again. Logs an INFO transition exactly once,
                    # when stranded state flips back to False.
                    log_decision(
                        ("ln_funds_stranded", wallet_id), False,
                        "Wallet %s: LN cashouts recovered; funds no "
                        "longer stranded in channels.", wallet_id,
                    )
                elif _ln_known_stale_for_cashout():
                    # Persistent LN failure — drain stranded channel
                    # funds out via loop-out so they don't accumulate.
                    log_decision(
                        ("cashout_rail_ln", wallet_id), "ln_stale_fallback",
                        "Cashout LN leg (wallet %s): failed and stale; "
                        "running drain helper", wallet_id,
                    )
                    await _drain_ln_for_cashout_if_enabled(
                        api, wallet_id, best_wallet,
                    )
                    # The drain helper is a silent no-op when
                    # LOOP_OUT_ENABLED is off or CASHOUT_ONCHAIN is
                    # unset. In that case the operator's LN funds are
                    # genuinely stuck — the script can't recover them
                    # without external action. Surface this loudly
                    # exactly once per state transition so it lands in
                    # decisions.log, the console, and the plugin Logs
                    # tab without spamming every tick.
                    drain_will_run = bool(LOOP_OUT_ENABLED and CASHOUT_ONCHAIN)
                    stranded = available_ln > 0 and not drain_will_run
                    if stranded:
                        days_stale = days_since_last_successful_ln_cashout()
                        log_decision(
                            ("ln_funds_stranded", wallet_id), True,
                            "STRANDED LN FUNDS: wallet %s has %d sat in "
                            "channels but LN cashouts have been failing "
                            "for %s days. Automatic recovery is OFF "
                            "(LOOP_OUT_ENABLED=%s, CASHOUT_ONCHAIN=%s). "
                            "To recover, do ONE of: "
                            "(a) set LOOP_OUT_ENABLED=True AND set "
                            "CASHOUT_ONCHAIN to a Bitcoin address (a "
                            "running loopd is required for each LND "
                            "wallet); "
                            "(b) close the channel cooperatively or by "
                            "force-close — the local balance returns "
                            "on-chain after the timelock and the next "
                            "tick's on-chain cashout will sweep it; "
                            "(c) manually pay an outbound LN invoice "
                            "to drain the channel into something you "
                            "control.",
                            wallet_id, available_ln, days_stale,
                            "True" if LOOP_OUT_ENABLED else "False",
                            "set" if CASHOUT_ONCHAIN else "unset",
                            level=logging.WARNING,
                        )
                    else:
                        # Either drain is wired up and will run, or
                        # there's nothing to strand. Clear any prior
                        # warning so the operator sees the recovery.
                        log_decision(
                            ("ln_funds_stranded", wallet_id), False,
                            "Wallet %s: LN drain pathway is configured "
                            "or no LN balance to strand.", wallet_id,
                        )
                else:
                    log_decision(
                        ("cashout_rail_ln", wallet_id), "ln_retry_next_tick",
                        "Cashout LN leg (wallet %s): failed but not yet "
                        "stale; will retry LN next tick", wallet_id,
                    )

        if PREFER_CASHOUT_ONCHAIN:
            # When the operator has chosen on-chain mode, drain LN
            # balance too (the funds would otherwise be stranded).
            log_decision(
                ("cashout_rail_ln", wallet_id), "ln_skipped_forced",
                "Cashout LN leg (wallet %s): skipped "
                "(PREFER_CASHOUT_ONCHAIN); draining LN balance",
                wallet_id,
            )
            await _drain_ln_for_cashout_if_enabled(
                api, wallet_id, best_wallet,
            )

        # --- on-chain cashout leg, ALWAYS attempted ---
        # Independent of the LN leg's outcome. Sweeps any on-chain
        # revenue (customer payments that came in on-chain) regardless
        # of LN state. safe_to_spend already accounts for the LSP
        # price floor via effective_min_reserve_onchain, so we won't
        # drain below what's needed to buy a fresh LSP channel.
        await _attempt_onchain_cashout(api, store_id, wallet_id, best_wallet)
    return True


async def _drain_ln_for_cashout_if_enabled(
    api: BitcartAPI, wallet_id: str, best_wallet: dict,
) -> None:
    """Loop-out LN excess to CASHOUT_ONCHAIN. Idempotent within
    drain_ln_to_onchain's own logic (it has its own threshold + cap +
    pending-channel guard). Gated by LOOP_OUT_ENABLED and the presence
    of CASHOUT_ONCHAIN as the swap destination."""
    if not (LOOP_OUT_ENABLED and CASHOUT_ONCHAIN):
        return
    try:
        await drain_ln_to_onchain(
            api, wallet=best_wallet, dest_addr=CASHOUT_ONCHAIN,
        )
    except Exception as e:
        logger.error(
            f"Exception in drain_ln_to_onchain wallet {wallet_id}: "
            f"{e} {traceback.print_exc()}"
        )


async def _attempt_onchain_cashout(
    api: BitcartAPI, store_id: str, wallet_id: str, best_wallet: dict,
) -> bool:
    """Send the wallet's on-chain excess to CASHOUT_ONCHAIN. Fires every
    tick (not just on LN fallback) so on-chain customer revenue doesn't
    pile up while LN cashouts are working. Returns True if a tx was
    broadcast.

    Reserve math: safe_to_spend() subtracts MIN_RESERVE_ONCHAIN PLUS the
    6-month LSP price high-water mark (effective_min_reserve_onchain),
    so the wallet always has enough headroom to fund a fresh LSP
    channel order."""
    if await has_pending_channel_activity(wallet=best_wallet, api=api):
        log_decision(
            ("onchain_cashout_blocked_pending", wallet_id),
            True,
            "Onchain cashout blocked on wallet %s: pending channel "
            "open or coop close. (Force closes do NOT block.)",
            wallet_id,
        )
        return False
    log_decision(
        ("onchain_cashout_blocked_pending", wallet_id),
        False,
        "Onchain cashout no longer blocked on wallet %s: pending "
        "channel activity cleared", wallet_id,
    )
    available_onchain_sats = await safe_to_spend(api, store_id)
    if FORCE_CASHOUT_AMOUNT_ONCHAIN:
        available_onchain_sats = FORCE_CASHOUT_AMOUNT_ONCHAIN
    if available_onchain_sats < 0:
        logger.warning(
            f"Reported negative onchain cashout due for wallet {wallet_id}"
        )
        return False
    if available_onchain_sats == 0:
        logger.debug(f'No onchain cashout available for wallet {wallet_id}')
        return False
    try:
        return await do_onchain_cashouts(api, wallet_id, available_onchain_sats)
    except Exception as e:
        logger.error(f'Exception in do_onchain_cashouts: {e} {traceback.print_exc()}')
        return False

async def topup_goal_amount(api:BitcartAPI,store_id:str)->Optional[int]:
    """How much on-chain balance a store should aim for so it can
    acquire the configured MIN_INBOUND_LIQUIDITY. Returns None on
    failure or zero-goal.

    Branches on MANUAL_CHANNEL_CREATION_ENABLED:

      Manual mode: caller funds each channel themselves. The goal
        is the sum of (per-channel size + per-channel on-chain fee
        reserve). With defaults (100k inbound, 2 channels), this
        is roughly ~125k sat — channel capital dominates.

      LSP mode (default): caller pays the LSP an invoice (a few
        percent of channel size) and the LSP funds their side. We
        only open ONE LSP channel per wallet at a time
        (see _wallet_has_open_lsp_order — one-LSP-channel-per-wallet
        invariant), so the topup goal is just the cost of one
        channel. effective_min_reserve_onchain() already encodes
        this: it returns max(MIN_RESERVE_ONCHAIN, 6-month recent
        LSP high-water price) capped at LSP_RESERVE_CAP_SAT.
        Typical LSP cost: a few thousand sat.

    The previous behavior always computed the manual-mode amount
    regardless of mode, over-asking by ~40× in the default LSP
    deployment.
    """
    if not MANUAL_CHANNEL_CREATION_ENABLED:
        # LSP mode. The goal IS the on-chain reserve floor — the
        # amount we need to keep on-hand to buy a fresh LSP channel.
        # If the recent 6-month LSP price is zero (no quotes yet),
        # we fall back to MIN_RESERVE_ONCHAIN; if quotes have been
        # high, we track them up to LSP_RESERVE_CAP_SAT. Either way,
        # the goal is single-channel-cost, NOT multi-channel sized.
        goal = effective_min_reserve_onchain()
        if goal <= 0:
            logger.error(
                f"topup_goal_amount LSP-mode goal is {goal} sat — "
                f"effective_min_reserve_onchain() returned non-positive. "
                f"Check MIN_RESERVE_ONCHAIN / LSP_RESERVE_CAP_SAT config "
                f"for store {store_id}"
            )
            return None
        return goal

    # Manual mode: caller opens channels themselves; topup must
    # cover the full channel size plus on-chain fee headroom per
    # channel. This is the historical calculation.
    liquidity_result = await store_needs_liquidity(
        store_id, api, MIN_INBOUND_LIQUIDITY, MIN_CHANNEL_COUNT,
        assume_zero=True,
    )
    if not liquidity_result:
        logger.error(f'Topup_goal_amount reports zero goal, this should not happen: {store_id}')
        return None
    liquidity_needed = liquidity_result.liquidity_needed_sat
    channels_needed = liquidity_result.channels_needed
    needed_channel_liquidity_sizes = common_functions.distribute_sats_over_channels(liquidity_needed, channels_needed)
    channel_sizes = [common_functions.liquidity_to_channel_size(item) for item in needed_channel_liquidity_sizes]
    addl_onchain_reserves_needed = sum(
        [common_functions.onchain_reserves_to_keep_for_channel(item) for item in channel_sizes])
    final_amount = addl_onchain_reserves_needed + sum(channel_sizes)
    if final_amount <= 0:
        logger.error(f'2Topup_goal_amount reports zero goal, this should not happen: {store_id}')
        return None
    return final_amount
async def store_needs_topup(api: BitcartAPI, store_id: str) -> Optional[int]:
    """
    If store needs top-up, returns amount needed (int, sats) for the top-up, None otherwise.
    """
    topup_goal=await topup_goal_amount(api,store_id)
    store_full = await api.get_store_by_id(store_id)
    wallet_full = await api.get_best_ln_wallet_for_store(store_full)
    current_onchain_balance=btc_to_sats(float(wallet_full['balance']))
    final_amount=topup_goal-current_onchain_balance
    if final_amount<=0:
        return None
    return final_amount


def btc_address_from_invoice(invoice: dict)->Optional[str]:
    for payment in invoice["payments"]:
        if payment["lightning"]:
            continue
        return payment["payment_url"]


async def calculate_topups(
    api: BitcartAPI
) -> Optional[Tuple[Union[str,None],Union[str,None]]]:
    """
    Create topup invoices for stores that need it. Returns URLs for own,bb topups
    """
    list_of_stores=await api.get_stores()
    for store in list_of_stores:
        try:
            store_needs_topup_result = await store_needs_topup(api, store["id"])
            if not store_needs_topup_result:
                continue
            fetched_wallet = await api.get_best_ln_wallet_for_store(store)
            amount_remaining = store_needs_topup_result+1000 #(add 1000 sats as a buffer)
            found_own_invoice = await api.get_invoice_by_note(note=TOPUP_NAME,require_unlimited=True)
            if not found_own_invoice:
                found_own_invoice = await api.create_invoice(
                    price_in_btc=0,
                    store_id=store["id"],
                    currency="BTC",
                    notes=TOPUP_NAME,
                    expiration_in_seconds=2628000,
                )
            found_barebits_invoice = await api.get_invoice_by_note(
                note=TOPUP_BAREBITS,
                require_unlimited=True,
            )
            if not found_barebits_invoice:
                found_barebits_invoice = await api.create_invoice(
                    price_in_btc=0,
                    store_id=store["id"],
                    currency="BTC",
                    notes=TOPUP_BAREBITS,
                    expiration_in_seconds=2628000,
                )
            if not found_own_invoice or not found_barebits_invoice:
                logger.error("Error in calculate_topups, invoice missing!")
                return None
            logger.info(
                "Warning: on-chain funds are low, you must top-up your wallet or wait for incoming on-chain payments. To send funds, see invoices"
            )



            own_invoice = btc_address_from_invoice(found_own_invoice)
            bb_invoice = btc_address_from_invoice(found_barebits_invoice)
            logger.info("Payment information:")
            logger.info(
                f"Amount needed: {sats_to_btc(amount_remaining)} BTC / {amount_remaining} sats"
            )
            logger.info(f"If Bitcart Admin is paying: {own_invoice}")
            logger.info(f"If BareBits is paying: {bb_invoice}")
            for notifier in NOTIFICATION_PROVIDERS:
                body = f"""
                Warning: your wallet on store {store['name']} (wallet id {fetched_wallet['id']}) does not have sufficient inbound liquidity. This does not need to be immediately remedied, but we suggest doing so for the best performance from your Bitcart installation.
                
                Insufficient inbound liquidity can result in your customers not being able to make payments with Bitcoin lightning (higher fees, slower payments). On-chain payments will continue to work. Additionally, while liquidity is too low, your Bitcart installation will hold onto on-chain payments until additional liquidity can be created, which delays the time between when you receive payments and when they are delivered to you.

                To remedy this, send {sats_to_btc(amount_remaining)} BTC to this address {own_invoice}

                Once your funds have been received by your Bitcart installation, they will be sent right back to you at {CASHOUT_LIGHTNING_ADDRESS} minus some minor transaction fees for channel creation.

                If you do not deposit more funds for liquidity, on-chain payments from customers will accumulate until sufficient liquidity is re-established. Once that has happened, those on-chain payments will be delivered to {CASHOUT_LIGHTNING_ADDRESS} minus fees.
                """
                subject=f"Warning: low liquidity on your Bitcart store {store['name']}"
                await run_every_x_days(my_func=notifier.notify,days=30,body=body,subject=subject)
            return own_invoice, bb_invoice
        except Exception as e:
            logger.error(f"Error calculating topups: {e} {store}")
    return None


async def safe_to_spend(api:BitcartAPI,store_id:str)->int:
    """
    Given store id, return amount of sats that are safe to spend. Assumes we have already met liquidity goals and a topup is not pending
    This is asking about sats that are safe to spend/move into new channels once all channels are created.
    """
    full_store=await api.get_store_by_id(store_id)
    full_wallet=await api.get_best_ln_wallet_for_store(full_store)
    wallet_id=full_wallet['id']
    wallet_balance=btc_to_sats(float(full_wallet['balance']))
    channel_count_result=await api.get_wallet_ln_channels(wallet_id,active_only=True)
    channel_sat_list=[]
    if channel_count_result:
        for channel in channel_count_result:
            local_balance=int(float(channel['local_balance']))
            remote_balance=int(float(channel['remote_balance']))
            channel_sat_list.append(local_balance+remote_balance)
        # Sum the per-channel reserve requirements so we have a single
        # scalar to compare against MIN_RESERVE_ONCHAIN.
        per_channel_reserves_total = sum(
            common_functions.onchain_reserves_to_keep_for_channel(item)
            for item in channel_sat_list
        )
    else:
        per_channel_reserves_total = 0

    # effective_min_reserve_onchain() bumps MIN_RESERVE_ONCHAIN up to the
    # 6-month LSP price peak (capped at LSP_RESERVE_CAP_SAT) so the wallet
    # always has enough headroom to pay for a new LSP-funded channel.
    floor = effective_min_reserve_onchain()
    max_reserve_requirement_found=max(floor, per_channel_reserves_total)
    sats_remaining=max(0,wallet_balance-max_reserve_requirement_found)
    return sats_remaining


async def has_pending_channel_activity(
    *,
    wallet: Dict[str, Any],
    api: "BitcartAPI",
) -> bool:
    """True if any channel open or cooperative close is in flight on `wallet`.

    Pending FORCE closes are deliberately ignored: their CSV/timeout
    delays can be days or weeks, and the funds going through that path
    are unavailable to cash out either way during that window. Blocking
    cashouts on them would stall normal operations indefinitely.

    Queries the underlying daemon directly so we own the state-vocabulary
    mapping. `classes.BitcartAPI.is_channel_open_pending` only matches the
    Electrum string 'OPENING' and silently returns False for LND wallets
    (Bitcart's btclnd daemon emits 'PENDING_OPEN' instead) — this helper
    is the correct replacement.

    LND:
      - `pending_open_channels`            -> block (~1-6 blocks to confirm)
      - `waiting_close_channels`           -> block (coop close, broadcast pending)
      - `pending_closing_channels`         -> block (coop close, mempool/unconfirmed)
      - `pending_force_closing_channels`   -> IGNORE (CSV delay = days/weeks)

    Electrum:
      - state in {OPENING, FUNDED}                 -> block
      - state == 'CLOSING' + LightningChannel row
        with cooperative_close_requested set       -> block
      - state == 'CLOSING' without that record,
        or state == 'FORCE_CLOSING'                -> IGNORE
    """
    wallet_id = wallet["id"]
    if wallet.get("currency") == "btclnd":
        try:
            resp = await lnd_rpc(api, wallet_id, "PendingChannels", {}, "Lightning") or {}
        except Exception as e:
            logger.warning(
                f"has_pending_channel_activity: PendingChannels failed for "
                f"wallet {wallet_id}: {e}; assuming pending activity (safe)"
            )
            return True
        if resp.get("pending_open_channels"):
            return True
        for key in ("waiting_close_channels", "pending_closing_channels"):
            if resp.get(key):
                return True
        # pending_force_closing_channels intentionally not checked.
        return False

    # Electrum / btc currency
    try:
        chans = await api.get_wallet_ln_channels(wallet_id)
    except Exception as e:
        logger.warning(
            f"has_pending_channel_activity: get_wallet_ln_channels failed "
            f"for wallet {wallet_id}: {e}; assuming pending activity (safe)"
        )
        return True
    for c in chans or []:
        state = c.get("state")
        if state in ("OPENING", "FUNDED"):
            return True
        if state == "CLOSING":
            cp = c.get("channel_point")
            if not cp:
                continue
            row = LightningChannel.get_or_none(LightningChannel.channel_point == cp)
            if row is not None and row.cooperative_close_requested:
                return True
            # No coop-close record -> treat as force close (or unknown,
            # which we also don't want to block on).
    return False


async def decide_onchain_to_ln(api:BitcartAPI):
    '''
    Figure out what on-chain funds are safe to spend making channels, make new channels if appropriate
    '''
    # Channel creation has been delegated to an LSP (e.g. Zeus); the
    # script no longer opens channels itself. See MANUAL_CHANNEL_CREATION_ENABLED.
    if not MANUAL_CHANNEL_CREATION_ENABLED:
        log_decision(
            "manual_channel_creation_gate",
            False,
            "decide_onchain_to_ln: skipped (MANUAL_CHANNEL_CREATION_ENABLED=False; "
            "channel creation is delegated to an external LSP)",
        )
        return
    # Don't move funds to LN if this tick's rail is on-chain — opening a
    # channel now would lock funds out of the cashout that runs later in
    # the same tick. should_prefer_onchain_cashout() also catches the
    # recency-fallback case where the *global* PREFER_CASHOUT_ONCHAIN is
    # False but LN has been failing for long enough to trigger fallback.
    if should_prefer_onchain_cashout():
        return
    store_list=await api.get_stores()
    for store in store_list:
        store_id=store['id']
        best_wallet=await api.get_best_ln_wallet_for_store(store)
        # Electrum guard, same rationale as the one in
        # move_onchain_to_ln: the manual rebalancing path expects to
        # pick channel partners from the LND-gossip-derived candidate
        # DB. Skip on Electrum wallets and let the operator manage
        # rebalancing manually.
        if best_wallet.get("currency") != "btclnd":
            log_decision(
                ("decide_onchain_to_ln_skipped_non_lnd", best_wallet["id"]),
                True,
                "decide_onchain_to_ln: store %s best wallet %s is "
                "currency=%s, not btclnd; manual rebalancing is LND-only",
                store_id, best_wallet["id"], best_wallet.get("currency"),
            )
            continue
        topup_result = await store_needs_topup(api,store_id)
        # Don't move onchain to ln if store needs topup
        if topup_result:
            logger.debug(f'Not moving addl onchain funds to LN because store needs topup {store_id}')
            continue
        # or needs liquidity
        liquidity_check_result=await store_needs_liquidity(store_id,api,MIN_INBOUND_LIQUIDITY,MIN_CHANNEL_COUNT)
        if liquidity_check_result:
            logger.debug(f'Not moving addl onchain funds to LN because store needs liquidity {store_id}')
            continue
        # or has recently closed channels
        close_pending_response = await get_most_recent_channel_close(api,best_wallet['id'])
        if close_pending_response:
            hours_ago = datetime.datetime.now() - datetime.timedelta(hours=12)
            if close_pending_response > hours_ago:
                logger.info(
                    f"Not moving addl onchain funds to LN bc pending channel closes store {store_id}"
                )
                continue
        # find out how much is safe to spend
        safe_to_spend_amount=await safe_to_spend(api,store_id)
        if safe_to_spend_amount<MIN_CHANNEL_SIZE_IN_SATS:
            logger.debug(f'Not moving addl onchain funds to LN because safe_to_spend_amount ({safe_to_spend_amount})<MIN_CHANNEL_SIZE_IN_SATS ({MIN_CHANNEL_SIZE_IN_SATS}) store: {store_id}')
            continue
        max_channel_size=common_functions.sats_to_max_channel_size(safe_to_spend_amount)
        if max_channel_size<MIN_CHANNEL_SIZE_IN_SATS:
            logger.warning(f'in decide_onchain_to_ln, max_channel_size<MIN_CHANNEL_SIZE_IN_SATS')
            continue
        best_wallet=await api.get_best_ln_wallet_for_store(store)
        logger.debug(f'Moving leftover onchain funds to ln... {max_channel_size} sats {sats_to_btc(max_channel_size)} BTC, store {store_id}')
        onchain_move_result=await move_onchain_to_ln(wallet_id=best_wallet['id'],amount_in_btc=sats_to_btc(max_channel_size),api=api)

async def setup_notifiers()->List[NotificationProvider]:
    return_list=[]
    if SMTP_USERNAME and SMTP_TO_EMAIL and SMTP_PASSWORD and SMTP_FROM_EMAIL and SMTP_FROM_NAME and SMTP_PORT and SMTP_SERVER:
        my_notifier=notifications.EmailNotificationProvider(name='mymail',from_email=SMTP_FROM_EMAIL,from_name=SMTP_FROM_NAME,password=SMTP_PASSWORD,username=SMTP_USERNAME,smtp_server=SMTP_SERVER,smtp_port=SMTP_PORT,tls_enabled=SMTP_TLS,ssl_enabled=SMTP_SSL,to_email=SMTP_TO_EMAIL)
        try:
            await my_notifier.test_connection()
        except Exception as e:
            logger.error(f'Error connecting to SMTP server: {e}')
        else:
            return_list.append(my_notifier)
    else:
        logger.warning('No SMTP notification provider config found')
    return return_list


# ---------------------------------------------------------------------------
# Submarine swaps (LN -> on-chain). Production swap initiation is gated by
# `LOOP_OUT_ENABLED`; without that flag, only the detection function runs.
# ---------------------------------------------------------------------------

from swap_providers import (
    LoopdManager, LoopProvider, SwapDirection, SwapProvider, SwapQuote,
    SwapResult,
)
from node_database import SwapPriceQuote
from loop_proto import client_pb2 as _loop_pb2


_LOOPD_MANAGER: Optional[LoopdManager] = None
SWAP_PROVIDERS: List[SwapProvider] = []


def _swap_provider_registry() -> List[SwapProvider]:
    """Lazy-init the swap-provider registry. Currently only Loop is wired
    up; additional providers (Boltz, etc.) would be appended here.

    Network + server config comes from LOOPD_NETWORK / LOOPD_SERVER_HOST
    / LOOPD_SERVER_NOTLS (see config.py). Operators on testnet/signet
    must override LOOPD_NETWORK; operators on regtest/simnet must
    additionally set LOOPD_SERVER_HOST (loopd has no built-in regtest
    server). The LoopdManager passes these through to per-wallet
    LoopdInstance subprocesses.
    """
    global _LOOPD_MANAGER, SWAP_PROVIDERS
    if SWAP_PROVIDERS:
        return SWAP_PROVIDERS
    if _LOOPD_MANAGER is None:
        _LOOPD_MANAGER = LoopdManager(
            network=LOOPD_NETWORK,
            # Empty string in config → None at the manager so loopd
            # falls through to its own per-network default.
            server_host=(LOOPD_SERVER_HOST or None),
            server_notls=LOOPD_SERVER_NOTLS,
        )
    SWAP_PROVIDERS = [LoopProvider(_LOOPD_MANAGER)]
    return SWAP_PROVIDERS


async def pick_best_swap_provider_for_out(
    amount_sat: int,
    *,
    wallet: Dict[str, Any],
    api: "BitcartAPI",
) -> Optional[Tuple[SwapProvider, SwapQuote]]:
    """Quote every registered provider for a LN->on-chain swap of `amount_sat`.

    LND-only: registered swap providers (loopd) talk to LND directly.
    Currently only called from initiate_lightning_to_onchain_swap which
    has its own currency guard, but defending here too against future
    direct callers.

    Side effects: every quote is persisted to the SwapPriceQuote table
    (regardless of which quote we end up taking).

    Returns the cheapest provider whose quote passes BOTH MAX_SWAP_FLAT
    and MAX_SWAP_PERCENT caps, paired with that provider's quote.
    Returns None if no provider responded, every quote exceeded a cap,
    or the wallet isn't LND-backed.
    """
    if wallet.get("currency") != "btclnd":
        log_decision(
            ("pick_swap_skip_non_lnd", wallet.get("id")), True,
            "pick_best_swap_provider_for_out: wallet %s is currency=%s, "
            "not btclnd — swaps are LND-only, skipping",
            wallet.get("id"), wallet.get("currency"),
        )
        return None
    providers = _swap_provider_registry()
    survivors: List[Tuple[SwapProvider, SwapQuote]] = []
    for provider in providers:
        try:
            quote = await provider.quote_out(amount_sat, wallet=wallet, api=api)
        except TypeError:
            quote = await provider.quote_out(amount_sat)
        except Exception as e:
            logger.warning(f"swap provider {provider.name} quote_out raised: {e}")
            continue
        if quote is None:
            continue
        try:
            SwapPriceQuote.create(
                provider=quote.provider,
                direction=quote.direction.value,
                amount_sat=int(quote.amount_sat),
                total_fee_sat=int(quote.total_fee_sat),
                fee_percent=float(quote.fee_percent),
            )
        except Exception as e:
            logger.warning(f"could not persist SwapPriceQuote: {e}")
        if quote.total_fee_sat > MAX_SWAP_FLAT:
            logger.info(
                f"swap quote rejected ({quote.provider}): "
                f"total_fee_sat={quote.total_fee_sat} > MAX_SWAP_FLAT={MAX_SWAP_FLAT}"
            )
            continue
        if quote.fee_percent > MAX_SWAP_PERCENT:
            logger.info(
                f"swap quote rejected ({quote.provider}): "
                f"fee_percent={quote.fee_percent:.4f} > MAX_SWAP_PERCENT={MAX_SWAP_PERCENT}"
            )
            continue
        survivors.append((provider, quote))
    if not survivors:
        return None
    survivors.sort(key=lambda t: t[1].total_fee_sat)
    return survivors[0]


async def initiate_lightning_to_onchain_swap(
    *,
    wallet: Dict[str, Any],
    api: "BitcartAPI",
    amount_sat: int,
    dest_addr: str,
) -> Optional[SwapResult]:
    """Initiate a reverse submarine swap: spend LN balance from `wallet`,
    receive `amount_sat` on-chain at `dest_addr`. `dest_addr` may be an
    address NOT controlled by the wallet (loop supports arbitrary
    destinations via LoopOutRequest.dest).

    Returns the SwapResult once the swap is accepted by the server, or None
    if no acceptable quote could be obtained or the server rejected the
    swap. Caller is responsible for monitoring completion.
    """
    if wallet.get("currency") != "btclnd":
        logger.warning(
            f"initiate_lightning_to_onchain_swap: wallet {wallet.get('id')} "
            f"is currency={wallet.get('currency')!r}, swaps are LND-only"
        )
        return None
    picked = await pick_best_swap_provider_for_out(amount_sat, wallet=wallet, api=api)
    if picked is None:
        logger.info(
            f"no swap provider produced an acceptable quote for "
            f"{amount_sat} sats (wallet {wallet.get('id')})"
        )
        return None
    provider, quote = picked
    logger.info(
        f"initiating LN->onchain swap via {provider.name}: "
        f"{amount_sat} sats -> {dest_addr}, est. total fee {quote.total_fee_sat} sat"
    )
    try:
        result = await provider.initiate_out(wallet, api, amount_sat, dest_addr)
    except Exception as e:
        logger.error(
            f"swap provider {provider.name} initiate_out raised: {e}",
            exc_info=True,
        )
        return None
    if result is None:
        return None
    logger.info(
        f"swap {result.swap_id[:16]}... initiated; htlc_address={result.htlc_address}"
    )
    return result


async def find_loop_out_candidates(api: "BitcartAPI") -> List[Dict[str, Any]]:
    """Walk every Bitcart wallet with currency='btclnd' and flag channels
    whose local_balance exceeds LOOP_OUT_TRIGGER_LOCAL_BALANCE_SAT.

    Returns a list of candidate dicts with keys: wallet_id, channel_point,
    remote_pubkey, local_balance_sat, remote_balance_sat.

    Pure read-only. Does NOT start any swap. Production caller currently
    just logs these (LOOP_OUT_ENABLED gates the actual initiation).
    """
    candidates: List[Dict[str, Any]] = []
    try:
        all_wallets = await api.get_wallets(limit=200)
    except Exception as e:
        logger.warning(f"find_loop_out_candidates: get_wallets failed: {e}")
        return candidates
    for w in (all_wallets or []):
        if w.get("currency") != "btclnd":
            continue
        try:
            raw = await lnd_rpc(api, w["id"], "ListChannels", {}, "Lightning") or {}
        except Exception as e:
            logger.warning(
                f"find_loop_out_candidates: ListChannels failed for "
                f"wallet {w['id']}: {e}"
            )
            continue
        for c in raw.get("channels") or []:
            if not c.get("active"):
                continue
            local = int(c.get("local_balance") or 0)
            if local <= LOOP_OUT_TRIGGER_LOCAL_BALANCE_SAT:
                continue
            candidates.append({
                "wallet_id": w["id"],
                "channel_point": c.get("channel_point", ""),
                "remote_pubkey": (c.get("remote_pubkey") or "").lower(),
                "local_balance_sat": local,
                "remote_balance_sat": int(c.get("remote_balance") or 0),
            })
    return candidates


async def cleanup_old_swap_quotes() -> int:
    """Delete SwapPriceQuote rows older than 6 months. Returns the number of
    rows deleted. Called via run_every_x_days from main()."""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=183)
    try:
        n = SwapPriceQuote.delete().where(
            SwapPriceQuote.fetched_at < cutoff
        ).execute()
        if n:
            logger.info(
                f"cleanup_old_swap_quotes: removed {n} rows older than 6 months"
            )
        return n
    except Exception as e:
        logger.warning(f"cleanup_old_swap_quotes failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# LSP-funded inbound liquidity orchestration. Quotes both registered LSP
# providers, applies the Zeus-preference rule, persists the quotes, and
# pays the chosen order on-chain. Only fires when liquidity_check decides
# a wallet needs more inbound; each create_order registers a real
# (abandonable) order on the LSP, so we deliberately don't poll on a
# schedule. LND-only — Electrum wallets short-circuit.
# ---------------------------------------------------------------------------

import lsp_providers as _lsp_providers
from node_database import LspPriceQuote, LspChannelOrder


_NON_TERMINAL_LSP_STATES = ("ORDERED", "PAID", "OPENED")


async def lsp_network_for_wallet(
    wallet: Dict[str, Any], api: "BitcartAPI",
) -> Optional[str]:
    """Returns a network string (e.g. 'mainnet', 'testnet', 'signet') that
    our LSP providers understand for this wallet, or None if either the
    wallet isn't LND-backed or its network has no LSP support.

    Normalization rules (all map LND's vocabulary → our internal one):
      - 'testnet3' → 'testnet'
      - 'testnet4' → 'testnet'  (with a separate decision log noting
        that Zeus's testnet endpoint is testnet3-specific, so the
        normalization is best-effort; an actual testnet4 chain would
        produce a chain-hash mismatch at LSP-side. We normalize anyway
        so operators reading "testnet" + Zeus's testnet decision log
        can connect the dots.)
      - 'regtest' → None (no public LSP runs on regtest).
      - 'signet' → 'signet' BUT operators should know that both Zeus
        and Megalithic point their 'signet' at Mutinynet (a fast-block
        signet variant). A wallet on real signet would get Mutinynet
        responses — see provider docstrings for the caveat.
      - anything else → None (with a warning).
    """
    if wallet.get("currency") != "btclnd":
        return None
    info = await api.get_lnd_info(wallet["id"])
    if not info:
        logger.warning(
            f"lsp_network_for_wallet: get_lnd_info returned nothing for "
            f"wallet {wallet['id']}; cannot determine network"
        )
        return None
    raw = (info.get("network") or "").lower()
    if raw in ("testnet3", "testnet4"):
        # Tell the operator EXACTLY which testnet flavor LND reports.
        # Zeus's testnet-lsps1.lnolymp.us serves testnet3 only — a
        # testnet4 LND will hit chain-hash mismatches at the LSP side
        # despite our internal label being just "testnet".
        if raw == "testnet4":
            log_decision(
                ("lsp_testnet_flavor", wallet["id"]),
                "testnet4",
                "Wallet %s is on testnet4. The script will request LSP "
                "quotes using the 'testnet' label, but Zeus's public "
                "testnet endpoint (testnet-lsps1.lnolymp.us) is "
                "testnet3-specific; Megalithic does not serve any "
                "testnet. Expect chain-hash mismatches or quote "
                "failures from any LSP that's actually serving "
                "testnet3 chains.",
                wallet["id"],
                level=logging.WARNING,
            )
        raw = "testnet"
    if raw == "regtest":
        log_decision(
            ("lsp_network_unsupported_for_wallet", wallet["id"]),
            "regtest",
            "Wallet %s is on regtest; no public LSP serves regtest. "
            "Skipping LSP request.",
            wallet["id"],
        )
        return None
    if raw not in ("mainnet", "testnet", "signet"):
        logger.warning(
            f"lsp_network_for_wallet: unknown LND network {raw!r} for "
            f"wallet {wallet['id']}; cannot serve"
        )
        return None
    return raw


def _wallet_has_open_lsp_order(wallet_id: str) -> bool:
    """True if the wallet has at least one LspChannelOrder in a
    non-terminal state. Enforces the 1-LSP-channel-per-wallet rule."""
    return LspChannelOrder.select().where(
        LspChannelOrder.wallet_id == wallet_id,
        LspChannelOrder.state.in_(list(_NON_TERMINAL_LSP_STATES)),
    ).exists()


def _can_quote_lsp_for_wallet(provider_name: str, wallet_id: str) -> bool:
    """Per-LSP-per-wallet daily throttle. Each LspPriceQuote.fetched_at is
    the timestamp of a successful create_order; if the latest is within
    LSP_QUOTE_THROTTLE_HOURS we skip this provider for now."""
    cutoff = datetime.datetime.now() - datetime.timedelta(
        hours=LSP_QUOTE_THROTTLE_HOURS,
    )
    return not LspPriceQuote.select().where(
        LspPriceQuote.provider == provider_name,
        LspPriceQuote.wallet_id == wallet_id,
        LspPriceQuote.fetched_at > cutoff,
    ).exists()


def max_lsp_quote_in_last_6_months_sat() -> int:
    """Largest fee_total_sat across ANY LSP for ANY wallet in the last
    6 months — restricted to quotes that would NOT be rejected by the
    current LSP_MAX_FEE_PERCENT cap. Used to compute the dynamic
    on-chain reserve floor.

    Filtering by the cap keeps the reserve floor honest: if our policy
    is "never pay above 1% of channel size", then the reserve only
    needs to cover quotes that survive that filter. Otherwise a freak
    high-quote from a misconfigured LSP would lock up wallet capital
    we'd never actually spend.
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(days=183)
    max_fee = 0
    for q in LspPriceQuote.select().where(LspPriceQuote.fetched_at > cutoff):
        if q.lsp_balance_sat <= 0:
            continue
        if q.fee_total_sat / q.lsp_balance_sat > LSP_MAX_FEE_PERCENT:
            continue
        if q.fee_total_sat > max_fee:
            max_fee = int(q.fee_total_sat)
    return max_fee


def effective_min_reserve_onchain() -> int:
    """The on-chain reserve floor used by safe_to_spend() and related
    callers. max(static MIN_RESERVE_ONCHAIN, 6-month LSP price peak),
    capped at LSP_RESERVE_CAP_SAT."""
    return min(LSP_RESERVE_CAP_SAT, max(
        int(MIN_RESERVE_ONCHAIN),
        max_lsp_quote_in_last_6_months_sat(),
    ))


def _pick_with_zeus_preference(
    quotes: Dict[str, Any],
) -> Optional[Any]:
    """Apply the ±10% Zeus-preference tiebreaker.

    `quotes` is a dict mapping provider_name -> (provider, LspQuote).
    Returns the chosen (provider, quote) tuple, or None if empty.

    Rule: if both Zeus and Megalithic returned quotes, and the pricier
    quote is within 110% of the cheaper, Zeus wins. Otherwise pick the
    cheaper. If only one provider quoted, return that one.

    Logs the comparison reasoning to decisions.log so the operator can
    see WHY a given provider was picked (or why nothing was).
    """
    zeus_pair = quotes.get("zeus")
    meg_pair = quotes.get("megalithic")
    if zeus_pair and meg_pair:
        zeus_fee = zeus_pair[1].fee_total_sat
        meg_fee = meg_pair[1].fee_total_sat
        cheaper_fee = min(zeus_fee, meg_fee)
        pricier_fee = max(zeus_fee, meg_fee)
        # Within ±10% (pricier <= cheaper * 1.10) -> Zeus wins.
        # Edge case: cheaper_fee == 0 -> both effectively free, prefer Zeus.
        if cheaper_fee <= 0 or pricier_fee <= cheaper_fee * 1.10:
            log_event(
                "LSP pay decision: chose zeus (fee=%d sat) over megalithic "
                "(fee=%d sat) — within ±10%% tiebreaker",
                zeus_fee, meg_fee,
            )
            return zeus_pair
        if zeus_fee < meg_fee:
            log_event(
                "LSP pay decision: chose zeus (fee=%d sat) over megalithic "
                "(fee=%d sat) — zeus is strictly cheaper outside ±10%% band",
                zeus_fee, meg_fee,
            )
            return zeus_pair
        log_event(
            "LSP pay decision: chose megalithic (fee=%d sat) over zeus "
            "(fee=%d sat) — megalithic is >10%% cheaper",
            meg_fee, zeus_fee,
        )
        return meg_pair
    if zeus_pair and not meg_pair:
        log_event(
            "LSP pay decision: chose zeus (fee=%d sat) — only provider available",
            zeus_pair[1].fee_total_sat,
        )
        return zeus_pair
    if meg_pair and not zeus_pair:
        log_event(
            "LSP pay decision: chose megalithic (fee=%d sat) — only provider available",
            meg_pair[1].fee_total_sat,
        )
        return meg_pair
    fallback = next(iter(quotes.values()), None)
    if fallback is not None:
        log_event(
            "LSP pay decision: chose %s (fee=%d sat) — only provider available",
            fallback[1].provider, fallback[1].fee_total_sat,
        )
    return fallback


async def pick_best_lsp_for_inbound(
    *,
    wallet: Dict[str, Any],
    api: "BitcartAPI",
    network: str,
) -> Optional[Any]:
    """Quote every LSP that supports `network` and isn't throttled,
    persist each quote, then pick via _pick_with_zeus_preference.

    LND-only: the LSP path uses LSPS1 over LND-specific peer connections
    + pays the LSP invoice from the LND wallet's on-chain side.
    Currently only called from request_inbound_liquidity_from_lsp which
    has its own currency guard, but defending here too against future
    direct callers.

    Returns (provider, LspQuote) or None if no provider returned a quote
    we'd consider taking, or the wallet isn't LND-backed.
    """
    if wallet.get("currency") != "btclnd":
        log_decision(
            ("pick_lsp_skip_non_lnd", wallet.get("id")), True,
            "pick_best_lsp_for_inbound: wallet %s is currency=%s, not "
            "btclnd — LSPs are LND-only, skipping",
            wallet.get("id"), wallet.get("currency"),
        )
        return None
    providers = _lsp_providers.get_lsp_providers()
    wallet_id = wallet["id"]
    try:
        pubkey_uri = await api.get_wallet_ln_node_id(wallet_id) or ""
    except Exception as e:
        logger.warning(
            f"pick_best_lsp_for_inbound: get_wallet_ln_node_id failed for "
            f"wallet {wallet_id}: {e}"
        )
        return None

    # Track WHY each provider was skipped so we can emit a single
    # actionable summary when no quotes come back. Without this, the
    # operator sees "no LSP returned a quote" and has to grep the
    # decision stream to figure out which path filtered out which LSP.
    skip_reasons: Dict[str, str] = {}
    quotes: Dict[str, Any] = {}
    for provider in providers:
        if network not in provider.supported_networks():
            log_decision(
                ("lsp_network_unsupported", provider.name, network),
                True,
                "LSP %s does not support network=%s for wallet %s",
                provider.name, network, wallet_id,
            )
            skip_reasons[provider.name] = f"unsupported_network={network}"
            continue
        if not _can_quote_lsp_for_wallet(provider.name, wallet_id):
            log_decision(
                ("lsp_throttled", provider.name, wallet_id),
                True,
                "Skipping LSP %s for wallet %s: throttled "
                "(<%dh since last quote)",
                provider.name, wallet_id, LSP_QUOTE_THROTTLE_HOURS,
            )
            skip_reasons[provider.name] = "throttled"
            continue
        try:
            quote = await provider.create_order(
                network=network,
                public_key=pubkey_uri,
                lsp_balance_sat=int(LSP_CHANNEL_SIZE_SAT),
                channel_expiry_blocks=int(LSP_CHANNEL_EXPIRY_BLOCKS),
            )
        except Exception as e:
            logger.warning(
                f"LSP {provider.name} create_order failed for wallet "
                f"{wallet_id}: {e}"
            )
            skip_reasons[provider.name] = f"create_order_error: {e}"
            continue
        # Persist EVERY quote we receive, even rejected ones — provides
        # an audit trail of what an LSP would have charged. The 6-month
        # reserve calc filters cap-exceeders out at read time.
        try:
            LspPriceQuote.create(
                provider=quote.provider,
                network=quote.network,
                wallet_id=wallet_id,
                order_id=quote.order_id,
                lsp_balance_sat=int(quote.lsp_balance_sat),
                fee_total_sat=int(quote.fee_total_sat),
                order_total_sat=int(quote.order_total_sat),
                channel_expiry_blocks=int(quote.channel_expiry_blocks),
            )
        except Exception as e:
            logger.warning(f"could not persist LspPriceQuote: {e}")

        # Fee-percent cap: reject quotes whose fee is more than
        # LSP_MAX_FEE_PERCENT of the channel size. Catches an LSP
        # quoting a 1% spec channel at 50% of channel size.
        if quote.lsp_balance_sat > 0:
            quote_pct = quote.fee_total_sat / quote.lsp_balance_sat
            if quote_pct > LSP_MAX_FEE_PERCENT:
                log_event(
                    "LSP %s quote rejected for wallet %s: fee %d sat / "
                    "channel %d sat = %.4f > cap %.4f",
                    provider.name, wallet_id, quote.fee_total_sat,
                    quote.lsp_balance_sat, quote_pct, LSP_MAX_FEE_PERCENT,
                )
                skip_reasons[provider.name] = (
                    f"fee_above_cap "
                    f"({quote.fee_total_sat}/{quote.lsp_balance_sat} "
                    f"= {quote_pct:.4f} > {LSP_MAX_FEE_PERCENT:.4f})"
                )
                continue
        quotes[provider.name] = (provider, quote)

    if not quotes:
        # Distinguish "no LSPs serve this network at all" from "some
        # were throttled / errored" — different operator actions. The
        # network-only case is especially common on testnet (Megalithic
        # doesn't serve it) and on testnet4 (Zeus's endpoint is
        # testnet3-only).
        all_skipped_for_network = (
            len(skip_reasons) > 0
            and all(
                r.startswith("unsupported_network=") for r in skip_reasons.values()
            )
        )
        if all_skipped_for_network:
            log_event(
                "LSP request for wallet %s returned no quote: NO LSPs "
                "support network=%s. Configured providers and their "
                "supported networks: %s",
                wallet_id, network,
                {p.name: p.supported_networks() for p in providers},
            )
        else:
            # Mixed reasons or all-errored — surface the per-provider
            # breakdown so the operator can act on the most common one.
            log_event(
                "LSP request for wallet %s on network=%s returned no "
                "quote. Per-provider reasons: %s",
                wallet_id, network, skip_reasons,
            )
        return None
    return _pick_with_zeus_preference(quotes)


async def request_inbound_liquidity_from_lsp(
    *,
    wallet: Dict[str, Any],
    api: "BitcartAPI",
) -> Optional[LspChannelOrder]:
    """Top-level entry. Called from liquidity_check when the per-wallet
    inbound-need check fires AND MANUAL_CHANNEL_CREATION_ENABLED=False.

    Pipeline:
      1. LND-only short-circuit.
      2. Skip if wallet's on-chain balance < LSP_MIN_ONCHAIN_FOR_QUOTE_SAT.
      3. Skip if the wallet already has a non-terminal LspChannelOrder
         (one-LSP-channel-per-wallet invariant).
      4. Determine the LSP-side network name; skip if no provider supports it.
      5. Quote both providers (throttled to 1×/day per provider),
         persist each quote to LspPriceQuote, pick via Zeus-preference.
      6. Persist an LspChannelOrder in state=ORDERED.
      7. Pay the order on-chain via electrum_pay_onchain. On success,
         flip state to PAID; on failure, FAILED.

    Returns the LspChannelOrder on a successful PAID transition, else None.
    """
    if wallet.get("currency") != "btclnd":
        log_decision(
            ("lsp_request_skip_non_lnd", wallet.get("id")),
            True,
            "Skipping LSP request: wallet %s is not LND-backed "
            "(LSPs are LND-only in this script)", wallet.get("id"),
        )
        return None
    wallet_id = wallet["id"]

    wallet_balance_sat = btc_to_sats(float(wallet.get("balance") or 0))
    if wallet_balance_sat < LSP_MIN_ONCHAIN_FOR_QUOTE_SAT:
        log_decision(
            ("lsp_request_below_min_onchain", wallet_id),
            True,
            "Skipping LSP request: wallet %s on-chain balance %d sat < "
            "LSP_MIN_ONCHAIN_FOR_QUOTE_SAT=%d",
            wallet_id, wallet_balance_sat, LSP_MIN_ONCHAIN_FOR_QUOTE_SAT,
        )
        return None
    log_decision(("lsp_request_below_min_onchain", wallet_id), False, "")

    if _wallet_has_open_lsp_order(wallet_id):
        log_decision(
            ("lsp_request_already_have", wallet_id),
            True,
            "Skipping LSP request: wallet %s already has an "
            "in-flight or open LSP-funded channel order",
            wallet_id,
        )
        return None
    log_decision(("lsp_request_already_have", wallet_id), False, "")

    network = await lsp_network_for_wallet(wallet, api)
    if network is None:
        return None

    picked = await pick_best_lsp_for_inbound(
        wallet=wallet, api=api, network=network,
    )
    if picked is None:
        log_event(
            "LSP request for wallet %s: no provider returned a quote",
            wallet_id,
        )
        return None
    provider, quote = picked

    order = LspChannelOrder.create(
        provider=provider.name,
        network=network,
        wallet_id=wallet_id,
        order_id=quote.order_id,
        lsp_peer_pubkey=quote.lsp_peer_pubkey,
        lsp_balance_sat=int(quote.lsp_balance_sat),
        fee_total_sat=int(quote.fee_total_sat),
        state="ORDERED",
    )
    log_event(
        "LSP order created: provider=%s order_id=%s fee=%d sat "
        "lsp_balance=%d sat; paying on-chain to %s",
        provider.name, quote.order_id, quote.fee_total_sat,
        quote.lsp_balance_sat, quote.onchain_address,
    )

    label = f"lsp_channel_order:{quote.order_id}"
    try:
        payment_ok = await electrum_pay_onchain(
            quote.onchain_address,
            sats_to_btc(int(quote.order_total_sat)),
            label=label,
            wallet=wallet, api=api,
        )
    except Exception as e:
        logger.error(
            f"LSP order {quote.order_id}: on-chain payment raised: {e}"
        )
        payment_ok = False

    if not payment_ok:
        order.state = "FAILED"
        order.save()
        log_event(
            "LSP order %s: on-chain payment FAILED", quote.order_id,
        )
        return None

    order.state = "PAID"
    order.save()
    log_event(
        "LSP order %s paid (%d sat); LSP will open channel from %s",
        quote.order_id, quote.order_total_sat, quote.lsp_peer_uri,
    )
    return order


# gRPC / LND startup phrases that indicate the daemon isn't ready to
# accept Lightning RPCs yet (wallet locked, server still starting,
# neutrino still loading). Treated as "retry next tick", not as a
# real failure — see ensure_lnd_wallets_peered_with_lsps.
_LND_NOT_READY_PATTERNS = (
    "wallet not ready",
    "wallet is not ready",
    "wallet not unlocked",
    "wallet is locked",
    "server is still in the process of starting",
    "server is not yet active",
    "rpc not ready",
    "rpc service not active",
    "is currently in the process of starting",
    "unimplemented",       # gRPC: service registered but not yet active
    "unavailable",         # gRPC: transient connection unavailable
    "connection refused",  # daemon not yet listening
)


def _looks_like_lnd_not_ready(error_message_lower: str) -> bool:
    """True if the error string suggests LND is mid-startup rather than
    in a permanent failure state. Caller treats matches as transient."""
    return any(p in error_message_lower for p in _LND_NOT_READY_PATTERNS)


async def refresh_lnd_node_database(api: "BitcartAPI") -> None:
    """Daily refresh of the LightningNode candidate DB from LND gossip.

    LN gossip is the same on every well-synced LND node — there's no
    routing/topology fact that one of our LND wallets knows and
    another doesn't — so we just pick the first LND wallet to
    minimize redundant graph downloads. If you operate dozens of LND
    wallets across many stores, none of them gets a meaningfully
    "better" graph from being asked.

    Gated on MANUAL_CHANNEL_CREATION_ENABLED. When False (the default
    LSP mode), channel creation is delegated entirely to LSPs and we
    never read from the candidate DB; pulling and persisting tens of
    MB of gossip we won't use is pure waste. Early-return with a
    `log_decision` transition so the operator sees "skipping daily
    LND graph pull (LSP mode)" exactly once when the mode flips.

    No-op for non-LND deployments. Errors are logged but never raised
    — graph refresh is best-effort; the script keeps using the
    most-recently-known LightningNode rows if a refresh fails.
    """
    if not MANUAL_CHANNEL_CREATION_ENABLED:
        log_decision(
            ("lnd_graph_pull_gated", "global"), "skipped_lsp_mode",
            "refresh_lnd_node_database: skipped — "
            "MANUAL_CHANNEL_CREATION_ENABLED=False, channel creation "
            "is delegated to LSPs; no need to maintain a candidate "
            "node list. Flip to True if you want to open channels "
            "yourself and re-enable the daily gossip pull.",
        )
        return
    log_decision(
        ("lnd_graph_pull_gated", "global"), "running_manual_mode",
        "refresh_lnd_node_database: enabled — "
        "MANUAL_CHANNEL_CREATION_ENABLED=True; pulling LND gossip "
        "to refresh candidate node list.",
    )
    try:
        wallets = await api.get_wallets(limit=200)
    except Exception as e:
        logger.warning(
            f"refresh_lnd_node_database: get_wallets failed: {e}"
        )
        return
    lnd_wallets = [w for w in wallets if w.get("currency") == "btclnd"]
    if not lnd_wallets:
        logger.debug(
            "refresh_lnd_node_database: no LND wallets — skipping. "
            "Gossip refresh is LND-only; Electrum wallets don't have "
            "a channel graph to query."
        )
        return
    chosen = lnd_wallets[0]
    wallet_id = chosen["id"]
    try:
        conn = await _get_lnd_connection(api, wallet_id)
    except Exception as e:
        logger.warning(
            f"refresh_lnd_node_database: could not build gRPC stub "
            f"for wallet {wallet_id}: {e}"
        )
        return
    stub = conn["stubs"]["Lightning"]
    log_event(
        "refresh_lnd_node_database: pulling graph from wallet %s "
        "(of %d LND wallet(s))", wallet_id, len(lnd_wallets),
    )
    # Record that we observed this wallet's LND. The first call
    # stamps the timestamp; subsequent calls are no-ops. The graph
    # pull below reads `_lnd_uptime_seconds(wallet_id)` to enforce
    # the minimum-uptime gate.
    _record_lnd_first_seen(wallet_id)
    try:
        stats = await lnd_graph_pull.pull_and_upsert(
            stub,
            min_capacity_sat=NODE_CRITERIA_MINIMUM_CAPACITY,
            min_channel_count=NODE_CRITERIA_MINIMUM_CHANNELCOUNT,
            min_age_days=NODE_CRITERIA_MINIMUM_AGE,
            gossip_min_node_count=GOSSIP_MIN_NODE_COUNT,
            gossip_min_uptime_seconds=GOSSIP_MIN_UPTIME_SECONDS,
            lnd_uptime_seconds=_lnd_uptime_seconds(wallet_id),
        )
        if stats.get("skipped"):
            # Don't write last-good-pull state. Operators will see the
            # decision log; pick_best_channel_partners reads the stored
            # timestamp from the LAST successful pull to decide
            # staleness.
            log_decision(
                ("gossip_pull_skipped",), stats.get("skip_reason", "unknown"),
                "refresh_lnd_node_database: gossip pull SKIPPED — %s; "
                "candidate DB unchanged. Operator: re-check the daemon "
                "after a sync window; selection-time gate will block "
                "channel opens if no good pull happens within "
                "GOSSIP_MAX_STALENESS_DAYS days.",
                stats.get("skip_reason"),
                level=logging.WARNING,
            )
        else:
            # Successful pull → persist the last-good timestamp and
            # the node count for the next staleness check.
            _set_last_gossip_pull_success(
                node_count=int(stats.get("total_graph_nodes") or 0)
            )
            log_event(
                "refresh_lnd_node_database: completed via wallet %s — %s",
                wallet_id, stats,
            )
    except Exception as e:
        logger.error(
            f"refresh_lnd_node_database: pull_and_upsert failed: {e} "
            f"{traceback.print_exc()}"
        )


# Self-tracked LND uptime. The LND proto checked in to this repo
# predates GetInfoResponse.uptime, so we approximate uptime by
# recording the first time we observed a GetInfo response from each
# wallet's LND. Lives in memory; resets when the script restarts.
#
# The reset semantic is intentional: when liquidityhelper restarts,
# we can't tell whether LND has been running for an hour or just
# started — so we treat both as "fresh" and wait the configured
# GOSSIP_MIN_UPTIME_SECONDS before trusting the gossip pull. That's
# a conservative choice (a wallet whose LND has been online for a
# week still has to wait 15s after our restart), but the alternative
# (assuming long uptime when we can't verify) is what we're trying
# to prevent in the first place.
_lnd_first_seen_at: Dict[str, datetime.datetime] = {}


def _record_lnd_first_seen(wallet_id: str) -> None:
    """Set the first-seen timestamp for `wallet_id` if not already
    recorded. Idempotent — subsequent calls are no-ops, so the
    earliest observation wins."""
    if wallet_id and wallet_id not in _lnd_first_seen_at:
        _lnd_first_seen_at[wallet_id] = datetime.datetime.now()


def _lnd_uptime_seconds(wallet_id: str) -> int:
    """Seconds since we first observed a GetInfo from this wallet's
    LND. Returns 0 if never observed — which the readiness gate
    treats as "below the minimum" and thus blocks the pull until
    we've at least made one observation."""
    first_seen = _lnd_first_seen_at.get(wallet_id)
    if first_seen is None:
        return 0
    return int((datetime.datetime.now() - first_seen).total_seconds())


# Storage keys used by the gossip last-good tracker. Lives in the
# `SimpleVariable` table (a generic kv store). The two halves are
# atomic-ish: we write the timestamp last so an interrupted write
# leaves the OLD timestamp + the new count (or both old), never
# the new timestamp without the count.
_GOSSIP_LAST_PULL_AT_KEY = "gossip_last_good_pull_at"
_GOSSIP_LAST_PULL_COUNT_KEY = "gossip_last_good_pull_node_count"


def _set_last_gossip_pull_success(*, node_count: int) -> None:
    """Persist the timestamp of the last successful gossip pull plus
    the node count it returned. Backed by SimpleVariable for simplicity
    — no need for a dedicated schema for two scalars."""
    now_iso = datetime.datetime.now().isoformat()
    try:
        SimpleVariable.replace(
            name=_GOSSIP_LAST_PULL_COUNT_KEY, value=str(node_count),
        ).execute()
        SimpleVariable.replace(
            name=_GOSSIP_LAST_PULL_AT_KEY, value=now_iso,
        ).execute()
    except Exception as e:
        logger.warning(
            f"could not persist last-good gossip pull state: {e}"
        )


def _get_last_gossip_pull_datetime() -> Optional[datetime.datetime]:
    """Return the timestamp of the last successful gossip pull, or
    None if no successful pull has ever been recorded. None is the
    "no record at all" signal that pick_best_channel_partners uses
    to refuse candidate selection."""
    try:
        row = SimpleVariable.get_or_none(
            SimpleVariable.name == _GOSSIP_LAST_PULL_AT_KEY
        )
    except Exception as e:
        logger.warning(f"could not read last-good gossip pull state: {e}")
        return None
    if row is None or not row.value:
        return None
    try:
        return datetime.datetime.fromisoformat(row.value)
    except (TypeError, ValueError):
        return None


async def process_pending_closes(api: "BitcartAPI") -> None:
    """Hourly retry loop for channels with an open coop-close request.

    LND's Lightning.CloseChannel(force=False) does NOT auto-retry. If the
    peer was offline when we asked, the request is dropped on their end
    and we have to re-issue once they come back. This loop walks every
    LightningChannel row with `cooperative_close_requested` set, and:

      - If the channel is no longer present in the wallet (close
        confirmed on-chain): clears the row's close markers — done.
      - If the channel is in pending-close state (waiting/closing):
        no-op, the close tx is already in flight.
      - If the channel is still OPEN and the first request is older
        than CHANNEL_COOP_CLOSE_TIMEOUT_DAYS: ESCALATE to force close
        (subject to per-wallet daily cap).
      - If the channel is still OPEN and the LAST attempt is older
        than CHANNEL_COOP_CLOSE_RETRY_INTERVAL_HOURS: RETRY the coop
        close.
      - If the channel is still OPEN but the last attempt is recent:
        no-op, give the peer time to respond.

    Force-close path is gated per-wallet (default 1/day) so an anomaly
    flagging many channels for escalation on the same day can't burst
    them all into the on-chain CSV timelock window simultaneously.
    """
    if not CHANNEL_COOP_CLOSE_RETRY_ENABLED:
        log_decision(
            ("coop_close_retry_gated", "global"), "disabled",
            "process_pending_closes: disabled "
            "(CHANNEL_COOP_CLOSE_RETRY_ENABLED=False); pending closes "
            "will not be retried or force-closed",
        )
        return
    log_decision(
        ("coop_close_retry_gated", "global"), "enabled",
        "process_pending_closes: enabled; hourly pass",
    )

    pending_rows = list(
        LightningChannel.select().where(
            LightningChannel.cooperative_close_requested.is_null(False)
        )
    )
    if not pending_rows:
        return

    try:
        wallets = await api.get_wallets(limit=200)
    except Exception as e:
        logger.warning(
            f"process_pending_closes: get_wallets failed: {e}"
        )
        return

    # Build channel-point → (wallet, channel_dict) map across every
    # wallet so we can look up the live state of each pending close
    # without N-by-M querying.
    cp_to_wallet: Dict[str, Tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = {}
    for wallet in wallets:
        try:
            channels = await api.get_wallet_ln_channels(
                wallet["id"], active_only=False, online_only=False,
            )
        except Exception as e:
            logger.warning(
                f"process_pending_closes: get_wallet_ln_channels "
                f"failed for {wallet['id']}: {e}"
            )
            continue
        for ch in channels or []:
            cp = ch.get("channel_point")
            if cp:
                cp_to_wallet[cp] = (wallet, ch)

    # Per-wallet force-close counter for the rate-limit decision.
    force_closes_this_run: Dict[str, int] = {}

    now = datetime.datetime.now()
    retry_interval = datetime.timedelta(
        hours=CHANNEL_COOP_CLOSE_RETRY_INTERVAL_HOURS,
    )
    timeout = datetime.timedelta(days=CHANNEL_COOP_CLOSE_TIMEOUT_DAYS)

    for row in pending_rows:
        cp = row.channel_point
        wallet_and_ch = cp_to_wallet.get(cp)
        if wallet_and_ch is None:
            # Channel is no longer in any wallet's channel list —
            # the close confirmed on-chain and was reaped by LND/
            # Electrum. Clear the markers; nothing more to do.
            log_decision(
                ("pending_close_resolved", cp), True,
                "process_pending_closes: channel %s no longer "
                "present in any wallet (close confirmed); clearing "
                "tracking markers.", cp,
            )
            row.cooperative_close_requested = None
            row.last_close_attempt_at = None
            row.force_close_initiated_at = None
            # Preserve cooperative_close_attempts for diagnostics.
            row.save()
            continue

        wallet, channel_dict = wallet_and_ch
        state = (channel_dict.get("state") or "").upper()
        # LND wallets don't always set 'state' on get_wallet_ln_channels;
        # treat missing as still-open and rely on PendingChannels later.
        if state in {"CLOSING", "FORCE_CLOSING", "PENDING_CLOSE",
                     "WAITING_CLOSE", "REDEEMED", "CLOSED"}:
            # Close already in flight or done. The next pass will
            # detect "no longer in channel list" once it confirms.
            continue

        first_request_age = now - row.cooperative_close_requested
        last_attempt_at = row.last_close_attempt_at or row.cooperative_close_requested
        since_last_attempt = now - last_attempt_at

        if (first_request_age > timeout
                and row.force_close_initiated_at is None):
            # Escalation path: been waiting longer than the timeout
            # with no successful coop. Check per-wallet rate limit.
            wallet_id = wallet["id"]
            if (force_closes_this_run.get(wallet_id, 0)
                    >= CHANNEL_FORCE_CLOSE_MAX_PER_DAY_PER_WALLET):
                log_decision(
                    ("force_close_rate_limited", wallet_id), True,
                    "process_pending_closes: wallet %s hit per-day "
                    "force-close cap (%d); deferring further "
                    "escalations to tomorrow",
                    wallet_id,
                    CHANNEL_FORCE_CLOSE_MAX_PER_DAY_PER_WALLET,
                )
                continue
            log_decision(
                ("pending_close_escalating", cp), True,
                "FORCE CLOSE: channel %s has been waiting %d days "
                "for cooperative close (peer unresponsive across %d "
                "attempts); escalating to force close. Funds will "
                "be locked behind the CSV timelock until the unilateral "
                "close confirms and resolves.",
                cp, first_request_age.days,
                row.cooperative_close_attempts,
                level=logging.WARNING,
            )
            existing_reason = row.close_reason or "(no original reason recorded)"
            try:
                close_result = await attempt_force_close(
                    cp, wallet=wallet, api=api,
                    reason=f"FORCE_CLOSE_AFTER_COOP_TIMEOUT: {existing_reason}",
                )
            except Exception as e:
                logger.error(
                    f"process_pending_closes: force_close failed "
                    f"for {cp}: {e} {traceback.print_exc()}"
                )
                close_result = None
            if close_result:
                force_closes_this_run[wallet_id] = (
                    force_closes_this_run.get(wallet_id, 0) + 1
                )
                log_decision(
                    ("pending_close_escalated", cp), True,
                    "process_pending_closes: force close submitted "
                    "for channel %s on wallet %s.",
                    cp, wallet_id, level=logging.WARNING,
                )
                # Force close ⇒ 1-year peer blacklist. We look up the
                # peer by channel_dict.remote_pubkey (the LightningChannel
                # row doesn't currently carry pubkey, so the channel-side
                # state can't tell us who the peer is).
                #
                # The LightningNode row for this peer may not exist
                # locally even though we obviously have a channel open
                # to them — three realistic cases:
                #   - LSP-opened channels (peer is the LSP node; may
                #     not be in our DB yet if the daily gossip pull
                #     hasn't processed it),
                #   - channels that pre-date this version of the
                #     script (no row was ever created),
                #   - channels opened manually outside the script.
                # In all three the channel itself is real and we still
                # want the blacklist recorded — `pick_best_channel_partners`
                # selects from LightningNode rows, so a future row
                # added by the next gossip pull will honor the
                # blacklist we set here. Create the row defensively
                # when missing.
                #
                # If remote_pubkey itself is empty (malformed channel
                # response), the force close still proceeds but the
                # blacklist write is skipped and a WARNING is logged
                # so the operator can set it manually if they care.
                peer_pubkey = (channel_dict.get("remote_pubkey") or "").lower()
                if peer_pubkey:
                    peer_row = LightningNode.get_or_none(
                        LightningNode.node_address == peer_pubkey
                    )
                    if peer_row is None:
                        peer_row = LightningNode(node_address=peer_pubkey)
                        peer_row.save(force_insert=True)
                    peer_row.force_close_blacklist_until = (
                        datetime.datetime.now()
                        + datetime.timedelta(
                            days=CHANNEL_FORCE_CLOSE_BLACKLIST_DAYS
                        )
                    )
                    peer_row.save()
                    log_decision(
                        ("force_close_blacklisted", peer_pubkey), True,
                        "Peer %s blacklisted from new channels until %s "
                        "(force-closed channel %s after %d-day timeout).",
                        peer_pubkey,
                        peer_row.force_close_blacklist_until, cp,
                        CHANNEL_COOP_CLOSE_TIMEOUT_DAYS,
                        level=logging.WARNING,
                    )
                else:
                    log_decision(
                        ("force_close_blacklist_skipped_no_pubkey", cp),
                        True,
                        "process_pending_closes: force-closed %s but "
                        "no remote_pubkey on the channel record; "
                        "could not set peer blacklist. Operator should "
                        "manually set force_close_blacklist_until on the "
                        "peer if known.", cp, level=logging.WARNING,
                    )
            else:
                log_decision(
                    ("pending_close_escalate_failed", cp), True,
                    "process_pending_closes: force close attempt for "
                    "%s returned no result; will retry next hour.",
                    cp, level=logging.WARNING,
                )
            continue

        if row.force_close_initiated_at is not None:
            # Force close already initiated and channel still in
            # OPEN list — the unilateral broadcast may still be
            # propagating. Wait, don't double-issue.
            continue

        # Coop retry path.
        if since_last_attempt < retry_interval:
            continue
        log_decision(
            ("pending_close_retry", cp), row.cooperative_close_attempts,
            "process_pending_closes: retrying coop close for %s "
            "(attempt #%d, first requested %d days ago)",
            cp, row.cooperative_close_attempts + 1,
            first_request_age.days,
        )
        try:
            # Retry of an existing coop close — don't supply a reason
            # so the original (audit_failure / offline / etc.) is
            # preserved on the row. _record_close_attempt only
            # overwrites close_reason when one is passed.
            await attempt_cooperative_close(cp, wallet=wallet, api=api)
        except Exception as e:
            logger.warning(
                f"process_pending_closes: coop retry failed for "
                f"{cp}: {e} (will try again next hour)"
            )


async def audit_existing_channels(api: "BitcartAPI") -> None:
    """Daily re-evaluation of every open Lightning channel against the
    degradation criteria in node_database.audit_existing_peer.

    Flow per peer:
      - audit_existing_peer(peer_node) returns (failed, reasons[]).
      - On pass: reset consecutive_failed_audits to 0.
      - On fail: increment consecutive_failed_audits, log the reasons,
        record last_audit_failure_at.
      - If consecutive_failed_audits crosses
        CHANNEL_AUDIT_CONSECUTIVE_FAILURES_TO_CLOSE → cooperatively
        close the channel, set audit_close_blacklist_until = now + N
        days, increment close-count for today's rate-limit cap.

    Safety:
      - Master switch CHANNEL_AUDIT_ENABLED.
      - Per-day cap CHANNEL_AUDIT_MAX_CLOSES_PER_DAY (default 1) —
        contains blast radius of any graph-pull anomaly.
      - "Missing data" (peer's gossip metrics not yet computed) is
        treated as 'skip audit this tick', not 'fail'.

    Notification: every close emits a WARNING decision listing each
    failing criterion by name. The plugin Logs tab + decisions.log file
    surface this to the operator.
    """
    if not CHANNEL_AUDIT_ENABLED:
        log_decision(
            ("channel_audit_gated", "global"), "disabled",
            "audit_existing_channels: disabled "
            "(CHANNEL_AUDIT_ENABLED=False); skipping daily audit",
        )
        return
    log_decision(
        ("channel_audit_gated", "global"), "enabled",
        "audit_existing_channels: enabled; running daily peer audit",
    )

    try:
        wallets = await api.get_wallets(limit=200)
    except Exception as e:
        logger.warning(f"audit_existing_channels: get_wallets failed: {e}")
        return

    closes_today = 0
    for wallet in wallets:
        if closes_today >= CHANNEL_AUDIT_MAX_CLOSES_PER_DAY:
            log_decision(
                ("channel_audit_rate_limited", "global"), True,
                "audit_existing_channels: hit per-day close cap "
                "(%d); deferring remaining audits to tomorrow",
                CHANNEL_AUDIT_MAX_CLOSES_PER_DAY,
            )
            break
        wallet_id = wallet["id"]
        try:
            channels = await api.get_wallet_ln_channels(
                wallet_id, active_only=True, online_only=False,
            )
        except Exception as e:
            logger.warning(
                f"audit_existing_channels: get_wallet_ln_channels "
                f"failed for {wallet_id}: {e}"
            )
            continue
        for channel in channels:
            if closes_today >= CHANNEL_AUDIT_MAX_CLOSES_PER_DAY:
                break
            peer_pubkey = (channel.get("remote_pubkey") or "").lower()
            channel_point = channel.get("channel_point")
            if not peer_pubkey or not channel_point:
                continue
            peer_node = LightningNode.get_or_none(
                LightningNode.node_address == peer_pubkey
            )
            if peer_node is None:
                # No row yet — graph pull hasn't seen this peer. Skip
                # silently; treating absence as failure would cascade
                # in fresh installs before the first daily pull.
                continue

            failed, reasons = audit_existing_peer(peer_node)
            if not failed:
                if peer_node.consecutive_failed_audits > 0:
                    log_decision(
                        ("channel_audit_streak", peer_pubkey), 0,
                        "Channel audit (peer %s): passed; "
                        "resetting failure streak", peer_pubkey,
                    )
                peer_node.consecutive_failed_audits = 0
                peer_node.save()
                continue

            # Failed audit — increment streak, log reasons.
            peer_node.consecutive_failed_audits = (
                (peer_node.consecutive_failed_audits or 0) + 1
            )
            peer_node.last_audit_failure_at = datetime.datetime.now()
            peer_node.save()
            log_decision(
                ("channel_audit_streak", peer_pubkey),
                peer_node.consecutive_failed_audits,
                "Channel audit (peer %s): failure #%d, reasons=%s "
                "(close at %d consecutive failures)",
                peer_pubkey, peer_node.consecutive_failed_audits,
                ",".join(reasons),
                CHANNEL_AUDIT_CONSECUTIVE_FAILURES_TO_CLOSE,
            )

            if (peer_node.consecutive_failed_audits
                    < CHANNEL_AUDIT_CONSECUTIVE_FAILURES_TO_CLOSE):
                continue

            # Threshold reached → close + blacklist.
            log_decision(
                ("channel_audit_closing", channel_point), True,
                "CHANNEL AUDIT CLOSE: peer %s failed %d consecutive "
                "daily audits, reasons=%s. Cooperatively closing "
                "channel %s and blacklisting peer for %d days.",
                peer_pubkey, peer_node.consecutive_failed_audits,
                ",".join(reasons), channel_point,
                CHANNEL_AUDIT_BLACKLIST_DAYS,
                level=logging.WARNING,
            )
            try:
                close_result = await attempt_cooperative_close(
                    channel_point, wallet=wallet, api=api,
                    reason=f"AUDIT_FAILURE: {','.join(reasons)}",
                )
            except Exception as e:
                logger.error(
                    f"audit_existing_channels: cooperative close "
                    f"failed for {channel_point}: {e} "
                    f"{traceback.print_exc()}"
                )
                close_result = None
            if close_result:
                closes_today += 1
                peer_node.audit_close_blacklist_until = (
                    datetime.datetime.now()
                    + datetime.timedelta(days=CHANNEL_AUDIT_BLACKLIST_DAYS)
                )
                # Reset the streak; the blacklist field is now the
                # active gate. If the operator manually clears the
                # blacklist, audits start fresh.
                peer_node.consecutive_failed_audits = 0
                peer_node.save()
                log_decision(
                    ("channel_audit_closed", peer_pubkey), True,
                    "Channel %s coop-close initiated; peer %s "
                    "blacklisted until %s.",
                    channel_point, peer_pubkey,
                    peer_node.audit_close_blacklist_until,
                    level=logging.WARNING,
                )
            else:
                # Close failed — log and try again on the next audit
                # pass. Don't blacklist (operator may want to retry
                # manually). Don't reset streak (the failure stands).
                log_decision(
                    ("channel_audit_close_failed", channel_point), True,
                    "Channel audit: coop close attempt for %s "
                    "returned no result; will retry on next audit.",
                    channel_point, level=logging.WARNING,
                )


async def audit_lsp_network_compatibility(api: "BitcartAPI") -> None:
    """One-pass audit: for every LND wallet, list which LSPs can
    actually serve its network and which can't. Emits decision logs
    keyed by (wallet, provider) so the table is greppable later.

    Why this exists: operators only discover network-incompatibility
    lazily today, when the first real LSP request fires and fails
    silently or floods the decision stream. This function gives a
    single, scan-friendly summary at engine startup (once per day via
    run_every_x_days) so operators can confirm before relying on it.

    LSPs aren't required infrastructure (the script falls back to
    manual channel management) so we never RAISE — only emit
    decisions. The dashboard reads from the same decision stream so
    operators can also see this in the log viewer.
    """
    try:
        wallets = await api.get_wallets()
    except Exception as e:
        logger.warning(
            f"audit_lsp_network_compatibility: get_wallets failed: {e}"
        )
        return
    if not wallets:
        return
    providers = _lsp_providers.get_lsp_providers()
    matrix: Dict[str, Dict[str, str]] = {}
    for wallet in wallets:
        if wallet.get("currency") != "btclnd":
            continue
        wallet_id = wallet["id"]
        network = await lsp_network_for_wallet(wallet, api)
        if network is None:
            # lsp_network_for_wallet has already logged the reason
            # (regtest / unknown / no LND info). Record in the matrix
            # so the summary line shows "<none>".
            matrix[wallet_id] = {"<network>": "unsupported_or_unknown"}
            continue
        per_provider: Dict[str, str] = {}
        for provider in providers:
            if network in provider.supported_networks():
                per_provider[provider.name] = "supported"
            else:
                per_provider[provider.name] = (
                    f"unsupported (supports {provider.supported_networks()})"
                )
            log_decision(
                ("lsp_compatibility", wallet_id, provider.name),
                per_provider[provider.name],
                "LSP compatibility: wallet %s (network=%s) × %s → %s",
                wallet_id, network, provider.name,
                per_provider[provider.name],
            )
        matrix[wallet_id] = {"<network>": network, **per_provider}

    # One top-line summary so the operator can scan a single log line
    # to see the whole compatibility picture.
    log_event(
        "LSP compatibility audit: %d LND wallet(s) reviewed; matrix=%s",
        sum(1 for v in matrix.values() if v.get("<network>") not in (None, "unsupported_or_unknown")),
        matrix,
    )


async def ensure_lnd_wallets_peered_with_lsps(api: "BitcartAPI") -> None:
    """For each LND wallet, idempotently `Lightning.ConnectPeer` to each
    LSP's lightning node on the wallet's network. Zeus and Megalithic
    both refuse `create_order` if the client isn't already peered, so
    this needs to run before any LSP request flow.

    Called once per main() tick. Connection persistence (`perm=True`)
    means LND will auto-reconnect on disconnect between ticks; this
    function only kicks the initial dial. Per-wallet+provider state is
    tracked via log_decision so repeated "already connected" results
    don't spam decisions.log.

    Gated by LSP_AUTO_PEER (default True). No-op when False — operators
    who manage peering out of band can disable.
    """
    if not LSP_AUTO_PEER:
        return
    try:
        wallets = await api.get_wallets(limit=200)
    except Exception as e:
        logger.warning(
            f"ensure_lnd_wallets_peered_with_lsps: get_wallets failed: {e}"
        )
        return

    providers = _lsp_providers.get_lsp_providers()
    for wallet in (wallets or []):
        if wallet.get("currency") != "btclnd":
            continue
        wallet_id = wallet["id"]
        network = await lsp_network_for_wallet(wallet, api)
        if network is None:
            continue
        for provider in providers:
            if network not in provider.supported_networks():
                # Same shape as the skip log in pick_best_lsp_for_inbound
                # so the two paths produce a uniform audit trail. Without
                # this, operators wondering why a particular LSP isn't
                # being dialed for their wallet's network see nothing
                # in the decision stream.
                log_decision(
                    ("lsp_peer_skip_unsupported_network",
                     provider.name, wallet_id),
                    network,
                    "Skipping LSP %s peering for wallet %s: provider "
                    "does not support network=%s "
                    "(supported=%s).",
                    provider.name, wallet_id, network,
                    provider.supported_networks(),
                )
                continue
            try:
                # get_all_peer_uris returns the union of the hardcoded
                # fallback AND every URI in get_info.uris[]. ConnectPeer
                # is idempotent, so dialing every one is harmless and
                # buys robustness against LSP pubkey rotation and stale
                # documentation.
                peer_uris = await provider.get_all_peer_uris(network=network)
            except Exception as e:
                logger.warning(
                    f"ensure_lnd_wallets_peered_with_lsps: "
                    f"could not get peer URIs from {provider.name}: {e}"
                )
                continue
            if not peer_uris:
                # No hardcoded value and get_info returned nothing —
                # Megalithic Mutinynet is the canonical case (sentinel
                # fallback, get_info reachable only after we know the
                # peer, chicken-and-egg). Surface as a decision.
                log_decision(
                    ("lsp_peer_status", wallet_id, provider.name),
                    "no_uri",
                    "LSP peer URI unknown for %s on %s; get_info did "
                    "not return a uris[] entry and no hardcoded "
                    "fallback exists. Cannot connect.",
                    provider.name, network,
                )
                continue

            # Track per-(wallet, provider) outcome across the URI list.
            # If ANY URI connects (or is already connected), record
            # success; only flag failure if all URIs fail.
            any_success = False
            last_failure: Optional[Exception] = None
            for peer_uri in peer_uris:
                try:
                    pubkey, host_port = peer_uri.split("@", 1)
                except ValueError:
                    logger.warning(
                        f"ensure_lnd_wallets_peered_with_lsps: malformed "
                        f"peer URI {peer_uri!r} from {provider.name}; skipping"
                    )
                    continue
                pubkey = pubkey.lower()
                request_params = {
                    "addr": {"pubkey": pubkey, "host": host_port},
                    "perm": True,
                }
                try:
                    await lnd_rpc(api, wallet_id, "ConnectPeer",
                                  request_params, "Lightning")
                    any_success = True
                    log_event(
                        "LSP peer connect: wallet=%s provider=%s "
                        "network=%s pubkey=%s",
                        wallet_id, provider.name, network,
                        pubkey[:16] + "...",
                    )
                except Exception as e:
                    msg = str(e).lower()
                    if "already connected" in msg:
                        any_success = True
                    elif _looks_like_lnd_not_ready(msg):
                        # LND-side transient, applies to every URI on
                        # this wallet; no point trying remaining ones.
                        last_failure = e
                        log_decision(
                            ("lsp_peer_status", wallet_id, provider.name),
                            "starting_up",
                            "LSP peer connect deferred: wallet %s -> %s "
                            "(%s) - LND still starting; will retry next tick",
                            wallet_id, provider.name, network,
                        )
                        logger.info(
                            f"ensure_lnd_wallets_peered_with_lsps: LND for "
                            f"wallet {wallet_id} is still starting up "
                            f"({provider.name} peer); will retry next tick"
                        )
                        break
                    else:
                        # Per-URI failure (likely stale pubkey for a
                        # rotated peer). Log at DEBUG and try the next
                        # URI; we may still succeed via another.
                        last_failure = e
                        logger.debug(
                            "ConnectPeer to %s failed for wallet %s "
                            "provider %s (will try other URIs): %s",
                            pubkey[:16], wallet_id, provider.name, e,
                        )

            if any_success:
                log_decision(
                    ("lsp_peer_status", wallet_id, provider.name),
                    "connected",
                    "LSP peer connected: wallet %s -> %s (%s)",
                    wallet_id, provider.name, network,
                )
            elif last_failure is not None and not _looks_like_lnd_not_ready(
                str(last_failure).lower()
            ):
                # Every URI failed for a non-transient reason. Worth
                # warning — operator may need to update hardcoded
                # fallback or check LSP availability.
                logger.warning(
                    f"ensure_lnd_wallets_peered_with_lsps: ALL URIs "
                    f"failed for wallet={wallet_id} provider={provider.name}; "
                    f"last error: {last_failure}"
                )
                log_decision(
                    ("lsp_peer_status", wallet_id, provider.name),
                    "all_failed",
                    "LSP peer connect FAILED on all %d URIs: wallet %s "
                    "-> %s (%s); last error: %s",
                    len(peer_uris), wallet_id, provider.name, network,
                    last_failure,
                )


async def cleanup_old_lsp_quotes() -> int:
    """Delete LspPriceQuote rows older than 6 months. Mirror of
    cleanup_old_swap_quotes; called once per day from main()."""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=183)
    try:
        n = LspPriceQuote.delete().where(
            LspPriceQuote.fetched_at < cutoff
        ).execute()
        if n:
            logger.info(
                f"cleanup_old_lsp_quotes: removed {n} rows older than 6 months"
            )
        return n
    except Exception as e:
        logger.warning(f"cleanup_old_lsp_quotes failed: {e}")
        return 0


_ACCOUNT_ADDR_TYPE_PROTO = {
    "p2tr": _loop_pb2.TAPROOT_PUBKEY,
    "taproot": _loop_pb2.TAPROOT_PUBKEY,
}


def _build_liquidity_params():
    """Translate AUTOLOOP_* config vars into a LiquidityParameters proto.

    Destination handling: loop supports three mutually-exclusive on-chain
    destination modes (see config comments). If both AUTOLOOP_ACCOUNT and
    AUTOLOOP_DEST_ADDRESS are set, account-derived wins because it gives
    fresh addresses AND keeps custody of the keys; we log a warning and
    leave dest_address empty so loopd uses the account path.
    """
    p = _loop_pb2.LiquidityParameters()
    p.autoloop = bool(AUTOLOOP_ENABLED)

    if AUTOLOOP_ACCOUNT and AUTOLOOP_DEST_ADDRESS:
        logger.warning(
            "AUTOLOOP_ACCOUNT and AUTOLOOP_DEST_ADDRESS are both set; "
            "account wins (loop derives fresh addresses from the imported "
            "xpub). Ignoring AUTOLOOP_DEST_ADDRESS=%r.",
            AUTOLOOP_DEST_ADDRESS,
        )
    if AUTOLOOP_ACCOUNT:
        p.account = AUTOLOOP_ACCOUNT
        addr_type = _ACCOUNT_ADDR_TYPE_PROTO.get(
            (AUTOLOOP_ACCOUNT_ADDR_TYPE or "").lower()
        )
        if addr_type is None:
            logger.warning(
                "AUTOLOOP_ACCOUNT_ADDR_TYPE=%r is not supported by loop "
                "(only 'p2tr' is). Falling back to p2tr.",
                AUTOLOOP_ACCOUNT_ADDR_TYPE,
            )
            addr_type = _loop_pb2.TAPROOT_PUBKEY
        p.account_addr_type = addr_type
    elif AUTOLOOP_DEST_ADDRESS:
        p.autoloop_dest_address = AUTOLOOP_DEST_ADDRESS
    # else: leave both unset -> loopd default (fresh LND-generated address per swap)

    p.autoloop_budget_sat = int(AUTOLOOP_BUDGET_SAT)
    p.autoloop_budget_refresh_period_sec = int(AUTOLOOP_BUDGET_REFRESH_PERIOD_SEC)
    p.auto_max_in_flight = int(AUTOLOOP_MAX_IN_FLIGHT)
    p.min_swap_amount = int(AUTOLOOP_MIN_SWAP_AMOUNT_SAT)
    p.max_swap_amount = int(AUTOLOOP_MAX_SWAP_AMOUNT_SAT)
    if AUTOLOOP_FEE_PPM:
        p.fee_ppm = int(AUTOLOOP_FEE_PPM)
    p.max_swap_fee_ppm = int(AUTOLOOP_MAX_SWAP_FEE_PPM)
    p.max_routing_fee_ppm = int(AUTOLOOP_MAX_ROUTING_FEE_PPM)
    p.max_prepay_routing_fee_ppm = int(AUTOLOOP_MAX_PREPAY_ROUTING_FEE_PPM)
    p.max_prepay_sat = int(AUTOLOOP_MAX_PREPAY_SAT)
    p.max_miner_fee_sat = int(AUTOLOOP_MAX_MINER_FEE_SAT)
    p.sweep_conf_target = int(AUTOLOOP_SWEEP_CONF_TARGET)
    p.htlc_conf_target = int(AUTOLOOP_HTLC_CONF_TARGET)
    p.sweep_fee_rate_sat_per_vbyte = int(AUTOLOOP_SWEEP_FEE_RATE_SAT_PER_VBYTE)
    p.failure_backoff_sec = int(AUTOLOOP_FAILURE_BACKOFF_SEC)
    p.easy_autoloop = bool(AUTOLOOP_EASY_MODE)
    p.easy_autoloop_local_target_sat = int(AUTOLOOP_EASY_LOCAL_TARGET_SAT)
    p.fast_swap_publication = bool(AUTOLOOP_FAST_SWAP_PUBLICATION)
    for pk_hex in (AUTOLOOP_EASY_EXCLUDED_PEERS or []):
        try:
            p.easy_autoloop_excluded_peers.append(bytes.fromhex(pk_hex))
        except ValueError:
            logger.warning(
                f"skipping malformed pubkey in AUTOLOOP_EASY_EXCLUDED_PEERS: {pk_hex!r}"
            )
    return p


async def configure_autoloop(wallet: Dict[str, Any], api: "BitcartAPI") -> bool:
    """Push current AUTOLOOP_* config to the wallet's loopd. Best-effort:
    logs warning + returns False if loopd rejects the params. With
    AUTOLOOP_ENABLED=False we still push (with autoloop=false) so any
    previously-enabled autoloop on that loopd gets turned off.

    LND-only: loopd is a Lightning Labs daemon that talks to LND
    specifically. Returns False for non-LND wallets without attempting
    any configuration.
    """
    if wallet.get("currency") != "btclnd":
        log_decision(
            ("configure_autoloop_skip_non_lnd", wallet.get("id")), True,
            "configure_autoloop: wallet %s is currency=%s, not btclnd — "
            "loopd is LND-only, skipping",
            wallet.get("id"), wallet.get("currency"),
        )
        return False
    providers = _swap_provider_registry()
    loop_provider = next((p for p in providers if isinstance(p, LoopProvider)), None)
    if loop_provider is None:
        return False
    # No dest-address requirement: when both AUTOLOOP_DEST_ADDRESS and
    # AUTOLOOP_ACCOUNT are unset, loopd falls back to fresh LND-owned
    # addresses, which is a valid mode.
    params = _build_liquidity_params()
    return await loop_provider.configure_autoloop(wallet, api, params)


def _resolve_internal_api_url() -> str:
    """Pick the right URL for calling Bitcart's own API back.

    - In plugin mode the engine runs inside the backend container.
      There is no nginx on 127.0.0.1 there (nginx is in a separate
      compose service), so http://127.0.0.1/api is connection-refused.
      Gunicorn itself listens on :8000 *without* the /api prefix
      (root_path=/api strips it on incoming requests from nginx).
    - In standalone mode the engine runs on a laptop hitting the VPS
      through an SSH-forwarded port 80 → nginx → backend. The /api
      prefix is required there because nginx routes /api/* to the
      backend.

    Heuristic for "we're inside the backend container": the bitcart
    Docker image sets BITCART_BACKEND_ROOTPATH for its own internals.
    Operators can override either side with LIQUIDITYHELPER_API_URL.
    """
    override = _os.environ.get("LIQUIDITYHELPER_API_URL")
    if override:
        return override
    if _os.environ.get("BITCART_BACKEND_ROOTPATH"):
        return "http://localhost:8000"
    return "http://127.0.0.1/api"


_INTERNAL_API_URL = _resolve_internal_api_url()


async def _get_dashboard_api() -> "BitcartAPI":
    """Construct a `BitcartAPI` client for ad-hoc requests from plugin
    endpoints (currently: the dashboard router).

    Reuses the same URL + token the tick loop uses. The token is
    expected to be set on the module's `AUTH_TOKEN` global by the
    plugin's `worker_setup` (or by env in standalone mode). Callers
    must `await api.close()` when done — each call constructs a fresh
    client, so leaving them open leaks a connection pool.
    """
    return BitcartAPI(_INTERNAL_API_URL, AUTH_TOKEN)


async def main():
    global LAST_FEE_CHECK
    global START_TIME
    global AUTH_TOKEN
    global NOTIFICATION_PROVIDERS
    # See _resolve_internal_api_url(): /api prefix from outside, no
    # prefix when running inside the backend container.
    BITCART_URL = _INTERNAL_API_URL
    maybe_emit_heartbeat()
    # delete old cache entries
    try:
        SimpleCacheField.delete_expired()
    except Exception as e:
        logger.error(f'Error deleting expired cache entries {e} {traceback.print_exc()}')
    # init notifications
    try:
        if len(NOTIFICATION_PROVIDERS)==0:
            NOTIFICATION_PROVIDERS=await run_every_x_hours(my_func=setup_notifiers,hours=6)
    except Exception as e:
        logger.error(f'Not able to setup notifications, please see logs! {e}')
    # init Bitcart API
    try:
        logger.info("Initializing bitcart API....")
        api = BitcartAPI(BITCART_URL, AUTH_TOKEN)
        if not AUTH_TOKEN:
            token_object=SimpleVariable.get_or_none(
                SimpleVariable.name == "BITCARTAPITOKEN"
            )
            if token_object:
                AUTH_TOKEN = token_object.value
        api = BitcartAPI(BITCART_URL, AUTH_TOKEN)
        if not AUTH_TOKEN:
            logger.info(
                "🔎 Detected first run, attempting to create account for Bitcart API.."
            )

            AUTH_TOKEN = await api.setup_first_user(ADMIN_EMAIL, ADMIN_PASSWORD)
            if AUTH_TOKEN:
                new_object = SimpleVariable(
                    name="BITCARTAPITOKEN", value=AUTH_TOKEN
                )
                new_object.save(force_insert=True)
            else:
                logger.critical(
                    "Critical error: no auth token for bitcart API. Maybe bitcart isnt started yet? Sleeping for 30 seconds"
                )
                await asyncio.sleep(30)
                return
        # Check authentication
        api = BitcartAPI(BITCART_URL, AUTH_TOKEN)
        auth_result = await api.is_authenticated()
        if not auth_result:
            logger.error(
                "⚠️ Bitcart Authentication failed..."
            )
            await asyncio.sleep(60)
            return
    except Exception as e:
        logger.error(
            f"⚠️ Bitcart api auth error {e}, sleeping... {traceback.print_exc()}"
        )
        await asyncio.sleep(60)
        return
    # create first wallet if it doesn't exist, must be done before creating first store
    first_wallet_response = None
    try:
        first_wallet_response = await first_wallet_check_create(api)
    except Exception as e:
        logger.error(f"Error in wallet creation stage1 {e} {traceback.print_exc()}")
        traceback.print_exc()
        await asyncio.sleep(60)
        return
    if not first_wallet_response:
        logger.error(f"Error in wallet creation stage2")
        await asyncio.sleep(60)
        return

    # create our wallet for each store if it doesn't exist
    wallet_creation_response = None
    try:
        wallet_creation_response = await wallet_creation(api)
    except Exception as e:
        logger.error(f"Error in wallet creation stage {e} {traceback.print_exc()}")
        traceback.print_exc()
        await asyncio.sleep(60)
        return
    if not wallet_creation_response:
        logger.error(f"2Error in wallet creation stage")
        await asyncio.sleep(60)
        return
    # calculate top-ups
    try:
        if DEBUG_STEPS:
            breakpoint()
        topup_answer = await calculate_topups(api)
    except Exception as e:
        logger.error("Error calculating top-ups")
    # check available inbound liquidity
    liquidity_check_response = None
    if START_TIME > (datetime.datetime.now() - datetime.timedelta(seconds=30)) and not SKIP_WALLET_DELAY:
        logger.info(
            "Sleeping 30 seconds before requesting liquidity status so wallet has a chance to come online..."
        )
        await asyncio.sleep(30)
    # Ensure each LND wallet is peered with its LSPs. Placed AFTER the
    # startup wallet-warmup wait so first-run ConnectPeer calls don't
    # race against LND's unlock/sync sequence. Throttled to once per
    # day via run_every_x_days because ConnectPeer with perm=True
    # makes LND auto-reconnect on disconnect — re-issuing every tick
    # is pure noise. LSP create_order calls will surface a peer
    # missing if anything is amiss between runs.
    try:
        await run_every_x_days(
            my_func=ensure_lnd_wallets_peered_with_lsps, days=1, api=api,
        )
    except Exception as e:
        logger.error(
            f"Error in ensure_lnd_wallets_peered_with_lsps: {e} "
            f"{traceback.print_exc()}"
        )
    # Daily pre-flight: log which LSPs serve which wallets' networks
    # so an operator can spot misconfiguration (e.g. testnet wallet
    # configured to use only Megalithic) without waiting for a real
    # request to fail. Same daily cadence as the peering call — the
    # answer rarely changes between configs.
    try:
        await run_every_x_days(
            my_func=audit_lsp_network_compatibility, days=1, api=api,
        )
    except Exception as e:
        logger.error(
            f"Error in audit_lsp_network_compatibility: {e} "
            f"{traceback.print_exc()}"
        )
    # Refresh the LightningNode candidate DB from LND gossip once per
    # day. LN gossip is identical across well-synced LND nodes, so we
    # just pick the first LND wallet — no value in fanning out across
    # wallets for the same data.
    try:
        await run_every_x_days(
            my_func=refresh_lnd_node_database, days=1, api=api,
        )
    except Exception as e:
        logger.error(
            f"Error in refresh_lnd_node_database: {e} "
            f"{traceback.print_exc()}"
        )
    # Daily audit of every open channel against the degradation
    # criteria (HIGH_FEE_RATE / LOW_EFFECTIVE_DEGREE / LOW_TWO_HOP_REACH
    # / LOW_CAPACITY / LOW_OUTBOUND_CAPACITY). Three consecutive daily
    # failures → coop close + 180-day audit blacklist. Defaults are
    # opt-out (CHANNEL_AUDIT_ENABLED=True) with a 1-close-per-day cap.
    try:
        await run_every_x_days(
            my_func=audit_existing_channels, days=1, api=api,
        )
    except Exception as e:
        logger.error(
            f"Error in audit_existing_channels: {e} "
            f"{traceback.print_exc()}"
        )
    # Hourly retry of stuck coop closes. LND doesn't auto-retry a coop
    # close request the peer didn't respond to; we re-issue once an
    # hour until either the peer signs OR the 10-day timeout fires
    # and we escalate to force close (per-wallet 1/day cap).
    try:
        await run_every_x_hours(
            my_func=process_pending_closes, hours=1, api=api,
        )
    except Exception as e:
        logger.error(
            f"Error in process_pending_closes: {e} "
            f"{traceback.print_exc()}"
        )
    # update closed channel stats
    try:
        channel_closings_result=await update_channel_closings(api)
    except Exception as e:
        logger.error(f'Error in updating channel closings: {e}:{traceback.print_exc()}')
    # add more liquidity as needed
    try:
        if DEBUG_STEPS:
            breakpoint()
        liquidity_check_response = await liquidity_check(api)
    except Exception as e:
        logger.error(
            f"Error in checking available inbound liquidity: {e}:{traceback.print_exc()}"
        )
    # calculate onchain -> LN moves
    # this is only done if liquidity/reserves are sufficient
    try:
        if DEBUG_STEPS:
            breakpoint()
        onchain_to_ln_result = await run_every_x_seconds(my_func=decide_onchain_to_ln, seconds=90, api=api)
    except Exception as e:
        logger.error(f'Error calling onchain_to_ln_result: {e}:{traceback.print_exc()}')
    # Calculate and send fees, this check is heavy and shouldn't be run more than once per day
    fee_response = None
    try:
        if DEBUG_STEPS:
            breakpoint()
        if FORCE_FEE_CHECK:
            fee_response= await run_every_x_seconds(my_func=calculate_fees,seconds=1,api=api)
        else:
            fee_response= await run_every_x_days(my_func=calculate_fees,days=1, api=api)
    except Exception as e:
        logger.error(f"Error in calculating fees: {e} {traceback.print_exc()}")

    # Detection pass: walk LND wallets and surface channels whose
    # local balance exceeds LOOP_OUT_TRIGGER_LOCAL_BALANCE_SAT.
    # Detection is unconditional — it just observes and logs. The
    # actual swap initiation lives in do_cashouts (via
    # _drain_ln_for_cashout_if_enabled → drain_ln_to_onchain) and is
    # gated by LOOP_OUT_ENABLED + LN staleness OR PREFER_CASHOUT_ONCHAIN.
    # An operator with LOOP_OUT_ENABLED=False sees the candidate list
    # in decisions.log without any side effect.
    try:
        if DEBUG_STEPS:
            breakpoint()
        loop_out_candidates = await find_loop_out_candidates(api)
        # Candidate-count is a re-evaluated state — log only when the
        # number of candidates changes between ticks. Operational
        # detail (per-candidate channel info) goes to the main log
        # at DEBUG so it's available for diagnosis without flooding
        # decisions.log on every tick.
        log_decision(
            "loop_out_candidate_count",
            len(loop_out_candidates),
            "loop-out: %d candidate channel(s) with local balance > %d sat",
            len(loop_out_candidates), LOOP_OUT_TRIGGER_LOCAL_BALANCE_SAT,
        )
        for cand in loop_out_candidates:
            logger.debug(
                "loop-out candidate: wallet=%s channel_point=%s "
                "local_balance=%d sat remote_balance=%d sat remote_pubkey=%s",
                cand.get("wallet_id"), cand.get("channel_point"),
                cand.get("local_balance_sat"), cand.get("remote_balance_sat"),
                cand.get("remote_pubkey"),
            )
        if loop_out_candidates and LOOP_OUT_ENABLED:
            log_decision(
                "loop_out_enabled_but_unwired",
                True,
                "LOOP_OUT_ENABLED=True but automated loop-out execution is "
                "not yet wired; operator must invoke "
                "initiate_lightning_to_onchain_swap() manually."
            )
    except Exception as e:
        logger.error(f"Error in loop-out candidate scan: {e} {traceback.print_exc()}")

    # Purge swap-quote history older than 6 months. Cheap, so daily is fine.
    try:
        await run_every_x_days(my_func=cleanup_old_swap_quotes, days=1)
    except Exception as e:
        logger.error(f"Error in cleanup_old_swap_quotes scheduling: {e}")
    try:
        await run_every_x_days(my_func=cleanup_old_lsp_quotes, days=1)
    except Exception as e:
        logger.error(f"Error in cleanup_old_lsp_quotes scheduling: {e}")

    # Calculate and send cashouts, should basically be the same code as calculating fees
    cashout_response = None
    try:
        if DEBUG_STEPS:
            breakpoint()
        cashout_response = await do_cashouts(api)
    except Exception as e:
        logger.error(f"Error in calculating cashouts: {e} {traceback.print_exc()}")
    if not cashout_response:
        logger.error(f"2Error in calculating cashouts")


# Debug-mode trigger. When DEBUG_MODE is True, run_tick_loop awaits
# this event before each main() call instead of auto-cycling — the
# operator (or the Logs-tab "Run one tick" button) fires it to step
# through one iteration. Lives at module scope so settings-change hooks
# and HTTP endpoints can both reach it. Created at module import; the
# event is loop-agnostic until first awaited.
_debug_run_once_trigger: asyncio.Event = asyncio.Event()


def trigger_debug_run_once() -> None:
    """Fire one debug-mode tick. Safe to call from any thread or any
    coroutine context — asyncio.Event.set() is loop-thread-safe in
    Python 3.10+. No-op when DEBUG_MODE is False (the run_tick_loop
    won't be waiting on this trigger in that case)."""
    _debug_run_once_trigger.set()


async def run_tick_loop(stop_event: Optional[asyncio.Event] = None) -> None:
    """Run main() in a loop forever (until SINGLE_RUN is set or
    `stop_event` is signalled). The loop body is wrapped so that ANY
    uncaught exception inside main() is logged and the loop continues
    after a 60-second wait. The contract is: this function never
    returns abnormally — short of asyncio cancellation, the only way
    out is SINGLE_RUN=True or stop_event.set().

    Debug mode (DEBUG_MODE=True): instead of cycling continuously,
    the loop awaits an explicit trigger before each main() call. The
    Logs-tab "Run one tick" button fires the trigger; toggling
    DEBUG_MODE off (via the settings page) also fires it so the loop
    can resume normal continuous operation without a click.

    When the plugin shuts down it sets stop_event; the current main()
    is allowed to finish, and then we exit cleanly without cancelling
    mid-tick. CancelledError from cooperative cancellation is the
    one exception we DO propagate — that's how asyncio shuts the
    task down on Bitcart restart.
    """
    # Defensive clear of any stale trigger state from initialization.
    # The trigger is an asyncio.Event; if some path during plugin
    # bootstrapping (e.g., a startup-time settings_changed hook fire)
    # set it before run_tick_loop reached its first iteration, the
    # first DEBUG_MODE wait would see an already-set event and
    # immediately fall through to main() — exactly the "don't auto-
    # run on first install" guarantee we promise the operator. The
    # clear is unconditional because at THIS point the loop is by
    # definition not yet waiting on the trigger, so any set state
    # is by definition stale (no legitimate trigger could have
    # come from a parked wait).
    _debug_run_once_trigger.clear()
    while True:
        # Debug gate. Re-read DEBUG_MODE every iteration so the
        # operator can toggle it live via the settings page without a
        # restart. When debug mode is on, block here until either:
        # (a) the operator clicks Run-one-tick → trigger fires;
        # (b) the operator disables debug mode → the settings hook
        #     fires the trigger to unblock us;
        # (c) shutdown sets stop_event → we wake and exit;
        # (d) cooperative cancellation propagates a CancelledError.
        if globals().get("DEBUG_MODE"):
            log_decision(
                ("debug_mode_waiting",), True,
                "DEBUG_MODE=True; tick loop is waiting for an explicit "
                "run-one-tick trigger before the next iteration.",
            )
            try:
                if stop_event is not None:
                    # Race the trigger against stop_event so shutdown
                    # doesn't get stuck waiting for a debug click that
                    # never comes.
                    trigger_task = asyncio.create_task(
                        _debug_run_once_trigger.wait()
                    )
                    stop_task = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait(
                        {trigger_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    if stop_event.is_set():
                        return
                else:
                    await _debug_run_once_trigger.wait()
            except asyncio.CancelledError:
                raise
            finally:
                # Always clear so the next trigger fire is required.
                _debug_run_once_trigger.clear()
            # If DEBUG_MODE was flipped off while we were waiting,
            # fall through to the normal main() call below. The next
            # loop iteration's DEBUG_MODE check will pick up the new
            # value and resume continuous looping.
        try:
            await main()
        except asyncio.CancelledError:
            # Cooperative cancellation — let it propagate so the task
            # ends cleanly. The plugin's shutdown() path relies on
            # this to unwind the tick loop on Bitcart restart.
            raise
        except Exception:
            # Any other exception: log and wait. Per the operator-
            # stated invariant, the main loop MUST continue running
            # even if something inside is broken. A flat 60-second
            # wait protects against tight-loops on persistent errors
            # without making transient hiccups too costly to recover
            # from.
            logger.exception(
                "Uncaught exception in main(); main loop will continue "
                "after 60-second backoff"
            )
            await asyncio.sleep(60)
        # Re-read these globals at loop tail: the plugin's settings
        # bridge may have flipped SINGLE_RUN while we were sleeping.
        if globals().get("SINGLE_RUN"):
            return
        if stop_event is not None and stop_event.is_set():
            return
        # Explicit yield: in production main() blocks on HTTP/IO so
        # the event loop is naturally serviced, but if a caller (or
        # test) patches main() to a fast coroutine that never awaits,
        # this loop would otherwise be CPU-tight and starve every
        # other coroutine on the same event loop — including the
        # timer that would set stop_event. Yielding is cheap; do it
        # always.
        await asyncio.sleep(0)


def _maybe_register_rig_teardown_hook() -> None:
    """Standalone-only test-rig escape hatch.

    When `LIQUIDITYHELPER_RIG_TEARDOWN_SCRIPT` env var is set to a
    valid file path, run that script when this Python process exits
    or receives SIGTERM. The fulltest PyCharm config sets it to
    `~/liquidityhelper_fulltest/local/stop_electrum.sh` so closing
    the run also kills the laptop's regtest Electrum GUI that
    port_forward.sh launched as a pre-launch hook.

    Why an env var instead of an unconditional cleanup: production
    deployments (the systemd-managed standalone install, the bitcart
    plugin install) run liquidityhelper without ever launching a
    laptop-side Electrum, so an unconditional cleanup would either
    no-op silently (best case) or invoke a nonexistent script and
    log a confusing error. Gating on an env var the rig EXPLICITLY
    sets keeps engine code production-clean: the hook is inert
    unless someone tells it which script to run.

    SIGTERM handling: Python's default behavior on SIGTERM is to
    raise SystemExit BUT it does NOT run atexit handlers reliably
    before the process dies (depends on signal-handler thread timing).
    We register a SIGTERM handler that calls the teardown synchronously
    so PyCharm's Stop button (which sends SIGTERM) reliably triggers
    Electrum cleanup. Other clean-exit paths (SINGLE_RUN=True returning,
    a normal exit, KeyboardInterrupt unwinding) flow through atexit
    which fires the same teardown.
    """
    import atexit
    import os
    import signal
    import subprocess

    script = os.environ.get("LIQUIDITYHELPER_RIG_TEARDOWN_SCRIPT", "")
    if not script or not os.path.isfile(script):
        return

    _teardown_done = False
    def _teardown() -> None:
        nonlocal _teardown_done
        if _teardown_done:
            return
        _teardown_done = True
        try:
            # Bounded — if the script hangs we don't want to delay
            # process exit indefinitely. 15s is generous; the
            # stop_electrum.sh helper completes in ~3s normally.
            subprocess.run([script], check=False, timeout=15)
        except Exception:
            pass

    atexit.register(_teardown)

    def _on_signal(signum: int, frame) -> None:    # noqa: ANN001
        _teardown()
        # Re-raise as default exit so the process actually dies after
        # cleanup (don't just return — that would resume whatever
        # asyncio.run was doing).
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_signal)
    # SIGINT is normally handled by Python's KeyboardInterrupt path,
    # which DOES flow through atexit. No special handling needed.


if __name__ == "__main__":
    _maybe_register_rig_teardown_hook()
    asyncio.run(run_tick_loop())
