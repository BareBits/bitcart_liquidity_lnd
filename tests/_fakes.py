"""
Test doubles for code that depends on BitcartAPI.

Goal: let unit tests in `bitcart_api_tests.py` exercise the consumption-side
logic of functions that take a `BitcartAPI` parameter without standing up a
real Bitcart instance or the regtest LND fixture. FakeBitcartAPI is duck-
typed (not a subclass) so it doesn't accidentally invoke httpx.

Each fake method matches the shape of the real BitcartAPI method's return
value closely enough for callers to do their normal dict lookups; production
features irrelevant to a specific test (pagination, retries, etc.) are
omitted intentionally.

Extend by adding more methods as new functions need to be unit-tested.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class FakeBitcartAPI:
    """In-memory fake of BitcartAPI for unit tests.

    Usage:
        api = FakeBitcartAPI()
        wallet = api.add_wallet("w1", currency="btc")
        api.add_store("s1", wallets=["w1"])
        api.add_channel("w1", local_balance=50_000, remote_balance=30_000)
        result = await store_needs_liquidity("s1", api)

    Implements the subset of BitcartAPI methods that current unit tests
    need. Tests that touch un-faked methods will get a clean
    AttributeError pointing at what to implement next.
    """

    def __init__(self) -> None:
        self.stores: Dict[str, Dict[str, Any]] = {}
        self.wallets: Dict[str, Dict[str, Any]] = {}
        self.channels_by_wallet: Dict[str, List[Dict[str, Any]]] = {}
        # LND PendingChannels response, per wallet_id. Tests that exercise
        # `has_pending_channel_activity` populate this via
        # set_lnd_pending_channels(); production code reaches it via
        # liquidityhelper.lnd_rpc(...) which the unit tests monkeypatch
        # to consult this dict.
        self.pending_channels_by_wallet: Dict[str, Dict[str, Any]] = {}
        # Invoices + payouts, keyed by store_id. Tests that exercise the
        # dashboard or fee-calculation paths populate these via
        # add_invoice() / add_payout().
        self.invoices_by_store: Dict[str, List[Dict[str, Any]]] = {}
        self.payouts: List[Dict[str, Any]] = []
        # BTC/USD rate override. None means get_btc_usd_rate() returns
        # None (the "rate unavailable" path the dashboard handles).
        self.btc_usd_rate: Optional[float] = None
        # On-chain history + LN history per wallet_id. Used to feed
        # new_calc_invoice_stats in tests; real code dispatches to
        # list_onchain_history / list_ln_payments_with_labels.
        self.onchain_history_by_wallet: Dict[str, List[Dict[str, Any]]] = {}
        self.ln_history_by_wallet: Dict[str, List[Dict[str, Any]]] = {}

    # ----- setup helpers (sync) -----

    def add_store(
        self,
        store_id: str,
        name: str = "test-store",
        wallets: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        store = {"id": store_id, "name": name, "wallets": list(wallets or []), **extra}
        self.stores[store_id] = store
        return store

    def add_wallet(
        self,
        wallet_id: str,
        currency: str = "btc",
        xpub: str = "fake-xpub",
        balance: float = 0.0,
        **extra: Any,
    ) -> Dict[str, Any]:
        wallet = {
            "id": wallet_id,
            "currency": currency,
            "xpub": xpub,
            "balance": str(balance),
            **extra,
        }
        self.wallets[wallet_id] = wallet
        return wallet

    def add_channel(
        self,
        wallet_id: str,
        local_balance: int = 0,
        remote_balance: int = 0,
        active: bool = True,
        peer_state: str = "GOOD",
        state: str = "OPEN",
        remote_pubkey: str = "deadbeef" * 8,
        channel_point: str = "",
        **extra: Any,
    ) -> Dict[str, Any]:
        ch = {
            "local_balance": local_balance,
            "remote_balance": remote_balance,
            "active": active,
            "peer_state": peer_state,
            "state": state,
            "remote_pubkey": remote_pubkey,
            "channel_point": channel_point,
            **extra,
        }
        self.channels_by_wallet.setdefault(wallet_id, []).append(ch)
        return ch

    # ----- BitcartAPI async surface (subset) -----

    def set_lnd_pending_channels(
        self,
        wallet_id: str,
        *,
        pending_open: int = 0,
        waiting_close: int = 0,
        pending_closing: int = 0,
        pending_force_closing: int = 0,
    ) -> None:
        """Populate the fake LND PendingChannels response for `wallet_id`.

        Counts (not channel details) are enough — the production code
        only checks for the presence of any entry in each list. Mirrors
        the LND proto field names so tests read naturally.
        """
        def _stub_list(n: int) -> List[Dict[str, Any]]:
            return [{"channel": {"channel_point": f"stub:{i}"}} for i in range(n)]

        self.pending_channels_by_wallet[wallet_id] = {
            "pending_open_channels": _stub_list(pending_open),
            "waiting_close_channels": _stub_list(waiting_close),
            "pending_closing_channels": _stub_list(pending_closing),
            "pending_force_closing_channels": _stub_list(pending_force_closing),
        }

    async def get_store_by_id(self, store_id: str) -> Optional[Dict[str, Any]]:
        return self.stores.get(store_id)

    async def get_stores(self) -> List[Dict[str, Any]]:
        return list(self.stores.values())

    async def get_store(self, store_id: str) -> Optional[Dict[str, Any]]:
        return self.stores.get(store_id)

    async def get_wallet(self, wallet_id: str) -> Optional[Dict[str, Any]]:
        return self.wallets.get(wallet_id)

    async def get_wallets(self, limit: int = 200) -> List[Dict[str, Any]]:
        return list(self.wallets.values())

    async def get_best_ln_wallet_for_store(
        self, store: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        for wid in store.get("wallets", []):
            w = self.wallets.get(wid)
            if w:
                return w
        return None

    async def get_wallet_ln_channels(
        self,
        wallet_id: str,
        active_only: bool = False,
        online_only: bool = False,
    ) -> List[Dict[str, Any]]:
        chans = list(self.channels_by_wallet.get(wallet_id, []))
        if active_only:
            chans = [c for c in chans if c.get("active")]
        if online_only:
            chans = [c for c in chans if c.get("peer_state") in ("GOOD", "CONNECTED")]
        return chans

    # ----- invoice / payout / rate (dashboard tests) -----

    def add_invoice(
        self,
        store_id: str,
        *,
        invoice_id: str = "inv1",
        paid_date: Optional[str] = "2026-01-01T00:00:00",
        notes: str = "",
        payments: Optional[List[Dict[str, Any]]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Append an invoice for `store_id`. By default a single LN
        payment matching the FakeBitcartAPI's first wallet is added;
        callers can pass `payments=[]` for an unpaid invoice or supply
        their own list for richer scenarios.
        """
        if payments is None:
            # Default: 0.0001 BTC = 10_000 sats, LN, on first wallet
            first_wallet_id = next(iter(self.wallets), "")
            payments = [{
                "amount": "0.0001",
                "currency": "btc",
                "lightning": True,
                "wallet_id": first_wallet_id,
                "is_used": True,
                "created": "2026-01-01T00:00:00",
            }]
        inv = {
            "id": invoice_id,
            "store_id": store_id,
            "order_id": invoice_id,
            "notes": notes,
            "payments": payments,
            "paid_currency": "btc",
            "price": "0.0001",
            "status": "complete" if paid_date else "pending",
            "currency": "USD",
            "tx_hashes": [],
            "paid_date": paid_date,
            **extra,
        }
        self.invoices_by_store.setdefault(store_id, []).append(inv)
        return inv

    def add_payout(self, **fields: Any) -> Dict[str, Any]:
        self.payouts.append(fields)
        return fields

    async def get_invoices(
        self, limit: int = 50, offset: int = 0, store_id: Optional[str] = None, **kw: Any,
    ) -> List[Dict[str, Any]]:
        if store_id is not None:
            return list(self.invoices_by_store.get(store_id, []))
        out: List[Dict[str, Any]] = []
        for invs in self.invoices_by_store.values():
            out.extend(invs)
        return out

    async def get_payouts(self) -> List[Dict[str, Any]]:
        return list(self.payouts)

    async def get_btc_usd_rate(self) -> Optional[float]:
        return self.btc_usd_rate

    # The dashboard backend computes fee/network breakdowns via
    # new_calc_invoice_stats, which in turn calls list_onchain_history /
    # list_ln_payments_with_labels. Tests monkey-patch those functions
    # to read from the fake's dicts below; see dashboard_tests.py.

    def add_onchain_tx(
        self,
        wallet_id: str,
        *,
        fee_sat: int = 0,
        amount_sat: int = 0,
        label: str = "",
        incoming: bool = False,
        **extra: Any,
    ) -> Dict[str, Any]:
        tx = {
            "fee_sat": fee_sat,
            "amount_sat": amount_sat,
            "label": label,
            "incoming": incoming,
            **extra,
        }
        self.onchain_history_by_wallet.setdefault(wallet_id, []).append(tx)
        return tx

    def add_ln_tx(
        self,
        wallet_id: str,
        *,
        amount_msat: int = 0,
        fee_msat: int = 0,
        label: str = "",
        tx_type: str = "payment",
        **extra: Any,
    ) -> Dict[str, Any]:
        tx = {
            "amount_msat": amount_msat,
            "fee_msat": fee_msat,
            "label": label,
            "type": tx_type,
            **extra,
        }
        self.ln_history_by_wallet.setdefault(wallet_id, []).append(tx)
        return tx
