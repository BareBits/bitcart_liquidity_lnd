"""Tests for the store-owner notifier (bitcart_plugin/owner_notifications.py)
and the engine's dispatch hook.

We do NOT exercise Bitcart's real email/DB. We stub the api.* modules so the
notifier imports standalone, then assert that for various store/owner
combinations the notifier resolves the right recipient and forms the
correct send to Bitcart's email machinery (the "request to the Bitcart
API"). A separate test pins the engine -> injected-notifier contract.
"""

from __future__ import annotations

import asyncio
import sys
import types


# --- Stub the bitcart runtime ONLY when it isn't importable (standalone CI).
try:  # pragma: no cover - exercised differently per environment
    from bitcart_plugin import owner_notifications  # noqa: F401
except ImportError:
    def _stub(name: str, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    class _Stmt:
        def where(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

    class _Col:  # supports `models.User.is_superuser.is_(True)` in the stmt
        def is_(self, *a, **k):
            return self

    _stub("dishka", Scope=types.SimpleNamespace(REQUEST="REQUEST"))
    _stub("sqlalchemy", select=lambda *a, **k: _Stmt())
    _stub("api")
    _stub("api.models", User=type("User", (), {"is_superuser": _Col(), "created": _Col()}))
    _stub("api.schemas")
    _stub("api.schemas.policies", Policy=type("Policy", (), {}))
    _stub("api.services")
    _stub("api.services.crud")
    _stub(
        "api.services.crud.repositories",
        StoreRepository=type("StoreRepository", (), {}),
        UserRepository=type("UserRepository", (), {}),
    )
    _stub("api.services.settings", SettingService=type("SettingService", (), {}))
    _stub("api.utils")
    _stub("api.utils.email", Email=type("Email", (), {}))
    sys.modules["api"].models = sys.modules["api.models"]

    from bitcart_plugin import owner_notifications  # noqa: F401


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class _Store:
    def __init__(self, user_id):
        self.user_id = user_id


class _Usr:
    def __init__(self, email):
        self.email = email


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _Session:
    def __init__(self, superuser):
        self._su = superuser

    async def execute(self, stmt):
        return _Result(self._su)


class _StoreRepo:
    def __init__(self, stores):
        self._stores = stores

    async def get_one_or_none(self, **kw):
        return self._stores.get(kw.get("id"))


class _UserRepo:
    def __init__(self, users, superuser=None):
        self._users = users
        self.session = _Session(superuser)

    async def get_one_or_none(self, **kw):
        return self._users.get(kw.get("id"))


# --------------------------------------------------------------------------
# resolve_owner_email — recipient selection for various store/owner combos
# --------------------------------------------------------------------------
def test_recipient_is_store_owner():
    sr = _StoreRepo({"s1": _Store(user_id="u1")})
    ur = _UserRepo({"u1": _Usr("owner@x.com")}, superuser=_Usr("admin@x.com"))
    assert _run(owner_notifications.resolve_owner_email(sr, ur, "s1")) == "owner@x.com"


def test_recipient_falls_back_to_admin_when_no_owner():
    sr = _StoreRepo({"s1": _Store(user_id=None)})
    ur = _UserRepo({}, superuser=_Usr("admin@x.com"))
    assert _run(owner_notifications.resolve_owner_email(sr, ur, "s1")) == "admin@x.com"


def test_recipient_falls_back_when_owner_has_no_email():
    sr = _StoreRepo({"s1": _Store(user_id="u1")})
    ur = _UserRepo({"u1": _Usr("")}, superuser=_Usr("admin@x.com"))
    assert _run(owner_notifications.resolve_owner_email(sr, ur, "s1")) == "admin@x.com"


def test_recipient_falls_back_when_store_missing():
    sr = _StoreRepo({})
    ur = _UserRepo({}, superuser=_Usr("admin@x.com"))
    assert _run(owner_notifications.resolve_owner_email(sr, ur, "ghost")) == "admin@x.com"


def test_recipient_none_when_nothing_resolves():
    sr = _StoreRepo({})
    ur = _UserRepo({}, superuser=None)
    assert _run(owner_notifications.resolve_owner_email(sr, ur, "ghost")) is None


# --------------------------------------------------------------------------
# notify() — the formed send to Bitcart's email machinery
# --------------------------------------------------------------------------
class _FakeEmail:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.sent = []

    def is_enabled(self):
        return self.enabled

    def send_mail(self, where, text, subject="(none)"):
        self.sent.append((where, text, subject))


class _Container:
    def __init__(self, mapping):
        self._m = mapping

    def __call__(self, scope=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, cls):
        return self._m[cls]


async def _aret(v):
    return v


def _build(monkeypatch, *, stores, users, superuser, email_enabled=True):
    fake_email = _FakeEmail(enabled=email_enabled)

    class _EmailNS:
        @staticmethod
        def get_email(policy):
            return fake_email

    monkeypatch.setattr(owner_notifications, "Email", _EmailNS)
    mapping = {
        owner_notifications.SettingService: types.SimpleNamespace(
            get_setting=lambda scheme, name=None: _aret(object())
        ),
        owner_notifications.StoreRepository: _StoreRepo(stores),
        owner_notifications.UserRepository: _UserRepo(users, superuser),
    }
    notifier = owner_notifications.make_store_owner_notifier(_Container(mapping))
    return notifier, fake_email


def test_notify_sends_to_store_owner(monkeypatch):
    notifier, email = _build(
        monkeypatch,
        stores={"s1": _Store("u1")},
        users={"u1": _Usr("owner@x.com")},
        superuser=_Usr("admin@x.com"),
    )
    _run(notifier("s1", "Low liquidity", "please top up"))
    assert email.sent == [("owner@x.com", "please top up", "Low liquidity")]


def test_notify_admin_fallback_when_no_owner(monkeypatch):
    notifier, email = _build(
        monkeypatch,
        stores={"s1": _Store(None)},
        users={},
        superuser=_Usr("admin@x.com"),
    )
    _run(notifier("s1", "Low liquidity", "please top up"))
    assert email.sent == [("admin@x.com", "please top up", "Low liquidity")]


def test_notify_skips_when_policy_smtp_disabled(monkeypatch):
    notifier, email = _build(
        monkeypatch,
        stores={"s1": _Store("u1")},
        users={"u1": _Usr("owner@x.com")},
        superuser=None,
        email_enabled=False,
    )
    _run(notifier("s1", "Low liquidity", "please top up"))
    assert email.sent == []


def test_notify_skips_when_no_recipient(monkeypatch):
    notifier, email = _build(
        monkeypatch, stores={}, users={}, superuser=None, email_enabled=True
    )
    _run(notifier("ghost", "Low liquidity", "please top up"))
    assert email.sent == []


# --------------------------------------------------------------------------
# Engine dispatch contract: _send_store_owner_email -> injected notifier
# --------------------------------------------------------------------------
def test_engine_dispatches_to_injected_notifier():
    import liquidityhelper

    calls = []

    async def fake(store_id, subject, body):
        calls.append((store_id, subject, body))

    liquidityhelper.set_store_owner_notifier(fake)
    try:
        _run(liquidityhelper._send_store_owner_email("s9", "subj", "body"))
        assert calls == [("s9", "subj", "body")]
        # No notifier registered (standalone) -> no-op, no error.
        liquidityhelper.set_store_owner_notifier(None)
        _run(liquidityhelper._send_store_owner_email("s9", "subj", "body"))
        assert calls == [("s9", "subj", "body")]
    finally:
        liquidityhelper.set_store_owner_notifier(None)
