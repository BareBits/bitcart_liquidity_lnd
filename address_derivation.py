"""BIP32 child-address derivation for on-chain payment destinations.

The engine no longer accepts fixed on-chain addresses for cashouts,
dev-fee payments, or referral payments. Each destination is configured
as an xpub instead; we derive a fresh receive-chain address per send
(BIP-32 standard receive chain `<xpub>/0/<index>`). The counter for
each xpub is persisted in `DerivedAddressIndex` so a fresh address is
used on every send across engine restarts.

Inputs we accept:

  Mainnet xpub flavors
    `xpub`  -> P2PKH         (legacy, `1...` addresses)
    `ypub`  -> P2SH-P2WPKH   (wrapped segwit, `3...`)
    `zpub`  -> P2WPKH        (native segwit, `bc1q...`)

  Testnet xpub flavors (also used for signet, Mutinynet, regtest —
  the engine selects the HRP at encode time based on the deployment
  network, not the xpub version bytes)
    `tpub`  -> P2PKH
    `upub`  -> P2SH-P2WPKH
    `vpub`  -> P2WPKH        (-> `tb1q...` on testnet/signet/Mutinynet,
                              re-encoded `bcrt1q...` on regtest)

  Depth handling: we accept BOTH depth-1 xpubs (Electrum native-segwit
  default — derives via `m/0'`) AND depth-3 xpubs (BIP-44/49/84
  standard — derives via `m/84'/0'/0'` etc). From whichever depth the
  xpub is at, we derive `/0/<index>` to produce the receive-chain
  address. Library-provided strict BIP-84 helpers reject the depth-1
  case, so we use the lower-level Bip32Slip10Secp256k1 throughout.

  Network validation: at validate_xpub() we cross-check the xpub's
  version-byte family (mainnet vs testnet) against the deployment's
  detected Bitcoin network. A mainnet zpub on a testnet deployment is
  rejected — sending real-money fees to a testnet wallet would be
  catastrophic, and vice-versa nothing would arrive.

  Multi-tenant note for shared xpubs (e.g. the BareBits fee xpub):
  every install increments its OWN local counter from 0, so addresses
  across installs will collide on the receive side. The 100%-reuse
  status quo (a single hardcoded address forever) is strictly worse;
  per-install offset assignment is a future refinement.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import base58
from bip_utils import (
    Bip32KeyNetVersions,
    Bip32Slip10Secp256k1,
    P2PKHAddr,
    P2SHAddr,
    P2WPKHAddr,
)
from bip_utils.bip.bip32 import Bip32PrivateKey  # noqa: F401  (re-exported for callers)

logger = logging.getLogger(__name__)


# Version-byte registry. Maps the leading 4 bytes of the deserialized
# extended-key payload to its (network, address-type) interpretation,
# plus the Bip32KeyNetVersions instance the deserializer needs.
#
# Sourced from the BIP standards:
#   xpub/ypub/zpub  (BIP 32 / BIP 49 / BIP 84 mainnet pub)
#   tpub/upub/vpub  (their testnet counterparts)
_VERSION_BYTES = {
    # mainnet
    "0488b21e": ("mainnet", "p2pkh"),     # xpub
    "049d7cb2": ("mainnet", "p2sh_p2wpkh"),  # ypub
    "04b24746": ("mainnet", "p2wpkh"),    # zpub
    # testnet (also covers signet, Mutinynet, regtest at the xpub level)
    "043587cf": ("testnet", "p2pkh"),     # tpub
    "044a5262": ("testnet", "p2sh_p2wpkh"),  # upub
    "045f1cf6": ("testnet", "p2wpkh"),    # vpub
}

# Matching priv-counterparts so Bip32Slip10Secp256k1 will accept the
# extended key without complaining about wrong net version.
_NET_VERSIONS = {
    "0488b21e": Bip32KeyNetVersions(
        pub_net_ver=bytes.fromhex("0488b21e"),
        priv_net_ver=bytes.fromhex("0488ade4"),
    ),
    "049d7cb2": Bip32KeyNetVersions(
        pub_net_ver=bytes.fromhex("049d7cb2"),
        priv_net_ver=bytes.fromhex("049d7878"),
    ),
    "04b24746": Bip32KeyNetVersions(
        pub_net_ver=bytes.fromhex("04b24746"),
        priv_net_ver=bytes.fromhex("04b2430c"),
    ),
    "043587cf": Bip32KeyNetVersions(
        pub_net_ver=bytes.fromhex("043587cf"),
        priv_net_ver=bytes.fromhex("04358394"),
    ),
    "044a5262": Bip32KeyNetVersions(
        pub_net_ver=bytes.fromhex("044a5262"),
        priv_net_ver=bytes.fromhex("044a4e28"),
    ),
    "045f1cf6": Bip32KeyNetVersions(
        pub_net_ver=bytes.fromhex("045f1cf6"),
        priv_net_ver=bytes.fromhex("045f18bc"),
    ),
}

# HRP per deployment network. Native-segwit addresses get encoded with
# this prefix; legacy P2PKH and P2SH-P2WPKH use the base58 version byte
# instead, which we resolve from the deployment network in the same
# way.
_NETWORK_HRP = {
    "mainnet": "bc",
    # testnet3, testnet4, signet, Mutinynet — all use `tb`
    "testnet": "tb",
    "regtest": "bcrt",
}

# Base58 version bytes for legacy P2PKH and P2SH-P2WPKH on each
# deployment network. Native-segwit doesn't use these (it uses HRP-
# based bech32 above), but we still support legacy / wrapped-segwit
# xpub flavors so operators with older wallets aren't excluded.
_NETWORK_P2PKH_VERSION = {
    "mainnet": b"\x00",   # `1...` addresses
    "testnet": b"\x6f",   # `m.../n...`
    "regtest": b"\x6f",   # regtest reuses testnet P2PKH version byte
}
_NETWORK_P2SH_VERSION = {
    "mainnet": b"\x05",   # `3...` addresses
    "testnet": b"\xc4",   # `2...`
    "regtest": b"\xc4",   # same as testnet
}


class XpubError(ValueError):
    """Raised when an xpub fails to decode or doesn't match its expected
    deployment network."""


def _decode_extended_key(xpub: str) -> Tuple[str, str, int]:
    """Decode base58-check, return (version_hex, address_type, depth).

    Raises XpubError on malformed input or unknown version bytes."""
    try:
        raw = base58.b58decode_check(xpub.strip())
    except Exception as e:
        raise XpubError(f"xpub failed base58 decode: {e}") from e
    if len(raw) < 5:
        raise XpubError("xpub too short")
    version_hex = raw[:4].hex()
    if version_hex not in _VERSION_BYTES:
        raise XpubError(
            f"xpub version bytes 0x{version_hex} not recognized "
            f"(expected one of {sorted(_VERSION_BYTES.keys())})"
        )
    depth = raw[4]
    network, address_type = _VERSION_BYTES[version_hex]
    return version_hex, address_type, depth


def _xpub_network_family(xpub: str) -> str:
    """Return "mainnet" or "testnet" based on the xpub's version
    bytes alone. Testnet xpubs (tpub/upub/vpub) are also used for
    signet/Mutinynet/regtest — the HRP swap happens at encode time."""
    version_hex, _, _ = _decode_extended_key(xpub)
    return _VERSION_BYTES[version_hex][0]


def _xpub_address_type(xpub: str) -> str:
    """Return "p2pkh" | "p2sh_p2wpkh" | "p2wpkh" based on version
    bytes."""
    version_hex, _, _ = _decode_extended_key(xpub)
    return _VERSION_BYTES[version_hex][1]


def validate_xpub(xpub: str, deployment_network: str) -> Tuple[bool, Optional[str]]:
    """Validate `xpub` for use on a deployment running `deployment_network`.

    Returns `(ok, reason_if_not_ok)`. Used by the engine at startup
    and as a per-call sanity check inside the payment paths so a
    misconfigured xpub never silently lands sats in the wrong network.

    `deployment_network` is one of "mainnet", "testnet", "regtest"
    (testnet3/testnet4/signet/Mutinynet all map to "testnet" in
    `_detect_bitcoin_network`'s output).
    """
    if not xpub:
        return False, "xpub is empty/unset"
    try:
        xpub_family = _xpub_network_family(xpub)
    except XpubError as e:
        return False, str(e)
    # Testnet xpubs serve all non-mainnet deployments. Mainnet xpubs
    # are strictly mainnet only.
    if deployment_network == "mainnet":
        if xpub_family != "mainnet":
            return False, (
                f"deployment is mainnet but xpub is a {xpub_family} "
                f"format (xpub/ypub/zpub required, not tpub/upub/vpub)"
            )
    else:
        if xpub_family != "testnet":
            return False, (
                f"deployment is {deployment_network} but xpub is a "
                f"{xpub_family} format (tpub/upub/vpub required, not "
                f"xpub/ypub/zpub)"
            )
    return True, None


def _hrp_for_network(network: str) -> str:
    if network not in _NETWORK_HRP:
        raise XpubError(
            f"unknown deployment network {network!r}; expected one of "
            f"{sorted(_NETWORK_HRP.keys())}"
        )
    return _NETWORK_HRP[network]


def _encode_address(pub_key_bytes: bytes, address_type: str, network: str) -> str:
    """Encode the compressed pubkey to an address according to the
    xpub's address type and the deployment network's HRP/version-byte."""
    if address_type == "p2wpkh":
        return P2WPKHAddr.EncodeKey(
            pub_key_bytes,
            hrp=_hrp_for_network(network),
            wit_ver=0,
        )
    if address_type == "p2sh_p2wpkh":
        return P2SHAddr.EncodeKey(
            pub_key_bytes,
            net_ver=_NETWORK_P2SH_VERSION[network],
        )
    if address_type == "p2pkh":
        return P2PKHAddr.EncodeKey(
            pub_key_bytes,
            net_ver=_NETWORK_P2PKH_VERSION[network],
        )
    raise XpubError(f"unsupported address type {address_type!r}")


def peek_address(xpub: str, network: str, index: int) -> str:
    """Return the address at the given index without touching DB state.

    Used by tests and diagnostics. Production code calls derive_next_
    address() instead, which also updates the persisted counter."""
    version_hex, address_type, _ = _decode_extended_key(xpub)
    key_net_ver = _NET_VERSIONS[version_hex]
    root = Bip32Slip10Secp256k1.FromExtendedKey(xpub.strip(), key_net_ver)
    child = root.DerivePath(f"0/{index}")
    pub_key_bytes = child.PublicKey().RawCompressed().ToBytes()
    return _encode_address(pub_key_bytes, address_type, network)


def derive_next_address(xpub: str, purpose: str, network: str) -> str:
    """Derive and consume the next unused receive-chain address.

    Increments and persists the DerivedAddressIndex counter for `xpub`
    BEFORE returning the address — so a crash between derivation and
    broadcast doesn't reuse the same address. A skipped index is
    harmless (gap-limit on the recipient side handles it).

    `purpose` is stored as `last_purpose` on the row purely for
    operator-side diagnostics; the counter itself is per-xpub.

    Raises XpubError on malformed xpub or unknown network.
    """
    # Late import to avoid circular module load — node_database imports
    # config, which imports utilities that may want this module to load
    # cleanly without DB-side effects.
    from node_database import DerivedAddressIndex
    row, _ = DerivedAddressIndex.get_or_create(
        xpub=xpub.strip(),
        defaults={"last_purpose": purpose, "next_index": 0},
    )
    used_index = int(row.next_index)
    addr = peek_address(xpub, network, used_index)
    row.next_index = used_index + 1
    row.last_purpose = purpose
    from common_functions import utcnow_naive
    row.updated = utcnow_naive()
    row.save()
    logger.info(
        "address_derivation: derived %s for purpose=%s at index=%d "
        "(xpub fingerprint=%s)",
        addr, purpose, used_index, _xpub_short(xpub),
    )
    return addr


def _xpub_short(xpub: str) -> str:
    """Short identifier safe for logs — first 12 chars of the xpub.
    The xpub itself is public information, so this is just to keep
    log lines from getting unwieldy."""
    return xpub.strip()[:12]
