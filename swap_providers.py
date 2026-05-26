"""
Submarine swap providers.

Currently implements reverse swaps (LN -> on-chain) via Lightning Labs' `loop`.
The abstract `SwapProvider` interface lets us plug in additional providers
(Boltz, etc.) later without touching the orchestration code in
`liquidityhelper.py`.

Layout per LND wallet:
    ./loop_bin/loop          # CLI client (downloaded once on first use)
    ./loop_bin/loopd         # daemon (downloaded once on first use)
    ./loop_data/<wallet_id>/
        lnd-tls.cert         # copy of the wallet's LND TLS cert (loopd reads)
        lnd-admin.macaroon   # copy of the wallet's LND admin macaroon
        <network>/           # loopd's own state dir (mainnet/testnet/signet/regtest, set by LOOPD_NETWORK)
            tls.cert
            tls.key
            macaroons/
                loop.macaroon
            ...

Wiring in production:
  - `LOOP_OUT_ENABLED=False` (default) — `find_loop_out_candidates`
    runs each tick and logs which channels would be candidates; no
    loopd is started and no swap is initiated.
  - `LOOP_OUT_ENABLED=True` — `liquidityhelper._drain_ln_for_cashout_if_enabled`
    (called from the do_cashouts loop) invokes `drain_ln_to_onchain`, which
    uses LoopdManager to spawn a per-wallet loopd subprocess and initiate
    the swap. Triggered when
    LN cashouts have been failing past the staleness threshold OR
    when PREFER_CASHOUT_ONCHAIN=True. Tests can also call the swap
    functions directly to bypass the gate.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import logging
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import grpc
import httpx
from google.protobuf.json_format import MessageToDict

from loop_proto import client_pb2, client_pb2_grpc

if TYPE_CHECKING:
    from classes import BitcartAPI

logger = logging.getLogger(__name__)

LOOP_VERSION = "v0.31.1-beta"
PROJECT_ROOT = Path(__file__).resolve().parent
LOOP_BIN_DIR = PROJECT_ROOT / "loop_bin"
LOOP_DATA_DIR = PROJECT_ROOT / "loop_data"


# ---------------------------------------------------------------------------
# Provider interface (extensible to Boltz / others)
# ---------------------------------------------------------------------------


class SwapDirection(Enum):
    OUT = "out"   # LN -> on-chain (reverse submarine swap)
    IN = "in"     # on-chain -> LN (forward submarine swap)  -- reserved for later


@dataclass
class SwapQuote:
    provider: str
    direction: SwapDirection
    amount_sat: int
    swap_fee_sat: int       # provider's flat + ppm fee
    miner_fee_sat: int      # est on-chain fees (for loop-out: HTLC sweep only; HTLC publish is server-side)
    total_fee_sat: int      # swap_fee_sat + miner_fee_sat
    fee_percent: float      # total_fee_sat / amount_sat
    # Optional raw provider response for debugging / audit
    raw: Optional[Dict[str, Any]] = None


@dataclass
class SwapResult:
    provider: str
    swap_id: str            # 64-char hex (the swap's payment_hash)
    direction: SwapDirection
    amount_sat: int
    total_fee_sat: int
    htlc_address: str       # on-chain HTLC address loopd published
    state: str              # provider-reported state (e.g. "INITIATED")


class SwapProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def quote_out(self, amount_sat: int) -> Optional[SwapQuote]:
        """Ask the provider what a reverse swap of `amount_sat` would cost.
        Returns None if the provider rejects the amount (out of bounds, etc.)."""

    @abstractmethod
    async def initiate_out(
        self,
        wallet: Dict[str, Any],
        api: "BitcartAPI",
        amount_sat: int,
        dest_addr: str,
    ) -> Optional[SwapResult]:
        """Initiate a reverse swap. Returns SwapResult once the swap is
        accepted by the server (not necessarily settled on-chain). Returns
        None if the provider refuses the request."""


# ---------------------------------------------------------------------------
# Loop binary download
# ---------------------------------------------------------------------------


def _platform_tag() -> str:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    if sysname == "linux" and machine in ("x86_64", "amd64"):
        return "linux-amd64"
    if sysname == "linux" and machine in ("aarch64", "arm64"):
        return "linux-arm64"
    if sysname == "darwin" and machine in ("x86_64", "amd64"):
        return "darwin-amd64"
    if sysname == "darwin" and machine in ("arm64", "aarch64"):
        return "darwin-arm64"
    raise RuntimeError(f"Unsupported platform: {sysname}/{machine}")


def _extract_loop_binaries(tarball_path: Path, bin_dir: Path) -> None:
    """Sync helper: extract `loop` and `loopd` from the downloaded
    tarball. Pulled out so the download path can run it via
    asyncio.to_thread — tarfile has no async API, and decompressing a
    ~30MB tarball synchronously would block the event loop for tens of
    ms on slower hardware."""
    with tarfile.open(tarball_path, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            basename = Path(m.name).name
            if basename in ("loop", "loopd"):
                src = tar.extractfile(m)
                if src is None:
                    continue
                dest = bin_dir / basename
                with open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                dest.chmod(0o755)


async def ensure_loop_binaries(bin_dir: Path = LOOP_BIN_DIR) -> Dict[str, Path]:
    """Download `loop` and `loopd` into bin_dir if not already there.
    Idempotent / cached.

    Async because the download blocks for seconds-to-minutes on a slow
    network, and in plugin mode this freezes the entire Bitcart worker.
    The download streams chunk-by-chunk via httpx.AsyncClient; the
    tar extraction (sync stdlib API) is offloaded to a worker thread.

    Returns paths keyed by binary name.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    loop_bin = bin_dir / "loop"
    loopd_bin = bin_dir / "loopd"
    if loop_bin.exists() and loopd_bin.exists():
        return {"loop": loop_bin, "loopd": loopd_bin}

    plat = _platform_tag()
    # Strip the leading "v" for the tarball stem.
    stem = LOOP_VERSION.lstrip("v")
    url = (
        f"https://github.com/lightninglabs/loop/releases/download/"
        f"{LOOP_VERSION}/loop-{plat}-v{stem}.tar.gz"
    )
    logger.info(f"downloading loop {LOOP_VERSION} from {url}")
    tarball_path = bin_dir / "loop.tar.gz"
    # follow_redirects=True: GitHub release assets 302 to an S3 URL.
    # Without this, httpx would return the redirect response and the
    # extracted tarball would be ~256 bytes of HTML.
    async with httpx.AsyncClient(
        timeout=300.0, follow_redirects=True,
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(tarball_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

    await asyncio.to_thread(_extract_loop_binaries, tarball_path, bin_dir)

    tarball_path.unlink(missing_ok=True)
    if not (loop_bin.exists() and loopd_bin.exists()):
        raise RuntimeError(
            f"loop tarball didn't contain expected binaries; check {url}"
        )
    return {"loop": loop_bin, "loopd": loopd_bin}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Per-wallet loopd subprocess + gRPC client
# ---------------------------------------------------------------------------


@dataclass
class LoopdInstance:
    wallet_id: str
    bin_dir: Path
    data_dir: Path
    lnd_grpc_host: str            # e.g. "127.0.0.1:10009"
    lnd_tls_cert_bytes: bytes
    lnd_macaroon_bytes: bytes
    network: str                  # "regtest" | "signet" | "testnet" | "mainnet"
    # Optional overrides — test harness sets these to point loopd at the
    # regtest loopserver instead of the real Lightning Labs production server.
    server_host: Optional[str] = None
    server_notls: bool = False

    rpc_port: int = field(default_factory=_free_port)
    rest_port: int = field(default_factory=_free_port)
    proc: Optional[asyncio.subprocess.Process] = None
    _channel: Optional[grpc.aio.Channel] = None
    _swap_stub: Optional[client_pb2_grpc.SwapClientStub] = None

    @property
    def loop_macaroon_path(self) -> Path:
        # loopd writes its client macaroon to <loopdir>/<network>/loop.macaroon
        return self.data_dir / self.network / "loop.macaroon"

    @property
    def loop_tls_cert_path(self) -> Path:
        # loopd writes its self-signed tls.cert to <loopdir>/<network>/tls.cert
        return self.data_dir / self.network / "tls.cert"

    @property
    def lnd_tls_cert_path(self) -> Path:
        return self.data_dir / "lnd-tls.cert"

    @property
    def lnd_macaroon_path(self) -> Path:
        return self.data_dir / "lnd-admin.macaroon"

    async def start(self) -> None:
        """Spawn the loopd subprocess and wait for it to come up.

        Fully async — subprocess spawn via asyncio.create_subprocess_exec,
        port-readiness check via asyncio.open_connection. None of the
        wait blocks the event loop, so plugin-mode workers stay
        responsive while loopd boots (~10-60s).
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lnd_tls_cert_path.write_bytes(self.lnd_tls_cert_bytes)
        self.lnd_macaroon_path.write_bytes(self.lnd_macaroon_bytes)
        self.lnd_macaroon_path.chmod(0o600)

        args = [
            str(self.bin_dir / "loopd"),
            f"--network={self.network}",
            f"--lnd.host={self.lnd_grpc_host}",
            f"--lnd.tlspath={self.lnd_tls_cert_path}",
            f"--lnd.macaroonpath={self.lnd_macaroon_path}",
            f"--rpclisten=127.0.0.1:{self.rpc_port}",
            f"--restlisten=127.0.0.1:{self.rest_port}",
            f"--loopoutmaxparts=5",
            f"--debuglevel=info",
        ]
        # loopd's --datadir is relative to a default $HOME-style path; override
        # with --loopdir to put everything under our chosen tree.
        args.append(f"--loopdir={self.data_dir}")
        if self.server_host:
            args.append(f"--server.host={self.server_host}")
        if self.server_notls:
            args.append("--server.notls")

        # File-handle safety: open the log first, then try spawn. On
        # spawn failure (binary missing, permission error) close the
        # log FD before raising so we don't leak it. On success, the
        # subprocess inherits the FD and is responsible for it; we
        # close OUR local handle since we don't read from it.
        log = open(self.data_dir / "loopd.log", "wb")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception:
            log.close()
            raise
        # Subprocess has dup'd the FD; close our local one.
        log.close()

        # Once the subprocess is spawned, any exception below leaves it
        # alive but unreferenced (caller in LoopdManager.get_loopd_for_
        # wallet won't reach `_instances[wid] = instance`). That
        # orphan would hold the rpc_port and prevent the next start
        # from binding. Wrap the readiness wait so we self-stop on any
        # failure path before re-raising.
        try:
            # Wait for loopd to publish its own tls.cert + loop.macaroon
            # AND for the RPC port to be listening. 120 iterations × 0.5s
            # = 60 seconds of patience.
            for _ in range(120):
                if self.loop_tls_cert_path.exists() and self.loop_macaroon_path.exists():
                    try:
                        reader, writer = await asyncio.wait_for(
                            asyncio.open_connection("127.0.0.1", self.rpc_port),
                            timeout=1.0,
                        )
                    except (OSError, asyncio.TimeoutError):
                        pass
                    else:
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except Exception:
                            pass
                        return
                if self.proc.returncode is not None:
                    raise RuntimeError(
                        f"loopd {self.wallet_id} exited early; "
                        f"check {self.data_dir / 'loopd.log'}"
                    )
                await asyncio.sleep(0.5)
            raise RuntimeError(
                f"loopd {self.wallet_id} never published tls.cert + macaroon "
                f"within 60s"
            )
        except Exception:
            # Self-clean before re-raising so the caller doesn't have
            # to. `stop()` is idempotent — safe even if `self.proc`
            # has already exited.
            try:
                await self.stop()
            except Exception:
                # Cleanup failure shouldn't mask the original error;
                # log and continue with the original exception.
                logger.exception(
                    f"loopd {self.wallet_id}: cleanup failed during start() rollback"
                )
            raise

    async def stop(self) -> None:
        """Terminate the subprocess and wait for it to exit.

        Async because we wait up to 15s for graceful shutdown before
        SIGKILL; a sync wait would freeze the event loop for the full
        timeout when loopd ignores SIGTERM.
        """
        if self.proc is None:
            return
        if self.proc.returncode is not None:
            return   # already exited
        try:
            self.proc.terminate()
        except ProcessLookupError:
            return   # raced with natural exit; nothing to do
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            try:
                await self.proc.wait()
            except Exception:
                pass

    def grpc_swap_stub(self) -> client_pb2_grpc.SwapClientStub:
        if self._swap_stub is not None:
            return self._swap_stub
        ssl_creds = grpc.ssl_channel_credentials(
            root_certificates=self.loop_tls_cert_path.read_bytes()
        )
        macaroon_hex = codecs.encode(self.loop_macaroon_path.read_bytes(), "hex").decode()

        def macaroon_callback(_context, callback):
            callback([("macaroon", macaroon_hex)], None)

        creds = grpc.composite_channel_credentials(
            ssl_creds, grpc.metadata_call_credentials(macaroon_callback)
        )
        self._channel = grpc.aio.secure_channel(
            f"127.0.0.1:{self.rpc_port}", creds,
            options=[("grpc.ssl_target_name_override", "localhost")],
        )
        self._swap_stub = client_pb2_grpc.SwapClientStub(self._channel)
        return self._swap_stub

    async def close_grpc(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._swap_stub = None


class LoopdManager:
    """Process-wide registry of loopd subprocesses, keyed by LND wallet_id.

    Each Bitcart-managed LND wallet gets its own loopd because loopd is tied
    to a single LND backend by macaroon. Looking up the same wallet twice
    reuses the existing instance.
    """

    def __init__(
        self,
        bin_dir: Path = LOOP_BIN_DIR,
        data_root: Path = LOOP_DATA_DIR,
        network: str = "mainnet",
        server_host: Optional[str] = None,
        server_notls: bool = False,
    ) -> None:
        self.bin_dir = bin_dir
        self.data_root = data_root
        self.network = network
        self.server_host = server_host
        self.server_notls = server_notls
        self._instances: Dict[str, LoopdInstance] = {}

    async def get_loopd_for_wallet(
        self, wallet: Dict[str, Any], api: "BitcartAPI",
    ) -> LoopdInstance:
        wid = wallet["id"]
        if wid in self._instances:
            return self._instances[wid]
        await ensure_loop_binaries(self.bin_dir)
        # Pull LND's tls.cert + admin.macaroon for this wallet via Bitcart.
        info = await api.get_lnd_info(wid)
        if not info:
            raise RuntimeError(f"could not get LND info for wallet {wid}")
        # Fail fast on network mismatch. loopd and LND will refuse to
        # interoperate (chain-hash check) if their networks disagree,
        # but the failure mode at swap time is a confusing gRPC error;
        # catching it here gives the operator a clear "change the
        # config" message. The bitcart fork's /lndinfo includes a
        # `network` field; older builds may omit it, in which case we
        # log a warning and proceed (the chain-hash check still
        # protects correctness, just with a worse error message).
        lnd_network = (info.get("network") or "").lower()
        if lnd_network and lnd_network != self.network.lower():
            raise RuntimeError(
                f"loopd network mismatch for wallet {wid!r}: this "
                f"LoopdManager was constructed with network="
                f"{self.network!r} but the wallet's LND reports "
                f"network={lnd_network!r}. Update LOOPD_NETWORK in "
                f"config.py (or your plugin settings) to match the "
                f"wallet's network, or choose a different wallet."
            )
        if not lnd_network:
            logger.warning(
                f"LND for wallet {wid!r} did not report a network field "
                f"in /lndinfo; can't pre-flight check loopd network "
                f"match. Operating under the assumption network="
                f"{self.network!r}."
            )
        lnd_tls = base64.b64decode(info["tls_cert"])
        lnd_macaroon = base64.b64decode(info["macaroon"])
        lnd_grpc_host = f"{info.get('host', '127.0.0.1')}:{info['grpc_port']}"
        instance = LoopdInstance(
            wallet_id=wid,
            bin_dir=self.bin_dir,
            data_dir=self.data_root / wid,
            lnd_grpc_host=lnd_grpc_host,
            lnd_tls_cert_bytes=lnd_tls,
            lnd_macaroon_bytes=lnd_macaroon,
            network=self.network,
            server_host=self.server_host,
            server_notls=self.server_notls,
        )
        await instance.start()
        self._instances[wid] = instance
        return instance

    def register_existing(self, instance: LoopdInstance) -> None:
        """Test hook: register a manually-constructed LoopdInstance."""
        self._instances[instance.wallet_id] = instance

    async def stop_all(self) -> None:
        for inst in list(self._instances.values()):
            await inst.close_grpc()
            await inst.stop()
        self._instances.clear()


# ---------------------------------------------------------------------------
# LoopProvider — concrete SwapProvider implementation
# ---------------------------------------------------------------------------


class LoopProvider(SwapProvider):
    name = "loop"

    def __init__(self, manager: LoopdManager):
        self.manager = manager

    async def _get_any_loopd(self, wallet: Dict[str, Any], api: "BitcartAPI") -> LoopdInstance:
        return await self.manager.get_loopd_for_wallet(wallet, api)

    async def quote_out(
        self, amount_sat: int, *,
        wallet: Optional[Dict[str, Any]] = None,
        api: Optional["BitcartAPI"] = None,
    ) -> Optional[SwapQuote]:
        if wallet is None:
            # We can't get a quote without a loopd, and loopd is keyed by
            # wallet_id, so a wallet must be supplied. `api` may be None if
            # the loopd instance for this wallet is already cached/registered.
            raise ValueError(
                "LoopProvider.quote_out needs a wallet to identify which "
                "loopd to query"
            )
        loopd = await self._get_any_loopd(wallet, api)
        stub = loopd.grpc_swap_stub()
        req = client_pb2.QuoteRequest(amt=amount_sat)
        try:
            resp = await stub.LoopOutQuote(req, timeout=15.0)
        except grpc.aio.AioRpcError as e:
            logger.warning(f"loop quote_out failed: {e.details()}")
            return None
        swap_fee = int(resp.swap_fee_sat) if resp.swap_fee_sat else 0
        miner_fee = int(resp.htlc_sweep_fee_sat) if resp.htlc_sweep_fee_sat else 0
        total_fee = swap_fee + miner_fee
        return SwapQuote(
            provider=self.name,
            direction=SwapDirection.OUT,
            amount_sat=amount_sat,
            swap_fee_sat=swap_fee,
            miner_fee_sat=miner_fee,
            total_fee_sat=total_fee,
            fee_percent=(total_fee / amount_sat) if amount_sat else 0.0,
            raw=MessageToDict(resp, preserving_proto_field_name=True),
        )

    async def initiate_out(
        self,
        wallet: Dict[str, Any],
        api: "BitcartAPI",
        amount_sat: int,
        dest_addr: str,
    ) -> Optional[SwapResult]:
        loopd = await self.manager.get_loopd_for_wallet(wallet, api)
        stub = loopd.grpc_swap_stub()
        # Fetch a quote first so we know what fees to authorize.
        try:
            q = await stub.LoopOutQuote(client_pb2.QuoteRequest(amt=amount_sat), timeout=15.0)
        except grpc.aio.AioRpcError as e:
            logger.warning(f"loop pre-initiate quote failed: {e.details()}")
            return None

        request = client_pb2.LoopOutRequest(
            amt=amount_sat,
            dest=dest_addr,
            max_swap_routing_fee=int(q.swap_fee_sat * 2 + 100),
            max_prepay_routing_fee=int(q.prepay_amt_sat * 2 + 100) if hasattr(q, "prepay_amt_sat") else 5000,
            max_swap_fee=int(q.swap_fee_sat),
            max_prepay_amt=int(q.prepay_amt_sat) if hasattr(q, "prepay_amt_sat") else 50000,
            max_miner_fee=int(q.htlc_sweep_fee_sat * 2 + 1000),
            swap_publication_deadline=0,
        )
        try:
            resp = await stub.LoopOut(request, timeout=60.0)
        except grpc.aio.AioRpcError as e:
            logger.warning(f"loop initiate_out failed: {e.details()}")
            return None
        swap_id_hex = bytes(resp.id_bytes).hex()
        return SwapResult(
            provider=self.name,
            swap_id=swap_id_hex,
            direction=SwapDirection.OUT,
            amount_sat=amount_sat,
            total_fee_sat=int(q.swap_fee_sat) + int(q.htlc_sweep_fee_sat),
            htlc_address=resp.htlc_address,
            state="INITIATED",
        )

    async def configure_autoloop(
        self,
        wallet: Dict[str, Any],
        api: "BitcartAPI",
        params: client_pb2.LiquidityParameters,
    ) -> bool:
        """Push autoloop config to this wallet's loopd via SetLiquidityParams.
        Returns True on success, False on validation failure (caller logs)."""
        loopd = await self.manager.get_loopd_for_wallet(wallet, api)
        stub = loopd.grpc_swap_stub()
        req = client_pb2.SetLiquidityParamsRequest(parameters=params)
        try:
            await stub.SetLiquidityParams(req, timeout=15.0)
            return True
        except grpc.aio.AioRpcError as e:
            logger.warning(
                f"autoloop config rejected for wallet {wallet['id']}: {e.details()}"
            )
            return False
