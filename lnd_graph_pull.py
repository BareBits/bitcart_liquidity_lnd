"""LND-gossip-based node discovery (replaces the Magma scraper).

We pull two pieces of information from LND directly:

  1. DescribeGraph()                — the whole LN channel graph. We
     use it to enumerate candidate nodes, filter on capacity/channel
     count/age criteria, and decide who's worth refreshing in detail.
  2. GetNodeInfo(pubkey, include_channels=True) — per-node detail
     (addresses, total capacity, exact channel list) for the winning
     candidates only.

Both work identically in neutrino-backed LND. Gossip is a P2P protocol
over Lightning, independent of how LND tracks the Bitcoin chain.

Security stance
---------------
LN gossip is UNTRUSTED. Any node operator on the network can announce
arbitrary aliases, addresses, and feature bits. We treat every string
in a gossip message as adversarial input. Concretely:

  - Pubkeys must match a strict 66-char lowercase hex regex.
  - Addresses are anchored full-match against per-type regexes
    (IPv4: dotted-quad-with-port; IPv6: bracketed; tor v3: 56-char
    base32 + .onion + port). Anything else is dropped silently.
  - Numeric fields (capacity, channel count, last_update) are
    bounds-checked. Values outside sane physical ranges are rejected.
  - Inputs are length-capped before regex match so a pathological
    multi-KB "address" can't even trigger ReDoS exploration.

We never shell out, never eval/exec, never format strings into SQL
(peewee parameterises). Storage is JSON via the existing Peewee model.
The block-time estimator does NOT call out to any network or chain
service — it derives time from block height using the standard
600-second average. Tradeoff: ±~1 day worst case, plenty good enough
for the age-in-days criterion the script uses.
"""

from __future__ import annotations

import datetime
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Tuple

import grpc

from common_functions import utcnow_naive
from lnd_proto import lightning_pb2, lightning_pb2_grpc
from node_database import LightningNode

logger = logging.getLogger("liquidityhelper.lnd_graph_pull")


# ---------------------------------------------------------------------------
# Security: validation regexes + bounds
# ---------------------------------------------------------------------------

# Lightning pubkey: 33-byte compressed secp256k1 → 66 hex chars. LND
# uses lowercase in gossip, but for defence-in-depth we still anchor
# fully and case-fold before checking.
_PUBKEY_RE = re.compile(r"^[0-9a-f]{66}$")

# IPv4 host:port. Octet-range and port-range checked AFTER regex match.
_IPV4_HOST_PORT_RE = re.compile(
    r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3}):(\d{1,5})$"
)

# IPv6 host:port. LND emits IPv6 in bracketed form: "[2001:db8::1]:9735".
# We accept hex groups separated by colons inside the brackets; the
# "double-colon for omitted zeroes" shorthand is allowed.
_IPV6_HOST_PORT_RE = re.compile(
    r"^\[([0-9a-fA-F:]{2,45})\]:(\d{1,5})$"
)

# Tor v3 onion service: 56 chars base32 (lowercase letters a-z + digits
# 2-7) + ".onion" + ":" + port. Tor v2 (16-char) was deprecated in 2021
# and removed from the network; we don't accept it.
_TOR_V3_HOST_PORT_RE = re.compile(
    r"^([a-z2-7]{56})\.onion:(\d{1,5})$"
)

# DNS-style host:port (some operators announce e.g. "node.example.com:9735").
# We DON'T accept these — DNS resolution at gossip-ingest time would
# expose us to operator-controlled DNS rebinding, and the script doesn't
# actually need DNS connectivity (LND handles the connect-by-name when
# we ConnectPeer later). Keeping the regex unused here for clarity.

# Maximum length of any single address string we'll even inspect. LND's
# gossip serialisation caps an individual address well below this, but
# enforce a hard ceiling so even a malformed peer can't waste CPU on
# regex matching megabytes.
_MAX_ADDRESS_LEN = 256

# Maximum length for the alias field (LN protocol caps it at 32 bytes
# UTF-8 in the BOLT spec, but defence-in-depth means we double it for
# the application layer).
_MAX_ALIAS_LEN = 64

# Bounds for numeric fields. Bitcoin's total supply is ~21M BTC =
# 2.1e15 sats. A node's total capacity can never exceed that.
_MAX_TOTAL_CAPACITY_SAT = 21_000_000 * 100_000_000

# Defensive upper bound on channel count per node. The real network max
# today is ~5000 (ACINQ). 200_000 is far above any plausible value but
# still catches obviously corrupt gossip.
_MAX_CHANNEL_COUNT = 200_000

# Block height upper bound. Bitcoin is ~850k blocks today; in 100 years
# we'd be around ~6M. 100M is a comfortable ceiling.
_MAX_BLOCK_HEIGHT = 100_000_000


# ---------------------------------------------------------------------------
# Parsers — each returns None on bad input rather than raising. Callers
# treat None as "skip this datum" so we never let one malformed gossip
# field block ingestion of the rest.
# ---------------------------------------------------------------------------

def parse_pubkey(s: object) -> Optional[str]:
    """Return the lowercase 66-char hex pubkey if `s` matches; else None.

    Accepts only strings of exact length. Any deviation — wrong case,
    extra chars, prefix, embedded null — is rejected.
    """
    if not isinstance(s, str):
        return None
    if len(s) != 66:
        return None
    candidate = s.lower()
    if not _PUBKEY_RE.fullmatch(candidate):
        return None
    return candidate


def parse_ipv4_host_port(s: str) -> Optional[str]:
    """Return the validated 'a.b.c.d:port' string if all octets are in
    0..255 and port is 1..65535. Else None."""
    if not isinstance(s, str) or len(s) > _MAX_ADDRESS_LEN:
        return None
    m = _IPV4_HOST_PORT_RE.fullmatch(s)
    if not m:
        return None
    a, b, c, d, p = m.groups()
    octets = (int(a), int(b), int(c), int(d))
    if any(o < 0 or o > 255 for o in octets):
        return None
    port = int(p)
    if port < 1 or port > 65535:
        return None
    return f"{octets[0]}.{octets[1]}.{octets[2]}.{octets[3]}:{port}"


def parse_ipv6_host_port(s: str) -> Optional[str]:
    """Return validated '[host]:port' if the inside is a plausible IPv6
    literal and the port is in range."""
    if not isinstance(s, str) or len(s) > _MAX_ADDRESS_LEN:
        return None
    m = _IPV6_HOST_PORT_RE.fullmatch(s)
    if not m:
        return None
    host_part, p = m.groups()
    # No more than two consecutive colons (only "::" is allowed).
    if ":::" in host_part:
        return None
    # Limit hex groups; an IPv6 address has at most 8 groups of 4 hex
    # chars. With "::" we accept fewer groups; cap conservatively.
    groups = host_part.split(":")
    if len(groups) > 8:
        return None
    for g in groups:
        if g and len(g) > 4:   # "" allowed inside "::"
            return None
        if g and not re.fullmatch(r"[0-9a-fA-F]+", g):
            return None
    port = int(p)
    if port < 1 or port > 65535:
        return None
    return f"[{host_part.lower()}]:{port}"


def parse_tor_v3_host_port(s: str) -> Optional[str]:
    """Return validated tor v3 onion service address if it matches the
    56-char base32 pattern and port is in range. Tor v2 is deprecated
    and explicitly NOT accepted."""
    if not isinstance(s, str) or len(s) > _MAX_ADDRESS_LEN:
        return None
    m = _TOR_V3_HOST_PORT_RE.fullmatch(s.lower())
    if not m:
        return None
    addr, p = m.groups()
    port = int(p)
    if port < 1 or port > 65535:
        return None
    return f"{addr}.onion:{port}"


@dataclass
class ParsedAddresses:
    ipv4: Optional[str]
    ipv6: Optional[str]
    tor: Optional[str]


def extract_addresses(addresses: Iterable[lightning_pb2.NodeAddress]) -> ParsedAddresses:
    """Walk LND's repeated NodeAddress field and pick out the first
    valid address of each type. Anything that doesn't match a parser
    is silently dropped. Multiple addresses of the same type — first
    one wins; that's how LND tends to order announcements anyway."""
    ipv4 = None
    ipv6 = None
    tor = None
    for entry in addresses:
        # `entry.addr` is the operator-controlled string. `entry.network`
        # is also operator-controlled; we don't trust it for routing
        # decisions — we re-classify by trying the parsers in order.
        addr = getattr(entry, "addr", "")
        if not addr or not isinstance(addr, str):
            continue
        if len(addr) > _MAX_ADDRESS_LEN:
            continue
        if ipv4 is None:
            v = parse_ipv4_host_port(addr)
            if v:
                ipv4 = v
                continue
        if ipv6 is None:
            v = parse_ipv6_host_port(addr)
            if v:
                ipv6 = v
                continue
        if tor is None:
            v = parse_tor_v3_host_port(addr)
            if v:
                tor = v
                continue
    return ParsedAddresses(ipv4=ipv4, ipv6=ipv6, tor=tor)


def parse_alias(s: object) -> Optional[str]:
    """Return alias with control chars stripped, length-capped. None
    on non-string or empty input. We don't currently STORE alias —
    this exists for future use and for safe log emission."""
    if not isinstance(s, str):
        return None
    cleaned = "".join(c for c in s if c.isprintable())[:_MAX_ALIAS_LEN]
    return cleaned or None


def sane_capacity_sat(n: object) -> bool:
    return isinstance(n, int) and 0 <= n <= _MAX_TOTAL_CAPACITY_SAT


def sane_channel_count(n: object) -> bool:
    return isinstance(n, int) and 0 <= n <= _MAX_CHANNEL_COUNT


def sane_block_height(n: object) -> bool:
    return isinstance(n, int) and 0 <= n <= _MAX_BLOCK_HEIGHT


# ---------------------------------------------------------------------------
# Block-height → block-time estimation (neutrino-mode-friendly).
# ---------------------------------------------------------------------------

# Bitcoin block 0 (genesis) timestamp: 2009-01-03 18:15:05 UTC.
_GENESIS_BLOCK_TIME = 1231006505
# Difficulty retargets target 600s/block, but hashrate has grown
# monotonically since 2009, so actual blocks come in slightly faster
# than target on average. Empirical post-2009 average is ~575 sec/block
# (15.5 years to reach block 850_000 → 850000*x = ~15.5*365*86400 →
# x ≈ 575). Using this empirical figure keeps the estimate within
# ~30 days of truth, vs ~245 days with a naive 600.
_AVG_BLOCK_INTERVAL_SEC = 575


def estimate_block_time(block_height: int) -> Optional[datetime.datetime]:
    """Approximate the timestamp of block N as
        genesis_time + N * _AVG_BLOCK_INTERVAL_SEC (currently 575s).

    Using the empirical post-2009 average (575s/block, not the protocol
    target of 600s) keeps the estimate within ~30 days of truth — vs
    ~245 days with a naive 600. This is plenty good for the
    age-in-days candidate filter. Returns None for implausible heights.
    """
    if not sane_block_height(block_height):
        return None
    ts = _GENESIS_BLOCK_TIME + block_height * _AVG_BLOCK_INTERVAL_SEC
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).replace(tzinfo=None)


def block_height_from_channel_id(channel_id: int) -> Optional[int]:
    """Lightning's short-channel-id encoding packs the funding tx
    location into a single uint64:

        bits 63..40: block_height (24 bits)
        bits 39..16: tx index within the block (24 bits)
        bits 15..0:  output index within the tx (16 bits)

    We only care about the block_height. Returns None for clearly
    invalid IDs (negative, zero, above sanity bound)."""
    if not isinstance(channel_id, int) or channel_id <= 0:
        return None
    height = channel_id >> 40
    if not sane_block_height(height):
        return None
    return height


# ---------------------------------------------------------------------------
# DescribeGraph + GetNodeInfo client wrappers
# ---------------------------------------------------------------------------

# Default deadline for a single gRPC call. DescribeGraph can return a
# big graph; 60s gives slow nodes room without hanging the tick loop.
_GRAPH_RPC_TIMEOUT = 60


async def fetch_channel_graph(stub: lightning_pb2_grpc.LightningStub) -> lightning_pb2.ChannelGraph:
    """One DescribeGraph call, returning all public nodes + edges. We
    pass include_unannounced=False so we only see channels that gossip
    has confirmed; unannounced (private) channels would be useless
    anyway since they can't route external payments.

    Async because liquidityhelper builds its LightningStub from
    `grpc.aio.secure_channel`. Sync stubs would deadlock the event loop
    on the multi-MB DescribeGraph response."""
    req = lightning_pb2.ChannelGraphRequest(include_unannounced=False)
    return await stub.DescribeGraph(req, timeout=_GRAPH_RPC_TIMEOUT)


async def fetch_get_info(stub: lightning_pb2_grpc.LightningStub) -> lightning_pb2.GetInfoResponse:
    """LND's GetInfo. Used by the gossip-readiness gate to read
    synced_to_graph, uptime, and chains[0].network. Cheap (microseconds
    on a local stub) so we don't bother caching."""
    req = lightning_pb2.GetInfoRequest()
    return await stub.GetInfo(req, timeout=_GRAPH_RPC_TIMEOUT)


# Networks for which we skip the node-count check entirely. Testnet/
# signet/regtest/etc. legitimately have small graphs (a few dozen to
# a few hundred nodes); enforcing a mainnet-sized threshold there
# would block the pull permanently. We still enforce synced_to_graph
# and the uptime gate on every network — those signals are network-
# size-agnostic.
_NON_MAINNET_NETWORKS = frozenset({
    "testnet", "testnet3", "testnet4", "signet", "regtest", "simnet",
})


def evaluate_gossip_readiness(
    info: lightning_pb2.GetInfoResponse,
    graph: lightning_pb2.ChannelGraph,
    *,
    uptime_seconds: int,
    min_node_count: int,
    min_uptime_seconds: int,
) -> tuple[bool, str, dict]:
    """Returns (ok, reason, details).

    All three checks must pass for `ok=True`:
      1. info.synced_to_graph is True.
      2. uptime_seconds >= min_uptime_seconds. Uptime is supplied by
         the CALLER (typically the engine's self-tracker, which
         records the first time it saw a GetInfo response from this
         wallet's LND). The LND proto checked into this repo predates
         `GetInfoResponse.uptime`, so we can't read it from `info`.
      3. graph has >= min_node_count nodes — only on mainnet. Any
         testnet/signet/regtest variant skips this check.

    `details` is a dict suitable for logging: includes raw values of
    each signal so an operator looking at the decision log can see
    exactly why a pull was deferred."""
    # network is reported in chains[0].network; default to a string
    # we can match on without crashing if the field is missing.
    network = ""
    try:
        if info.chains:
            network = (info.chains[0].network or "").lower()
    except Exception:
        network = ""

    node_count = len(graph.nodes)
    synced_to_graph = bool(getattr(info, "synced_to_graph", False))

    details = {
        "network": network or "(unknown)",
        "synced_to_graph": synced_to_graph,
        "uptime_seconds": uptime_seconds,
        "node_count": node_count,
        "min_node_count_required": (
            min_node_count if network not in _NON_MAINNET_NETWORKS else 0
        ),
        "min_uptime_seconds_required": min_uptime_seconds,
    }

    if not synced_to_graph:
        return (
            False,
            "LND reports synced_to_graph=False — gossip subsystem "
            "still catching up",
            details,
        )
    if uptime_seconds < min_uptime_seconds:
        return (
            False,
            f"LND uptime {uptime_seconds}s is below the minimum "
            f"{min_uptime_seconds}s — daemon needs time to absorb "
            f"gossip updates. (Uptime is self-tracked since the proto "
            f"shipped in this repo predates GetInfoResponse.uptime; "
            f"a script restart resets the counter, which is the "
            f"conservative choice — we'd rather wait an extra 15s "
            f"after our own restart than trust gossip that might be "
            f"sparse.)",
            details,
        )
    # Node-count check: only enforced on mainnet. Non-mainnet variants
    # have legitimately small graphs.
    if network not in _NON_MAINNET_NETWORKS:
        if node_count < min_node_count:
            return (
                False,
                f"DescribeGraph returned only {node_count} nodes "
                f"(below the mainnet floor of {min_node_count}) — "
                f"refusing to recompute connectivity metrics against "
                f"an artificially sparse graph",
                details,
            )
    return (True, "ok", details)


async def fetch_node_info(
    stub: lightning_pb2_grpc.LightningStub, pubkey: str,
) -> Optional[lightning_pb2.NodeInfo]:
    """GetNodeInfo with include_channels=True. Returns None on RPC
    errors so the caller can skip and continue with other nodes."""
    if not parse_pubkey(pubkey):
        return None
    req = lightning_pb2.NodeInfoRequest(pub_key=pubkey, include_channels=True)
    try:
        return await stub.GetNodeInfo(req, timeout=_GRAPH_RPC_TIMEOUT)
    except grpc.RpcError as e:
        logger.info(
            "GetNodeInfo failed for %s: %s", pubkey[-8:], _safe_error_msg(e),
        )
        return None


def _safe_error_msg(e: grpc.RpcError) -> str:
    """Stringify a gRPC error without exposing the entire details
    blob (which contains operator-controlled content from the remote
    node's error response). Cap length."""
    try:
        details = e.details() or ""
    except Exception:
        details = ""
    return details[:200]


# ---------------------------------------------------------------------------
# Candidate filtering + upsert pipeline
# ---------------------------------------------------------------------------

@dataclass
class CandidateCriteria:
    """Filter applied to DescribeGraph nodes before we even fetch
    their detail. Values default to the script's existing
    NODE_CRITERIA_MINIMUM_* constants when called from production."""
    min_capacity_sat: int
    min_channel_count: int
    min_age_days: int
    # last_update freshness cutoff. Nodes whose most recent gossip
    # announcement is older than this are stale/abandoned and we skip
    # them. 14 days catches the "zombie channel" problem mentioned in
    # the neutrino-mode discussion (LND doesn't prune as aggressively
    # in neutrino mode; stale entries linger).
    max_node_announcement_age_days: int = 14


def iter_graph_candidates(
    graph: lightning_pb2.ChannelGraph,
    criteria: CandidateCriteria,
) -> Iterator[Tuple[str, int]]:
    """Walk the graph, yield (pubkey, channel_count) for nodes that
    pass the lightweight filters. Detail queries (GetNodeInfo) are
    expensive enough that we only want them for survivors.

    Channel count and total capacity are computed by walking the
    `edges` repeated field — DescribeGraph's `nodes` list doesn't
    carry those aggregates."""
    now_ts = int(time.time())
    max_age_sec = criteria.max_node_announcement_age_days * 86_400

    # Aggregate channel count + capacity per node from the edges list.
    channels_per_node: dict[str, int] = {}
    capacity_per_node: dict[str, int] = {}
    for edge in graph.edges:
        cap = edge.capacity
        if not sane_capacity_sat(cap):
            continue
        for pk_field in ("node1_pub", "node2_pub"):
            pk_raw = getattr(edge, pk_field, "")
            pk = parse_pubkey(pk_raw)
            if pk is None:
                continue
            channels_per_node[pk] = channels_per_node.get(pk, 0) + 1
            capacity_per_node[pk] = capacity_per_node.get(pk, 0) + cap

    # Now walk nodes, apply criteria.
    for node in graph.nodes:
        pk = parse_pubkey(getattr(node, "pub_key", ""))
        if pk is None:
            continue
        last_update = getattr(node, "last_update", 0)
        if not isinstance(last_update, int) or last_update <= 0:
            continue
        if now_ts - last_update > max_age_sec:
            continue
        chan_count = channels_per_node.get(pk, 0)
        if not sane_channel_count(chan_count):
            continue
        if chan_count < criteria.min_channel_count:
            continue
        cap = capacity_per_node.get(pk, 0)
        if not sane_capacity_sat(cap):
            continue
        if cap < criteria.min_capacity_sat:
            continue
        yield pk, chan_count


def derive_oldest_channel(node_info: lightning_pb2.NodeInfo) -> Optional[datetime.datetime]:
    """Walk node_info.channels, find the smallest valid block_height,
    convert to an approximate datetime. None if no parseable channel."""
    heights: list[int] = []
    for ch in node_info.channels:
        h = block_height_from_channel_id(getattr(ch, "channel_id", 0))
        if h is not None:
            heights.append(h)
    if not heights:
        return None
    return estimate_block_time(min(heights))


def derive_smallest_channel(node_info: lightning_pb2.NodeInfo) -> Optional[int]:
    """Smallest capacity among the node's announced channels. None
    when channels list is empty or contains only invalid entries."""
    caps = [
        getattr(ch, "capacity", 0)
        for ch in node_info.channels
        if sane_capacity_sat(getattr(ch, "capacity", 0))
    ]
    return min(caps) if caps else None


# Hard upper bound on fee rates we'll even record. LN fee rates above
# 100_000 ppm (10%) are either misconfiguration or a node trying to
# prevent routing through them; either way we don't want such values
# distorting our median. Cap above any plausible legitimate value.
_MAX_SANE_FEE_RATE_PPM = 100_000


def _outbound_fee_rate_for_node(
    edge: lightning_pb2.ChannelEdge, target_pubkey: str,
) -> Optional[int]:
    """For a channel edge where `target_pubkey` is one of the two ends,
    return the fee_rate_milli_msat that `target_pubkey` publishes for
    routing OUTBOUND on this channel.

    LND's ChannelEdge has node1_pub/node2_pub identifying the two ends,
    and node1_policy/node2_policy giving each side's policy. node1's
    policy describes what node1 charges to forward OUT through this
    channel. We pick the policy that matches `target_pubkey`.

    Returns None if:
      - target_pubkey isn't on this edge,
      - the matching policy is disabled (operator-signalled "don't
        route here"),
      - fee_rate_milli_msat is missing or insanely high.
    """
    node1 = parse_pubkey(getattr(edge, "node1_pub", ""))
    node2 = parse_pubkey(getattr(edge, "node2_pub", ""))
    target = parse_pubkey(target_pubkey)
    if target is None:
        return None
    if target == node1:
        policy = getattr(edge, "node1_policy", None)
    elif target == node2:
        policy = getattr(edge, "node2_policy", None)
    else:
        return None
    if policy is None:
        return None
    if getattr(policy, "disabled", False):
        # Operator has explicitly disabled this outbound direction.
        # Excluding it from the median both prevents disabled channels
        # from skewing the average (their fee rates can be unset/zero)
        # and avoids us optimistically counting capacity we can't use.
        return None
    rate = getattr(policy, "fee_rate_milli_msat", 0)
    if not isinstance(rate, int):
        return None
    if rate < 0 or rate > _MAX_SANE_FEE_RATE_PPM:
        return None
    return rate


# Freshness window for considering a gossip policy "live". Channels
# whose most recent policy update is older than this on BOTH sides
# look like zombies: announced once and forgotten. Same constant as
# the candidate-filter freshness window so the two checks agree.
_POLICY_FRESHNESS_SEC = 14 * 86_400


def _policy_outbound_usable(policy, now_ts: int) -> bool:
    """A `RoutingPolicy` lets the channel direction route outbound when:
      - the operator hasn't flipped `disabled`,
      - the policy has been gossip-updated at least once (last_update > 0),
      - that update is within the freshness window.
    Anything else, we treat the direction as not-usable for routing.
    """
    if policy is None:
        return False
    if getattr(policy, "disabled", False):
        return False
    last_update = getattr(policy, "last_update", 0)
    if not isinstance(last_update, int) or last_update <= 0:
        return False
    if now_ts - last_update > _POLICY_FRESHNESS_SEC:
        return False
    return True


def build_outbound_adjacency(
    graph: lightning_pb2.ChannelGraph,
) -> dict[str, set[str]]:
    """Build a DIRECTED adjacency map from the gossip graph.

    `adj[X]` is the set of pubkeys Y such that the channel between X
    and Y has X's outbound policy usable (enabled and fresh). This is
    the "from X, who can I route to?" view — different from an
    undirected channel-existence map.

    Returns an empty dict for an empty graph. Malformed pubkeys are
    silently skipped (defence-in-depth — gossip is untrusted).
    """
    now_ts = int(time.time())
    adj: dict[str, set[str]] = {}
    for edge in graph.edges:
        n1 = parse_pubkey(getattr(edge, "node1_pub", ""))
        n2 = parse_pubkey(getattr(edge, "node2_pub", ""))
        if not n1 or not n2 or n1 == n2:
            continue   # malformed pubkey or self-loop
        # node1's outbound on this edge is described by node1_policy.
        if _policy_outbound_usable(getattr(edge, "node1_policy", None), now_ts):
            adj.setdefault(n1, set()).add(n2)
        if _policy_outbound_usable(getattr(edge, "node2_policy", None), now_ts):
            adj.setdefault(n2, set()).add(n1)
    return adj


def compute_effective_degree(
    adj: dict[str, set[str]], pubkey: str,
) -> int:
    """Count of effective outbound edges from `pubkey`. Equivalent to
    the number of channels where `pubkey` can actually forward an HTLC."""
    return len(adj.get(pubkey, set()))


def compute_two_hop_reach(
    adj: dict[str, set[str]], pubkey: str,
) -> int:
    """Distinct nodes reachable from `pubkey` in 1 or 2 hops via the
    directed effective-adjacency graph. Excludes `pubkey` itself.

    For a candidate node we'd open a channel to, this approximates
    "how much of the network can payments leaving here reach without
    needing long routes." A high score correlates with good routing
    reliability for arbitrary destinations.

    Note: we sum INTO a set, so duplicates from many overlapping paths
    don't double-count. A node whose 100 peers all only know the same
    50 other nodes has reach = 100 + 50 - overlap (could be ~50).
    """
    peers = adj.get(pubkey, set())
    reached: set[str] = set(peers)
    for p in peers:
        reached.update(adj.get(p, set()))
    reached.discard(pubkey)
    return len(reached)


def derive_median_outbound_fee_rate(
    node_info: lightning_pb2.NodeInfo,
) -> Optional[int]:
    """Median fee_rate_milli_msat across the node's enabled outbound
    channel policies. Used by is_node_blacklisted's HIGH_FEE_RATE
    check and pick_best_channel_partners's within-bucket ordering.

    Returns None when no enabled outbound policies are available —
    in which case the node will be rejected as UNKNOWN_FEE_RATE.
    """
    target = parse_pubkey(getattr(node_info.node, "pub_key", ""))
    if target is None:
        return None
    rates: list[int] = []
    for edge in node_info.channels:
        rate = _outbound_fee_rate_for_node(edge, target)
        if rate is not None:
            rates.append(rate)
    if not rates:
        return None
    rates.sort()
    n = len(rates)
    # Integer median: lower-of-two for even counts. We're picking a
    # threshold-comparison value, not displaying a precise average;
    # the rounding direction doesn't matter operationally.
    return rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) // 2


def _outbound_min_htlc_for_node(
    edge: lightning_pb2.ChannelEdge, target_pubkey: str,
) -> Optional[int]:
    """Return target_pubkey's `min_htlc_msat` outbound policy on this
    edge, or None if not applicable.

    Same side-selection + disabled-skip semantics as
    _outbound_fee_rate_for_node. Note: LND's gRPC exposes the field
    as `min_htlc` for the millisat value (despite the bare name).
    Returns None on negative or absurdly high values (>1e12 msat =
    >10 BTC per HTLC; almost certainly misconfiguration).
    """
    node1 = parse_pubkey(getattr(edge, "node1_pub", ""))
    node2 = parse_pubkey(getattr(edge, "node2_pub", ""))
    target = parse_pubkey(target_pubkey)
    if target is None:
        return None
    if target == node1:
        policy = getattr(edge, "node1_policy", None)
    elif target == node2:
        policy = getattr(edge, "node2_policy", None)
    else:
        return None
    if policy is None or getattr(policy, "disabled", False):
        return None
    val = getattr(policy, "min_htlc", 0)
    if not isinstance(val, int):
        return None
    if val < 0 or val > 1_000_000_000_000:
        return None
    return val


def _outbound_max_htlc_for_node(
    edge: lightning_pb2.ChannelEdge, target_pubkey: str,
) -> Optional[int]:
    """Return target_pubkey's `max_htlc_msat` outbound policy on this
    edge, or None if not applicable.

    Distinguishes "explicitly 0" (== peer set no max — treat as
    missing, exclude from median) from a positive value. Caps at
    21M BTC × 100M sat × 1000 msat to reject corrupt values.
    """
    node1 = parse_pubkey(getattr(edge, "node1_pub", ""))
    node2 = parse_pubkey(getattr(edge, "node2_pub", ""))
    target = parse_pubkey(target_pubkey)
    if target is None:
        return None
    if target == node1:
        policy = getattr(edge, "node1_policy", None)
    elif target == node2:
        policy = getattr(edge, "node2_policy", None)
    else:
        return None
    if policy is None or getattr(policy, "disabled", False):
        return None
    val = getattr(policy, "max_htlc_msat", 0)
    if not isinstance(val, int):
        return None
    # max_htlc_msat == 0 means "not set" in older LND or "no max" in
    # some interpretations — treat as missing rather than as a literal
    # zero (literal zero would say "I refuse to forward anything",
    # which makes no sense as a configured value).
    if val <= 0:
        return None
    # Cap at 21M BTC × 100M sat × 1000 msat/sat as the absolute
    # ceiling — anything higher is corrupt.
    if val > 21_000_000 * 100_000_000 * 1000:
        return None
    return val


def derive_median_min_htlc_msat(
    node_info: lightning_pb2.NodeInfo,
) -> Optional[int]:
    """Median min_htlc (msat) across the node's enabled outbound
    policies. Used by HIGH_MIN_HTLC gate.

    Returns None when no enabled policies are available."""
    target = parse_pubkey(getattr(node_info.node, "pub_key", ""))
    if target is None:
        return None
    values: list[int] = []
    for edge in node_info.channels:
        v = _outbound_min_htlc_for_node(edge, target)
        if v is not None:
            values.append(v)
    if not values:
        return None
    values.sort()
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) // 2


def derive_median_max_htlc_msat(
    node_info: lightning_pb2.NodeInfo,
) -> Optional[int]:
    """Median max_htlc_msat across the node's enabled outbound
    policies. Used by LOW_MAX_HTLC gate.

    Returns None when no enabled policies have a positive max_htlc
    (peers that haven't set the field at all are excluded — see
    _outbound_max_htlc_for_node for the "0 = missing" treatment)."""
    target = parse_pubkey(getattr(node_info.node, "pub_key", ""))
    if target is None:
        return None
    values: list[int] = []
    for edge in node_info.channels:
        v = _outbound_max_htlc_for_node(edge, target)
        if v is not None:
            values.append(v)
    if not values:
        return None
    values.sort()
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) // 2


def upsert_lightning_node(
    pubkey: str,
    node_info: lightning_pb2.NodeInfo,
    *,
    min_age_days: int,
    effective_degree: Optional[int] = None,
    two_hop_reach: Optional[int] = None,
) -> Optional[LightningNode]:
    """Convert a validated NodeInfo into a LightningNode row, either
    inserting fresh or merging into the existing row. Returns the row
    that survived, or None if the data failed minimum validity.

    `effective_degree` and `two_hop_reach` are computed by the caller
    from the full graph (build_outbound_adjacency) — they're not
    derivable from NodeInfo alone since 2-hop reach needs the peers'
    own peer lists. Either may be None when called outside the daily
    pull (e.g. seed loaders); the blacklist will reject NULL as
    UNKNOWN_CONNECTEDNESS on the next selection pass."""
    pubkey = parse_pubkey(pubkey)
    if pubkey is None:
        return None

    total_capacity = getattr(node_info, "total_capacity", 0)
    num_channels = getattr(node_info, "num_channels", 0)
    if not sane_capacity_sat(total_capacity):
        return None
    if not sane_channel_count(num_channels):
        return None

    addresses = extract_addresses(getattr(node_info.node, "addresses", []))
    oldest = derive_oldest_channel(node_info)
    smallest = derive_smallest_channel(node_info)
    median_fee_ppm = derive_median_outbound_fee_rate(node_info)
    median_min_htlc = derive_median_min_htlc_msat(node_info)
    median_max_htlc = derive_median_max_htlc_msat(node_info)
    last_update_ts = getattr(node_info.node, "last_update", 0)
    last_seen = (
        datetime.datetime.fromtimestamp(last_update_ts, tz=datetime.timezone.utc).replace(tzinfo=None)
        if isinstance(last_update_ts, int) and last_update_ts > 0
        else None
    )

    # Age filter: reject if the oldest channel is too young. Done here
    # rather than in iter_graph_candidates because we need the
    # GetNodeInfo data to know channel ages.
    if oldest is not None:
        # `oldest` is UTC-naive (from estimate_block_time via fromtimestamp(..., tz=UTC).replace(tzinfo=None)).
        # Use utcnow_naive() for the comparison so the math is in the same timezone.
        age_days = (utcnow_naive() - oldest).days
        if age_days < min_age_days:
            return None

    existing = LightningNode.get_or_none(LightningNode.node_address == pubkey)
    if existing is None:
        node = LightningNode(
            node_address=pubkey,
            oldest_channel=oldest,
            number_of_channels=num_channels,
            total_capacity=total_capacity,
            smallest_channel_size=smallest,
            oldest_known_date=oldest or utcnow_naive(),
            tor_address=addresses.tor,
            ipv4_address=addresses.ipv4,
            ipv6_address=addresses.ipv6,
            last_lnd_query=utcnow_naive(),
            lnd_queries=1,
            last_seen_online=last_seen,
            median_outbound_fee_rate_ppm=median_fee_ppm,
            median_min_htlc_msat=median_min_htlc,
            median_max_htlc_msat=median_max_htlc,
            effective_channel_count=effective_degree,
            two_hop_reach=two_hop_reach,
        )
        node.set_oldest_known_date()
        node.save(force_insert=True)
        return node

    # Merge into existing: choose newer data for mutable fields,
    # preserve our local-only fields (uptime checks, close counts,
    # last_channel_creation_attempt).
    existing.number_of_channels = num_channels
    existing.total_capacity = total_capacity
    if smallest is not None:
        existing.smallest_channel_size = smallest
    if oldest is not None:
        # `oldest_channel` only shrinks — older channels never become
        # younger. Keep the older of the two.
        existing.oldest_channel = (
            min(existing.oldest_channel, oldest)
            if existing.oldest_channel else oldest
        )
    if addresses.tor:
        existing.tor_address = addresses.tor
    if addresses.ipv4:
        existing.ipv4_address = addresses.ipv4
    if addresses.ipv6:
        existing.ipv6_address = addresses.ipv6
    if last_seen and (
        existing.last_seen_online is None or last_seen > existing.last_seen_online
    ):
        existing.last_seen_online = last_seen
    # Always replace the median fee rate with the latest computed
    # value (or None if no enabled policies were visible this pull).
    # The blacklist treats None as UNKNOWN_FEE_RATE — operator chose
    # "reject unknown" — so a node that loses all its policies between
    # pulls correctly exits the candidate pool.
    existing.median_outbound_fee_rate_ppm = median_fee_ppm
    # Same policy for HTLC-limit metrics: latest value replaces
    # whatever was there. A node that raises its min_htlc or drops
    # its max_htlc between pulls correctly trips the gates next pass.
    existing.median_min_htlc_msat = median_min_htlc
    existing.median_max_htlc_msat = median_max_htlc
    # Same policy for connectedness metrics: latest value (possibly
    # None) replaces whatever was there. A node that lost most of its
    # effective edges between pulls correctly drops below the floor.
    existing.effective_channel_count = effective_degree
    existing.two_hop_reach = two_hop_reach
    existing.last_lnd_query = utcnow_naive()
    existing.lnd_queries = (existing.lnd_queries or 0) + 1
    existing.set_oldest_known_date()
    existing.save()
    return existing


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def merge_lightning_node(
    existing_node: LightningNode, new_node: LightningNode,
) -> None:
    """Generic merge utility for LightningNode rows: take a new
    (possibly partial) node row and layer its non-null fields onto
    the existing one. Used when bootstrapping from JSON seed lists.

    Field semantics:

      choose_larger_number — counters that only grow (queries, closes,
                             failed/total uptime checks).
      choose_oldest_date   — birthdate-like fields where older wins.
      choose_most_recent_date — last-seen-like fields where newer wins.
      choose_newer_value   — replace whatever's there (current snapshot).

    Saves the row.
    """
    choose_smaller_number: set[str] = set()
    choose_larger_number = {
        "lnd_queries", "remote_close_count",
        "failed_uptime_checks", "total_uptime_checks",
    }
    choose_oldest_date = {"oldest_known_date", "oldest_channel"}
    choose_most_recent_date = {"last_channel_creation_attempt", "last_seen_online"}
    choose_newer_value = {
        "number_of_channels", "total_capacity", "smallest_channel_size",
        "tor_address", "ipv4_address", "ipv6_address", "last_lnd_query",
    }
    choose_older_value: set[str] = set()

    for attribute_name, attribute_value in vars(new_node)["__data__"].items():
        if "__" in attribute_name or attribute_name.startswith("_"):
            continue
        if not attribute_value:
            continue
        existing_value = getattr(existing_node, attribute_name, None)
        if not existing_value:
            setattr(existing_node, attribute_name, attribute_value)
            continue
        if attribute_name in choose_newer_value:
            setattr(existing_node, attribute_name, attribute_value)
            continue
        if attribute_name in choose_older_value:
            continue
        if isinstance(attribute_value, (int, float)):
            if attribute_name in choose_smaller_number:
                setattr(existing_node, attribute_name,
                        min(existing_value, attribute_value))
            elif attribute_name in choose_larger_number:
                setattr(existing_node, attribute_name,
                        max(existing_value, attribute_value))
            else:
                logger.warning(
                    "merge_lightning_node: unhandled number %s=%s",
                    attribute_name, attribute_value,
                )
        elif isinstance(attribute_value, datetime.datetime):
            if attribute_name in choose_oldest_date:
                setattr(existing_node, attribute_name,
                        min(existing_value, attribute_value))
            elif attribute_name in choose_most_recent_date:
                setattr(existing_node, attribute_name,
                        max(existing_value, attribute_value))
            else:
                logger.warning(
                    "merge_lightning_node: unhandled datetime %s=%s",
                    attribute_name, attribute_value,
                )
        elif isinstance(attribute_value, str):
            # String fields land in `choose_newer_value` above, but
            # if a brand-new string field is added someday, replace
            # by default.
            setattr(existing_node, attribute_name, attribute_value)
    existing_node.set_oldest_known_date()
    existing_node.save()


async def refresh_single_node(
    stub: lightning_pb2_grpc.LightningStub,
    existing_node: LightningNode,
    *,
    min_age_days: int = 0,
) -> Optional[LightningNode]:
    """Re-pull a specific known node from LND.

    Returns the (possibly updated) LightningNode row, or None if the
    refresh failed validation."""
    pubkey = parse_pubkey(existing_node.node_address)
    if pubkey is None:
        return None
    info = await fetch_node_info(stub, pubkey)
    if info is None:
        return None
    return upsert_lightning_node(pubkey, info, min_age_days=min_age_days)


async def pull_and_upsert(
    stub: lightning_pb2_grpc.LightningStub,
    *,
    min_capacity_sat: int,
    min_channel_count: int,
    min_age_days: int,
    max_node_announcement_age_days: int = 14,
    max_nodes_per_run: int = 500,
    gossip_min_node_count: int = 250,
    gossip_min_uptime_seconds: int = 15,
    lnd_uptime_seconds: int = 0,
) -> dict:
    """Discover candidate Lightning nodes from gossip + persist them.

    Async: the gRPC channel built by liquidityhelper is `grpc.aio`, so
    every stub call must be awaited.

    Runs a readiness gate BEFORE upserting anything. The gate checks
    LND.GetInfo.synced_to_graph, GetInfo.uptime, and (on mainnet only)
    DescribeGraph node count. If any signal fails, the function
    returns early with `skipped=True` in stats and writes NOTHING to
    the candidate DB — protecting the previously-stored connectivity
    metrics from being recomputed against a sparse adjacency.

    Returns a stats dict for logging/observability:
      { skipped: bool, skip_reason: str|None, readiness: dict,
        total_graph_nodes, candidates_after_lightweight_filter,
        upserted, skipped_get_node_info, skipped_validation }
    """
    criteria = CandidateCriteria(
        min_capacity_sat=min_capacity_sat,
        min_channel_count=min_channel_count,
        min_age_days=min_age_days,
        max_node_announcement_age_days=max_node_announcement_age_days,
    )
    stats: dict = {
        "skipped": False,
        "skip_reason": None,
        "readiness": {},
        "total_graph_nodes": 0,
        "candidates_after_lightweight_filter": 0,
        "upserted": 0,
        "skipped_get_node_info": 0,
        "skipped_validation": 0,
    }
    # Readiness pre-flight. GetInfo first (cheap) then DescribeGraph
    # (potentially expensive). If GetInfo fails we can't make the
    # readiness decision either way — treat as a hard skip.
    try:
        info = await fetch_get_info(stub)
    except grpc.RpcError as e:
        logger.error(
            "GetInfo failed during gossip pre-flight: %s",
            _safe_error_msg(e),
        )
        stats["skipped"] = True
        stats["skip_reason"] = "GetInfo failed during pre-flight"
        return stats
    try:
        graph = await fetch_channel_graph(stub)
    except grpc.RpcError as e:
        logger.error("DescribeGraph failed: %s", _safe_error_msg(e))
        stats["skipped"] = True
        stats["skip_reason"] = "DescribeGraph failed"
        return stats
    ok, reason, details = evaluate_gossip_readiness(
        info, graph,
        uptime_seconds=lnd_uptime_seconds,
        min_node_count=gossip_min_node_count,
        min_uptime_seconds=gossip_min_uptime_seconds,
    )
    stats["readiness"] = details
    stats["total_graph_nodes"] = len(graph.nodes)
    if not ok:
        stats["skipped"] = True
        stats["skip_reason"] = reason
        logger.warning(
            "gossip pull skipped: %s; signals=%s", reason, details,
        )
        return stats
    # Build the directed adjacency map ONCE from the full graph. We
    # reuse it for every candidate's effective_degree and 2-hop reach
    # computation, so the per-candidate cost stays bounded.
    adjacency = build_outbound_adjacency(graph)
    candidates = list(iter_graph_candidates(graph, criteria))[:max_nodes_per_run]
    stats["candidates_after_lightweight_filter"] = len(candidates)
    for pubkey, _ in candidates:
        node_info = await fetch_node_info(stub, pubkey)
        if node_info is None:
            stats["skipped_get_node_info"] += 1
            continue
        effective_degree = compute_effective_degree(adjacency, pubkey)
        reach = compute_two_hop_reach(adjacency, pubkey)
        result = upsert_lightning_node(
            pubkey, node_info, min_age_days=min_age_days,
            effective_degree=effective_degree, two_hop_reach=reach,
        )
        if result is None:
            stats["skipped_validation"] += 1
        else:
            stats["upserted"] += 1
    logger.info(
        "lnd_graph_pull: %s", stats,
    )
    return stats
