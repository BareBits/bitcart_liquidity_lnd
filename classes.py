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
logger = logging.getLogger(__name__)
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
    ln_network_fees_paid_for_bb_topup_returns_in_sats: int # not actually used yet, using misc_ln_network_fees_in_sats
    onchain_network_fees_paid_for_bb_topup_returns_in_sats: int
    ln_network_fees_paid_for_fee_payments_in_sats:int # not actually used yet, using misc_ln_network_fees_in_sats
    onchain_network_fees_paid_for_fee_payments_in_sats: int
    ln_network_fees_paid_for_payouts_in_sats: int # not actually used yet, using misc_ln_network_fees_in_sats
    misc_ln_network_fees_in_sats: int # these are fees not correlated to a specific payout/fee since we don't need that amount of precision yet
    onchain_network_fees_paid_for_payouts_in_sats: int
    onchain_network_fees_paid_for_channel_opens_in_sats: int
    onchain_network_fees_paid_for_channel_closes_in_sats: int
    def calc_total_bb_fees_paid_in_sats(self,include_onchain_network_fees:bool,include_ln_network_fees:bool)->int:
        if not include_onchain_network_fees and not include_ln_network_fees:
            return self.total_bb_fees_paid_in_sats
        base_fee=self.total_bb_fees_paid_in_sats
        if include_ln_network_fees:
            base_fee+=(self.ln_network_fees_paid_for_payouts_in_sats+
                       self.ln_network_fees_paid_for_fee_payments_in_sats +
                       self.ln_network_fees_paid_for_bb_topup_returns_in_sats+
                       self.misc_ln_network_fees_in_sats
                       )
        if include_onchain_network_fees:
            base_fee+=(
                    self.onchain_network_fees_paid_for_bb_topup_returns_in_sats +
                    self.onchain_network_fees_paid_for_fee_payments_in_sats +
                    self.onchain_network_fees_paid_for_payouts_in_sats +
                    self.onchain_network_fees_paid_for_channel_opens_in_sats +
                    self.onchain_network_fees_paid_for_channel_closes_in_sats
            )
        return base_fee
    def calc_total_eligible_revenue_in_sats(self)->int:
        return self.revenue_eligible_for_fee
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
async def get_lightning_invoice(lightning_address, amount_sats=500):
    """
    Request a Lightning invoice from a Lightning address.

    Args:
        lightning_address (str): Lightning address in format "user@domain.com"
        amount_sats (int): Amount in satoshis (default: 500)

    Returns:
        dict: Response containing the invoice or error information
    """
    try:
        # Parse the lightning address
        if '@' not in lightning_address:
            return {"error": "Invalid Lightning address format"}

        username, domain = lightning_address.split('@', 1)

        # Step 1: Get the LNURL-pay endpoint
        well_known_url = f"https://{domain}/.well-known/lnurlp/{username}"

        response = requests.get(well_known_url, timeout=30)
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

        # Step 2: Request the invoice
        callback_url = lnurl_data['callback']
        amount_msats = amount_sats * 1000  # Convert to millisatoshis

        # Add amount parameter to callback URL
        separator = '&' if '?' in callback_url else '?'
        invoice_url = f"{callback_url}{separator}amount={amount_msats}"

        invoice_response = requests.get(invoice_url, timeout=10)
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

    except requests.exceptions.RequestException as e:
        return {"error": f"Network error: {str(e)}"}
    except json.JSONDecodeError as e:
        return {"error": f"JSON decode error: {str(e)}"}
    except Exception as e:
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
            limit: Maximum number of invoices to retrieve (default: 50)

        Returns:
            Most recent response + List of contents of 'result' from query or None
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
                                logger.error(f"Error retrieving query: {traceback.print_exc()}")
                                return None
                    results_to_return.extend(jsoned['result'])
                    if jsoned['next']:
                        current_count+=int(jsoned['count'])
                    else:
                        return response, results_to_return
        except Exception as e:
            logger.error(f"Error retrieving query: {e} {traceback.print_exc()}")
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
        Check if the API client has an authentication token.

        Returns:
            bool: True if auth token is available, False otherwise
        """
        if not self.auth_token:
            return False
        try:
            response, results = await self._query(
                f"{self.base_url}/wallets",
                params={},
            )
        except Exception as e:
            logger.error(f"Error connecting to BitCart API: {e}")
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
        Get outbound liquidity in sats for a given wallet

        Args:

        Returns:
            Sats or None if errored
        """
        try:
            total_outbound=0
            current_channels=await self.get_wallet_ln_channels(wallet_id)
            for channel in current_channels:
                if channel['state']!='OPEN':
                    continue
                total_outbound+=channel['local_balance']
            return total_outbound
        except Exception as e:
            print(f"Error retrieving store by id: {e}")
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
            print(f"Error retrieving store by id: {e}")
            return None
    async def get_store_total_liquidity(self, store_id:str) -> Optional[int]:
        """
        Get live inbound + outbound liquidity in sats for a given wallet.

        Args:

        Returns:
            Sats or None if errored
        """
        try:
            total=0
            full_store=await self.get_store_by_id(store_id)
            best_wallet=await self.get_best_ln_wallet_for_store(full_store)
            current_channels=await self.get_wallet_ln_channels(best_wallet['id'])
            for channel in current_channels:
                if channel['state']!='OPEN':
                    continue
                if channel['peer_state']!='GOOD':
                    continue
                total+=float(channel['remote_balance'])
                total += float(channel['remote_balance'])
            return total
        except Exception as e:
            print(f"Error retrieving store total liq: {e} {traceback.print_exc()}")
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
            print(f"Error retrieving wallets: {e}")
            return None
    async def get_payouts(self) -> Optional[List[Dict]]:
        """
        Retrieve a list of payouts.

        Args:
        Returns:
            List of wallet dictionaries or None if error occurred
        """
        try:

            response,results = await self._query(
            f"{self.base_url}/payouts",
            )
            return results

        except Exception as e:
            print(f"Error retrieving payouts: {e}")
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
                print(f"Failed to retrieve wallets: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"Error retrieving wallets: {e}")
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
                print(f"Failed to retrieve store by id: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"Error retrieving store by id: {e}")
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
                    print('Err 7774353')
                    continue
                if not retrieved_wallet.get('lightning_enabled', False):
                    continue
                if retrieved_wallet['name']!='liquidityhelper':
                    continue
                existing_balance = float(best_wallet['balance'])
                found_balance = float(retrieved_wallet.get('balance', 0))
                if found_balance >= existing_balance:
                    best_wallet = retrieved_wallet
                    best_wallet_found = True
                if best_wallet_found:
                    return best_wallet
                else:
                    return None
        except Exception as e:
            print(f"xError retrieving wallets: {e}")
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
            logger.error(f"Error retrieving LND info for {walletid}: {e}")
            return None

    async def get_wallet_ln_node_id(self, walletid:str) -> Optional[str]:
        """
        Return wallets node id/pubkey.

        Args:
            walletid: Wallet channels

        Returns:
            List of invoice dictionaries or None if error occurred
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
                print(f"Failed to retrieve wallets: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"Error retrieving wallets: {e}")
            return None
    async def get_wallet_ln_channels(self, walletid:str,active_only:bool=False,online_only:bool=False) -> Optional[List[Dict]]:
        """
        Return wallets LN info.

        Args:
            walletid: Wallet channels

        Returns:
            List of invoice dictionaries or None if error occurred
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
                channel_state=channel['state']
                peer_state=channel['peer_state']
                if channel_state in {'REDEEMED','CLOSED','OPENING'}:
                    continue
                elif channel_state=='OPEN':
                    if peer_state not in {'GOOD'}:
                        logger.warning(f'Found unknown peer state in get_wallet_ln_channels: {peer_state}')
                    if not online_only:
                        return_list.append(channel)
                        continue
                    if online_only and peer_state!='GOOD':
                        continue
                    elif online_only and peer_state=='GOOD':
                        return_list.append(channel)
                        continue
                else:
                    logger.warning(f'Found unknown channel state in get_wallet_ln_channels: {channel_state}')
            return return_list
        except Exception as e:
            print(f"Error retrieving channels: {e}")
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
            print(f"Error retrieving stores: {e}")
            return None
    async def get_invoice_by_note(self, limit: int = 250,
                           note:Optional[str]=None,require_unlimited:bool=False) -> Optional[List[Dict]]:
        """
        Returns the first invoice found which matches note and is not expired

        Args:
            limit: Maximum number of invoices to retrieve (default: 50)
            note: the note to search for
            require_unlimited: require returned invoice have no invoice amount

        Returns:
            List of invoice dictionaries or None if error occurred
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
                            if require_unlimited:
                                if float(invoice['price'])==0.00:
                                    return invoice
                            else:
                                return invoice
                return None
            else:
                print(f"Failed to retrieve invoices: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"Error retrieving invoices: {e}")
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
            print(f"Error retrieving invoices: {e}")
            return None


    async def get_channel_by_id(self, wallet_id:str,channel_id: str) -> Optional[Dict[str,Union[str,int,float]]]:
        """
        Retrieve a specific channel by its ID.

        Args:
            channel_id: The ID of the channel to retrieve

        Returns:
            Invoice dictionary or None if not found/error occurred
        """
        try:
            response = await self.get_wallet_ln_channels(wallet_id)
            for channel in response:
                if channel['channel_id']==channel_id:
                    return channel
        except Exception as e:
            print(f"Error retrieving channel {channel_id}: {e}")
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
                print(f"Failed to retrieve invoice {invoice_id}: {response.status_code}")
                return None

        except Exception as e:
            print(f"Error retrieving invoice {invoice_id}: {e}")
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
                print(f"Failed to add wallet to store. Store {store_id} wallets {wallet_ids} {response.text}")
                return False

        except Exception as e:
            print(f"Error adding wallet to store {store_id} invoice: {e}")
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
                print(f"Failed to close channel: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"Error closing channel: {e}")
            return None
    async def open_ln_channel(self,wallet_id:str,dest_node:str,amount_in_btc:float) -> Optional[Dict]:
        """
        Create a new LN channel

        Args:

        Returns:
            Channel anchor point or None if errored
        """
        try:
            post_data = {
                "amount": amount_in_btc,
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
            print(f"1Error creating channel: {e}")
            return None
    async def create_wallet_seed(self,) -> Optional[Dict]:
        """
        Create a new wallet.

        Args:

        Returns:
            Created wallet or None if error occurred
        """
        try:
            post_data = {
                "currency": 'btc',
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
                print(f"Failed to create invoice: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"Error creating invoice: {e}")
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
        Given wallet ID, return True if a channel open is pending or unable to figure out if a channel open is pending
        """
        try:
            current_channels=await self.get_wallet_ln_channels(wallet_id)
            for channel in current_channels:
                if channel['state']=='CLOSING':
                    return True
            return False
        except Exception as e:
            print(f'Error in is_channel_open_pending: {e}')
            return True
    async def is_channel_open_pending(self,wallet_id:str)->bool:
        """
        Given wallet ID, return True if a channel open is pending or unable to figure out if a channel open is pending
        """
        try:
            current_channels=await self.get_wallet_ln_channels(wallet_id)
            for channel in current_channels:
                if channel['state']=='OPENING':
                    return True
            return False
        except Exception as e:
            print(f'Error in is_channel_open_pending: {e}')
            return True
    async def create_wallet(self,seed:str) -> Optional[Dict]:
        """
        Create a new wallet with a given seed

        Args:

        Returns:
            Created wallet or None if error occurred
        """
        try:
            post_data = {
                "name": 'liquidityhelper',
                "xpub": seed,
                "lightning_enabled":True,
                'currency':'btc'
            }

            response = await self.client.post(
                f"{self.base_url}/wallets",
                json=post_data,
                headers=self._get_headers()
            )

            if response.status_code == 200:
                return response.json()
            else:
                print(f"Failed to create wallet: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"Error creating wallet: {e}")
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
            print(f"Error creating payout: {e}")
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
                print(f"1Failed to create payout: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"Error creating payout: {e}")
            return None
    async def create_invoice(self, price_in_btc: Optional[float], store_id: int, currency: str = "USD",
                             order_id: str = None, description: str = "",
                             buyer_email: str = "", notification_url: str = "",
                             redirect_url: str = "", expiration_in_seconds:Optional[int]=None, notes:Optional[str]='') -> Optional[Dict]:
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
                'expiration': expiration_in_seconds,
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
                print(f"Failed to create invoice: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"Error creating invoice: {e}")
            return None

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()



