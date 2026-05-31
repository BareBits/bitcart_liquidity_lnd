import json
from typing import Any, Union,List,Dict,Tuple,Iterable,Set
import httpx
from datetime import datetime
from common_functions import sats_to_btc, btc_to_sats
from config import *
from user_config import *
from dataclasses import dataclass
import requests,logging
import traceback
# Child of the main liquidityhelper logger so logs from this module land
# in liquidityhelper.log + stdout via the parent's handlers (previously
# the logger was named "classes" and had no handlers, dropping all output).
logger = logging.getLogger("liquidityhelper.classes")


def _log_api_failure(method_label: str, e: Exception) -> None:
    """Standardized BitcartAPI error log: includes the exception
    message AND a full traceback so operators can debug from logs
    alone. Use from every BitcartAPI method's `except` branch.

    The pattern previously was `logger.error(f"...: {e}")` which
    discarded the stack. This helper consolidates the right pattern
    so a single edit can change logging level / format for the
    whole API client."""
    logger.error(
        f"{method_label} failed: {e} {traceback.format_exc()}"
    )


@dataclass
class CalculatedFees:
    """
    Calculated Fees
    """
    store_id: str
    total_revenue_in_sats: int
    total_fee_due_in_sats: int
    total_fees_paid_in_sats: int # INCLUDES network fees from payouts
    total_skipped_fees_in_sats_due_to_promo_period: int
    total_skipped_fees_in_sats_due_to_self_topups: int
    total_skipped_fees_in_sats_due_to_bb_topups: int
    total_onchain_network_fees_from_payouts:int
    total_ln_network_fees_from_payouts: int
@dataclass
class BitcartInvoice:
    """
    All in sats
    """
    id: str
    order_id: str
    store_id:str
    notes:str
    payments:List[Dict[str,Union[str,float,None]]]
    paid_currency:str
    price:str
    status:str
    currency:str
    tx_hashes:Optional[List[str]]
    paid_date:Optional[str]=None
    refund_id:Optional[str]=None
    is_used:Optional[str]=None
    def is_paid(self)->bool:
        if self.paid_date:
            return True
        return False
    def is_refunded(self)->bool:
        if self.refund_id:
            return True
        return False
    def is_bb_topup_invoice(self)->bool:
        if self.notes == TOPUP_BAREBITS:
            return True
        return False
    def is_self_topup_invoice(self)->bool:
        if self.notes == TOPUP_NAME:
            return True
        return False

@dataclass
class StoreStats:
    """
    All in sats. Numbers are all positive.
    """
    store_id: str
    onchain_total_revenue_in_sats: int
    ln_total_revenue_in_sats:int
    total_bb_fees_paid_in_sats: int  # does not include network fees
    revenue_eligible_for_fee:int # total revenue eligible for fee
    # note these ineligible revenue sections stack. An invoice may be ineligible for multiple reasons!
    ineligible_revenue_because_not_liquidityhelper_wallet_in_sats: int
    ineligible_revenue_because_not_ln_transaction_in_sats: int
    ineligible_revenue_because_of_promo_in_sats: int
    ineligible_revenue_because_of_topups_in_sats: int
    ineligible_revenue_because_of_bb_topups_in_sats: int
    ln_network_fees_paid_for_bb_topup_returns_in_sats: int # currently always zero; bb-topup returns route through misc_ln_network_fees_in_sats
    onchain_network_fees_paid_for_bb_topup_returns_in_sats: int
    ln_network_fees_paid_for_fee_payments_in_sats:int
    onchain_network_fees_paid_for_fee_payments_in_sats: int
    ln_network_fees_paid_for_payouts_in_sats: int
    misc_ln_network_fees_in_sats: int # these are fees not correlated to a specific payout/fee since we don't need that amount of precision yet
    onchain_network_fees_paid_for_payouts_in_sats: int
    onchain_network_fees_paid_for_channel_opens_in_sats: int
    onchain_network_fees_paid_for_channel_closes_in_sats: int
    onchain_network_fees_paid_for_swaps_in_sats: int
    # Miner fee for the on-chain tx that paid an LSP for a channel order.
    # Separate from channel-open miner fees because the channel itself
    # is opened by the LSP, not by us — we just pay them a service fee.
    onchain_network_fees_paid_for_lsp_orders_in_sats: int
    # The principal of each LSP channel-order payment. Since LSPS1
    # `client_balance_sat=0` in our requests, this equals the LSP's
    # service fee for opening the channel. Counted against the 2% fee
    # cap alongside on-chain network fees.
    onchain_lsp_service_fees_paid_in_sats: int
    # Referral fee bookkeeping. `total_referral_fees_paid_in_sats` is
    # the principal sent to REFERRAL_FEE_DEST; the LN network fee
    # incurred sending it is captured separately so it can be deducted
    # from the developer's 2% (matching the rest of the policy) rather
    # than from the flat referral fee.
    total_referral_fees_paid_in_sats: int
    ln_network_fees_paid_for_referral_payments_in_sats: int
    # On-chain miner fee for the fallback referral payment path. Same
    # treatment as the LN-side fee: deducted from the developer's 2%
    # rather than from the flat referral fee.
    onchain_network_fees_paid_for_referral_payments_in_sats: int
    # Miner fee for outgoing on-chain txs that weren't initiated by an
    # engine-labeled path — operator-initiated sends via the Bitcart
    # admin UI / lncli sendcoins, LND anchor sweeps, and anything else
    # the wallet broadcast without one of the known application labels.
    # LND tags these `label='external'` (or leaves them blank). The
    # operator paid them, so they belong in the fee total; bucketed
    # separately so the dashboard breakdown doesn't mislabel them as
    # channel-open miner fees.
    onchain_network_fees_paid_for_external_in_sats: int = 0
    # Routing fees paid on circular-rebalance self-payments. Counted
    # toward the 2% developer-fee cap so the operator isn't double-
    # charged when rebalancing eats into the fee budget. Sats received
    # via a rebalance do NOT count as revenue (rebalance invoices
    # bypass Bitcart's invoice store), only the fee is real cost.
    ln_network_fees_paid_for_rebalances_in_sats: int = 0
    def calc_total_bb_fees_paid_in_sats(self,include_onchain_network_fees:bool,include_ln_network_fees:bool)->int:
        if not include_onchain_network_fees and not include_ln_network_fees:
            return self.total_bb_fees_paid_in_sats
        base_fee=self.total_bb_fees_paid_in_sats
        if include_ln_network_fees:
            base_fee+=(self.ln_network_fees_paid_for_payouts_in_sats+
                       self.ln_network_fees_paid_for_fee_payments_in_sats +
                       self.ln_network_fees_paid_for_bb_topup_returns_in_sats+
                       self.misc_ln_network_fees_in_sats +
                       # LN fee for sending the referral payment counts
                       # against the dev's 2% — the distributor isn't on
                       # the hook for the network cost of their delivery.
                       self.ln_network_fees_paid_for_referral_payments_in_sats +
                       # Rebalance routing fees count alongside other
                       # network fees so the operator isn't double-
                       # charged when channel maintenance eats budget.
                       self.ln_network_fees_paid_for_rebalances_in_sats
                       )
        if include_onchain_network_fees:
            base_fee+=(
                    self.onchain_network_fees_paid_for_bb_topup_returns_in_sats +
                    self.onchain_network_fees_paid_for_fee_payments_in_sats +
                    self.onchain_network_fees_paid_for_payouts_in_sats +
                    self.onchain_network_fees_paid_for_channel_opens_in_sats +
                    self.onchain_network_fees_paid_for_channel_closes_in_sats +
                    self.onchain_network_fees_paid_for_swaps_in_sats +
                    # LSP costs: included alongside network fees so the
                    # 2% cap incorporates LSP service fees rather than
                    # passing them on top of it. Miner fee on the LSP
                    # payment is a real network fee; the service fee
                    # principal is a real cost to the operator that
                    # came out of receiving the customer's revenue.
                    self.onchain_network_fees_paid_for_lsp_orders_in_sats +
                    self.onchain_lsp_service_fees_paid_in_sats +
                    # On-chain miner fee for the fallback referral
                    # delivery. Same policy as the LN-side referral
                    # fee: distributor doesn't eat delivery costs.
                    self.onchain_network_fees_paid_for_referral_payments_in_sats +
                    # Miner fee for outgoing txs not initiated by an
                    # engine-labeled path (operator manual sends,
                    # anchor sweeps, etc.). Real fee the wallet paid;
                    # counts against the 2% cap so the operator isn't
                    # over-charged when external sends consume fees
                    # the engine would otherwise have credited.
                    self.onchain_network_fees_paid_for_external_in_sats
            )
        return base_fee
    def calc_total_eligible_revenue_in_sats(self)->int:
        return self.revenue_eligible_for_fee

    def calc_remaining_referral_fee_due_in_sats(self, referral_fee_amount: float) -> int:
        """Amount of referral fee still owed to REFERRAL_FEE_DEST.

        Flat policy: just `eligible_revenue × REFERRAL_FEE_AMOUNT` minus
        what's already been paid out under that label. Network fees do
        NOT reduce the referral fee (the distributor should receive the
        configured percentage in full)."""
        if referral_fee_amount <= 0:
            return 0
        total_due = int(self.calc_total_eligible_revenue_in_sats() * referral_fee_amount)
        return max(0, total_due - self.total_referral_fees_paid_in_sats)
    def calc_total_revenue(self)->int:
        """"
        Returns total revenue (excluding topups)
        """
        total_revenue=self.ln_total_revenue_in_sats+self.onchain_total_revenue_in_sats
        total_revenue=total_revenue-self.ineligible_revenue_because_of_bb_topups_in_sats-self.ineligible_revenue_because_of_topups_in_sats
        return total_revenue

@dataclass
class PayoutInfo:
    """
    Calculated Payouts
    """
    store_id: str
    total_paid_in_sats_ln: int
    total_paid_in_sats_onchain: int
    total_network_fees_paid_ln: int
    total_network_fees_paid_onchain: int
async def get_lightning_invoice(
    lightning_address, amount_sats=500, comment=None,
):
    """
    Request a Lightning invoice from a Lightning address.

    Args:
        lightning_address (str): Lightning address in format "user@domain.com"
        amount_sats (int): Amount in satoshis (default: 500)
        comment (str | None): Optional LUD-12 comment string. If the
            recipient's LNURL metadata advertises a positive
            `commentAllowed`, the comment is URL-encoded and appended
            to the callback URL as `&comment=<encoded>`. The recipient
            typically threads it into the BOLT-11 invoice's `d`
            (description) field. Comments longer than the recipient's
            advertised maximum are truncated to fit. When the
            recipient doesn't advertise `commentAllowed` (or sets it
            to 0), the comment is silently dropped per spec — this
            avoids the LNURL callback rejecting our request when the
            recipient doesn't support comments.

    Returns:
        dict: Response containing the invoice or error information
    """
    try:
        # Parse the lightning address
        if '@' not in lightning_address:
            return {"error": "Invalid Lightning address format"}

        username, domain = lightning_address.split('@', 1)

        # Step 1: Get the LNURL-pay endpoint. Async via httpx so the
        # 30s LNURL lookup never blocks the event loop — previously
        # this used sync `requests.get` which froze the Bitcart worker
        # for the full timeout on a flaky LNURL host.
        well_known_url = f"https://{domain}/.well-known/lnurlp/{username}"

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(well_known_url)
            response.raise_for_status()
            lnurl_data = response.json()

        if 'callback' not in lnurl_data:
            return {"error": "Invalid LNURL-pay response", "details": lnurl_data}

        # Check amount limits
        min_sendable = lnurl_data.get('minSendable', 0) // 1000  # Convert from millisats
        max_sendable = lnurl_data.get('maxSendable', float('inf')) // 1000

        if amount_sats < min_sendable:
            return {"error": f"Amount too small. Minimum: {min_sendable} sats"}

        if amount_sats > max_sendable:
            return {"error": f"Amount too large. Maximum: {max_sendable} sats"}

        # Step 2: Request the invoice. Build the callback URL with the
        # mandatory `amount=` parameter plus the optional LUD-12
        # `comment=` parameter when the recipient supports it. The
        # comment is URL-encoded to handle arbitrary content safely
        # (a value with spaces, `&`, `=`, etc. won't malform the
        # query string).
        callback_url = lnurl_data['callback']
        amount_msats = amount_sats * 1000  # Convert to millisatoshis

        from urllib.parse import quote
        parts = [f"amount={amount_msats}"]
        if comment:
            comment_allowed = lnurl_data.get("commentAllowed", 0)
            if isinstance(comment_allowed, int) and comment_allowed > 0:
                # Truncate to the recipient's advertised max. Spec
                # allows truncation; the receiving end SHOULD have
                # advertised a length that fits its invoice-memo
                # constraints, so respecting the cap is the safest
                # option.
                truncated = str(comment)[:comment_allowed]
                parts.append(f"comment={quote(truncated, safe='')}")
        separator = '&' if '?' in callback_url else '?'
        invoice_url = f"{callback_url}{separator}{'&'.join(parts)}"

        async with httpx.AsyncClient(timeout=10) as client:
            invoice_response = await client.get(invoice_url)
            invoice_response.raise_for_status()
            invoice_data = invoice_response.json()

        if 'pr' not in invoice_data:
            return {"error": "No invoice received", "details": invoice_data}

        return {
            "success": True,
            "invoice": invoice_data['pr'],
            "amount_sats": amount_sats,
            "lightning_address": lightning_address,
            "metadata": {
                "description": lnurl_data.get('metadata', ''),
                "min_sendable": min_sendable,
                "max_sendable": max_sendable if max_sendable != float('inf') else None
            }
        }

    except httpx.HTTPError as e:
        # LNURL resolver is on the cashout / dev-fee invoice path —
        # operator needs to see failures in the log, not just an
        # error dict returned to the caller.
        logger.warning(
            f"lnurl_to_invoice: network error for {lightning_address}: {e} "
            f"{traceback.format_exc()}"
        )
        return {"error": f"Network error: {str(e)}"}
    except json.JSONDecodeError as e:
        logger.warning(
            f"lnurl_to_invoice: malformed JSON from {lightning_address}: "
            f"{e} {traceback.format_exc()}"
        )
        return {"error": f"JSON decode error: {str(e)}"}
    except Exception as e:
        # Catch-all — log loudly so unexpected schema drift in the
        # LNURL response surfaces.
        logger.exception(
            f"lnurl_to_invoice: unexpected error for {lightning_address}"
        )
        return {"error": f"Unexpected error: {str(e)}"}


async def is_hot_wallet(wallet:dict)->bool:
    if wallet['lightning_enabled']:
        return True
    xpub=wallet.get('xpub','')
    if xpub.count(' ')==11: # is a seed phrase
        return True
    return False


class BitcartAPI:
    """
    Bitcart API client for connecting to Bitcart instance and managing invoices.
    """

    def __init__(self, base_url: str, auth_token: str = None):
        """
        Initialize the Bitcart API client.

        Args:
            base_url: The base URL of your Bitcart instance (e.g., 'https://your-bitcart.com')
            auth_token: Your Bitcart API authentication token (Bearer token)
        """
        self.base_url = base_url.rstrip('/')
        self.auth_token = auth_token
        self.client = httpx.AsyncClient(timeout=30.0)
    async def _query(self, url:str,params:Optional[Dict[str,Any]]=None,limit:Optional[int]=None) -> Tuple[httpx.Response,List[Dict[str,Any]]]:
        """
        Return results of a paginated query. "Result" is the most recent result.

        Args:
            url: endpoint to GET.
            params: optional query-string dict.
            limit: optional per-page row cap (passed as the API's `limit` parameter).
        Returns:
            Tuple of (last httpx response, accumulated list of `result` entries).
            Returns None only on credential rejection.
        """
        current_count=0
        results_to_return=[]
        if not params:
            params=dict()
        if limit:
            params['limit']=limit
        try:
            while True:
                if current_count!=0:
                    params['offset']=current_count
                response = await self.client.get(
                    f"{url}",
                    params=params,
                    headers=self._get_headers()
                )
                jsoned=response.json()
                if isinstance(jsoned,list):
                    results_to_return.extend(jsoned)
                    return response, results_to_return
                else:
                    if 'result' not in jsoned:
                        if 'detail' in jsoned:
                            if jsoned['detail']=='Could not validate credentials':
                                logger.error(f"Error retrieving query: {traceback.format_exc()}")
                                return None
                    results_to_return.extend(jsoned['result'])
                    if jsoned['next']:
                        current_count+=int(jsoned['count'])
                    else:
                        return response, results_to_return
        except Exception as e:
            logger.error(f"Error retrieving query: {e} {traceback.format_exc()}")
            raise
    async def setup_first_user(self,email:str,password:str)->Optional[str]:
        """
        Setup first admin user, returns API key or None if unsuccesssul
        """
        post_data = {
            'email':email,
            'password':password,
            'is_superuser':True,
        }

        response = await self.client.post(
            f"{self.base_url}/users/",
            json=post_data,
            headers=self._get_headers(),follow_redirects=True
        )

        if response.status_code == 200:
            json_response = json.loads(response.text)
            if json_response['token']:
                self.auth_token=json_response['token']
                return json_response['token']
            else:
                logger.error(f"1Failed to create initial user: {response.status_code} - {response.text}")
        elif response.status_code == 400:
            logger.error(f"2Failed to create initial user: {response.status_code} - {response.text}")
        logger.error(f"3Failed to create initial user: {response.status_code} - {response.text}")
        return None
    async def is_authenticated(self) -> bool:
        """
        Check whether the API client is authenticated against the configured
        Bitcart instance. Returns False if no auth_token is set OR if a probe
        GET /wallets fails (network error / bad token). Returns True only
        after a successful round-trip.
        """
        if not self.auth_token:
            return False
        try:
            response, results = await self._query(
                f"{self.base_url}/wallets",
                params={},
            )
        except Exception as e:
            _log_api_failure("is_authenticated", e)
            return False
        return True

    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers
    async def get_outbound_liquidity(self, wallet_id:str) -> Optional[float]:
        """
        Get outbound liquidity in sats for a given wallet.

        Iterates the wallet's channels and sums local_balance on
        channels in OPEN state. Skips Electrum BACKUP rows entirely
        (they appear when a wallet is restored from seed before full
        channel-state recovery; they have a `state` field but lack
        local_balance / peer_state / remote_pubkey, so a naive read
        used to KeyError into the outer except and return None —
        which made callers' int(None) raise TypeError and silently
        skip fee/referral payments).

        Returns:
            Sats or None if errored.
        """
        try:
            total_outbound=0
            current_channels=await self.get_wallet_ln_channels(wallet_id)
            for channel in (current_channels or []):
                # BACKUP rows come from electrum.commands.list_channels:
                # they have only {type, short_channel_id, channel_id,
                # channel_point, state} — no balances. Skip rather than
                # KeyError. The active 'CHANNEL'-type and LND-shaped
                # channels still flow through normally.
                if channel.get('type') == 'BACKUP':
                    continue
                if channel.get('state') != 'OPEN':
                    continue
                total_outbound += int(channel.get('local_balance') or 0)
            return total_outbound
        except Exception as e:
            logger.error(f"Error retrieving outbound liquidity for wallet {wallet_id}: {e} {traceback.format_exc()}")
            return None
    async def get_store_inbound_liquidity(self, store_id:str) -> Optional[int]:
        """
        Get live inbound liquidity in sats for a given wallet.
        Does NOT count local balance/outbound in this

        Args:

        Returns:
            Sats or None if errored
        """
        try:
            total_inbound=0
            full_store=await self.get_store_by_id(store_id)
            for wallet_id in full_store['wallets']:
                current_channels=await self.get_wallet_ln_channels(wallet_id)
                for channel in current_channels:
                    if channel['state']!='OPEN':
                        continue
                    total_inbound+=channel['remote_balance']
            return total_inbound
        except Exception as e:
            _log_api_failure("get_store_inbound_liquidity", e)
            return None
    async def get_store_total_liquidity(self, store_id:str) -> Optional[int]:
        """
        Get live INBOUND liquidity in sats for the best LN wallet of a given
        store, counting only channels in OPEN state with a healthy peer_state.
        (Despite the name, outbound is NOT included.)

        Args:
            store_id: Store id

        Returns:
            Sats or None if errored
        """
        try:
            total=0
            full_store=await self.get_store_by_id(store_id)
            best_wallet=await self.get_best_ln_wallet_for_store(full_store)
            current_channels=await self.get_wallet_ln_channels(best_wallet['id'])
            for channel in current_channels:
                # Electrum BACKUP rows lack peer_state and remote_balance;
                # skip them rather than KeyError-ing into the outer except.
                if channel.get('type') == 'BACKUP':
                    continue
                if channel.get('state') != 'OPEN':
                    continue
                # peer_state vocabulary: Electrum uses 'GOOD' for healthy,
                # LND-via-Bitcart-proxy uses 'OPEN' (bitcart_fork's btclnd
                # daemon emits OPEN when ch.active else DISCONNECTED).
                # Accept either so LND wallets aren't silently zeroed.
                if channel.get('peer_state') not in self._ONLINE_PEER_STATES:
                    continue
                total += float(channel.get('remote_balance') or 0)
            return total
        except Exception as e:
            logger.error(f"Error retrieving store total liq: {e} {traceback.format_exc()}")
            return None
    async def get_wallets(self, limit: int = 50, offset: int = 0) -> Optional[List[Dict]]:
        """
        Retrieve a list of wallets.

        Args:
            limit: Maximum number of wallets to retrieve (default: 50)
            offset: Number of wallets to skip (default: 0)

        Returns:
            List of wallet dictionaries or None if error occurred
        """
        try:
            params = {
                "limit": limit,
                "offset": offset
            }

            response,results = await self._query(
                f"{self.base_url}/wallets",
                params=params,
            )

            return results

        except Exception as e:
            _log_api_failure("get_wallets", e)
            return None
    async def get_payouts(self) -> Optional[List[Dict]]:
        """
        Retrieve a list of payouts.

        Args:
        Returns:
            List of payout dictionaries or None if error occurred
        """
        try:

            response,results = await self._query(
            f"{self.base_url}/payouts",
            )
            return results

        except Exception as e:
            _log_api_failure("get_payouts", e)
            return None
    async def get_wallet(self, wallet_id:str,limit: int = 50, offset: int = 0) -> Optional[Dict]:
        """
        Retrieve a wallet.

        Args:
            wallet_id: the wallet to get
            limit: Maximum number of wallets to retrieve (default: 50)
            offset: Number of wallets to skip (default: 0)

        Returns:
            Wallet dictionary or None if error occurred or none found
        """
        try:
            params = {
                "limit": limit,
                "offset": offset
            }

            response = await self.client.get(
                f"{self.base_url}/wallets/{wallet_id}",
                params=params,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to retrieve wallets: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            _log_api_failure("get_wallet", e)
            return None
    async def get_store_by_id(self, store_id:str) -> Optional[Dict]:
        """
        Retrieve a store.

        Args:

        Returns:
            Store dict or None if not found
        """
        try:
            response = await self.client.get(
                f"{self.base_url}/stores/{store_id}",
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to retrieve store by id: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            _log_api_failure("get_store_by_id", e)
            return None
    async def get_best_ln_wallet_for_store(self,store:dict) -> Optional[Dict[str,Any]]:
        """
        Return the best lightning-enabled wallet to make a channel with. May return a zero-balance wallet.
        Requires wallet have name 'liquidityhelper'

        Args:

        Returns:
            Wallet dictionary or None if error occurred
        """
        try:
            best_wallet = {'balance': 0}
            best_wallet_found = False
            for known_wallet in sorted(store['wallets']):
                retrieved_wallet=await self.get_wallet(known_wallet)
                if not isinstance(retrieved_wallet,dict):
                    logger.error('Err 7774353')
                    continue
                if retrieved_wallet['name']!='liquidityhelper':
                    continue
                return retrieved_wallet
            return None
        except Exception as e:
            _log_api_failure("get_best_ln_wallet_for_store", e)
            return None
    async def get_lnd_info(self, walletid: str) -> Optional[Dict[str, Any]]:
        """
        Return LND gRPC connection info for the given wallet.

        Hits the /wallets/{id}/lndinfo endpoint added in the BareBits bitcart
        fork. Result contains host, grpc_port, network, tls_cert (b64) and
        macaroon (b64). Cached on the instance keyed by wallet id.
        """
        if not hasattr(self, "_lnd_info_cache"):
            self._lnd_info_cache: Dict[str, Dict[str, Any]] = {}
        if walletid in self._lnd_info_cache:
            return self._lnd_info_cache[walletid]
        try:
            response = await self.client.get(
                f"{self.base_url}/wallets/{walletid}/lndinfo",
                headers=self._get_headers(),
            )
            if response.status_code == 200:
                info = response.json()
                self._lnd_info_cache[walletid] = info
                return info
            logger.error(
                f"Failed to retrieve LND info for {walletid}: "
                f"{response.status_code} - {response.text}"
            )
            return None
        except Exception as e:
            _log_api_failure("get_lnd_info", e)
            return None

    async def get_wallet_ln_node_id(self, walletid:str) -> Optional[str]:
        """
        Return wallets node id/pubkey.

        Args:
            walletid: Wallet id

        Returns:
            Node id/pubkey string (from /wallets/{id}/checkln) or None if error occurred.
        """
        try:

            response = await self.client.get(
                f"{self.base_url}/wallets/{walletid}/checkln",
                params={},
                headers=self._get_headers(),
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to retrieve wallets: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            _log_api_failure("get_wallet_ln_node_id", e)
            return None
    # Bidialect channel-state sets. Bitcart's /wallets/{id}/channels
    # endpoint passes through whatever the underlying daemon emits, so
    # we accept BOTH Electrum and LND vocabularies:
    #
    #   Electrum: OPEN, OPENING, REDEEMED, CLOSED, CLOSING, FUNDED,
    #             FORCE_CLOSING
    #   LND:      OPEN, PENDING_OPEN, PENDING_CLOSE, PENDING_FORCE_CLOSE,
    #             WAITING_CLOSE, CLOSING, FORCE_CLOSING, CLOSED
    #   Also: `ACTIVE` accepted as an alias for OPEN to defend against
    #         daemon variants that report it that way.
    #
    # An unrecognized state lands in the debug-log-and-skip branch at
    # the bottom; we used to also warn-and-skip on perfectly-healthy
    # LND peer states (peer_state != 'GOOD'), which spammed logs.
    # Demoted to debug after that change — the function's own docstring
    # documents the silent-skip behavior.
    _OPEN_CHANNEL_STATES = {'OPEN', 'ACTIVE'}
    _NON_OPEN_CHANNEL_STATES = {
        # Electrum
        'OPENING', 'FUNDED', 'REDEEMED', 'CLOSED', 'CLOSING', 'FORCE_CLOSING',
        # LND
        'PENDING_OPEN', 'PENDING_CLOSE', 'PENDING_FORCE_CLOSE',
        'WAITING_CLOSE',
    }
    _ONLINE_PEER_STATES = {
        # Electrum-style:
        'GOOD', 'CONNECTED', 'ACTIVE', 'ONLINE',
        # LND-via-bitcart-proxy reports an active channel's peer_state as
        # "OPEN" (bitcart_fork/daemons/btclnd.py emits OPEN if ch.active
        # else DISCONNECTED). Without this entry, online_only=True filters
        # drop EVERY active LND channel; cascading effect: store_needs_
        # liquidity falsely reports zero healthy channels → spurious LSP
        # orders. (The dashboard's _get_inbound_liquidity bypasses the
        # proxy with a direct lnd_rpc call because of exactly this bug;
        # adding OPEN here keeps the proxy path correct too so we don't
        # need bypasses everywhere.)
        'OPEN',
    }

    async def get_wallet_ln_channels(self, walletid:str,active_only:bool=False,online_only:bool=False) -> Optional[List[Dict]]:
        """Return channels for the wallet, optionally filtered.

        Args:
            walletid: Wallet id
            active_only: when True, exclude channels not currently in
                an OPEN state (whether the state is reported as the
                Electrum 'OPEN' or the LND 'OPEN' string — they happen
                to coincide; the difference is in pending/closed states).
            online_only: when True, additionally exclude OPEN channels
                whose peer connection isn't healthy. Accepts both the
                Electrum 'GOOD' vocabulary and LND-style boolean
                `active=True` (interpreted as ONLINE) or the strings
                CONNECTED/ACTIVE/ONLINE.

        Returns:
            List of channel dicts. Unknown channel states log a debug
            message (not a warning) and are skipped silently — this
            prevents log floods on Bitcart-side daemon updates that
            introduce new state strings.
        """
        try:

            response,results = await self._query(
                f"{self.base_url}/wallets/{walletid}/channels",
                params={},
            )
            if not active_only and not online_only:
                return results
            return_list=[]
            for channel in results:
                channel_state = (channel.get('state') or '').upper()
                # peer_state may be the Electrum string OR an LND-style
                # boolean from `active`. Normalize to "is online?".
                raw_peer_state = channel.get('peer_state')
                if isinstance(raw_peer_state, bool):
                    is_peer_online = raw_peer_state
                elif isinstance(raw_peer_state, str):
                    is_peer_online = raw_peer_state.upper() in self._ONLINE_PEER_STATES
                else:
                    # Some LND-via-Bitcart responses may use `active` instead
                    # of `peer_state`. Fall back to that.
                    is_peer_online = bool(channel.get('active'))

                if channel_state in self._NON_OPEN_CHANNEL_STATES:
                    continue
                if channel_state in self._OPEN_CHANNEL_STATES:
                    if online_only and not is_peer_online:
                        continue
                    return_list.append(channel)
                    continue
                # Unknown state: log at DEBUG (not WARNING) to avoid
                # flooding when Bitcart adds new state strings, and skip.
                logger.debug(
                    f'get_wallet_ln_channels: skipping channel with '
                    f'unrecognized state={channel_state!r} '
                    f'(wallet {walletid})'
                )
            return return_list
        except Exception as e:
            _log_api_failure("get_wallet_ln_channels", e)
            return None
    async def get_stores(self) -> Optional[List[Dict[str,Any]]]:
        """
        Retrieve a list of stores.

        Returns:
            List of store dictionaries or None if error occurred
        """
        try:
            response,storelist = await self._query(
                f"{self.base_url}/stores",
            )

            return storelist
        except Exception as e:
            _log_api_failure("get_stores", e)
            return None
    async def get_invoice_by_note(self, limit: int = 250,
                           note:Optional[str]=None,require_pending:bool=False) -> Optional[List[Dict]]:
        """
        Returns the first invoice found which matches note and is not expired

        Args:
            limit: Maximum number of invoices to scan (default: 250).
            note: the note to search for
            require_pending: only return invoices with status == "pending"
                (skip paid / complete / expired / invalid). Used by the
                topup-invoice flow so a paid-and-still-listed invoice
                doesn't get reused instead of creating a fresh one for
                the next deficit.

        Returns:
            First invoice dict matching `note` with `time_left > 90` (and,
            if require_pending=True, status == "pending"); None if not
            found or on error.
        """
        try:
            params = {
                "limit": limit,
                'query':f'notes:{note}'
            }
            response = await self.client.get(
                f"{self.base_url}/invoices",
                params=params,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                jsoned=response.json()
                for invoice in jsoned['result']:
                    if invoice['notes']==note:
                        if invoice['time_left']>90:
                            if require_pending:
                                if invoice.get('status') == 'pending':
                                    return invoice
                            else:
                                return invoice
                return None
            else:
                logger.error(f"Failed to retrieve invoices: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            _log_api_failure("get_invoice_by_note", e)
            return None
    async def get_invoices(self, limit: int = 50, offset: int = 0,
                           store_id: str = None) -> Optional[List[Dict]]:
        """
        Retrieve a list of recent invoices from Bitcart.

        Args:
            limit: Maximum number of invoices to retrieve (default: 50)
            offset: Number of invoices to skip (default: 0)
            store_id: Optional store ID to filter invoices

        Returns:
            List of invoice dictionaries or None if error occurred
        """
        try:
            params:Dict[str,Any] = {
                "limit": limit,
                "offset": offset
            }

            if store_id:
                params["store_id"] = store_id

            response,results = await self._query(
                f"{self.base_url}/invoices",
                params=params,
            )

            return results

        except Exception as e:
            _log_api_failure("get_invoices", e)
            return None


    async def get_channel_by_id(self, wallet_id:str,channel_id: str) -> Optional[Dict[str,Union[str,int,float]]]:
        """
        Retrieve a specific channel by its ID.

        Args:
            channel_id: The ID of the channel to retrieve

        Returns:
            Channel dictionary or None if not found/error occurred
        """
        try:
            response = await self.get_wallet_ln_channels(wallet_id)
            for channel in response:
                if channel['channel_id']==channel_id:
                    return channel
        except Exception as e:
            _log_api_failure("get_channel_by_id", e)
            return None
        return None
    async def get_invoice_by_id(self, invoice_id: str) -> Optional[Dict]:
        """
        Retrieve a specific invoice by its ID.

        Args:
            invoice_id: The ID of the invoice to retrieve

        Returns:
            Invoice dictionary or None if not found/error occurred
        """
        try:
            response = await self.client.get(
                f"{self.base_url}/invoices/{invoice_id}",headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to retrieve invoice {invoice_id}: {response.status_code}")
                return None

        except Exception as e:
            _log_api_failure("get_invoice_by_id", e)
            return None

    async def add_wallet_to_store(self, wallet_ids: List[str], store_id: str) -> bool:
        """
        Add a wallet to a store

        Args:
            wallet_ids: List of wallet IDs (required)
            store_id: Store ID (str) (required)

        Returns:
            True if successful, false if not
        """
        try:
            invoice_data = {
                "wallets": wallet_ids,
            }

            response = await self.client.patch(
                f"{self.base_url}/stores/{store_id}",
                json=invoice_data,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return True
            else:
                logger.error(f"Failed to add wallet to store. Store {store_id} wallets {wallet_ids} {response.text}")
                return False

        except Exception as e:
            logger.error(
                f"Error adding wallet to store {store_id} invoice: {e} "
                f"{traceback.format_exc()}"
            )
            return False
    async def close_ln_channel(self,wallet_id:str,channel_point:str,force:bool=False) -> Optional[str]:
        """
        Close an LN channel

        Args:

        Returns:
            txid of closing transaction
        """
        try:
            post_data = {
                "channel_point": channel_point,
                "force": force,
            }

            response = await self.client.post(
                f"{self.base_url}/wallets/{wallet_id}/channels/close",
                json=post_data,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to close channel: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(
                f"Error closing channel: {e} {traceback.format_exc()}"
            )
            return None
    async def open_ln_channel(self,wallet_id:str,dest_node:str,amount_sats:int) -> Optional[Dict]:
        """
        Create a new LN channel. `amount_sats` is the channel capacity in
        satoshis (NOT BTC). BareBits's btclnd daemon (the only target this
        path is gated to) takes the value as int sats and feeds it directly
        to LND's OpenChannelSync.local_funding_amount; passing BTC here would
        silently open a 0-sat channel (int(Decimal("0.001")) == 0).

        Args:

        Returns:
            Channel anchor point or None if errored
        """
        try:
            post_data = {
                "amount": int(amount_sats),
                "node_id": dest_node,
            }

            response = await self.client.post(
                f"{self.base_url}/wallets/{wallet_id}/channels/open",
                json=post_data,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to create channel w {dest_node}: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(
                f"1Error creating channel: {e} {traceback.format_exc()}"
            )
            return None
    async def create_wallet_seed(self, currency: str = 'btc') -> Optional[Dict]:
        """
        Generate a new wallet seed phrase for the given currency.

        Args:
            currency: Bitcart crypto code. Defaults to 'btc' (Electrum) for
                back-compat. Pass 'btclnd' on deployments where Bitcart was
                built with the LND container so the daemon returns an
                LND-compatible seed.

        Returns:
            Created wallet seed dict or None if error occurred
        """
        try:
            post_data = {
                "currency": currency,
                "hot_wallet": True,
            }

            response = await self.client.post(
                f"{self.base_url}/wallets/create",
                json=post_data,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to create invoice: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            _log_api_failure("create_wallet_seed", e)
            return None
    async def is_channel_change_pending(self,wallet_id:str)->bool:
        """
        returns True if any channel opens/closes pending on wallet
        """
        close_result=await self.is_channel_close_pending(wallet_id)
        if close_result:
            return True
        open_result=await self.is_channel_open_pending(wallet_id)
        if open_result:
            return True
        return False
    async def is_channel_close_pending(self,wallet_id:str)->bool:
        """
        Given wallet ID, return True if a channel CLOSE is pending or
        unable to determine whether one is pending (errors fall through
        to True so callers can treat ambiguity as "wait, don't act").
        """
        try:
            current_channels=await self.get_wallet_ln_channels(wallet_id)
            for channel in current_channels:
                if channel['state']=='CLOSING':
                    return True
            return False
        except Exception as e:
            _log_api_failure("is_channel_close_pending", e)
            return True
    async def is_channel_open_pending(self,wallet_id:str)->bool:
        """
        Given wallet ID, return True if a channel open is pending or unable to figure out if a channel open is pending.

        Match both state vocabularies: Electrum emits 'OPENING' / 'FUNDED',
        Bitcart's btclnd daemon normalizes LND's PendingChannels into
        'PENDING_OPEN'. Previously we only matched 'OPENING', which
        silently returned False for any LND wallet.
        """
        try:
            current_channels=await self.get_wallet_ln_channels(wallet_id)
            for channel in current_channels:
                if channel['state'] in ('OPENING', 'FUNDED', 'PENDING_OPEN'):
                    return True
            return False
        except Exception as e:
            _log_api_failure("is_channel_open_pending", e)
            return True
    async def create_wallet(self, seed: str, currency: str = 'btc') -> Optional[Dict]:
        """
        Create a new wallet with a given seed.

        Args:
            seed: seed phrase / xpub for the wallet.
            currency: Bitcart crypto code. Defaults to 'btc' (Electrum) for
                back-compat. Pass 'btclnd' on LND-capable deployments — the
                Bitcart server will then provision an LND-backed wallet.

        Returns:
            Created wallet or None if error occurred
        """
        try:
            post_data = {
                "name": 'liquidityhelper',
                "xpub": seed,
                "lightning_enabled":True,
                'currency': currency,
            }

            response = await self.client.post(
                f"{self.base_url}/wallets",
                json=post_data,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to create wallet: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            _log_api_failure("create_wallet", e)
            return None
    async def create_store(self,store_name:str,wallet_id_list:List[str]) -> Optional[Dict]:
        """
        Create a new store

        Args:
        Returns:
            Created store or None if error occurred
        """
        try:
            post_data = {
                "name": store_name,
                "wallets": wallet_id_list,
            }

            response = await self.client.post(
                f"{self.base_url}/stores",
                json=post_data,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"1Failed to create store: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(
                f"Error creating payout: {e} {traceback.format_exc()}"
            )
            return None
    async def create_payout_onchain(self,store_id:str,wallet_id:str,amount_in_sats:int,destination_address:str,max_fee:int=None,reason:str='') -> Optional[Dict]:
        """
        Create a new payout

        Args:
        Returns:
            Created payout or None if error occurred
        """
        try:
            post_data = {
                "destination": destination_address,
                "store_id": store_id,
                "wallet_id":wallet_id,
                "metadata": {'reason':reason},
                "amount": sats_to_btc(amount_in_sats),
                "max_fee":max_fee,
                "currency":'btc'
            }

            response = await self.client.post(
                f"{self.base_url}/payouts",
                json=post_data,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"1Failed to create payout: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(
                f"Error creating payout: {e} {traceback.format_exc()}"
            )
            return None
    async def create_invoice(self, price_in_btc: Optional[float], store_id: int, currency: str = "USD",
                             order_id: str = None, description: str = "",
                             buyer_email: str = "", notification_url: str = "",
                             redirect_url: str = "", expiration_in_minutes:Optional[int]=None, notes:Optional[str]='') -> Optional[Dict]:
        """
        Create a new invoice.

        Args:
            price_in_btc: Invoice amount (required, None means no invoice amount)
            store_id: Store ID (required)
            currency: Currency code (default: USD)
            order_id: Custom order ID (auto-generated if not provided)
            description: Invoice description
            buyer_email: Buyer's email address
            notification_url: URL for webhook notifications
            redirect_url: URL to redirect after payment
            expiration_in_minutes: optional invoice expiration in MINUTES;
                passed through as the API's `expiration` field, which
                Bitcart stores in minutes and multiplies by 60 to derive
                `expiration_seconds` server-side. Values that exceed
                LND's 1-year (525,600 minutes) max expiry on lightning-
                enabled wallets will cause Bitcart's payment-method
                creation to throw, leaving the invoice with no
                payments rows.
            notes: optional free-text tag stored on the invoice; only included in the
                request when truthy. Used by topup-invoice flow (TOPUP_NAME / TOPUP_BAREBITS).

        Returns:
            Created invoice dictionary or None if error occurred
        """
        try:
            if order_id is None:
                order_id = f"order_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

            invoice_data = {
                "price": str(price_in_btc),
                "store_id": str(store_id),
                "currency": currency,
                "order_id": str(order_id),
                "buyer_email": buyer_email,
                "notification_url": notification_url,
                "redirect_url": redirect_url,
                "description": description,
                'expiration': expiration_in_minutes,
            }
            if notes:
                invoice_data['notes']=notes

            response = await self.client.post(
                f"{self.base_url}/invoices",
                json=invoice_data,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to create invoice: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            _log_api_failure("create_invoice", e)
            return None

    async def get_btc_usd_rate(self) -> Optional[float]:
        """Fetch the current BTC→USD spot price from Bitcart's
        `/cryptos/rate` endpoint (crypto + fiat passed as query
        parameters, see param-shape note below). Returns None on any
        failure (network error, non-JSON response, missing field) so
        callers can show "USD unavailable" without crashing.

        Bitcart proxies CoinGecko under the hood; calling it via Bitcart
        means we use whatever rate Bitcart itself shows on store pages —
        keeps the dashboard consistent with the store admin UI.
        """
        # NOTE: `_query` is for paginated LIST endpoints that return
        # `{result: [...], next, count}`. /cryptos/rate is a scalar
        # endpoint that returns the rate as a bare JSON number, which
        # `_query` blows up on (KeyError 'result'). Make a direct
        # request here instead.
        #
        # Param shape is `currency=<crypto>&fiat_currency=<fiat>` (NOT
        # `coin=<crypto>&currency=<fiat>` — that was the old shape and
        # the bitcart server returns "Unsupported currency"). Defaults
        # are btc/USD, which is what we want, but pass them explicitly
        # so the call survives any future default change upstream.
        try:
            response = await self.client.get(
                f"{self.base_url}/cryptos/rate",
                params={"currency": "btc", "fiat_currency": "USD"},
                headers=self._get_headers(),
            )
            body = response.json()
            if isinstance(body, (int, float)):
                return float(body)
            # Defensive: future bitcart builds might wrap it.
            if isinstance(body, dict) and "rate" in body:
                return float(body["rate"])
            logger.warning(f"unexpected /cryptos/rate response shape: {body!r}")
            return None
        except Exception as e:
            logger.warning(f"failed to fetch BTC/USD rate from Bitcart: {e} {traceback.format_exc()}")
            return None

    async def get_supported_currencies(self) -> Optional[Set[str]]:
        """Return the set of crypto-currency codes the Bitcart server
        supports (e.g. {"btc", "btclnd", "btccln", "eth", ...}).

        Used by wallet-creation flows to pick the best wallet flavor
        for this deployment — prefer `btclnd` when available (richer
        Lightning feature set than the bare Electrum daemon), fall
        back to `btc` (Electrum) when Bitcart wasn't built with the
        LND daemon container.

        Returns None on transport failure (so callers can distinguish
        "couldn't determine" from "supports nothing"); an empty set
        is conceivable but unlikely in practice.

        Bitcart's `/cryptos` endpoint returns a paginated list:
          {"result": [...codes...], "next": ..., "count": N}
        For most deployments the list is short enough that a single
        page suffices, but we use the existing _query paginator for
        forward-compat with installs that wire many altcoins.
        """
        try:
            response, items = await self._query(
                f"{self.base_url}/cryptos", limit=200,
            )
            if response is None:
                return None
            codes: Set[str] = set()
            for item in items or []:
                # `/cryptos` historically returns either a list of
                # bare code strings ["btc", "btclnd", ...] or a list
                # of dicts [{"code": "btc", ...}, ...]. Accept both.
                if isinstance(item, str):
                    codes.add(item.lower())
                elif isinstance(item, dict):
                    code = item.get("code") or item.get("name")
                    if isinstance(code, str):
                        codes.add(code.lower())
            return codes
        except Exception as e:
            logger.warning(f"failed to fetch supported currencies: {e} {traceback.format_exc()}")
            return None

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()



