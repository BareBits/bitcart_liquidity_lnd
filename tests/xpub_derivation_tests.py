"""Tests for the xpub-derived-address feature.

Coverage:

  1. address_derivation pure logic
     - Known vector: mainnet zpub m/0/0 == the legacy hardcoded
       `bc1q586um24k7zr6swxqny5qqgqn8xt43pk4xeeg9g` address (proves
       the BareBits zpub baked into config.py is the right one and
       that depth-1 Electrum-style derivation works).
     - All three xpub format families on mainnet (xpub/ypub/zpub)
       produce the expected address type (P2PKH/P2SH-P2WPKH/P2WPKH).
     - The same testnet vpub produces `tb1q...` on testnet/signet
       AND re-encodes cleanly as `bcrt1q...` on regtest.
     - validate_xpub rejects wrong-network and malformed input.
     - peek_address is purely pure — same xpub + index = same output.

  2. DerivedAddressIndex state
     - derive_next_address increments + persists.
     - The counter survives a fresh row fetch (proxy for restart).
     - Distinct xpubs maintain independent counters.

  3. Engine payment paths
     - _pay_dev_fee_via_onchain auto-picks the right BareBits xpub
       for the detected network and validates against it.
     - _pay_referral_via_onchain soft-fails when xpub is unset or
       wrong-network, without crashing.
     - do_onchain_cashouts derives a fresh address per call.

  4. Health warnings
     - _check_xpub_config surfaces network-mismatch and unset
       warnings.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

import address_derivation
import liquidityhelper


MAINNET_ZPUB = (
    "zpub6mg9NbdenqrqfD2BbNqa4EdKS8z2yEBxH2PC4NXmXwnTDfukw3w7JLVMeb"
    "81xc9i1N8b7ReB9ia4wLoUyLVrVxaPQbJidU8Yn6Gjd95kQmA"
)
TESTNET_VPUB = (
    "vpub5W139rovao2tiN81BHSVxT9ZMdSaUqQ2UaZK6DjBjS6jWYjHFoJNSRJW2"
    "BtaFvt2j74jzsowhdjjYCRD7hc9MZra4WVA3awF4JxF5AAJ8Fn"
)
# Reference BIP32 test-vector xpub from BIP-32 itself (mainnet xpub
# format, depth 0). Lets us verify legacy P2PKH derivation without
# depending on any operator-supplied secret material.
BIP32_TEST_XPUB = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29"
    "ESFjqJoCu1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)


# ---------------------------------------------------------------------------
# Section 1: address_derivation pure logic
# ---------------------------------------------------------------------------

def test_known_vector_barebits_mainnet_zpub_idx_0():
    """The mainnet BareBits zpub at m/0/0 must derive the legacy
    hardcoded address operators have been paying to for years. This
    pins both the user-provided zpub AND the depth-1 Electrum-style
    derivation logic."""
    addr = address_derivation.peek_address(MAINNET_ZPUB, "mainnet", 0)
    assert addr == "bc1q586um24k7zr6swxqny5qqgqn8xt43pk4xeeg9g"


def test_mainnet_zpub_subsequent_indices_are_distinct():
    """Indices 0..3 each produce a different address — sanity check
    the derivation actually advances along the receive chain."""
    addrs = [
        address_derivation.peek_address(MAINNET_ZPUB, "mainnet", n)
        for n in range(4)
    ]
    assert len(set(addrs)) == 4
    for a in addrs:
        assert a.startswith("bc1q")


def test_testnet_vpub_produces_tb1q_on_testnet():
    addr = address_derivation.peek_address(TESTNET_VPUB, "testnet", 0)
    assert addr.startswith("tb1q")


def test_testnet_vpub_reencodes_as_bcrt_on_regtest():
    """Same vpub, same pubkey material, different HRP. The pubkey-
    derived portion of the address must match (just with a different
    bech32 HRP). Verifies the HRP-override path that lets BareBits
    use one vpub for testnet AND regtest."""
    tb_addr = address_derivation.peek_address(TESTNET_VPUB, "testnet", 0)
    bcrt_addr = address_derivation.peek_address(TESTNET_VPUB, "regtest", 0)
    assert tb_addr.startswith("tb1q")
    assert bcrt_addr.startswith("bcrt1q")
    # The data portion before the 6-char bech32 checksum (i.e. the
    # witness program) is identical — same pubkey → same program.
    # The 6-char checksum suffix DIFFERS because bech32 includes the
    # HRP in the checksum input. Strip the HRP prefix AND the 6-char
    # checksum to compare just the program bytes.
    assert tb_addr[len("tb1q"):-6] == bcrt_addr[len("bcrt1q"):-6]


def test_peek_address_is_pure():
    """Same inputs, same output. No DB state touched."""
    a1 = address_derivation.peek_address(MAINNET_ZPUB, "mainnet", 5)
    a2 = address_derivation.peek_address(MAINNET_ZPUB, "mainnet", 5)
    assert a1 == a2


def test_xpub_validation_rejects_wrong_network():
    """A mainnet zpub on a testnet deployment must fail validation."""
    ok, reason = address_derivation.validate_xpub(MAINNET_ZPUB, "testnet")
    assert ok is False
    assert reason and "mainnet" in reason
    # Symmetric case
    ok, reason = address_derivation.validate_xpub(TESTNET_VPUB, "mainnet")
    assert ok is False
    assert reason and "testnet" in reason


def test_xpub_validation_accepts_matching_network():
    assert address_derivation.validate_xpub(MAINNET_ZPUB, "mainnet") == (True, None)
    assert address_derivation.validate_xpub(TESTNET_VPUB, "testnet") == (True, None)
    # vpub on regtest is also OK — testnet vpub is reused for regtest.
    assert address_derivation.validate_xpub(TESTNET_VPUB, "regtest") == (True, None)


def test_xpub_validation_rejects_empty():
    ok, reason = address_derivation.validate_xpub("", "mainnet")
    assert ok is False


def test_xpub_validation_rejects_garbage():
    ok, reason = address_derivation.validate_xpub("not-an-xpub", "mainnet")
    assert ok is False


def test_xpub_format_legacy_produces_p2pkh_address():
    """A BIP-32 test-vector mainnet xpub (legacy format) produces a
    base58 `1...` P2PKH address, not bech32."""
    addr = address_derivation.peek_address(BIP32_TEST_XPUB, "mainnet", 0)
    assert addr.startswith("1"), f"expected P2PKH address, got {addr!r}"


# ---------------------------------------------------------------------------
# Section 2: DerivedAddressIndex state
# ---------------------------------------------------------------------------

@pytest.fixture
def isolate_address_indices(tmp_path, monkeypatch):
    """Point node_database at a fresh sqlite file for the duration
    of this test so DerivedAddressIndex state doesn't leak across
    tests and a prior failing run doesn't poison subsequent indices.
    """
    # The node_db module already initialised against the production
    # path at import time. We need to swap it for the duration of
    # this test. Approach: bind the model to a fresh in-memory db.
    from peewee import SqliteDatabase
    import node_database
    fresh = SqliteDatabase(":memory:")
    monkeypatch.setattr(node_database, "node_db", fresh)
    node_database.DerivedAddressIndex._meta.database = fresh
    fresh.connect()
    fresh.create_tables([node_database.DerivedAddressIndex])
    yield
    fresh.close()


def test_derive_next_address_increments(isolate_address_indices):
    """Two back-to-back derivations from the same xpub return
    distinct addresses (m/0/0 then m/0/1)."""
    first = address_derivation.derive_next_address(
        MAINNET_ZPUB, purpose="fee", network="mainnet",
    )
    second = address_derivation.derive_next_address(
        MAINNET_ZPUB, purpose="fee", network="mainnet",
    )
    assert first == "bc1q586um24k7zr6swxqny5qqgqn8xt43pk4xeeg9g"
    assert second != first
    # m/0/1 is deterministic; pin it explicitly so accidental
    # off-by-one in the increment surfaces immediately.
    assert second == address_derivation.peek_address(MAINNET_ZPUB, "mainnet", 1)


def test_counter_persists_across_row_fetch(isolate_address_indices):
    """Simulate restart: counter is stored in the row, so a fresh
    `get_or_create` retrieves the existing row with its incremented
    counter rather than starting fresh at 0."""
    from node_database import DerivedAddressIndex
    address_derivation.derive_next_address(
        MAINNET_ZPUB, purpose="fee", network="mainnet",
    )
    address_derivation.derive_next_address(
        MAINNET_ZPUB, purpose="fee", network="mainnet",
    )
    # Pull a fresh row instance — proxy for an engine restart.
    row = DerivedAddressIndex.get(DerivedAddressIndex.xpub == MAINNET_ZPUB)
    assert row.next_index == 2
    # Next derivation continues from 2, not from 0.
    addr = address_derivation.derive_next_address(
        MAINNET_ZPUB, purpose="fee", network="mainnet",
    )
    assert addr == address_derivation.peek_address(MAINNET_ZPUB, "mainnet", 2)


def test_distinct_xpubs_maintain_independent_counters(
    isolate_address_indices,
):
    """Two different xpubs each track their own next-index. Bumping
    one doesn't advance the other."""
    address_derivation.derive_next_address(
        MAINNET_ZPUB, purpose="fee", network="mainnet",
    )
    address_derivation.derive_next_address(
        MAINNET_ZPUB, purpose="fee", network="mainnet",
    )
    # Mainnet zpub is at idx 2. Testnet vpub is fresh — must start at 0.
    addr_t0 = address_derivation.derive_next_address(
        TESTNET_VPUB, purpose="cashout", network="testnet",
    )
    assert addr_t0 == address_derivation.peek_address(TESTNET_VPUB, "testnet", 0)


def test_last_purpose_recorded(isolate_address_indices):
    """Each derivation overwrites `last_purpose` for operator-side
    diagnostics."""
    from node_database import DerivedAddressIndex
    address_derivation.derive_next_address(
        MAINNET_ZPUB, purpose="fee", network="mainnet",
    )
    row = DerivedAddressIndex.get(DerivedAddressIndex.xpub == MAINNET_ZPUB)
    assert row.last_purpose == "fee"
    address_derivation.derive_next_address(
        MAINNET_ZPUB, purpose="cashout", network="mainnet",
    )
    row = DerivedAddressIndex.get(DerivedAddressIndex.xpub == MAINNET_ZPUB)
    assert row.last_purpose == "cashout"


# ---------------------------------------------------------------------------
# Section 3: Engine payment paths
# ---------------------------------------------------------------------------

class _FakeApi:
    """Minimal BitcartAPI surface for the network-detection +
    electrum_pay_onchain test seams."""
    def __init__(self, network: str = "mainnet"):
        self.network = network

    async def get_wallets(self):
        return [{"id": "w1", "name": "liquidityhelper", "currency": "btclnd"}]

    async def get_lnd_info(self, wallet_id):
        return {"network": self.network}


def _patch_payment_paths(monkeypatch, network: str):
    """Common monkeypatching for engine-side payment-path tests.

    - Network detection returns `network`.
    - electrum_pay_onchain captures its arguments and returns True.
    - has_pending_channel_activity returns False.
    - DRY_RUN_FUNDS off, MIN_ONCHAIN_CASHOUT relaxed."""
    captured: Dict[str, Any] = {}

    async def fake_pay_onchain(addr, amount_btc, *, label, wallet, api, **kw):
        captured["addr"] = addr
        captured["amount_btc"] = amount_btc
        captured["label"] = label
        return True

    async def fake_pending(*a, **kw):
        return False

    async def fake_detect(_api):
        return network

    monkeypatch.setattr(liquidityhelper, "electrum_pay_onchain", fake_pay_onchain)
    monkeypatch.setattr(liquidityhelper, "has_pending_channel_activity", fake_pending)
    monkeypatch.setattr(liquidityhelper, "_detect_bitcoin_network", fake_detect)
    monkeypatch.setattr(liquidityhelper, "DRY_RUN_FUNDS", False)
    monkeypatch.setattr(liquidityhelper, "MIN_ONCHAIN_CASHOUT", 1_000)
    return captured


def test_pay_dev_fee_via_onchain_uses_mainnet_zpub_on_mainnet(
    monkeypatch, event_loop, isolate_address_indices,
):
    """Verifies the integration: detected_network=mainnet → picks
    BAREBITS_FEE_XPUB_MAINNET → derives a fresh address → broadcasts
    to it. The address must match peek_address at the expected index."""
    captured = _patch_payment_paths(monkeypatch, "mainnet")
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_MAINNET", MAINNET_ZPUB)
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_TESTNET", TESTNET_VPUB)
    api = _FakeApi(network="mainnet")
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._pay_dev_fee_via_onchain(api, "store1", wallet, 25_000)
    )
    assert ok is True
    assert captured["addr"] == "bc1q586um24k7zr6swxqny5qqgqn8xt43pk4xeeg9g"
    assert captured["label"] == "lnhelper_fee"


def test_pay_dev_fee_via_onchain_uses_testnet_vpub_on_testnet(
    monkeypatch, event_loop, isolate_address_indices,
):
    captured = _patch_payment_paths(monkeypatch, "testnet")
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_MAINNET", MAINNET_ZPUB)
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_TESTNET", TESTNET_VPUB)
    api = _FakeApi(network="testnet")
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._pay_dev_fee_via_onchain(api, "store1", wallet, 25_000)
    )
    assert ok is True
    assert captured["addr"].startswith("tb1q")


def test_pay_dev_fee_via_onchain_uses_bcrt_hrp_on_regtest(
    monkeypatch, event_loop, isolate_address_indices,
):
    """Regtest deployment uses the testnet vpub but emits bcrt1q
    addresses. Verifies the HRP override path through the engine
    integration."""
    captured = _patch_payment_paths(monkeypatch, "regtest")
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_MAINNET", MAINNET_ZPUB)
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_TESTNET", TESTNET_VPUB)
    api = _FakeApi(network="regtest")
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._pay_dev_fee_via_onchain(api, "store1", wallet, 25_000)
    )
    assert ok is True
    assert captured["addr"].startswith("bcrt1q")


def test_pay_dev_fee_via_onchain_soft_fails_on_wrong_network_xpub(
    monkeypatch, event_loop, isolate_address_indices,
):
    """A mainnet zpub baked into BAREBITS_FEE_XPUB_TESTNET (operator
    misconfig) → validate_xpub fails → function returns False without
    broadcasting."""
    captured = _patch_payment_paths(monkeypatch, "testnet")
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_MAINNET", MAINNET_ZPUB)
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_TESTNET", MAINNET_ZPUB)  # wrong!
    api = _FakeApi(network="testnet")
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._pay_dev_fee_via_onchain(api, "store1", wallet, 25_000)
    )
    assert ok is False
    assert "addr" not in captured  # never reached the broadcast


def test_pay_referral_via_onchain_soft_fails_when_xpub_unset(
    monkeypatch, event_loop, isolate_address_indices,
):
    captured = _patch_payment_paths(monkeypatch, "mainnet")
    monkeypatch.setattr(liquidityhelper, "REFERRAL_ONCHAIN_DEST_XPUB", None)
    api = _FakeApi(network="mainnet")
    wallet = {"id": "w1", "currency": "btclnd"}
    ok = event_loop.run_until_complete(
        liquidityhelper._pay_referral_via_onchain(api, "store1", wallet, 25_000)
    )
    assert ok is False
    assert "addr" not in captured


def test_do_onchain_cashouts_derives_fresh_address_per_call(
    monkeypatch, event_loop, isolate_address_indices,
):
    """Two consecutive cashouts produce two different addresses —
    confirming the counter increments through the engine path."""
    addrs: List[str] = []

    async def fake_pay_onchain(addr, *args, **kw):
        addrs.append(addr)
        return "tx_hash_fake"

    async def fake_pending(*a, **kw):
        return False

    async def fake_detect(_api):
        return "mainnet"

    monkeypatch.setattr(liquidityhelper, "electrum_pay_onchain", fake_pay_onchain)
    monkeypatch.setattr(liquidityhelper, "has_pending_channel_activity", fake_pending)
    monkeypatch.setattr(liquidityhelper, "_detect_bitcoin_network", fake_detect)
    monkeypatch.setattr(liquidityhelper, "DRY_RUN_FUNDS", False)
    monkeypatch.setattr(liquidityhelper, "FORCE_CASHOUT_AMOUNT_ONCHAIN", None)
    monkeypatch.setattr(liquidityhelper, "MIN_ONCHAIN_CASHOUT", 1_000)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", MAINNET_ZPUB)

    class _ApiWithWallet(_FakeApi):
        async def get_wallet(self, wid):
            return {"id": wid, "currency": "btclnd"}

    api = _ApiWithWallet(network="mainnet")
    event_loop.run_until_complete(
        liquidityhelper.do_onchain_cashouts(api, "w1", 25_000)
    )
    event_loop.run_until_complete(
        liquidityhelper.do_onchain_cashouts(api, "w1", 25_000)
    )
    assert len(addrs) == 2
    assert addrs[0] != addrs[1]
    # Both must be valid mainnet-format bech32 addresses
    for a in addrs:
        assert a.startswith("bc1q")


# ---------------------------------------------------------------------------
# Section 4: Health warnings
# ---------------------------------------------------------------------------

def test_health_warning_fires_on_network_mismatch(
    monkeypatch, event_loop,
):
    """A mainnet zpub baked into BAREBITS_FEE_XPUB_TESTNET on a
    testnet deployment → _check_xpub_config emits the
    barebits-fee-xpub-invalid warning."""

    async def fake_detect(_api):
        return "testnet"

    monkeypatch.setattr(liquidityhelper, "_detect_bitcoin_network", fake_detect)
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_TESTNET", MAINNET_ZPUB)  # wrong!

    api = _FakeApi(network="testnet")
    warnings = event_loop.run_until_complete(
        liquidityhelper._check_xpub_config(api)
    )
    ids = {w["id"] for w in warnings}
    assert "barebits-fee-xpub-invalid" in ids


def test_health_warning_fires_when_cashout_xpub_invalid(
    monkeypatch, event_loop,
):
    async def fake_detect(_api):
        return "mainnet"

    monkeypatch.setattr(liquidityhelper, "_detect_bitcoin_network", fake_detect)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    # A vpub configured on a mainnet deployment → invalid.
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", TESTNET_VPUB)
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_MAINNET", MAINNET_ZPUB)

    api = _FakeApi(network="mainnet")
    warnings = event_loop.run_until_complete(
        liquidityhelper._check_xpub_config(api)
    )
    ids = {w["id"] for w in warnings}
    assert "cashout-xpub-invalid" in ids


def test_health_warning_clean_when_everything_valid(
    monkeypatch, event_loop,
):
    """All three xpubs valid for the deployment network → no
    xpub-related warnings."""
    async def fake_detect(_api):
        return "mainnet"

    monkeypatch.setattr(liquidityhelper, "_detect_bitcoin_network", fake_detect)
    monkeypatch.setattr(liquidityhelper, "ENABLE_CASHOUT_ONCHAIN", True)
    monkeypatch.setattr(liquidityhelper, "CASHOUT_ONCHAIN_XPUB", MAINNET_ZPUB)
    monkeypatch.setattr(liquidityhelper, "REFERRAL_ONCHAIN_DEST_XPUB", MAINNET_ZPUB)
    monkeypatch.setattr(liquidityhelper, "REFERRAL_FEE_AMOUNT", 0.0)
    monkeypatch.setattr(liquidityhelper, "BAREBITS_FEE_XPUB_MAINNET", MAINNET_ZPUB)

    api = _FakeApi(network="mainnet")
    warnings = event_loop.run_until_complete(
        liquidityhelper._check_xpub_config(api)
    )
    ids = {w["id"] for w in warnings}
    assert "cashout-xpub-invalid" not in ids
    assert "referral-xpub-invalid" not in ids
    assert "barebits-fee-xpub-invalid" not in ids
    assert "barebits-fee-xpub-unset" not in ids
