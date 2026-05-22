"""Tests for BB_STOREID and its wiring into LN fee/referral payments
via the LUD-12 `comment` field.

BB_STOREID is attached as `comment=storeid:<value>` on the LNURL
callback URL. The recipient threads the comment into the BOLT-11
invoice's `d` (description) field when they support LUD-12
(advertised via `commentAllowed: N` in the LNURL metadata response).

The chain of responsibility:
  _pay_dev_fee_via_ln / _pay_referral_via_ln
    → lnurl_to_invoice(comment=f"storeid:{BB_STOREID}")
    → get_lightning_invoice(comment=...)
    → URL becomes ".../cb?amount=N&comment=storeid%3A<value>"
       (only if commentAllowed > 0)

Tests pin every link.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse, parse_qs

import pytest

import liquidityhelper
import classes as classes_mod


# ---------------------------------------------------------------------------
# Config + schema
# ---------------------------------------------------------------------------

def test_bb_storeid_default_is_default():
    import config
    assert config.BB_STOREID == "default"


def test_bb_storeid_in_settings_schema():
    from bitcart_plugin.settings_schema import PluginSettings
    field = PluginSettings.model_fields.get("BB_STOREID")
    assert field is not None
    assert field.default == "default"


# ---------------------------------------------------------------------------
# get_lightning_invoice — LUD-12 comment handling
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal httpx.Response stand-in."""
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")
    def json(self):
        return self._json


def _install_fake_requests(monkeypatch, *, lnurl_meta, invoice_data):
    """Stub httpx.AsyncClient.get to capture invoked URLs.

    get_lightning_invoice uses `async with httpx.AsyncClient(...)` then
    `await client.get(url)`. We patch the AsyncClient.get coroutine so
    every call lands here regardless of which AsyncClient instance is
    used (the function constructs two — one for LNURL lookup, one for
    invoice fetch)."""
    calls = []
    async def fake_get(self, url, *args, **kwargs):
        calls.append(url)
        if "/.well-known/lnurlp/" in url:
            return _FakeResponse(lnurl_meta)
        return _FakeResponse(invoice_data)
    monkeypatch.setattr(classes_mod.httpx.AsyncClient, "get", fake_get)
    return calls


def test_comment_appended_when_recipient_supports_lud12(monkeypatch, event_loop):
    """commentAllowed > 0 in the LNURL metadata → comment is appended
    to the callback URL, URL-encoded."""
    calls = _install_fake_requests(
        monkeypatch,
        lnurl_meta={
            "callback": "https://example.com/lnurl/cb",
            "minSendable": 1000, "maxSendable": 100_000_000,
            "commentAllowed": 200,
        },
        invoice_data={"pr": "lnbc1..."},
    )
    result = event_loop.run_until_complete(
        classes_mod.get_lightning_invoice(
            "fees@example.com", amount_sats=10000,
            comment="storeid:mystore",
        )
    )
    assert result["success"] is True
    cb = [c for c in calls if "/lnurl/cb" in c][0]
    qs = parse_qs(urlparse(cb).query)
    assert qs["amount"] == ["10000000"]
    assert qs["comment"] == ["storeid:mystore"]


def test_comment_dropped_when_recipient_omits_commentAllowed(monkeypatch, event_loop):
    """LNURL metadata without `commentAllowed` → comment is silently
    dropped (per spec). Pin against the previous bug where a custom
    parameter would be sent regardless of recipient support."""
    calls = _install_fake_requests(
        monkeypatch,
        lnurl_meta={
            "callback": "https://example.com/cb",
            "minSendable": 1000, "maxSendable": 100_000_000,
            # No commentAllowed at all.
        },
        invoice_data={"pr": "lnbc1..."},
    )
    event_loop.run_until_complete(
        classes_mod.get_lightning_invoice(
            "fees@example.com", amount_sats=10000,
            comment="storeid:should-be-dropped",
        )
    )
    cb = [c for c in calls if "/cb" in c and "amount" in c][0]
    qs = parse_qs(urlparse(cb).query)
    assert "comment" not in qs, (
        "comment must NOT be appended when recipient doesn't "
        "advertise commentAllowed"
    )
    assert qs["amount"] == ["10000000"]


def test_comment_dropped_when_commentAllowed_is_zero(monkeypatch, event_loop):
    """commentAllowed: 0 is explicit 'no comments' → drop."""
    calls = _install_fake_requests(
        monkeypatch,
        lnurl_meta={
            "callback": "https://example.com/cb",
            "minSendable": 1000, "maxSendable": 100_000_000,
            "commentAllowed": 0,
        },
        invoice_data={"pr": "lnbc1..."},
    )
    event_loop.run_until_complete(
        classes_mod.get_lightning_invoice(
            "fees@example.com", amount_sats=10000, comment="anything",
        )
    )
    cb = [c for c in calls if "/cb" in c and "amount" in c][0]
    assert "comment" not in parse_qs(urlparse(cb).query)


def test_comment_truncated_to_commentAllowed_limit(monkeypatch, event_loop):
    """A comment longer than commentAllowed gets truncated to that
    length — the recipient can't accept more than they advertised."""
    calls = _install_fake_requests(
        monkeypatch,
        lnurl_meta={
            "callback": "https://example.com/cb",
            "minSendable": 1000, "maxSendable": 100_000_000,
            "commentAllowed": 10,   # 10 char ceiling
        },
        invoice_data={"pr": "lnbc1..."},
    )
    event_loop.run_until_complete(
        classes_mod.get_lightning_invoice(
            "fees@example.com", amount_sats=10000,
            comment="storeid:way-too-long-tag-for-recipient",
        )
    )
    cb = [c for c in calls if "/cb" in c and "amount" in c][0]
    qs = parse_qs(urlparse(cb).query)
    # First 10 chars of "storeid:way-too-long..." is "storeid:wa"
    assert qs["comment"] == ["storeid:wa"]


def test_comment_url_encoded_for_special_chars(monkeypatch, event_loop):
    """A storeid with spaces, &, or = gets percent-encoded so it
    can't malform the query string."""
    calls = _install_fake_requests(
        monkeypatch,
        lnurl_meta={
            "callback": "https://example.com/cb",
            "minSendable": 1000, "maxSendable": 100_000_000,
            "commentAllowed": 200,
        },
        invoice_data={"pr": "lnbc1..."},
    )
    event_loop.run_until_complete(
        classes_mod.get_lightning_invoice(
            "fees@example.com", amount_sats=10000,
            comment="storeid:weird value&with=signs",
        )
    )
    cb = [c for c in calls if "/cb" in c and "amount" in c][0]
    # Raw form never appears unescaped:
    assert "weird value&with=signs" not in cb
    # But parse_qs decodes the percent-encoded value back to the original:
    qs = parse_qs(urlparse(cb).query)
    assert qs["comment"] == ["storeid:weird value&with=signs"]


def test_comment_works_with_callback_existing_query(monkeypatch, event_loop):
    """LNURL spec allows the callback URL to already have a `?...`
    segment; we must use `&` for our params in that case."""
    calls = _install_fake_requests(
        monkeypatch,
        lnurl_meta={
            "callback": "https://example.com/cb?tenant=alice",
            "minSendable": 1000, "maxSendable": 100_000_000,
            "commentAllowed": 200,
        },
        invoice_data={"pr": "lnbc1..."},
    )
    event_loop.run_until_complete(
        classes_mod.get_lightning_invoice(
            "fees@example.com", amount_sats=10000,
            comment="storeid:deploy1",
        )
    )
    cb = [c for c in calls if "/cb" in c and "amount" in c][0]
    qs = parse_qs(urlparse(cb).query)
    assert qs["tenant"] == ["alice"]    # pre-existing preserved
    assert qs["amount"] == ["10000000"]
    assert qs["comment"] == ["storeid:deploy1"]


def test_no_comment_arg_unchanged(monkeypatch, event_loop):
    """Calling without `comment` produces a URL with just
    `?amount=N` — backward-compatible with the pre-BB_STOREID era."""
    calls = _install_fake_requests(
        monkeypatch,
        lnurl_meta={
            "callback": "https://example.com/cb",
            "minSendable": 1000, "maxSendable": 100_000_000,
            "commentAllowed": 200,
        },
        invoice_data={"pr": "lnbc1..."},
    )
    event_loop.run_until_complete(
        classes_mod.get_lightning_invoice(
            "fees@example.com", amount_sats=10000,
        )
    )
    cb = [c for c in calls if "amount" in c][0]
    qs = parse_qs(urlparse(cb).query)
    assert list(qs.keys()) == ["amount"]


def test_empty_comment_does_not_attach(monkeypatch, event_loop):
    """comment="" is falsy — same behavior as no comment supplied at
    all. Pin against the case where an operator sets BB_STOREID to ''
    and we'd otherwise still send `?comment=storeid:`."""
    calls = _install_fake_requests(
        monkeypatch,
        lnurl_meta={
            "callback": "https://example.com/cb",
            "minSendable": 1000, "maxSendable": 100_000_000,
            "commentAllowed": 200,
        },
        invoice_data={"pr": "lnbc1..."},
    )
    event_loop.run_until_complete(
        classes_mod.get_lightning_invoice(
            "fees@example.com", amount_sats=10000, comment="",
        )
    )
    cb = [c for c in calls if "amount" in c][0]
    assert "comment" not in parse_qs(urlparse(cb).query)


# ---------------------------------------------------------------------------
# lnurl_to_invoice forwards comment
# ---------------------------------------------------------------------------

def test_lnurl_to_invoice_forwards_comment(monkeypatch, event_loop):
    """lnurl_to_invoice must pass `comment` through to
    get_lightning_invoice unchanged. Pin the middle of the chain."""
    captured = {}
    async def fake_get_lightning_invoice(addr, amount, comment=None):
        captured["addr"] = addr
        captured["amount"] = amount
        captured["comment"] = comment
        return {"success": True, "invoice": "lnbc1", "amount_sats": amount}
    monkeypatch.setattr(
        liquidityhelper, "get_lightning_invoice", fake_get_lightning_invoice,
    )
    inv = event_loop.run_until_complete(
        liquidityhelper.lnurl_to_invoice(
            "fees@x.com", 5000, comment="storeid:alpha",
        )
    )
    assert inv == "lnbc1"
    assert captured["comment"] == "storeid:alpha"


def test_lnurl_to_invoice_default_comment_is_none(monkeypatch, event_loop):
    """When the caller doesn't pass a comment, lnurl_to_invoice
    forwards None (not an empty string). Important so
    get_lightning_invoice's `if comment:` falsy check works."""
    captured = {}
    async def fake_get_lightning_invoice(addr, amount, comment=None):
        captured["comment"] = comment
        return {"success": True, "invoice": "lnbc1", "amount_sats": amount}
    monkeypatch.setattr(
        liquidityhelper, "get_lightning_invoice", fake_get_lightning_invoice,
    )
    event_loop.run_until_complete(
        liquidityhelper.lnurl_to_invoice("fees@x.com", 5000)
    )
    assert captured["comment"] is None


# ---------------------------------------------------------------------------
# Fee + referral payment paths use BB_STOREID
# ---------------------------------------------------------------------------

def test_pay_dev_fee_via_ln_passes_storeid_as_comment(monkeypatch, event_loop):
    """End of chain: _pay_dev_fee_via_ln passes
    comment=f"storeid:{BB_STOREID}" to lnurl_to_invoice."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_FEE_SENDING_LN", True)
    monkeypatch.setattr(liquidityhelper, "DRY_RUN_FUNDS", False)
    monkeypatch.setattr(liquidityhelper, "FORCE_FEE_INVOICE", None)
    monkeypatch.setattr(liquidityhelper, "BB_STOREID", "test-deploy-alpha")
    monkeypatch.setattr(liquidityhelper, "MIN_FEE_OUT", 1)

    captured = {}
    async def fake_lnurl_to_invoice(dest, amount, comment=None):
        captured["dest"] = dest
        captured["comment"] = comment
        return "lnbc1fake"
    async def fake_pay(*a, **kw):
        return True
    monkeypatch.setattr(liquidityhelper, "lnurl_to_invoice", fake_lnurl_to_invoice)
    monkeypatch.setattr(liquidityhelper, "electrum_pay_ln_invoice", fake_pay)

    class _Api:
        async def get_outbound_liquidity(self, wallet_id):
            return 1_000_000
    api = _Api()
    wallet = {"id": "w1", "currency": "btclnd"}

    ok = event_loop.run_until_complete(
        liquidityhelper._pay_dev_fee_via_ln(api, "store1", wallet, 5000)
    )
    assert ok is True
    assert captured["comment"] == "storeid:test-deploy-alpha"


def test_pay_referral_via_ln_passes_storeid_as_comment(monkeypatch, event_loop):
    monkeypatch.setattr(liquidityhelper, "REFERRAL_FEE_DEST", "refs@example.com")
    monkeypatch.setattr(liquidityhelper, "DRY_RUN_FUNDS", False)
    monkeypatch.setattr(liquidityhelper, "BB_STOREID", "test-deploy-beta")
    monkeypatch.setattr(liquidityhelper, "MIN_FEE_OUT", 1)

    captured = {}
    async def fake_lnurl_to_invoice(dest, amount, comment=None):
        captured["dest"] = dest
        captured["comment"] = comment
        return "lnbc1fake"
    async def fake_pay(*a, **kw):
        return True
    monkeypatch.setattr(liquidityhelper, "lnurl_to_invoice", fake_lnurl_to_invoice)
    monkeypatch.setattr(liquidityhelper, "electrum_pay_ln_invoice", fake_pay)

    class _Api:
        async def get_outbound_liquidity(self, wallet_id):
            return 1_000_000
    api = _Api()
    wallet = {"id": "w1", "currency": "btclnd"}

    ok = event_loop.run_until_complete(
        liquidityhelper._pay_referral_via_ln(api, "store1", wallet, 5000)
    )
    assert ok is True
    assert captured["dest"] == "refs@example.com"
    assert captured["comment"] == "storeid:test-deploy-beta"


def test_force_fee_invoice_bypasses_lnurl(monkeypatch, event_loop):
    """FORCE_FEE_INVOICE skips lnurl_to_invoice entirely — no
    comment to attach because no LNURL call. Pin the debug-override
    path so a future refactor doesn't accidentally drag the LNURL
    flow in when an operator has explicitly overridden."""
    monkeypatch.setattr(liquidityhelper, "ENABLE_FEE_SENDING_LN", True)
    monkeypatch.setattr(liquidityhelper, "DRY_RUN_FUNDS", False)
    monkeypatch.setattr(liquidityhelper, "FORCE_FEE_INVOICE", "lnbc1forced")
    monkeypatch.setattr(liquidityhelper, "MIN_FEE_OUT", 1)

    lnurl_calls: list = []
    async def fake_lnurl_to_invoice(*a, **kw):
        lnurl_calls.append((a, kw))
        return "lnbc1fake"
    async def fake_pay(*a, **kw):
        return True
    monkeypatch.setattr(liquidityhelper, "lnurl_to_invoice", fake_lnurl_to_invoice)
    monkeypatch.setattr(liquidityhelper, "electrum_pay_ln_invoice", fake_pay)

    class _Api:
        async def get_outbound_liquidity(self, wallet_id):
            return 1_000_000
    api = _Api()
    wallet = {"id": "w1", "currency": "btclnd"}

    event_loop.run_until_complete(
        liquidityhelper._pay_dev_fee_via_ln(api, "store1", wallet, 5000)
    )
    assert lnurl_calls == []
