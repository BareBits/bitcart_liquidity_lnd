"""Pytest fixtures for the integration test suite. Provides `lnd_pair`, `lnd_pair_no_channels`, `lnd_electrum_pair`, and `loop_rig` — each per-test fixture brings up its own subprocess topology (see fixture bodies for details).

All binaries auto-downloaded into ./tests/_bin/ on first run and cached there.
All runtime state lives under ./tests/_data/ and is wiped at session start.
No dependency on anything outside the tests/ directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Optional

import grpc
import pytest
from google.protobuf.json_format import MessageToDict, ParseDict

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
BIN_DIR = TESTS_DIR / "_bin"
DATA_DIR = TESTS_DIR / "_data"

# Add the project root to sys.path so we can import lnd_proto + liquidityhelper.
sys.path.insert(0, str(PROJECT_ROOT))

from lnd_proto import (
    lightning_pb2,
    lightning_pb2_grpc,
    walletunlocker_pb2,
    walletunlocker_pb2_grpc,
)

# ---------------------------------------------------------------------------
# Binary management — download bitcoind + lnd into BIN_DIR on first use.
# ---------------------------------------------------------------------------

BITCOIND_VERSION = "28.0"
LND_VERSION = "0.20.1-beta"
FULCRUM_VERSION = "2.1.1"
ELECTRUM_VERSION = "4.5.8"


def _detect_platform() -> Dict[str, str]:
    """Return short platform tags used to pick the right release tarball."""
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    if sysname == "linux" and machine in ("x86_64", "amd64"):
        return {
            "bitcoin": "x86_64-linux-gnu", "lnd": "linux-amd64",
            "fulcrum": "x86_64-linux",
        }
    if sysname == "linux" and machine in ("aarch64", "arm64"):
        return {
            "bitcoin": "aarch64-linux-gnu", "lnd": "linux-arm64",
            "fulcrum": "arm64-linux",
        }
    if sysname == "darwin" and machine in ("x86_64", "amd64"):
        return {"bitcoin": "x86_64-apple-darwin", "lnd": "darwin-amd64"}
    if sysname == "darwin" and machine in ("arm64", "aarch64"):
        return {"bitcoin": "arm64-apple-darwin", "lnd": "darwin-arm64"}
    raise RuntimeError(f"Unsupported platform: {sysname}/{machine}")


def _download(url: str, dest: Path) -> None:
    """Download a file with a clear progress line."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  downloading {url}")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.rename(dest)


def _extract_member(tarball: Path, member_basename: str, dest: Path) -> None:
    """Extract a single file (matched by basename) from a tarball to `dest`."""
    with tarfile.open(tarball, "r:gz") as tar:
        for m in tar.getmembers():
            if Path(m.name).name == member_basename and m.isfile():
                src = tar.extractfile(m)
                if src is None:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                dest.chmod(0o755)
                return
    raise FileNotFoundError(f"Could not find {member_basename} in {tarball}")


def _ensure_binaries() -> Dict[str, Path]:
    """Make sure bitcoind, lnd, lncli, and Fulcrum are present under BIN_DIR;
    return paths."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    plat = _detect_platform()

    bitcoind = BIN_DIR / "bitcoind"
    bitcoin_cli = BIN_DIR / "bitcoin-cli"
    lnd = BIN_DIR / "lnd"
    lncli = BIN_DIR / "lncli"
    fulcrum = BIN_DIR / "Fulcrum"

    if not (bitcoind.exists() and bitcoin_cli.exists()):
        print(f"[fixture] fetching bitcoind {BITCOIND_VERSION} ({plat['bitcoin']})")
        url = (
            f"https://bitcoincore.org/bin/bitcoin-core-{BITCOIND_VERSION}/"
            f"bitcoin-{BITCOIND_VERSION}-{plat['bitcoin']}.tar.gz"
        )
        tarball = BIN_DIR / "bitcoin.tar.gz"
        _download(url, tarball)
        _extract_member(tarball, "bitcoind", bitcoind)
        _extract_member(tarball, "bitcoin-cli", bitcoin_cli)
        tarball.unlink()

    if not (lnd.exists() and lncli.exists()):
        print(f"[fixture] fetching lnd {LND_VERSION} ({plat['lnd']})")
        url = (
            f"https://github.com/lightningnetwork/lnd/releases/download/v{LND_VERSION}/"
            f"lnd-{plat['lnd']}-v{LND_VERSION}.tar.gz"
        )
        tarball = BIN_DIR / "lnd.tar.gz"
        _download(url, tarball)
        _extract_member(tarball, "lnd", lnd)
        _extract_member(tarball, "lncli", lncli)
        tarball.unlink()

    if not fulcrum.exists():
        if "fulcrum" not in plat:
            raise RuntimeError(
                f"Fulcrum has no release binary for platform {plat}"
            )
        print(f"[fixture] fetching Fulcrum {FULCRUM_VERSION} ({plat['fulcrum']})")
        url = (
            f"https://github.com/cculianu/Fulcrum/releases/download/v{FULCRUM_VERSION}/"
            f"Fulcrum-{FULCRUM_VERSION}-{plat['fulcrum']}.tar.gz"
        )
        tarball = BIN_DIR / "fulcrum.tar.gz"
        _download(url, tarball)
        _extract_member(tarball, "Fulcrum", fulcrum)
        tarball.unlink()

    return {
        "bitcoind": bitcoind, "bitcoin-cli": bitcoin_cli,
        "lnd": lnd, "lncli": lncli, "fulcrum": fulcrum,
    }


def _ensure_electrum_installed() -> None:
    """pip-install Electrum into the running interpreter if not already present.
    Electrum isn't on PyPI under its canonical name; we install the official
    sdist from electrum.org. Cached after first install."""
    try:
        import electrum  # noqa: F401
        return
    except ImportError:
        pass
    print(f"[fixture] pip installing Electrum {ELECTRUM_VERSION}")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "cryptography"],
        check=True,
    )
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--quiet",
            f"https://download.electrum.org/{ELECTRUM_VERSION}/"
            f"Electrum-{ELECTRUM_VERSION}.tar.gz",
        ],
        check=True,
    )
    # Electrum pins protobuf < 4, which is incompatible with the protobuf
    # version our LND-generated stubs (lnd_proto/) require (>= 5.27 for
    # `runtime_version`). Restore the modern protobuf after; Electrum's only
    # protobuf usage is legacy BIP70 payment request decoding which our
    # tests don't exercise.
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
         "protobuf>=5.27"],
        check=True,
    )


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fee URL stub — LND in neutrino mode polls a JSON fee endpoint. If it's
# unreachable, the logs fill with errors and LND falls back to relay-fee
# defaults. Serving a static JSON eliminates the noise and makes fee
# estimation deterministic.
# ---------------------------------------------------------------------------

_FEE_JSON = (
    b'{"fee_by_block_target":'
    b'{"1":50000,"2":30000,"3":20000,"6":10000,"12":5000,"24":2000,'
    b'"144":1000,"504":1000,"1008":1000}}'
)


class _FeeStubHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_FEE_JSON)))
        self.end_headers()
        self.wfile.write(_FEE_JSON)

    def log_message(self, *args, **kwargs):  # silence the access log
        pass


def _start_fee_stub() -> int:
    """Spin up a tiny HTTP server on a free port returning the fee JSON.
    Returns the port. Runs as a daemon thread; cleaned up at interpreter exit."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FeeStubHandler)
    port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()
    return port


# ---------------------------------------------------------------------------
# bitcoind controller
# ---------------------------------------------------------------------------


@dataclass
class BitcoindNode:
    bin_dir: Path
    data_dir: Path
    rpc_port: int = field(default_factory=_free_port)
    p2p_port: int = field(default_factory=_free_port)
    rpc_user: str = "test"
    rpc_password: str = "test_regtest_rpc"
    # ZMQ ports (required by loopserver in `loop_rig`). Harmless when no
    # subscriber is connected, so we wire them up unconditionally.
    zmq_block_port: int = field(default_factory=_free_port)
    zmq_tx_port: int = field(default_factory=_free_port)
    proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Bitcoin Core puts regtest data under <datadir>/regtest/ automatically.
        # bitcoin.conf in the datadir is read on startup.
        # bitcoind auto-loads <datadir>/bitcoin.conf; passing -conf with an
        # absolute path is interpreted RELATIVE to -datadir in v25+ which
        # double-prepends. So we just drop the file in datadir and let bitcoind
        # find it.
        conf = self.data_dir / "bitcoin.conf"
        conf.write_text(
            "regtest=1\n"
            "server=1\n"
            "txindex=1\n"
            "blockfilterindex=1\n"
            "peerblockfilters=1\n"
            "[regtest]\n"
            f"rpcuser={self.rpc_user}\n"
            f"rpcpassword={self.rpc_password}\n"
            f"rpcbind=127.0.0.1:{self.rpc_port}\n"
            "rpcallowip=127.0.0.1/32\n"
            f"bind=127.0.0.1:{self.p2p_port}\n"
            f"zmqpubrawblock=tcp://127.0.0.1:{self.zmq_block_port}\n"
            f"zmqpubrawtx=tcp://127.0.0.1:{self.zmq_tx_port}\n"
            "wallet=miner\n"
            "fallbackfee=0.0002\n"
            "maxtxfee=1.0\n"
        )
        log = open(self.data_dir / "bitcoind.log", "wb")
        self.proc = subprocess.Popen(
            [
                str(self.bin_dir / "bitcoind"),
                f"-datadir={self.data_dir}",
                "-printtoconsole=0",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        # Wait until the RPC accepts a getblockcount call.
        for _ in range(60):
            try:
                self.cli("getblockcount")
                return
            except subprocess.CalledProcessError:
                time.sleep(0.5)
        raise RuntimeError("bitcoind didn't accept RPC within 30s")

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.cli("stop")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def cli(self, *args: str) -> str:
        """Run bitcoin-cli with our auth and return stdout (stripped)."""
        result = subprocess.run(
            [
                str(self.bin_dir / "bitcoin-cli"),
                "-regtest",
                f"-rpcconnect=127.0.0.1",
                f"-rpcport={self.rpc_port}",
                f"-rpcuser={self.rpc_user}",
                f"-rpcpassword={self.rpc_password}",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def ensure_miner_wallet(self) -> None:
        # bitcoin.conf has wallet=miner so it should auto-load; if not, create it.
        wallets = self.cli("listwallets")
        if '"miner"' in wallets:
            return
        try:
            self.cli("loadwallet", "miner")
        except subprocess.CalledProcessError:
            self.cli("createwallet", "miner")

    def mine_to_self(self, n: int) -> List[str]:
        addr = self.cli("-rpcwallet=miner", "getnewaddress")
        blocks = self.cli("generatetoaddress", str(n), addr)
        # Return is JSON array of hashes; we don't parse, callers don't need them.
        return [addr]

    def mine_to(self, addr: str, n: int) -> None:
        self.cli("generatetoaddress", str(n), addr)

    def send(self, addr: str, btc: float) -> str:
        return self.cli("-rpcwallet=miner", "sendtoaddress", addr, f"{btc:.8f}")


# ---------------------------------------------------------------------------
# LND controller
# ---------------------------------------------------------------------------


@dataclass
class LndNode:
    name: str
    bin_dir: Path
    lnddir: Path
    bitcoind_p2p_port: int
    fee_url: str
    rpc_port: int = field(default_factory=_free_port)
    p2p_port: int = field(default_factory=_free_port)
    password: bytes = b"testpassword1234"
    proc: Optional[subprocess.Popen] = None
    _macaroon_hex: str = ""
    _tls_cert: bytes = b""
    _channel: Optional[grpc.aio.Channel] = None
    _stub: Optional[lightning_pb2_grpc.LightningStub] = None
    _identity_pubkey: str = ""

    def start(self) -> None:
        self.lnddir.mkdir(parents=True, exist_ok=True)
        args = [
            str(self.bin_dir / "lnd"),
            f"--lnddir={self.lnddir}",
            "--bitcoin.active",
            "--bitcoin.regtest",
            "--bitcoin.node=neutrino",
            f"--neutrino.connect=127.0.0.1:{self.bitcoind_p2p_port}",
            f"--rpclisten=127.0.0.1:{self.rpc_port}",
            f"--listen=127.0.0.1:{self.p2p_port}",
            "--norest",
            "--tlsextraip=127.0.0.1",
            # 10.0.2.2 is podman's slirp4netns host-loopback alias; only used
            # by `loop_rig`'s loopserver container but harmless everywhere.
            "--tlsextraip=10.0.2.2",
            "--tlsextradomain=localhost",
            "--nobootstrap",
            "--protocol.wumbo-channels",
            # LND rejects keysend payments with "incorrect_payment_details"
            # unless this is set. The OWN_LIGHTNING_NODES integration tests
            # exercise the production keysend path (A->B cashout); the
            # destination LND needs to accept keysends or every keysend
            # cashout fails at the receiver, not the sender.
            "--accept-keysend",
            # The fixture opens A->B and B->A back-to-back. LND's default of
            # 1 pending channel per peer would reject the second open.
            "--maxpendingchannels=5",
            "--debuglevel=info",
            f"--fee.url={self.fee_url}",
            # Keep retry storms quiet.
            "--minbackoff=10s",
        ]
        log = open(self.lnddir / "lnd.log", "wb")
        self.proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)

        # tls.cert appears once LND is ready to accept WalletUnlocker requests.
        tls_path = self.lnddir / "tls.cert"
        for _ in range(120):
            if tls_path.exists():
                break
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"lnd {self.name} exited early; check {self.lnddir / 'lnd.log'}"
                )
            time.sleep(0.5)
        else:
            raise RuntimeError(f"lnd {self.name} never wrote tls.cert")
        self._tls_cert = tls_path.read_bytes()

    async def init_wallet(self) -> None:
        """Generate a seed and init the wallet via WalletUnlocker gRPC."""
        creds = grpc.ssl_channel_credentials(root_certificates=self._tls_cert)
        async with grpc.aio.secure_channel(
            f"127.0.0.1:{self.rpc_port}",
            creds,
            options=[("grpc.ssl_target_name_override", "localhost")],
        ) as ch:
            unlocker = walletunlocker_pb2_grpc.WalletUnlockerStub(ch)
            # WalletUnlocker is exposed before any wallet exists. Wait for it.
            for _ in range(60):
                try:
                    await unlocker.GenSeed(
                        walletunlocker_pb2.GenSeedRequest(), timeout=5.0
                    )
                    break
                except grpc.aio.AioRpcError:
                    await asyncio.sleep(0.5)
            else:
                raise RuntimeError(f"lnd {self.name} WalletUnlocker never came up")
            seed_resp = await unlocker.GenSeed(walletunlocker_pb2.GenSeedRequest())
            await unlocker.InitWallet(
                walletunlocker_pb2.InitWalletRequest(
                    wallet_password=self.password,
                    cipher_seed_mnemonic=list(seed_resp.cipher_seed_mnemonic),
                    recovery_window=0,
                )
            )
        # After InitWallet, LND restarts its RPC; the WalletUnlocker channel
        # gets closed by lnd. Wait for admin.macaroon to appear.
        macaroon_path = (
            self.lnddir
            / "data" / "chain" / "bitcoin" / "regtest" / "admin.macaroon"
        )
        for _ in range(120):
            if macaroon_path.exists():
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError(f"lnd {self.name} never produced admin.macaroon")
        self._macaroon_hex = macaroon_path.read_bytes().hex()

    async def open_lightning(self) -> None:
        """Open a Lightning service gRPC channel with macaroon auth."""
        ssl = grpc.ssl_channel_credentials(root_certificates=self._tls_cert)
        macaroon_hex = self._macaroon_hex

        def macaroon_callback(_ctx, callback):
            callback([("macaroon", macaroon_hex)], None)

        creds = grpc.composite_channel_credentials(
            ssl, grpc.metadata_call_credentials(macaroon_callback)
        )
        self._channel = grpc.aio.secure_channel(
            f"127.0.0.1:{self.rpc_port}",
            creds,
            options=[("grpc.ssl_target_name_override", "localhost")],
        )
        self._stub = lightning_pb2_grpc.LightningStub(self._channel)
        # Wait for Lightning to be RPC-active.
        for _ in range(120):
            try:
                info = await self._stub.GetInfo(lightning_pb2.GetInfoRequest(), timeout=5.0)
                self._identity_pubkey = info.identity_pubkey
                return
            except grpc.aio.AioRpcError:
                await asyncio.sleep(0.5)
        raise RuntimeError(f"lnd {self.name} Lightning.GetInfo never succeeded")

    async def wait_synced(self, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            info = await self._stub.GetInfo(lightning_pb2.GetInfoRequest())
            if info.synced_to_chain:
                return
            await asyncio.sleep(1.0)
        raise RuntimeError(f"lnd {self.name} did not reach synced_to_chain")

    async def new_address(self) -> str:
        resp = await self._stub.NewAddress(
            lightning_pb2.NewAddressRequest(type=lightning_pb2.AddressType.WITNESS_PUBKEY_HASH)
        )
        return resp.address

    async def wallet_balance_sats(self) -> int:
        resp = await self._stub.WalletBalance(lightning_pb2.WalletBalanceRequest())
        return int(resp.confirmed_balance)

    async def connect_peer(self, pubkey: str, host: str, port: int) -> None:
        try:
            await self._stub.ConnectPeer(
                lightning_pb2.ConnectPeerRequest(
                    addr=lightning_pb2.LightningAddress(pubkey=pubkey, host=f"{host}:{port}"),
                    perm=True,
                )
            )
        except grpc.aio.AioRpcError as e:
            # already connected is fine
            if "already connected" not in (e.details() or ""):
                raise
        # ConnectPeer returns once LND has *dialed* the peer, but the peer can
        # still be mid-handshake — OpenChannel will fail with "peer is not
        # online". Poll ListPeers until we see the pubkey.
        for _ in range(60):
            resp = await self._stub.ListPeers(lightning_pb2.ListPeersRequest())
            if any(p.pub_key == pubkey for p in resp.peers):
                return
            await asyncio.sleep(0.5)
        raise RuntimeError(f"lnd {self.name}: peer {pubkey[:16]} never came online")

    async def open_channel_sync(self, remote_pubkey_hex: str, sat: int) -> str:
        """Open a channel, return funding channel_point as 'txid:vout' string."""
        request = lightning_pb2.OpenChannelRequest(
            node_pubkey=bytes.fromhex(remote_pubkey_hex),
            local_funding_amount=sat,
            sat_per_vbyte=1,
            spend_unconfirmed=False,
        )
        # OpenChannelSync is unary and returns the funding ChannelPoint.
        cp = await self._stub.OpenChannelSync(request)
        # ChannelPoint has funding_txid_bytes (little-endian) + output_index
        txid = bytes(reversed(cp.funding_txid_bytes)).hex()
        return f"{txid}:{cp.output_index}"

    async def list_channels(self) -> List[Dict[str, Any]]:
        resp = await self._stub.ListChannels(lightning_pb2.ListChannelsRequest())
        return [MessageToDict(c, preserving_proto_field_name=True) for c in resp.channels]

    async def list_pending_channels(self) -> Dict[str, Any]:
        resp = await self._stub.PendingChannels(lightning_pb2.PendingChannelsRequest())
        return MessageToDict(resp, preserving_proto_field_name=True)

    async def stop_grpc(self) -> None:
        if self._channel is not None:
            await self._channel.close()

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    @property
    def identity_pubkey(self) -> str:
        return self._identity_pubkey

    @property
    def tls_cert(self) -> bytes:
        return self._tls_cert

    @property
    def macaroon_hex(self) -> str:
        return self._macaroon_hex

    @property
    def grpc_target(self) -> str:
        return f"127.0.0.1:{self.rpc_port}"


# ---------------------------------------------------------------------------
# Fulcrum (Electrum-protocol server) controller
# ---------------------------------------------------------------------------


@dataclass
class FulcrumServer:
    bin_dir: Path
    data_dir: Path
    bitcoind_rpc_port: int
    bitcoind_rpc_user: str
    bitcoind_rpc_password: str
    tcp_port: int = field(default_factory=_free_port)
    admin_port: int = field(default_factory=_free_port)
    proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        conf = self.data_dir / "fulcrum.conf"
        # Fulcrum config is key=value lines. "tcp" enables plain TCP (no
        # TLS), which is what Electrum's `server <host>:<port>:t` syntax
        # expects.
        conf.write_text(
            f"datadir = {self.data_dir}\n"
            f"bitcoind = 127.0.0.1:{self.bitcoind_rpc_port}\n"
            f"rpcuser = {self.bitcoind_rpc_user}\n"
            f"rpcpassword = {self.bitcoind_rpc_password}\n"
            f"tcp = 0.0.0.0:{self.tcp_port}\n"
            f"admin = 127.0.0.1:{self.admin_port}\n"
            f"polltime = 1\n"  # fast block detection in regtest
        )
        log = open(self.data_dir / "fulcrum.log", "wb")
        self.proc = subprocess.Popen(
            [str(self.bin_dir / "Fulcrum"), str(conf)],
            stdout=log, stderr=subprocess.STDOUT,
        )
        wait_port = self.tcp_port
        # Fulcrum needs to sync headers from bitcoind on first start (101 blocks).
        for _ in range(120):
            try:
                with socket.create_connection(("127.0.0.1", wait_port), timeout=1):
                    return
            except OSError:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"Fulcrum exited early; check {self.data_dir / 'fulcrum.log'}"
                )
            time.sleep(0.5)
        raise RuntimeError("Fulcrum never accepted TCP within 60s")

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ---------------------------------------------------------------------------
# Electrum daemon controller
# ---------------------------------------------------------------------------


@dataclass
class ElectrumDaemon:
    """Wraps an Electrum daemon process, its wallet file, and its JSON-RPC
    endpoint. Configured for regtest, full-gossip mode (no trampoline), and
    the canonical liquidityhelper RPC port/auth (5000 / electrum:electrumz)
    so `liquidityhelper.electrum_rpc(...)` works against it unmodified."""

    bin_dir: Path  # unused for Electrum itself (pip-installed) but kept for symmetry
    data_dir: Path
    fulcrum_tcp_port: int
    rpc_port: int = 5000
    rpc_user: str = "electrum"
    rpc_password: str = "electrumz"
    lightning_p2p_port: int = field(default_factory=_free_port)
    _xpub: str = ""
    _identity_pubkey: str = ""
    _proc: Optional[subprocess.Popen] = None
    _venv_python: Path = field(default_factory=lambda: Path(sys.executable))
    # Electrum daemon's actual RPC port (private). The xpub-stripping proxy
    # listens on the public `rpc_port` (5000) and forwards here.
    _daemon_rpc_port: int = field(default_factory=_free_port)
    _proxy_server: Any = None  # ThreadingHTTPServer; typed as Any to avoid forward refs
    _proxy_thread: Any = None

    @property
    def _electrum_bin(self) -> Path:
        # Electrum installs as a console script next to the venv's python
        # (e.g., .venv/bin/electrum). It's not a runnable -m module.
        return self._venv_python.parent / "electrum"

    # ----- subprocess driver -----

    def _electrum(self, *args: str, check: bool = True, input: Optional[str] = None,
                  timeout: Optional[float] = 60.0) -> subprocess.CompletedProcess:
        cmd = [
            str(self._electrum_bin),
            "--regtest", "--dir", str(self.data_dir), *args,
        ]
        return subprocess.run(
            cmd, check=check, capture_output=True, text=True,
            input=input, timeout=timeout,
        )

    # ----- lifecycle -----

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 1. Configure before launching daemon (uses --offline so no lockfile).
        self._electrum("--offline", "setconfig", "server",
                       f"localhost:{self.fulcrum_tcp_port}:t")
        self._electrum("--offline", "setconfig", "oneserver", "true")
        self._electrum("--offline", "setconfig", "rpchost", "127.0.0.1")
        # Daemon listens on a private port; the xpub-stripping proxy below
        # is what liquidityhelper.electrum_rpc actually hits at `rpc_port`.
        self._electrum("--offline", "setconfig", "rpcport", str(self._daemon_rpc_port))
        self._electrum("--offline", "setconfig", "rpcuser", self.rpc_user)
        self._electrum("--offline", "setconfig", "rpcpassword", self.rpc_password)
        # Lightning: full gossip, NO trampoline, listen for inbound peers, and
        # raise the wumbo cap so 0.2 BTC channel opens succeed.
        self._electrum("--offline", "setconfig", "lightning_listen",
                       f"0.0.0.0:{self.lightning_p2p_port}")
        # `use_gossip=true` is Electrum's "use full gossip (no trampoline)"
        # switch — the same ConfigVar that the GUI labels "Use trampoline
        # routing" toggles. Setting true => trampoline disabled.
        self._electrum("--offline", "setconfig", "use_gossip", "true")
        self._electrum("--offline", "setconfig", "lightning_max_funding_sat",
                       "100000000")

        # 2. Create wallet (no password).
        wallet_path = self.data_dir / "regtest" / "wallets" / "default_wallet"
        if not wallet_path.exists():
            self._electrum("--offline", "create")

        # 3. Start daemon.
        log = open(self.data_dir / "electrum.log", "wb")
        self._proc = subprocess.Popen(
            [str(self._electrum_bin),
             "--regtest", "--dir", str(self.data_dir),
             "daemon", "-d"],
            stdout=log, stderr=subprocess.STDOUT,
        )
        # daemon -d returns quickly; the actual daemon lives on. Wait until
        # the RPC port answers `getinfo`.
        for _ in range(120):
            try:
                self._electrum("getinfo", timeout=5.0)
                break
            except subprocess.CalledProcessError:
                pass
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Electrum daemon never responded to RPC")
        # 3b. Stand up the xpub-stripping proxy on `rpc_port` (5000). This
        # mimics what Bitcart's `btc` daemon does in production: it accepts
        # the `xpub` kwarg liquidityhelper's `electrum_rpc` injects into
        # every call, drops it (since raw Electrum commands like
        # `close_channel` don't accept that kwarg), and forwards the rest
        # to the underlying Electrum daemon.
        self._start_xpub_strip_proxy()
        # 4. Load wallet.
        self._electrum("load_wallet")
        # 5. Snapshot xpub + LN nodeid for later use.
        info = self._electrum("getinfo")
        self._xpub = (
            __import__("json").loads(info.stdout).get("default_wallet") or ""
        )
        # Electrum's `nodeid` returns "<pubkey>@host:port" — pubkey only.
        nodeid_resp = self._electrum("nodeid")
        full = (nodeid_resp.stdout or "").strip().strip('"')
        self._identity_pubkey = full.split("@", 1)[0]

    def stop(self) -> None:
        # Shut the proxy down first so liquidityhelper.electrum_rpc gets a
        # clean ConnectionError if anything fires during teardown.
        if self._proxy_server is not None:
            with contextlib.suppress(Exception):
                self._proxy_server.shutdown()
                self._proxy_server.server_close()
        # Try graceful first (releases the wallet lock cleanly).
        with contextlib.suppress(Exception):
            self._electrum("stop", check=False, timeout=10.0)
        if self._proc is not None:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _start_xpub_strip_proxy(self) -> None:
        """Run a tiny HTTP server on `self.rpc_port` that mirrors Bitcart's
        `btc` daemon's behavior of accepting the `xpub` kwarg liquidityhelper
        injects, stripping it, and forwarding the remainder to the real
        Electrum daemon."""
        import base64 as _b64
        import json as _json
        import urllib.request as _urlreq

        daemon_url = f"http://127.0.0.1:{self._daemon_rpc_port}"
        daemon_auth = _b64.b64encode(
            f"{self.rpc_user}:{self.rpc_password}".encode()
        ).decode()
        expected_auth = "Basic " + daemon_auth

        class _ProxyHandler(BaseHTTPRequestHandler):
            def do_POST(self_inner):  # noqa: N802
                # Re-auth the inbound caller against the same creds liquidityhelper
                # uses (electrum:electrumz). Reject anything else.
                if self_inner.headers.get("Authorization") != expected_auth:
                    self_inner.send_response(401)
                    self_inner.end_headers()
                    return
                length = int(self_inner.headers.get("Content-Length") or 0)
                raw = self_inner.rfile.read(length) if length else b""
                try:
                    body = _json.loads(raw.decode() or "{}")
                except Exception:
                    self_inner.send_response(400)
                    self_inner.end_headers()
                    return
                params = body.get("params") or {}
                if isinstance(params, dict):
                    params.pop("xpub", None)
                    body["params"] = params
                req = _urlreq.Request(
                    daemon_url,
                    data=_json.dumps(body).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": expected_auth,
                    },
                    method="POST",
                )
                try:
                    with _urlreq.urlopen(req, timeout=60) as resp:
                        payload = resp.read()
                        status = resp.getcode()
                except Exception as e:
                    self_inner.send_response(502)
                    self_inner.end_headers()
                    self_inner.wfile.write(str(e).encode())
                    return
                self_inner.send_response(status)
                self_inner.send_header("Content-Type", "application/json")
                self_inner.send_header("Content-Length", str(len(payload)))
                self_inner.end_headers()
                self_inner.wfile.write(payload)

            def log_message(self_inner, *args, **kwargs):  # silence
                pass

        self._proxy_server = ThreadingHTTPServer(("127.0.0.1", self.rpc_port), _ProxyHandler)
        self._proxy_thread = Thread(target=self._proxy_server.serve_forever, daemon=True)
        self._proxy_thread.start()

    # ----- accessors needed by tests -----

    @property
    def identity_pubkey(self) -> str:
        return self._identity_pubkey

    @property
    def xpub(self) -> str:
        # default_wallet is a path; Electrum's "xpub" identifier used by the
        # daemon is the wallet path itself when calling electrum_rpc — but
        # for our liquidityhelper code, `xpub` is the value passed to the
        # daemon as `xpub` in JSON-RPC params. The daemon uses the loaded
        # wallet when xpub is empty; we return "" to mean "use loaded".
        return ""

    # ----- thin helpers for fixture / test convenience -----

    def getunusedaddress(self) -> str:
        out = self._electrum("getunusedaddress").stdout.strip().strip('"')
        return out

    def getbalance_btc(self) -> float:
        import json as _json
        out = self._electrum("getbalance").stdout
        return float(_json.loads(out).get("confirmed", "0"))

    def open_channel(self, node_uri: str, amount_sat: int) -> str:
        """Returns funding channel_point in 'txid:vout' form.

        Electrum's open_channel takes the amount as a BTC-string ("0.2"),
        not satoshis — we convert here so callers can think in sats."""
        amount_btc = f"{amount_sat / 1e8:.8f}"
        out = self._electrum("open_channel", node_uri, amount_btc).stdout
        return out.strip().strip('"')

    def list_channels(self) -> List[Dict[str, Any]]:
        import json as _json
        out = self._electrum("list_channels").stdout
        return _json.loads(out)

    def add_peer(self, node_uri: str) -> None:
        self._electrum("add_peer", node_uri, check=False)


# ---------------------------------------------------------------------------
# Fixture model — pure per-test isolation
#
# Every test that requests `lnd_pair` gets its own bitcoind + LND-A + LND-B +
# funding + channels, in a fresh data subdirectory under tests/_data/. No
# state of any kind is shared between tests beyond the cached binaries in
# tests/_bin/ and the shared fee-URL stub.
#
# Tradeoff: each test costs ~25-35s of setup (binary download is cached,
# wallet init + neutrino sync + channel opens are the slow steps). Worth it
# for clean isolation.
# ---------------------------------------------------------------------------

@dataclass
class LndPair:
    """Everything a test using the rig might want: the regtest bitcoind, two
    LND nodes, and the two pre-opened channel points."""
    bitcoind: BitcoindNode
    a: LndNode
    b: LndNode
    a_to_b_channel_point: str
    b_to_a_channel_point: str


async def _setup_test_env_async(data_dir: Path, fee_url: str) -> LndPair:
    """Stand up bitcoind + LND-A + LND-B, fund each 2 BTC, open 0.2 BTC
    channels both ways, mine 6 confirmation blocks. All processes write into
    `data_dir/<service-name>/`."""
    btc = BitcoindNode(bin_dir=BIN_DIR, data_dir=data_dir / "bitcoind")
    btc.start()
    btc.ensure_miner_wallet()
    btc.mine_to_self(101)  # mature initial coinbase

    a = LndNode(
        name="A", bin_dir=BIN_DIR, lnddir=data_dir / "lnd-a",
        bitcoind_p2p_port=btc.p2p_port, fee_url=fee_url,
    )
    a.start()
    await a.init_wallet()
    await a.open_lightning()

    b = LndNode(
        name="B", bin_dir=BIN_DIR, lnddir=data_dir / "lnd-b",
        bitcoind_p2p_port=btc.p2p_port, fee_url=fee_url,
    )
    b.start()
    await b.init_wallet()
    await b.open_lightning()

    await a.wait_synced()
    await b.wait_synced()

    # Fund both LNDs with 2 BTC each.
    btc.send(await a.new_address(), 2.0)
    btc.send(await b.new_address(), 2.0)
    btc.mine_to_self(6)
    for n in (a, b):
        for _ in range(60):
            if await n.wallet_balance_sats() >= int(2 * 100_000_000 * 0.99):
                break
            await asyncio.sleep(1.0)
        else:
            raise RuntimeError(f"lnd {n.name} never saw confirmed funds")

    # Open 0.2 BTC channels in both directions.
    await a.connect_peer(b.identity_pubkey, "127.0.0.1", b.p2p_port)
    channel_sats = int(0.2 * 100_000_000)
    a_to_b_cp = await a.open_channel_sync(b.identity_pubkey, channel_sats)
    b_to_a_cp = await b.open_channel_sync(a.identity_pubkey, channel_sats)
    # 6 confs make the channel active for receiving/sending in principle, but
    # SendPaymentSync's path-finder rejects routes through channels it hasn't
    # seen a `channel_update` for yet (which arrives via gossip a few seconds
    # after the channel is announced at conf #6). Mining a handful of extra
    # blocks past the activation threshold plus a brief settle wait is the
    # cheapest reliable fix.
    btc.mine_to_self(10)

    # Wait for A to see both as active.
    for _ in range(60):
        chans = await a.list_channels()
        cps = {c.get("channel_point"): c.get("active") for c in chans}
        if cps.get(a_to_b_cp) is True and cps.get(b_to_a_cp) is True:
            break
        await asyncio.sleep(1.0)
    # Give gossip a moment to propagate channel_update messages so the
    # path-finder has the policy info it needs to route through these channels.
    await asyncio.sleep(3.0)

    return LndPair(
        bitcoind=btc, a=a, b=b,
        a_to_b_channel_point=a_to_b_cp,
        b_to_a_channel_point=b_to_a_cp,
    )


async def _teardown_test_env_async(pair: LndPair) -> None:
    """Hard teardown: close gRPC channels, SIGTERM all processes. The data
    dir is left in place for post-mortem debugging — it gets wiped at the
    start of the next pytest session by `_wipe_data_dir`."""
    with contextlib.suppress(Exception):
        await pair.a.stop_grpc()
    with contextlib.suppress(Exception):
        await pair.b.stop_grpc()
    pair.a.stop()
    pair.b.stop()
    pair.bitcoind.stop()


@dataclass
class LndPairNoChannels:
    """Minimal pair for tests that need to drive channel-open themselves
    (e.g. OWN_LIGHTNING_NODES direct-channel push tests). Same shape as
    LndPair minus the auto-opened channels."""
    bitcoind: BitcoindNode
    a: LndNode
    b: LndNode


async def _setup_lnd_pair_no_channels_async(
    data_dir: Path, fee_url: str,
) -> LndPairNoChannels:
    """bitcoind + LND-A + LND-B, both funded 2 BTC, peered, but NO
    channels opened. Used by tests that drive channel creation
    themselves and need to assert on the resulting channel's state
    (e.g. the OWN_LIGHTNING_NODES direct-channel-push cashout, where
    the push_sat behavior is what's under test)."""
    btc = BitcoindNode(bin_dir=BIN_DIR, data_dir=data_dir / "bitcoind")
    btc.start()
    btc.ensure_miner_wallet()
    btc.mine_to_self(101)

    a = LndNode(
        name="A", bin_dir=BIN_DIR, lnddir=data_dir / "lnd-a",
        bitcoind_p2p_port=btc.p2p_port, fee_url=fee_url,
    )
    a.start()
    await a.init_wallet()
    await a.open_lightning()

    b = LndNode(
        name="B", bin_dir=BIN_DIR, lnddir=data_dir / "lnd-b",
        bitcoind_p2p_port=btc.p2p_port, fee_url=fee_url,
    )
    b.start()
    await b.init_wallet()
    await b.open_lightning()

    await a.wait_synced()
    await b.wait_synced()

    btc.send(await a.new_address(), 2.0)
    btc.send(await b.new_address(), 2.0)
    btc.mine_to_self(6)
    for n in (a, b):
        for _ in range(60):
            if await n.wallet_balance_sats() >= int(2 * 100_000_000 * 0.99):
                break
            await asyncio.sleep(1.0)
        else:
            raise RuntimeError(f"lnd {n.name} never saw confirmed funds")

    # Peer A↔B so subsequent OpenChannel calls don't have to wait on
    # connection establishment. Channel opens are driven by individual
    # tests after this point.
    await a.connect_peer(b.identity_pubkey, "127.0.0.1", b.p2p_port)

    return LndPairNoChannels(bitcoind=btc, a=a, b=b)


async def _teardown_lnd_pair_no_channels_async(pair: LndPairNoChannels) -> None:
    with contextlib.suppress(Exception):
        await pair.a.stop_grpc()
    with contextlib.suppress(Exception):
        await pair.b.stop_grpc()
    pair.a.stop()
    pair.b.stop()
    pair.bitcoind.stop()


# ----- session-scoped helpers (one-time setup, shared by every test) -------

@pytest.fixture(scope="session")
def event_loop():
    """One asyncio loop reused across the whole pytest run. Tests still get
    fresh gRPC channels per-test because the per-test fixture builds them
    inside _setup_test_env_async; the loop is just the async runtime."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


@pytest.fixture(scope="session", autouse=True)
def _wipe_data_dir():
    """Wipe tests/_data/ at the start of the session so old per-test dirs
    don't accumulate forever. (Each test creates its own subdir below.)"""
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture(scope="session")
def _binaries():
    """Ensure bitcoind + lnd + lncli + Fulcrum are downloaded to tests/_bin/ (cached across sessions)."""
    return _ensure_binaries()


@pytest.fixture(scope="session")
def _fee_url():
    """Tiny HTTP fee-estimate stub. Session-scoped because it's stateless
    and serving the same JSON to every test is fine."""
    port = _start_fee_stub()
    return f"http://127.0.0.1:{port}/btc-fee-estimates.json"


# ----- per-test isolation hooks --------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_state():
    """Per-test isolation of every kind of cross-call state:

      - `liquidityhelper._LND_CONNECTIONS` (cached gRPC channels keyed by
        wallet_id — stale ones point at LND processes that have since died).
      - Both Peewee SQLite databases (`liquidityhelper.sqlite` via
        `database.db` and `known_ln_nodes.db` via `node_database.node_db`)
        are rebound to fresh `:memory:` databases for the duration of the
        test, then restored after. This is stricter than wiping rows: in-
        memory means test N literally cannot see any row test N-1 wrote,
        even if a `delete().execute()` was missed somewhere.
    """
    import liquidityhelper as _lh
    import database as _db_main
    import node_database as _db_node
    import peewee as _peewee

    main_models = list(_db_main.USED_TABLES)
    node_models = [
        _db_node.LightningNode, _db_node.LightningChannel,
        _db_node.LndPaymentLabel, _db_node.SwapPriceQuote,
        _db_node.LspPriceQuote, _db_node.LspChannelOrder,
    ]

    original_main_db = _db_main.db
    original_node_db = _db_node.node_db

    mem_main = _peewee.SqliteDatabase(":memory:")
    mem_node = _peewee.SqliteDatabase(":memory:")

    _lh._LND_CONNECTIONS.clear()
    mem_main.bind(main_models, bind_refs=False, bind_backrefs=False)
    mem_node.bind(node_models, bind_refs=False, bind_backrefs=False)
    mem_main.connect(reuse_if_open=True)
    mem_node.connect(reuse_if_open=True)
    mem_main.create_tables(main_models, safe=True)
    mem_node.create_tables(node_models, safe=True)

    try:
        yield
    finally:
        _lh._LND_CONNECTIONS.clear()
        # Restore the original (file-backed) bindings so anything in the
        # surrounding process that touches these models afterwards uses the
        # production DB again.
        original_main_db.bind(main_models, bind_refs=False, bind_backrefs=False)
        original_node_db.bind(node_models, bind_refs=False, bind_backrefs=False)
        mem_main.close()
        mem_node.close()


# ----- the main per-test fixture -------------------------------------------

@dataclass
class LndElectrumPair:
    """An LND node + an Electrum daemon (talking to bitcoind via Fulcrum),
    funded with 2 BTC each, with 0.2 BTC channels open in both directions.
    Used to exercise the Electrum-dispatch branches of liquidityhelper."""
    bitcoind: BitcoindNode
    fulcrum: FulcrumServer
    lnd: LndNode
    electrum: ElectrumDaemon
    # channel_point ("txid:vout") for the channel funded by Electrum -> LND
    electrum_to_lnd_channel_point: str
    # channel_point for the channel funded by LND -> Electrum
    lnd_to_electrum_channel_point: str


async def _setup_lnd_electrum_pair_async(data_dir: Path, fee_url: str) -> LndElectrumPair:
    """Stand up bitcoind + Fulcrum + LND + Electrum, fund each 2 BTC, open
    0.2 BTC channels each way, mine confirmations.

    All processes write into `data_dir/<service>/`."""
    _ensure_electrum_installed()

    btc = BitcoindNode(bin_dir=BIN_DIR, data_dir=data_dir / "bitcoind")
    btc.start()
    btc.ensure_miner_wallet()
    btc.mine_to_self(101)

    fulcrum = FulcrumServer(
        bin_dir=BIN_DIR, data_dir=data_dir / "fulcrum",
        bitcoind_rpc_port=btc.rpc_port,
        bitcoind_rpc_user=btc.rpc_user,
        bitcoind_rpc_password=btc.rpc_password,
    )
    fulcrum.start()

    lnd = LndNode(
        name="LND", bin_dir=BIN_DIR, lnddir=data_dir / "lnd",
        bitcoind_p2p_port=btc.p2p_port, fee_url=fee_url,
    )
    lnd.start()
    await lnd.init_wallet()
    await lnd.open_lightning()
    await lnd.wait_synced()

    electrum = ElectrumDaemon(
        bin_dir=BIN_DIR, data_dir=data_dir / "electrum",
        fulcrum_tcp_port=fulcrum.tcp_port,
    )
    electrum.start()

    # Fund both with 2 BTC each.
    btc.send(await lnd.new_address(), 2.0)
    btc.send(electrum.getunusedaddress(), 2.0)
    btc.mine_to_self(6)
    for _ in range(60):
        if await lnd.wallet_balance_sats() >= int(2 * 100_000_000 * 0.99):
            break
        await asyncio.sleep(1.0)
    else:
        raise RuntimeError("LND never saw confirmed funds")
    for _ in range(60):
        if electrum.getbalance_btc() >= 2.0 * 0.99:
            break
        await asyncio.sleep(1.0)
    else:
        raise RuntimeError("Electrum never saw confirmed funds")

    # Open 0.2 BTC channels in both directions. Electrum -> LND first; that
    # establishes the peer connection that LND -> Electrum reuses.
    channel_sats = int(0.2 * 100_000_000)
    lnd_node_uri = f"{lnd.identity_pubkey}@127.0.0.1:{lnd.p2p_port}"
    electrum_to_lnd_cp = electrum.open_channel(lnd_node_uri, channel_sats)

    # Now have LND open back. Peer is already connected from the first open.
    await lnd.connect_peer(
        electrum.identity_pubkey, "127.0.0.1", electrum.lightning_p2p_port,
    )
    lnd_to_electrum_cp = await lnd.open_channel_sync(
        electrum.identity_pubkey, channel_sats,
    )

    # Mine extra blocks past channel-ready so gossip can disseminate
    # channel_update messages before any test tries to route over them.
    btc.mine_to_self(10)
    await asyncio.sleep(3.0)

    # Wait for LND to see both channels active.
    for _ in range(60):
        chans = await lnd.list_channels()
        cps = {c.get("channel_point"): c.get("active") for c in chans}
        if cps.get(electrum_to_lnd_cp) is True and cps.get(lnd_to_electrum_cp) is True:
            break
        await asyncio.sleep(1.0)

    return LndElectrumPair(
        bitcoind=btc, fulcrum=fulcrum, lnd=lnd, electrum=electrum,
        electrum_to_lnd_channel_point=electrum_to_lnd_cp,
        lnd_to_electrum_channel_point=lnd_to_electrum_cp,
    )


async def _teardown_lnd_electrum_pair_async(pair: LndElectrumPair) -> None:
    with contextlib.suppress(Exception):
        await pair.lnd.stop_grpc()
    pair.electrum.stop()
    pair.lnd.stop()
    pair.fulcrum.stop()
    pair.bitcoind.stop()


@pytest.fixture(scope="function")
def lnd_electrum_pair(request, event_loop, _binaries, _fee_url, _wipe_data_dir) -> LndElectrumPair:
    """Full per-test setup for the Electrum dispatch tests."""
    short = uuid.uuid4().hex[:8]
    data_dir = DATA_DIR / f"{request.node.name}-{short}"
    data_dir.mkdir(parents=True, exist_ok=True)
    pair = event_loop.run_until_complete(_setup_lnd_electrum_pair_async(data_dir, _fee_url))
    try:
        yield pair
    finally:
        event_loop.run_until_complete(_teardown_lnd_electrum_pair_async(pair))


@pytest.fixture(scope="function")
def lnd_pair_no_channels(
    request, event_loop, _binaries, _fee_url, _wipe_data_dir,
) -> LndPairNoChannels:
    """Per-test bitcoind + 2 LNDs (A funded as bitcart's wallet, B as
    the 'clientnode' / OWN_LIGHTNING_NODES peer), funded 2 BTC each
    and pre-peered, but NO channels opened. Tests open channels
    themselves (typically via the function under test) so they can
    assert on the channel that gets created.

    Same data-dir layout + cleanup pattern as `lnd_pair`."""
    short = uuid.uuid4().hex[:8]
    data_dir = DATA_DIR / f"{request.node.name}-{short}"
    data_dir.mkdir(parents=True, exist_ok=True)
    pair = event_loop.run_until_complete(
        _setup_lnd_pair_no_channels_async(data_dir, _fee_url)
    )
    try:
        yield pair
    finally:
        event_loop.run_until_complete(
            _teardown_lnd_pair_no_channels_async(pair)
        )


@pytest.fixture(scope="function")
def lnd_pair(request, event_loop, _binaries, _fee_url, _wipe_data_dir) -> LndPair:
    """Full per-test setup: bitcoind + 2 LNDs + funding + 0.2 BTC channels
    both ways + 6 confirmation blocks. Fresh ports + a unique data dir per
    test, so state is fully independent across tests (including parallel
    runs via pytest-xdist).

    The data dir is `tests/_data/<test-name>-<short-uuid>/` — kept after the
    test for log inspection; wiped at the next session start."""
    short = uuid.uuid4().hex[:8]
    data_dir = DATA_DIR / f"{request.node.name}-{short}"
    data_dir.mkdir(parents=True, exist_ok=True)
    pair = event_loop.run_until_complete(_setup_test_env_async(data_dir, _fee_url))
    try:
        yield pair
    finally:
        event_loop.run_until_complete(_teardown_test_env_async(pair))


# ---------------------------------------------------------------------------
# Submarine-swap test rig (loopserver + loopd against real bitcoind/LNDs)
#
# Layout:
#   - bitcoind regtest                              (re-used from existing pattern)
#   - LND-A (CLIENT)   : the wallet liquidityhelper would point loopd at
#   - LND-S (SERVER)   : LND backing the loopserver; receives the swap LN payment
#   - loopserver (docker/podman, `lightninglabs/loopserver:latest`) — talks gRPC
#     to LND-S, listens on a host port that loopd connects to
#   - loopd (binary, spawned via swap_providers.LoopdInstance) — talks to LND-A
#     and to loopserver (via `--server.host` + `--server.notls`)
#
# Channel topology: A -> S with 0.2 BTC, so the client (A) can route a swap
# LN payment to the server-side LND (S), which is what the swap protocol
# requires for an LN -> on-chain (loop-out) swap to settle.
#
# Container runtime: prefers `podman` (rootless, no daemon required), falls
# back to `docker`. If neither is installed, tests requesting `loop_rig`
# are skipped — they cannot run without a swap server.
# ---------------------------------------------------------------------------

LOOPSERVER_IMAGE = "docker.io/lightninglabs/loopserver:latest"
# loopserver listens for swap-client gRPC on this fixed port. It is NOT
# configurable via flags in the published image; we port-publish it on the
# host so loopd can reach it at 127.0.0.1:LOOPSERVER_GRPC_PORT.
LOOPSERVER_GRPC_PORT = 11009
# Inside a podman/docker container running with slirp4netns (rootless
# default) `--allow_host_loopback=true`, the host's 127.0.0.1 is reachable
# at this gateway IP. We use it to point the in-container loopserver at
# bitcoind/LND-S which bind only on host loopback.
SLIRP_HOST_LOOPBACK_IP = "10.0.2.2"


def _container_engine() -> Optional[str]:
    for engine in ("podman", "docker"):
        if shutil.which(engine):
            return engine
    return None


@dataclass
class LoopServerContainer:
    """A running loopserver container, bound to an LND-S backend."""
    engine: str
    container_id: str
    listen_host: str    # "127.0.0.1"
    listen_port: int    # loopd connects to this with --server.notls
    log_path: Optional[Path] = None  # if set, logs are dumped here before rm

    def stop(self) -> None:
        if self.log_path is not None:
            try:
                logs = subprocess.run(
                    [self.engine, "logs", self.container_id],
                    capture_output=True, timeout=10,
                ).stdout or b""
                self.log_path.write_bytes(logs)
            except Exception:
                pass
        subprocess.run(
            [self.engine, "rm", "-f", self.container_id],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


@dataclass
class LoopRig:
    """Full submarine-swap rig: a regtest bitcoind, a client LND (A), a
    server-side LND (S) with a 0.2 BTC channel from A->S, a running
    loopserver container talking to S, plus a `loopd` instance attached
    to A.

    Tests interact with this via:
      - Inline wallet dicts (id == `test-wallet-<lnd-name>`); see autoloop_tests.py for the canonical pattern.
      - The pre-constructed `swap_providers.LoopdManager` accessible as
        `rig.loopd_manager` — it has the LoopdInstance for A pre-registered.
    """
    bitcoind: BitcoindNode
    a: LndNode
    s: LndNode
    a_to_s_channel_point: str
    loopserver: LoopServerContainer
    loopd_manager: Any  # swap_providers.LoopdManager — typed `Any` to avoid import cycle


async def _setup_loop_rig_async(data_dir: Path, fee_url: str) -> LoopRig:
    # Fail fast if no container runtime is available — caller (fixture) will
    # convert this into a pytest skip.
    engine = _container_engine()
    if engine is None:
        raise RuntimeError(
            "neither podman nor docker is installed; loop_rig requires one of "
            "them to run lightninglabs/loopserver"
        )

    btc = BitcoindNode(bin_dir=BIN_DIR, data_dir=data_dir / "bitcoind")
    btc.start()
    btc.ensure_miner_wallet()
    btc.mine_to_self(101)

    a = LndNode(
        name="A", bin_dir=BIN_DIR, lnddir=data_dir / "lnd-a",
        bitcoind_p2p_port=btc.p2p_port, fee_url=fee_url,
    )
    s = LndNode(
        name="S", bin_dir=BIN_DIR, lnddir=data_dir / "lnd-s",
        bitcoind_p2p_port=btc.p2p_port, fee_url=fee_url,
    )
    a.start(); await a.init_wallet(); await a.open_lightning()
    s.start(); await s.init_wallet(); await s.open_lightning()
    await a.wait_synced(); await s.wait_synced()

    btc.send(await a.new_address(), 2.0)
    btc.send(await s.new_address(), 2.0)
    btc.mine_to_self(6)
    for n in (a, s):
        for _ in range(60):
            if await n.wallet_balance_sats() >= int(2 * 100_000_000 * 0.99):
                break
            await asyncio.sleep(1.0)
        else:
            raise RuntimeError(f"lnd {n.name} never saw confirmed funds")

    # Open A->S so A can route swap LN payments to S.
    await a.connect_peer(s.identity_pubkey, "127.0.0.1", s.p2p_port)
    a_to_s_cp = await a.open_channel_sync(s.identity_pubkey, int(0.2 * 100_000_000))
    btc.mine_to_self(10)
    for _ in range(60):
        chans = await a.list_channels()
        cps = {c.get("channel_point"): c.get("active") for c in chans}
        if cps.get(a_to_s_cp) is True:
            break
        await asyncio.sleep(1.0)
    await asyncio.sleep(3.0)  # gossip settle for channel_update

    # Stand up loopserver in a container. The image expects:
    #   - an LND backend exposed via gRPC + macaroon  (we mount tls/macaroon)
    #   - a bitcoind backend exposed via RPC + ZMQ    (existing fixture wires it)
    # loopserver binds its gRPC on the host's :11009 (fixed default — not
    # configurable in this image). With --network=host the container shares
    # the host network namespace, so loopd on the host can reach it at
    # 127.0.0.1:11009 directly.
    grpc_port = LOOPSERVER_GRPC_PORT
    src_macaroon = (
        s.lnddir / "data" / "chain" / "bitcoin" / "regtest" / "admin.macaroon"
    )
    src_tls = s.lnddir / "tls.cert"
    # podman rootless remaps UIDs, so files owned by our host user appear as
    # an unmapped UID inside the container and yield EACCES. Stage copies
    # with mode 0644 in a dedicated dir, and mount those instead.
    stage = data_dir / "loopserver-mounts"
    stage.mkdir(parents=True, exist_ok=True)
    s_tls = stage / "tls.cert"
    s_macaroon = stage / "admin.macaroon"
    shutil.copy2(src_tls, s_tls)
    shutil.copy2(src_macaroon, s_macaroon)
    s_tls.chmod(0o644)
    s_macaroon.chmod(0o644)
    stage.chmod(0o755)
    # ":Z" is an SELinux relabel; harmless when SELinux is disabled but
    # required on Fedora-likes. Docker accepts it too.
    mount_suffix = ",Z" if engine == "podman" else ""
    # We deliberately avoid --network=host: the loopserver image bundles an
    # embedded postgres on :5432, which collides with any system postgres
    # the host happens to run. slirp4netns gives loopserver its own netns
    # (so postgres works) and `allow_host_loopback=true` lets it reach the
    # host's loopback-bound bitcoind/LND-S via SLIRP_HOST_LOOPBACK_IP.
    network_arg = (
        "--network=slirp4netns:allow_host_loopback=true"
        if engine == "podman"
        else "--add-host=host.docker.internal:host-gateway"
    )
    host_ip = SLIRP_HOST_LOOPBACK_IP if engine == "podman" else "host.docker.internal"
    container_args = [
        # NOTE: no --rm; we want logs to survive an early exit so the wait
        # loop below can capture them. Stop+rm explicitly in teardown.
        engine, "run", "-d",
        network_arg,
        "-p", f"{LOOPSERVER_GRPC_PORT}:{LOOPSERVER_GRPC_PORT}",
        # loopserver's embedded postgres runs a migration that does
        #   IF current_setting('TIMEZONE') <> 'Etc/UTC' THEN RAISE
        # and fails when TZ is plain 'UTC'. Force the canonical form.
        "-e", "TZ=Etc/UTC",
        "-e", "PGTZ=Etc/UTC",
        "-v", f"{s_tls}:/lnd/tls.cert:ro{mount_suffix}",
        "-v", f"{s_macaroon}:/lnd/admin.macaroon:ro{mount_suffix}",
        LOOPSERVER_IMAGE,
        "daemon",
        "--maxamt=5000000",
        "--insecure",                              # disable server-side TLS for tests
        "--no-commit-hash",
        f"--lnd.host={host_ip}:{s.rpc_port}",
        "--lnd.macaroonpath=/lnd/admin.macaroon",
        "--lnd.tlspath=/lnd/tls.cert",
        f"--bitcoin.host={host_ip}:{btc.rpc_port}",
        f"--bitcoin.user={btc.rpc_user}",
        f"--bitcoin.password={btc.rpc_password}",
        f"--bitcoin.zmqpubrawblock=tcp://{host_ip}:{btc.zmq_block_port}",
        f"--bitcoin.zmqpubrawtx=tcp://{host_ip}:{btc.zmq_tx_port}",
    ]
    try:
        container_id = subprocess.check_output(
            container_args, stderr=subprocess.STDOUT, timeout=120,
        ).decode().strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"failed to start loopserver via {engine}: {e.output.decode(errors='replace')}"
        ) from e

    # Wait for loopserver gRPC to accept TCP AND for the container to log
    # "Starting gRPC listener" (otherwise the published port is just the
    # podman proxy and connections get reset until loopserver is ready).
    started = False
    deadline = time.time() + 120
    while time.time() < deadline:
        logs_so_far = subprocess.run(
            [engine, "logs", container_id],
            capture_output=True, text=True, timeout=10,
        ).stdout or ""
        if ("RPC server listening" in logs_so_far
                or "starting gRPC listener" in logs_so_far.lower()
                or "Starting gRPC listener" in logs_so_far):
            try:
                with socket.create_connection(("127.0.0.1", grpc_port), timeout=1):
                    started = True
                    break
            except OSError:
                pass
        time.sleep(1.0)
    if not started:
        logs = subprocess.run(
            [engine, "logs", container_id],
            capture_output=True, text=True, timeout=10,
        ).stdout or ""
        subprocess.run([engine, "rm", "-f", container_id], check=False)
        raise RuntimeError(
            f"loopserver never logged gRPC-listener-ready within 120s; "
            f"last logs:\n{logs[-2000:]}"
        )

    server = LoopServerContainer(
        engine=engine, container_id=container_id,
        listen_host="127.0.0.1", listen_port=grpc_port,
        log_path=data_dir / "loopserver.log",
    )

    # Now build a LoopdManager + LoopdInstance attached to LND-A. We DO NOT
    # call the manager's `get_loopd_for_wallet` here because that path goes
    # through the Bitcart API; instead we construct + register the instance
    # directly, which is the documented test hook.
    from swap_providers import (
        LoopdManager, LoopdInstance, ensure_loop_binaries, LOOP_BIN_DIR,
    )
    await ensure_loop_binaries(LOOP_BIN_DIR)

    a_macaroon_path = (
        a.lnddir / "data" / "chain" / "bitcoin" / "regtest" / "admin.macaroon"
    )
    instance = LoopdInstance(
        wallet_id=f"test-wallet-{a.name.lower()}",
        bin_dir=LOOP_BIN_DIR,
        data_dir=data_dir / "loopd-a",
        lnd_grpc_host=f"127.0.0.1:{a.rpc_port}",
        lnd_tls_cert_bytes=(a.lnddir / "tls.cert").read_bytes(),
        lnd_macaroon_bytes=a_macaroon_path.read_bytes(),
        network="regtest",
        server_host=f"127.0.0.1:{grpc_port}",
        server_notls=True,
    )
    await instance.start()

    manager = LoopdManager(
        bin_dir=LOOP_BIN_DIR,
        data_root=data_dir / "loopd",
        network="regtest",
        server_host=f"127.0.0.1:{grpc_port}",
        server_notls=True,
    )
    manager.register_existing(instance)

    return LoopRig(
        bitcoind=btc, a=a, s=s,
        a_to_s_channel_point=a_to_s_cp,
        loopserver=server,
        loopd_manager=manager,
    )


async def _teardown_loop_rig_async(rig: "LoopRig") -> None:
    with contextlib.suppress(Exception):
        await rig.loopd_manager.stop_all()
    with contextlib.suppress(Exception):
        rig.loopserver.stop()
    with contextlib.suppress(Exception):
        await rig.a.stop_grpc()
    with contextlib.suppress(Exception):
        await rig.s.stop_grpc()
    rig.a.stop(); rig.s.stop(); rig.bitcoind.stop()


@pytest.fixture(scope="function")
def loop_rig(request, event_loop, _binaries, _fee_url, _wipe_data_dir) -> "LoopRig":
    """Full per-test swap rig (see _setup_loop_rig_async docstring).

    Skipped if neither podman nor docker is installed. Skipped if pulling /
    starting the loopserver image fails (network unavailable, image gone,
    etc.). Otherwise yields a `LoopRig`.
    """
    if _container_engine() is None:
        pytest.skip("loop_rig requires podman or docker; neither was found on PATH")
    short = uuid.uuid4().hex[:8]
    data_dir = DATA_DIR / f"looprig-{request.node.name}-{short}"
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        rig = event_loop.run_until_complete(
            _setup_loop_rig_async(data_dir, _fee_url)
        )
    except RuntimeError as e:
        pytest.skip(f"loop_rig setup failed: {e}")
        return  # unreachable; pytest.skip raises
    try:
        yield rig
    finally:
        event_loop.run_until_complete(_teardown_loop_rig_async(rig))
