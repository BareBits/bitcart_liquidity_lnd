"""Tests for _wallet_is_brand_new_lnd.

It gates the low-liquidity *email* (not the dashboard warning or the
top-up invoices) on a brand-new, never-funded LND wallet, so a fresh
install doesn't fire a "top up your wallet" email the operator already
expects. See calculate_topups() for the call site.
"""

from __future__ import annotations

import asyncio

import liquidityhelper


def _run(coro):
    """Drive a coroutine on a fresh one-shot loop (matches the other
    unit tests; avoids touching the thread-current-loop pointer)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patch_lnd_rpc(monkeypatch, *, transactions=None, raises=False):
    async def fake_lnd_rpc(api_obj, wallet_id, method, params, service):
        assert method == "GetTransactions"
        assert service == "Lightning"
        if raises:
            raise RuntimeError("simulated gRPC failure")
        return {"transactions": list(transactions or [])}

    monkeypatch.setattr(liquidityhelper, "lnd_rpc", fake_lnd_rpc)


def _forbid_lnd_rpc(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("lnd_rpc must not be called on this path")

    monkeypatch.setattr(liquidityhelper, "lnd_rpc", boom)


def test_brand_new_when_zero_balance_and_no_transactions(monkeypatch):
    _patch_lnd_rpc(monkeypatch, transactions=[])
    wallet = {"id": "w1", "currency": "btclnd", "balance": 0}
    assert _run(liquidityhelper._wallet_is_brand_new_lnd(None, wallet)) is True


def test_not_brand_new_when_has_transactions(monkeypatch):
    _patch_lnd_rpc(monkeypatch, transactions=[{"tx_hash": "abc"}])
    wallet = {"id": "w1", "currency": "btclnd", "balance": 0}
    assert _run(liquidityhelper._wallet_is_brand_new_lnd(None, wallet)) is False


def test_not_brand_new_when_balance_nonzero(monkeypatch):
    # Must short-circuit before the gRPC call.
    _forbid_lnd_rpc(monkeypatch)
    wallet = {"id": "w1", "currency": "btclnd", "balance": 0.0001}  # BTC
    assert _run(liquidityhelper._wallet_is_brand_new_lnd(None, wallet)) is False


def test_non_lnd_wallet_is_never_brand_new(monkeypatch):
    # Electrum wallets have no GetTransactions gRPC; never suppress.
    _forbid_lnd_rpc(monkeypatch)
    wallet = {"id": "w1", "currency": "btc", "balance": 0}
    assert _run(liquidityhelper._wallet_is_brand_new_lnd(None, wallet)) is False


def test_fail_open_on_grpc_error(monkeypatch):
    # gRPC failure → don't suppress (the email still fires) → False.
    _patch_lnd_rpc(monkeypatch, raises=True)
    wallet = {"id": "w1", "currency": "btclnd", "balance": 0}
    assert _run(liquidityhelper._wallet_is_brand_new_lnd(None, wallet)) is False
