"""LSPS1 provider abstraction and concrete REST implementations.

Two providers wired in this module: Zeus (lnolymp.us) and Megalithic.
Both speak the LSPS1 REST API (https://docs.zeusln.app/lsp/services/lsps1,
https://docs.megalithic.me/lightning-services/lsp1-get-inbound-liquidity-for-mobile-clients/).
LSPS1's BOLT8 message form is deliberately not implemented here; the ABC
is structured so a future BOLT8Provider could subclass alongside Zeus
and Megalithic without touching callers.

Each provider:
  - Knows which Bitcoin networks the LSP supports (mainnet/testnet/mutinynet/etc).
  - Exposes an async `get_info()` that returns the LSP's pricing parameters
    and supported channel-size range (LSPS1 spec).
  - Exposes an async `create_order()` that requests a channel of a given
    size + expiry. Returns an `LspQuote` carrying the price, the on-chain
    payment address, and the LN invoice. Each create_order call DOES
    register a real (abandonable) order on the LSP — the higher-level
    throttle in liquidityhelper enforces "no more than one quote per LSP
    per wallet per day" to keep us from spamming their order book.
  - Exposes an async `get_order()` to poll order state.

Callers should not import this module's REST internals directly; they
should use the provider instances via `get_lsp_providers()`. Tests
substitute mock instances via the same registry.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("liquidityhelper.lsp_providers")


# ---------------------------------------------------------------------------
# Bitcoin networks the script may run against, normalized as strings.
# These match the values LND's `GetInfo.chains[0].network` returns. The
# per-provider supported_networks lists below map these to LSP-side names.
# ---------------------------------------------------------------------------

# Network identifiers we use internally (LND's vocabulary)
NETWORK_MAINNET = "mainnet"
NETWORK_TESTNET = "testnet"
NETWORK_SIGNET = "signet"
NETWORK_REGTEST = "regtest"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class LspInfo:
    """Subset of LSPS1 get_info we actually consume."""
    min_channel_balance_sat: int       # LSP-side minimum (= our minimum inbound)
    max_channel_balance_sat: int
    max_channel_expiry_blocks: int
    min_required_channel_confirmations: int
    min_funding_confirms_within_blocks: int
    # Current peer URIs the LSP wants clients to connect to. Per LSPS1
    # spec this is the authoritative source — pubkeys here can be
    # rotated by the LSP without changing API URLs. The hardcoded URI
    # in `network_endpoints` is a startup fallback only.
    uris: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LspQuote:
    """A real, abandonable LSPS1 order. Contains everything we need to
    decide whether to pay it, and the payment endpoints if we do.
    """
    provider: str               # "zeus", "megalithic", ...
    network: str                # provider-side network name as returned
    order_id: str
    lsp_peer_pubkey: str        # hex, lowercase (the LSP's node id)
    lsp_peer_uri: str           # pubkey@host:port
    lsp_balance_sat: int        # the channel inbound we requested
    fee_total_sat: int          # the LSP's price for the service
    order_total_sat: int        # fee + any client_balance_sat we requested
    channel_expiry_blocks: int
    onchain_address: str        # where we pay (the on-chain payment path)
    bolt11_invoice: str         # alternate LN payment path; we don't use it today
    expires_at: Optional[datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LspOrderStatus:
    """LSPS1 get_order response — used to poll after payment."""
    order_id: str
    state: str                  # CREATED|EXPECT_PAYMENT|PAID|OPENING|COMPLETED|FAILED
    channel_point: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider ABC
# ---------------------------------------------------------------------------

class LSPProvider(ABC):
    """REST/BOLT8-agnostic provider interface. Concrete subclasses are
    REST-only today; future BOLT8 implementations bind the same surface."""

    # short stable id used in logs, DB rows, and the Zeus-preference rule
    name: str = "abstract"

    @abstractmethod
    def supported_networks(self) -> List[str]:
        """LND-vocabulary network names this provider can serve (e.g.
        ["mainnet", "testnet"]). Used to skip providers that can't speak
        the wallet's network."""

    @abstractmethod
    def lsp_peer_uri(self, *, network: str) -> str:
        """pubkey@host:port for the LSP's lightning peer on the given
        network. Used for diagnostics + storing into LspChannelOrder."""

    @abstractmethod
    async def get_info(self, *, network: str) -> LspInfo:
        """LSPS1 GET /api/v1/get_info"""

    @abstractmethod
    async def create_order(
        self,
        *,
        network: str,
        public_key: str,                  # client's node pubkey hex (or pubkey@uri)
        lsp_balance_sat: int,
        channel_expiry_blocks: int,
        refund_onchain_address: str = "",
        announce_channel: bool = False,
    ) -> LspQuote:
        """LSPS1 POST /api/v1/create_order. Registers a real order."""

    @abstractmethod
    async def get_order(
        self, *, network: str, order_id: str,
    ) -> LspOrderStatus:
        """LSPS1 GET /api/v1/get_order"""


# ---------------------------------------------------------------------------
# Shared REST plumbing for LSPS1-over-HTTPS providers
# ---------------------------------------------------------------------------

@dataclass
class _Endpoint:
    base_url: str
    lsp_peer_uri: str


class _RestLSPProvider(LSPProvider):
    """Concrete LSPS1-over-REST mixin. Subclasses just supply the
    network -> endpoint table; everything else is generic JSON shuffling.

    Path convention: `base_url` includes the full API prefix (e.g.
    `https://lsps1.lnolymp.us/api/v1` for Zeus, `.../api/lsps1/v1` for
    Megalithic). Internal `_get`/`_post` use relative paths
    (`get_info`, `create_order`, `get_order`).
    """

    # Subclasses override:
    name = "abstract-rest"
    network_endpoints: Dict[str, _Endpoint] = {}

    _http_timeout_sec: float = 30.0

    def __init__(self) -> None:
        # Per-network cache of every plausible peer URI for this LSP.
        # Includes the hardcoded fallback from network_endpoints AND
        # every URI reported by get_info().uris. Populated lazily by
        # get_all_peer_uris; never explicitly invalidated, since LSP
        # pubkey rotations are rare and a script restart is an
        # acceptable resync trigger.
        self._cached_uris: Dict[str, List[str]] = {}

    def supported_networks(self) -> List[str]:
        return list(self.network_endpoints.keys())

    def lsp_peer_uri(self, *, network: str) -> str:
        """Static fallback peer URI for this network. Use
        `get_active_peer_uri` for the live, get_info-sourced value."""
        endpoint = self.network_endpoints.get(network)
        if endpoint is None:
            raise ValueError(
                f"{self.name} does not support network={network!r}; "
                f"supported={self.supported_networks()}"
            )
        return endpoint.lsp_peer_uri

    async def get_all_peer_uris(self, *, network: str) -> List[str]:
        """Return every plausible peer URI for `network`: the hardcoded
        fallback in `network_endpoints` PLUS every URI returned by
        `get_info().uris`. Deduplicated, with the `UNKNOWN@...` sentinel
        excluded.

        Rationale: ConnectPeer is idempotent and free, and there's no
        downside to dialing peers that turn out to be stale — LND
        either succeeds, says "already connected", or fails politely.
        By dialing the union of (docs URI, get_info URIs) we tolerate:
          - A rotated LSP pubkey (get_info has the new one, hardcoded
            still works if connection is alive).
          - get_info briefly returning empty (hardcoded covers us).
          - An LSP that publishes multiple peer URIs (LSPS1 spec
            permits this).

        Cached for the lifetime of the provider instance to avoid
        per-tick get_info traffic. Script restart re-fetches.
        """
        if network in self._cached_uris:
            return self._cached_uris[network]

        uris: List[str] = []

        # Hardcoded fallback first
        endpoint = self.network_endpoints.get(network)
        if endpoint is not None:
            fb = endpoint.lsp_peer_uri
            if fb and not fb.startswith("UNKNOWN@"):
                uris.append(fb)

        # Dynamic from get_info
        try:
            info = await self.get_info(network=network)
            for u in (info.uris or []):
                u = str(u).strip()
                if not u or u.startswith("UNKNOWN@"):
                    continue
                if u not in uris:
                    uris.append(u)
            if info.uris:
                logger.info(
                    "%s get_info returned %d peer URI(s) for network=%s",
                    self.name, len(info.uris), network,
                )
        except Exception as e:
            logger.warning(
                "%s get_info failed for network=%s: %s; using hardcoded "
                "URI(s) only", self.name, network, e,
            )

        self._cached_uris[network] = uris
        return uris

    async def get_active_peer_uri(self, *, network: str) -> str:
        """Compatibility shim: return the first URI from
        `get_all_peer_uris`, or the hardcoded fallback if the list is
        empty. Kept for callers that genuinely want a single URI; the
        peer-connect flow uses `get_all_peer_uris` directly so it can
        dial every advertised peer."""
        uris = await self.get_all_peer_uris(network=network)
        if uris:
            return uris[0]
        # Empty list means: hardcoded was UNKNOWN AND get_info had no
        # uris. Return the raw fallback so callers can still inspect
        # it (and detect the sentinel themselves if they care).
        return self.network_endpoints[network].lsp_peer_uri

    def _endpoint(self, network: str) -> _Endpoint:
        endpoint = self.network_endpoints.get(network)
        if endpoint is None:
            raise ValueError(
                f"{self.name} does not support network={network!r}; "
                f"supported={self.supported_networks()}"
            )
        return endpoint

    def _url(self, network: str, path: str) -> str:
        base = self._endpoint(network).base_url.rstrip("/")
        return base + "/" + path.lstrip("/")

    async def _get(self, network: str, path: str, *, params=None) -> Dict[str, Any]:
        url = self._url(network, path)
        timeout = aiohttp.ClientTimeout(total=self._http_timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _post(self, network: str, path: str, *, json: Dict[str, Any]) -> Dict[str, Any]:
        url = self._url(network, path)
        timeout = aiohttp.ClientTimeout(total=self._http_timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=json) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get_info(self, *, network: str) -> LspInfo:
        body = await self._get(network, "get_info")
        options = body.get("options", body)
        uris_raw = body.get("uris") or options.get("uris") or []
        uris = [str(u) for u in uris_raw if u]
        return LspInfo(
            min_channel_balance_sat=int(options.get("min_channel_balance_sat", 0)),
            max_channel_balance_sat=int(options.get("max_channel_balance_sat", 0)),
            max_channel_expiry_blocks=int(options.get("max_channel_expiry_blocks", 0)),
            min_required_channel_confirmations=int(
                options.get("min_required_channel_confirmations", 0)
            ),
            min_funding_confirms_within_blocks=int(
                options.get("min_funding_confirms_within_blocks", 0)
            ),
            uris=uris,
            raw=body,
        )

    async def create_order(
        self,
        *,
        network: str,
        public_key: str,
        lsp_balance_sat: int,
        channel_expiry_blocks: int,
        refund_onchain_address: str = "",
        announce_channel: bool = False,
    ) -> LspQuote:
        # public_key may arrive as 'pubkey@host:port'; LSPS1 wants just hex.
        pubkey_hex = public_key.split("@")[0].lower()
        payload: Dict[str, Any] = {
            "public_key": pubkey_hex,
            "lsp_balance_sat": str(lsp_balance_sat),
            "client_balance_sat": "0",
            "required_channel_confirmations": 3,
            "funding_confirms_within_blocks": 6,
            "channel_expiry_blocks": channel_expiry_blocks,
            "announce_channel": announce_channel,
        }
        if refund_onchain_address:
            payload["refund_onchain_address"] = refund_onchain_address

        body = await self._post(network, "create_order", json=payload)
        payment = body.get("payment") or {}
        endpoint = self._endpoint(network)
        peer_uri = endpoint.lsp_peer_uri
        peer_pubkey = peer_uri.split("@")[0].lower()
        return LspQuote(
            provider=self.name,
            network=network,
            order_id=body["order_id"],
            lsp_peer_pubkey=peer_pubkey,
            lsp_peer_uri=peer_uri,
            lsp_balance_sat=int(body.get("lsp_balance_sat", lsp_balance_sat)),
            fee_total_sat=int(payment.get("fee_total_sat") or 0),
            order_total_sat=int(payment.get("order_total_sat") or 0),
            channel_expiry_blocks=int(body.get("channel_expiry_blocks", channel_expiry_blocks)),
            onchain_address=str(payment.get("onchain_address") or ""),
            bolt11_invoice=str(payment.get("bolt11_invoice") or ""),
            raw=body,
        )

    async def get_order(
        self, *, network: str, order_id: str,
    ) -> LspOrderStatus:
        body = await self._get(
            network, "get_order", params={"order_id": order_id}
        )
        channel = body.get("channel") or {}
        return LspOrderStatus(
            order_id=body.get("order_id", order_id),
            state=str(body.get("order_state") or "UNKNOWN"),
            channel_point=channel.get("funding_outpoint"),
            raw=body,
        )


# ---------------------------------------------------------------------------
# Concrete providers
# ---------------------------------------------------------------------------

class ZeusProvider(_RestLSPProvider):
    """Zeus LSPS1 (lnolymp.us). Verified against
    https://docs.zeusln.app/lsp/services/lsps1. Three networks:

      - mainnet  → lsps1.lnolymp.us
      - testnet  → testnet-lsps1.lnolymp.us. IMPORTANT: this serves
        testnet3 specifically — Zeus has not stood up a testnet4
        endpoint. A wallet on testnet4 will get chain-hash mismatches
        even though both report "testnet" under our internal label.
        lsp_network_for_wallet emits a WARNING decision log when it
        sees testnet4 to flag this for the operator.
      - signet   → mutinynet-lsps1.lnolymp.us. CAVEAT: this is the
        fast-block Mutinynet variant of signet, NOT the official
        Bitcoin signet (slow blocks). Wallets on real signet will
        receive Mutinynet responses and channel-open failures from
        chain-hash mismatches at the LSP side. There's no public
        Zeus endpoint for real signet at time of writing.

    Peer URIs here are the static fallback — get_active_peer_uri()
    prefers the URI returned by get_info."""
    name = "zeus"
    network_endpoints: Dict[str, _Endpoint] = {
        NETWORK_MAINNET: _Endpoint(
            base_url="https://lsps1.lnolymp.us/api/v1",
            lsp_peer_uri=(
                "031b301307574bbe9b9ac7b79cbe1700e31e544513eae0b5d7497483083f99e581"
                "@45.79.192.236:9735"
            ),
        ),
        NETWORK_TESTNET: _Endpoint(
            base_url="https://testnet-lsps1.lnolymp.us/api/v1",
            lsp_peer_uri=(
                "03e84a109cd70e57864274932fc87c5e6434c59ebb8e6e7d28532219ba38f7f6df"
                "@139.144.22.237:9735"
            ),
        ),
        NETWORK_SIGNET: _Endpoint(
            base_url="https://mutinynet-lsps1.lnolymp.us/api/v1",
            lsp_peer_uri=(
                "032ae843e4d7d177f151d021ac8044b0636ec72b1ce3ffcde5c04748db2517ab03"
                "@45.79.201.241:9735"
            ),
        ),
    }


class MegalithicProvider(_RestLSPProvider):
    """Megalithic LSPS1. Verified against
    https://docs.megalithic.me/lightning-services/lsp1-get-inbound-liquidity-for-mobile-clients/
    on 2026-05-18. Two networks:

      - mainnet → megalithic.me. Standard production endpoint.
      - signet  → lsp1.mutiny.megalith-node.com. CAVEAT: this is
        Mutinynet (fast-block signet variant), NOT the official
        Bitcoin signet. Wallets on real signet would get Mutinynet
        responses. There's no public Megalithic endpoint for real
        signet at time of writing.

    Megalithic does NOT serve testnet (testnet3 or testnet4). Wallets
    on any testnet that try to use this provider will be filtered out
    by `supported_networks()` with a decision log; check the LSP
    compatibility pre-flight summary at startup.

    API path prefix is `/api/lsps1/v1` — distinct from Zeus's `/api/v1`.
    Peer URI is dynamic and authoritatively sourced from get_info().uris;
    the values here are fallback only.
    """
    name = "megalithic"
    network_endpoints: Dict[str, _Endpoint] = {
        NETWORK_MAINNET: _Endpoint(
            base_url="https://megalithic.me/api/lsps1/v1",
            lsp_peer_uri=(
                "03e30fda71887a916ef5548a4d02b06fe04aaa1a8de9e24134ce7f139cf79d7579"
                "@64.23.162.51:9736"
            ),
        ),
        NETWORK_SIGNET: _Endpoint(
            base_url="https://lsp1.mutiny.megalith-node.com/api/lsps1/v1",
            # Megalithic doesn't publish a static Mutinynet pubkey;
            # get_active_peer_uri will source it dynamically from get_info.
            # If get_info fails we have no usable fallback — operator
            # would see a "connect failed" decision-log entry and need
            # to investigate.
            lsp_peer_uri="UNKNOWN@lsp1.mutiny.megalith-node.com:9735",
        ),
    }


# ---------------------------------------------------------------------------
# Provider registry — module-singleton, swappable for tests
# ---------------------------------------------------------------------------

_PROVIDERS: Optional[List[LSPProvider]] = None


def get_lsp_providers() -> List[LSPProvider]:
    """Returns the active provider list. Lazily-built, cached. Tests
    replace via `set_lsp_providers([...])` to inject mocks."""
    global _PROVIDERS
    if _PROVIDERS is None:
        _PROVIDERS = [ZeusProvider(), MegalithicProvider()]
    return _PROVIDERS


def set_lsp_providers(providers: List[LSPProvider]) -> None:
    """Test hook. Replace the module-singleton provider list."""
    global _PROVIDERS
    _PROVIDERS = list(providers)


def reset_lsp_providers() -> None:
    """Test cleanup: forget whatever was last set, next call rebuilds."""
    global _PROVIDERS
    _PROVIDERS = None
