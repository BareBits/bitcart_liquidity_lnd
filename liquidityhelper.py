import json, dataclasses,math
from time import sleep
from typing import Tuple, Union, Callable,Iterable,Set,Optional,Dict,List
import asyncio, database, node_db_update, inspect
from peewee import DoesNotExist
import requests
import time
import time

import notifications
from notifications import EmailNotificationProvider,NotificationProvider
from typing import Dict, Any, Optional


import common_functions
import config
from config import AUTH_TOKEN
import node_database
from megalithic import MegalithicLSPClient
import zeus, megalithic, traceback
from database import (
    SimpleDateTimeField,
    SimpleCacheField,
    LOrder,
    LastRunTracker,
    SimpleVariable,
    Notification,
)

from classes import (
    get_lightning_invoice,
    StoreStats,
    BitcartInvoice,
)
from common_functions import sats_to_btc, btc_to_sats
import datetime, sys

import logging
from logging.handlers import RotatingFileHandler
import queue

from zeus import ZeusLSPS1Client
from classes import BitcartAPI
import dateutil.parser
from config import *
from copy import deepcopy
import hashlib

# Setup logging
logger = logging.getLogger(__name__)
if LOG_LEVEL=='DEBUG':
    logger.setLevel(logging.DEBUG)
elif LOG_LEVEL=='WARNING':
    logger.setLevel(logging.WARNING)
elif LOG_LEVEL=='ERROR':
    logger.setLevel(logging.ERROR)
elif LOG_LEVEL=='INFO':
    logger.setLevel(logging.INFO)
else:
    logger.setLevel(logging.WARNING)
main_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

# Save logs to file
file_handler = RotatingFileHandler(
    "liquidityhelper.log", maxBytes=10000000, backupCount=5
)
file_handler.setLevel(logging.WARNING)
file_handler.setFormatter(main_formatter)
logger.addHandler(file_handler)

# Do queued logging to increase responsiveness
log_queue = queue.Queue(250)
# Configure the downstream console handler
console_handler = logging.StreamHandler(stream=sys.stdout)
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(main_formatter)
# Create and start the QueueListener with the console handler
listener = logging.handlers.QueueListener(log_queue, console_handler)
listener.start()
queue_handler = logging.handlers.QueueHandler(log_queue)
# Configure the logger to use the QueueHandler
logger.addHandler(queue_handler)


def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    logger.critical(
        "uncaught exception, application will terminate.",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


sys.excepthook = handle_uncaught_exception

from node_database import LightningNode, LightningChannel,is_node_blacklisted

LAST_FEE_CHECK = datetime.datetime.now()
START_TIME = datetime.datetime.now()
NOTIFICATION_PROVIDERS:List[NotificationProvider]=[]
def hash_string(mystring: str) -> str:
    # Encode the string to bytes
    encoded_string = mystring.encode("utf-8")
    # Create a new hash object and update it with the encoded string
    hash_object = hashlib.sha256(encoded_string)
    # Get the hexadecimal representation of the hash
    hex_digest = hash_object.hexdigest()

    return hex_digest


async def run_every_x_seconds(
    *args, my_func: Callable, hash_arguments: bool = False, seconds: int, **kwargs
):
    """
    Run a given function my_func but only if it hasn't been run in x seconds.
    If hash_arguments is false, every run of the function counts as a recent run, otherwise, a recent run only counts if the arguments match exactly (including order)
    Haven't thoroughly tested the hash_arguments function or made it order-agnostic
    """
    if hash_arguments:
        hash_source = str(my_func.__name__) + str(args) + str(kwargs)
    else:
        hash_source = str(my_func.__name__)
    object_hash = hash_string(hash_source)
    db_object: Optional[LastRunTracker] = LastRunTracker.get_or_none(name=object_hash)
    if not db_object:
        my_tracker = LastRunTracker(name=object_hash)
        my_tracker.save()
        logger.debug(f"Running :{my_func.__name__} bc never run before.")
        if inspect.iscoroutinefunction(my_func):
            return await my_func(*args, **kwargs)
        else:
            return my_func(*args, **kwargs)
    else:
        last_run = db_object.last_run
        time_difference = datetime.datetime.now() - last_run
        seconds_ago = time_difference.total_seconds()
        if seconds_ago > seconds:
            logger.debug(
                f"Running {my_func.__name__} bc hasnt been run in {seconds} seconds"
            )
            db_object.last_run = datetime.datetime.now()
            db_object.save()
            if inspect.iscoroutinefunction(my_func):
                return await my_func(*args, **kwargs)
            else:
                return my_func(*args, **kwargs)
        logger.debug(
            f"Not running {my_func.__name__} bc has been run {seconds_ago} ago which is less than target {seconds} seconds"
        )
        return None


async def run_every_x_minutes(*args, my_func: Callable, minutes: int, **kwargs):
    return await run_every_x_seconds(seconds=minutes * 60, my_func=my_func, *args, **kwargs)


async def run_every_x_hours(*args, my_func: Callable, hours: int, **kwargs):
    return await run_every_x_seconds(seconds=hours * 60 * 60, my_func=my_func, *args, **kwargs)


async def run_every_x_days(*args, my_func: Callable, days: int, **kwargs):
    return await run_every_x_seconds(seconds=days * 24 * 60 * 60, my_func=my_func, *args, **kwargs)


async def find_offline_channels(xpub: str):
    """
    Finds offline channels, closes if channels are of low quality
    """
    found_channels = await electrum_rpc("list_channels", myxpub=xpub)
    checked_peers = set()
    for channel in found_channels["result"]:
        peer_address = channel["remote_pubkey"].lower()
        peer_state = channel["peer_state"]
        channel_state = channel["state"]
        channel_id = channel["short_channel_id"]
        # make sure each peer is only checked once, we may have multiple channels with them though ideally we shouldn't
        if peer_address in checked_peers:
            continue
        checked_peers.add(peer_address)
        node_object: Optional[LightningNode] = LightningNode.get_or_none(
            LightningNode.node_address == peer_address
        )
        if not node_object:
            logger.warning(
                f"Warning: in find_offline_channels, peer detected w no matching object: {peer_address} for channel id {channel_id}\nChannel dump: {channel}"
            )
            node_object = LightningNode(
                node_address=peer_address,
                last_magma_query=datetime.datetime(1990, 12, 12, 12, 12, 12),
            )
            node_object.save(force_insert=True)
        if channel_state in {"REDEEMED", "CLOSED",'OPENING'}:
            continue
        elif channel_state == "OPEN":
            pass
        else:
            logger.warning(
                f"Warning: in find_offline_channels, unknown channel state detected: {channel_state} for peer, channel id: {peer_address}, {channel_id}\nChannel dump: {channel}"
            )
            continue
        node_object.total_uptime_checks += 1
        if peer_state in {"CONNECTED", "GOOD"}:
            node_object.last_seen_online = datetime.datetime.now()
            continue
        elif peer_state == "DISCONNECTED":
            node_object.failed_uptime_checks += 1
        else:
            logger.warning(
                f"Warning: in find_offline_channels, unknown peer state detected: {peer_state} for peer, channel id: {peer_address}, {channel_id}\nChannel dump: {channel}"
            )
        node_object.save()
        if should_close_channel(
            node_object.failed_uptime_checks,
            node_object.total_uptime_checks,
            node_object.last_seen_online,
            RUN_FREQUENCY_LIQUIDITYCHECK,
        ):
            logger.info(
                f"Attempting cooperative channel close due to should_close_channel: {channel_id}"
            )
            channel_point=channel["channel_point"]
            close_result = await attempt_cooperative_close(
                channel_point=channel_point, xpub=xpub
            )
            channel_object:Optional[LightningChannel]=LightningChannel.get_or_none(LightningChannel.channel_point==channel_point)
            if not channel_object:
                channel_object=LightningChannel(channel_point=channel_point,cooperative_close_requested=datetime.datetime.now())
                channel_object.save(force_insert=True)
            else:
                if not channel_object.cooperative_close_requested: # we only track the FIRST attempt
                    channel_object.cooperative_close_requested=datetime.datetime.now()
                    channel_object.save()
            return True


def get_channel_partners(
    url: str,
    max_retries: int = 5,
    initial_backoff: float = 1.0,
    backoff_multiplier: float = 2.0,
    timeout: int = 10,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[List[Dict[str, str]]]:
    """
    Fetch a JSON file from a URL and return it as a dictionary.
    Implements exponential backoff for failed requests.

    Args:
        url: The URL to fetch the JSON from
        max_retries: Maximum number of retry attempts (default: 5)
        initial_backoff: Initial backoff time in seconds (default: 1.0)
        backoff_multiplier: Multiplier for exponential backoff (default: 2.0)
        timeout: Request timeout in seconds (default: 10)
        headers: Optional headers to include in the request

    Returns:
        Dictionary containing the parsed JSON data

    Raises:
        requests.exceptions.RequestException: If all retry attempts fail
        ValueError: If the response is not valid JSON
    """
    backoff = initial_backoff
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            response = requests.get(url, timeout=timeout, headers=headers)
            response.raise_for_status()  # Raise exception for 4xx/5xx status codes

            # Parse and return JSON
            return response.json()

        except requests.exceptions.RequestException as e:
            last_exception = e

            # Don't retry on client errors (4xx except 429)
            if hasattr(e, "response") and e.response is not None:
                status_code = e.response.status_code
                if 400 <= status_code < 500 and status_code != 429:
                    raise

            # If this was the last attempt, raise the exception
            if attempt == max_retries:
                raise

            # Log retry attempt
            logger.warning(
                f"Request failed (attempt {attempt + 1}/{max_retries + 1}): {e}"
            )
            logger.warning(f"Retrying in {backoff:.2f} seconds...")

            # Wait before retrying
            time.sleep(backoff)

            # Increase backoff for next attempt
            backoff *= backoff_multiplier

        except ValueError as e:
            # JSON parsing error - don't retry
            raise ValueError(f"Invalid JSON response from {url}: {e}")

    # This should never be reached, but just in case
    raise last_exception


def payment_made(payment: dict) -> bool:
    """
    Helper function to determine if a payment has been made or not
    """
    if payment["is_used"]:
        return True
    return False

def is_ln_open_transaction(transaction: Dict[str, Union[str, float]]) -> bool:
    """
    Given transaction from electrum, return True if is a transaction to open a LN channel
    """
    if not transaction["label"]:
        return False
    if "OPEN CHANNEL" in transaction["label"].upper():
        return True
    return False


def is_ln_close_transaction(transaction: Dict[str, Union[str, float]]) -> bool:
    """
    Given transaction from electrum, return True if is a transaction to open a LN channel
    """
    if not transaction["label"]:
        return False
    if "CLOSE CHANNEL" in transaction["label"].upper():
        return True
    return False


async def electrum_rpc(method, myxpub: str, params: Dict[str, str] = None):
    if not params:
        params = {}
    params["xpub"] = myxpub
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 0}
    response = requests.post(
        f"http://localhost:5000", json=payload, auth=("electrum", "electrumz")
    )
    return response.json()


# ----------------------------------------------------------------------------
# LND gRPC bridge — counterpart to electrum_rpc for the BareBits LND fork.
# ----------------------------------------------------------------------------
import base64 as _base64
import codecs as _codecs

import grpc as _grpc
from google.protobuf.json_format import MessageToDict as _MessageToDict
from google.protobuf.json_format import ParseDict as _ParseDict

from lnd_proto import (
    chainnotifier_pb2 as _chainnotifier_pb2,
    chainnotifier_pb2_grpc as _chainnotifier_pb2_grpc,
    invoices_pb2 as _invoices_pb2,
    invoices_pb2_grpc as _invoices_pb2_grpc,
    lightning_pb2 as _lightning_pb2,
    lightning_pb2_grpc as _lightning_pb2_grpc,
    router_pb2 as _router_pb2,
    router_pb2_grpc as _router_pb2_grpc,
    signer_pb2 as _signer_pb2,
    signer_pb2_grpc as _signer_pb2_grpc,
    walletkit_pb2 as _walletkit_pb2,
    walletkit_pb2_grpc as _walletkit_pb2_grpc,
)

# service_name -> (stub_class, pb2_module)
_LND_SERVICES = {
    "Lightning": (_lightning_pb2_grpc.LightningStub, _lightning_pb2),
    "Router": (_router_pb2_grpc.RouterStub, _router_pb2),
    "WalletKit": (_walletkit_pb2_grpc.WalletKitStub, _walletkit_pb2),
    "Invoices": (_invoices_pb2_grpc.InvoicesStub, _invoices_pb2),
    "ChainNotifier": (_chainnotifier_pb2_grpc.ChainNotifierStub, _chainnotifier_pb2),
    "Signer": (_signer_pb2_grpc.SignerStub, _signer_pb2),
}

# wallet_id -> {"channel": grpc.aio.Channel, "stubs": {service_name: stub}}
_LND_CONNECTIONS: Dict[str, Dict[str, Any]] = {}

# Match the daemon's MAX_MSG_SIZE so large responses don't get truncated.
_LND_MAX_MSG_SIZE = 50 * 1024 * 1024


async def _get_lnd_connection(api: BitcartAPI, wallet_id: str) -> Dict[str, Any]:
    """Build (and cache) the gRPC channel + stubs for a wallet."""
    if wallet_id in _LND_CONNECTIONS:
        return _LND_CONNECTIONS[wallet_id]
    info = await api.get_lnd_info(wallet_id)
    if not info:
        raise RuntimeError(f"Could not fetch LND info for wallet {wallet_id}")
    cert = _base64.b64decode(info["tls_cert"])
    macaroon_hex = _codecs.encode(_base64.b64decode(info["macaroon"]), "hex").decode()
    ssl_creds = _grpc.ssl_channel_credentials(root_certificates=cert)

    def _macaroon_callback(_context, callback):
        callback([("macaroon", macaroon_hex)], None)

    creds = _grpc.composite_channel_credentials(
        ssl_creds, _grpc.metadata_call_credentials(_macaroon_callback)
    )
    channel = _grpc.aio.secure_channel(
        f"{info['host']}:{info['grpc_port']}",
        creds,
        options=[
            ("grpc.max_receive_message_length", _LND_MAX_MSG_SIZE),
            ("grpc.max_send_message_length", _LND_MAX_MSG_SIZE),
        ],
    )
    stubs = {name: stub_cls(channel) for name, (stub_cls, _) in _LND_SERVICES.items()}
    conn = {"channel": channel, "stubs": stubs, "info": info}
    _LND_CONNECTIONS[wallet_id] = conn
    return conn


async def lnd_rpc(
    api: BitcartAPI,
    wallet_id: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    service: str = "Lightning",
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """Bare LND gRPC dispatcher — counterpart to electrum_rpc.

    Resolves connection details for the wallet via api.get_lnd_info (cached),
    opens a TLS+macaroon gRPC channel (cached), and dispatches `method` on
    the chosen `service` stub. Request fields come from `params` (dict ->
    proto via ParseDict). Responses are converted back to plain dicts via
    MessageToDict with snake_case field names. Server-streaming responses
    are collected into a list[dict].

    Args:
        api: BitcartAPI instance for the bitcart server hosting this wallet.
        wallet_id: bitcart wallet id.
        method: gRPC method name on `service`, e.g. "ListChannels".
        params: dict of request fields (snake_case, matching the proto).
        service: which gRPC service hosts the method. One of:
                 Lightning, Router, WalletKit, Invoices, ChainNotifier, Signer.
                 Defaults to Lightning.

    Returns:
        dict for unary methods, list[dict] for server-streaming methods.
    """
    if service not in _LND_SERVICES:
        raise ValueError(
            f"Unknown LND gRPC service: {service}. "
            f"One of: {sorted(_LND_SERVICES)}"
        )
    conn = await _get_lnd_connection(api, wallet_id)
    stub = conn["stubs"][service]
    _, pb2_module = _LND_SERVICES[service]
    service_desc = pb2_module.DESCRIPTOR.services_by_name[service]
    if method not in service_desc.methods_by_name:
        raise ValueError(f"Unknown gRPC method: {service}.{method}")
    method_desc = service_desc.methods_by_name[method]
    if method_desc.client_streaming:
        raise NotImplementedError(
            f"{service}.{method} is client-streaming; not supported by lnd_rpc"
        )
    request_cls = getattr(pb2_module, method_desc.input_type.name)
    request = request_cls()
    if params:
        _ParseDict(params, request, ignore_unknown_fields=False)
    rpc_callable = getattr(stub, method)
    call = rpc_callable(request)
    if method_desc.server_streaming:
        return [
            _MessageToDict(msg, preserving_proto_field_name=True)
            async for msg in call
        ]
    return _MessageToDict(await call, preserving_proto_field_name=True)

async def electrum_pay_onchain(xpub:str,dest_addr:str,label:str,amount:float)->bool:
    """
    Send an on-chain payment. AMOUNT IS IN BTC, NOT SATS
    """
    # Make transaction
    pay_response=await electrum_rpc(
        "payto",
        xpub,params={'destination':dest_addr,
                     'amount':str(amount),
                     'feerate':1,
                     }
    )
    if not pay_response['result']:
        logger.warning(f'Error making payment: {pay_response}')
        return False
    transaction=pay_response['result']
    # Broadcast transaction
    broadcast_response = await electrum_rpc(
        "broadcast",
        xpub, params={'tx': transaction}
    )
    if not broadcast_response['result']:
        logger.warning(f'Error making payment broadcast: {broadcast_response}')
        return False
    # set label
    mykey=broadcast_response['result']
    label_response = await electrum_rpc(
        "setlabel",
        xpub,params={'key':mykey,'label':label}
    )
    return True
async def electrum_pay_ln_invoice(xpub:str,invoice:str,label:str)->bool:
    """
    Pay ln invoice, add label, return True if successful, False otherwise
    """
    pay_response=await electrum_rpc(
        "lnpay",
        xpub,params={'invoice':invoice}
    )
    if not pay_response['result']['success']:
        logger.warning(f'Error making payment: {pay_response}')
        return False
    mykey=pay_response['result']['payment_hash']
    label_response = await electrum_rpc(
        "setlabel",
        xpub,params={'key':mykey,'label':label}
    )
    return True

async def new_calc_invoice_stats(api: BitcartAPI) -> Dict[str, StoreStats]:
    """
    Remember all values should have abs() applied so they don't accidentally cancel out
    """
    store_list = await api.get_stores()
    payout_list = await api.get_payouts()
    fee_start_datetime = None
    if FEE_START_DATE:
        format_string = "%Y/%m/%d"
        fee_start_datetime = datetime.datetime.strptime(FEE_START_DATE, format_string)
    auth_store_dict: Dict[str, StoreStats] = {}
    reviewed_wallets = set()
    # Get data from invoices
    sorted_store_list = sorted(
        store_list, key=lambda x: x["created"]
    )  # sort is important to make sure data from any given wallet gets assigned to FIRST relevant store consistently
    for store in sorted_store_list:
        store_id = store["id"]
        auth_store_dict[store_id] = StoreStats(
            store_id=store_id,
            ln_total_revenue_in_sats=0,
            onchain_total_revenue_in_sats=0,
            total_bb_fees_paid_in_sats=0,
            ineligible_revenue_because_of_promo_in_sats=0,
            ineligible_revenue_because_of_topups_in_sats=0,
            ineligible_revenue_because_of_bb_topups_in_sats=0,
            ln_network_fees_paid_for_bb_topup_returns_in_sats=0,
            onchain_network_fees_paid_for_bb_topup_returns_in_sats=0,
            ln_network_fees_paid_for_fee_payments_in_sats=0,
            onchain_network_fees_paid_for_fee_payments_in_sats=0,
            ln_network_fees_paid_for_payouts_in_sats=0,
            onchain_network_fees_paid_for_payouts_in_sats=0,
            ineligible_revenue_because_not_liquidityhelper_wallet_in_sats=0,
            revenue_eligible_for_fee=0,
            ineligible_revenue_because_not_ln_transaction_in_sats=0,
            onchain_network_fees_paid_for_channel_opens_in_sats=0,
            onchain_network_fees_paid_for_channel_closes_in_sats=0,
            misc_ln_network_fees_in_sats=0,
        )
        full_wallet=await api.get_best_ln_wallet_for_store(store)
        wallet_id=full_wallet['id']
        store_stats = auth_store_dict[store_id]

        # Process data from invoices. Invoices must go first because they get us a little database to match onchain and LN payments to
        all_invoices = await api.get_invoices(store_id=store_id)
        for invoice in all_invoices:
            ineligible = False
            field_names = set(f.name for f in dataclasses.fields(BitcartInvoice))
            classified_invoice = BitcartInvoice(
                **{k: v for k, v in invoice.items() if k in field_names}
            )
            try:
                if not classified_invoice.is_paid():
                    continue # TODO make sure this works on multipayment invoices like our topup invoices
                if classified_invoice.is_refunded():
                    continue # we shouldnt have any of these
                for payment in invoice.get("payments", []):
                    if not payment_made(payment):
                        continue
                    if payment["currency"] != "btc":
                        logger.warning(
                            f"Warning: found payment in non-btc currency: {payment}"
                        )
                        continue
                    wallet_id = payment["wallet_id"]
                    if not wallet_id:
                        # sometimes no wallet id if wallet was deleted
                        logger.error(
                            f"Error: no wallet id found for invoice {invoice['id']} payment {payment}"
                        )
                    amount_in_sats = btc_to_sats(abs(float(payment["amount"])))
                    if amount_in_sats==0:
                        continue
                    payment_date = dateutil.parser.parse(payment["created"])
                    if payment["lightning"]:
                        store_stats.ln_total_revenue_in_sats += amount_in_sats
                    else:
                        store_stats.onchain_total_revenue_in_sats += amount_in_sats
                    # figure out if transaction is eligible for fee
                    if classified_invoice.is_self_topup_invoice():
                        store_stats.ineligible_revenue_because_of_topups_in_sats += (
                            amount_in_sats
                        )
                        ineligible = True
                    elif classified_invoice.is_bb_topup_invoice():
                        store_stats.ineligible_revenue_because_of_bb_topups_in_sats += (
                            amount_in_sats
                        )
                        ineligible = True
                    elif full_wallet['name'] != "liquidityhelper":
                        store_stats.ineligible_revenue_because_not_liquidityhelper_wallet_in_sats += (
                            amount_in_sats
                        )
                        ineligible = True
                    elif (
                        payment["lightning"] and not CHARGE_FEE_FOR_ONCHAIN_TRANSACTIONS
                    ):
                        store_stats.ineligible_revenue_because_not_ln_transaction_in_sats += (
                            amount_in_sats
                        )
                        ineligible = True
                    elif fee_start_datetime:
                        if payment_date.date() < fee_start_datetime.date():
                            store_stats.ineligible_revenue_because_of_promo_in_sats += (
                                amount_in_sats
                            )
                            ineligible = True
                    elif FEE_START_REVENUE:
                        if store_stats.calc_total_revenue()<FEE_START_REVENUE:
                            store_stats.ineligible_revenue_because_of_promo_in_sats += (
                                amount_in_sats
                            )
                            ineligible = True
                    if not ineligible:
                        store_stats.revenue_eligible_for_fee += amount_in_sats
            except Exception as e:
                logger.error(f"Error processing invoice in calc_fees: {e}:{invoice}")

        # Process data from wallet
        if wallet_id in reviewed_wallets:
            logger.debug("Skipping already reviewed wallet:, this shouldnt happen unless you have multiple stores sharing the same wallet {}".format(wallet_id))
            continue
        else:
            reviewed_wallets.add(wallet_id)
        # Get onchain history, channel opens/closes
        onchain_history = await electrum_rpc("onchain_history", full_wallet["xpub"])
        for transaction in onchain_history["result"]:
            if is_ln_open_transaction(transaction):
                if transaction.get("incoming"):
                    continue
                store_stats.onchain_network_fees_paid_for_channel_opens_in_sats += (
                    abs(float(transaction["fee_sat"]))
                )
            elif is_ln_close_transaction(transaction):
                store_stats.onchain_network_fees_paid_for_channel_closes_in_sats += abs(float(transaction['fee_sat']))
            elif transaction['incoming']==True:
                continue
            else:
                store_stats.onchain_network_fees_paid_for_channel_opens_in_sats+=abs(float(transaction['fee_sat']))
                logger.warning(f'Unhandled transaction: {transaction}') # TODO figure out how to handle the sweep local anchor transaction, not sure what it does
        # Get LN history + fees
        ln_history = await electrum_rpc("lightning_history", full_wallet["xpub"])
        for transaction in ln_history["result"]:
            if is_ln_open_transaction(transaction):
                continue #these are already counted in on-chain section
            if is_ln_close_transaction(transaction):
                continue  # these are already counted in on-chain section
            if transaction['amount_msat']>0:
                continue # ignore incoming transactions
            if transaction['type']=='payment' and transaction['amount_msat']<0: #outgoing
                if transaction['label'] == CASHOUT_REASON:
                    store_stats.ln_network_fees_paid_for_payouts_in_sats += abs(transaction['amount_msat']/1000)
                    continue
                if transaction['label'] == FEE_PAYOUT_REASON:
                    store_stats.ln_network_fees_paid_for_fee_payments_in_sats += abs(transaction['fee_msat']/1000)
                    store_stats.total_bb_fees_paid_in_sats += abs(transaction['amount_msat']/1000)
                    continue
                else:
                    store_stats.misc_ln_network_fees_in_sats+=abs(transaction['fee_msat']/1000)
                    logger.warning(f'Unhandled tx: {transaction}')
            else:
                logger.warning(f'Unhandled tx: {transaction}')

    return auth_store_dict


# COMMENTED OUT DEC 2025, DELETE IN 1 month if it doesn't break anything
# async def update_ln_payout_fees(api:BitcartAPI):
#     payout_list = await api.get_payouts()
#     for payout in payout_list:
#         if payout['currency']!='BTC':
#             continue
#         if payout['status'].lower()!='complete':
#             continue
#         if payout['metadata'].get('lnfees'):
#             continue
#         store_id=payout['store_id']
#         if store_id not in auth_store_dict:
#             logger.warning(f'in new_calc_invoice: store_id not in auth_store_dict {store_id}, skipping...')
#             continue
#         store_stats=auth_store_dict[store_id]
#         if 'payment_hash' in payout.get('metadata',{}):
#             # Is "fake" LN payout, OLDTODO add any ln fees found in the payout. We currently don't add this information in
#             amount_in_sats = btc_to_sats(float(payout['amount']))
#             network_fees_ln = btc_to_sats(float(payout['metadata'].get('lnfees',0)))
#         else:
#             amount_in_sats=btc_to_sats(float(payout['amount']))
#             #OLDTODO not sure how this looks on a real payout, update the below two lines when we have one
#             pass
#             #network_fees_chain+=btc_to_sats(float(payout['used_fee']))
#
#         if payout.get('metadata',{}).get('reason','')==FEE_PAYOUT_REASON:
#             pass
#             # OLDTODO re-enable etc store_dict['bb_fees_paid_in_sats'] += amount_in_sats_ln
#             continue
# COMMENTED OUT DEC 2025 DELETE IN a few months if nothing broke
# async def calc_invoice_stats(api:BitcartAPI)->Dict[str,InvoiceStats]:
#     store_list=await api.get_stores()
#     return_dict={}
#     fee_start_datetime = None
#     all_existing_payouts=await api.get_payouts()
#     fees_paid_dict=count_all_fees_paid_from_payouts(all_existing_payouts)
#     if FEE_START_DATE:
#         format_string = "%Y/%m/%d"
#         fee_start_datetime = datetime.datetime.strptime(FEE_START_DATE, format_string)
#     for store in store_list:
#         total_revenue_in_sats = 0
#         ineligible_revenue_promo=0 # amount of fee skipped due to promo period
#         topup_revenue = 0  # amount of fee skipped due to topups
#         bb_topup_revenue = 0  # amount of fee skipped due to topups
#         network_fees_in_sats=0
#         payoutinfo = fees_paid_dict.get(store['id'])
#
#
#         store_wallets={}
#         # find all wallets
#         for wallet_id in store['wallets']:
#             full_wallet=await api.get_wallet(wallet_id)
#             full_wallet_name=full_wallet['name']
#             store_wallets[wallet_id]=full_wallet_name
#         all_invoices = await api.get_invoices(store_id=store)
#         for invoice in all_invoices['result']:
#             try:
#                 if not invoice['paid_date']:
#                     continue
#                 if invoice['refund_id']:
#                     continue
#                 for payment in invoice.get('payments',[]):
#                     if payment['status'] == 'pending':
#                         continue
#                     if payment['currency']!='btc':
#                         logger.warning(f'Warning: found payment in non-btc currency: {payment}')
#                         continue
#                     wallet_id=payment['wallet_id']
#                     if not wallet_id:
#                         # sometimes no wallet id if wallet was deleted
#                         logger.error(f"Error: no wallet id found for invoice {invoice['id']} payment {payment}")
#                     if wallet_id not in store_wallets:
#                         # This can happen if a wallet was deleted, but is still associated with a past tx
#                         logger.error(f"Warning in calc_fees: wallet id {wallet_id} not found in store wallets: {store_wallets}")
#                     if not payment_made(payment):
#                         continue
#                     wallet_name = store_wallets.get(wallet_id, 'UNKNOWN')
#                     amount_in_sats= btc_to_sats(float(payment['amount']))
#                     payment_date=dateutil.parser.parse(payment['created'])
#                     if wallet_name == 'liquidityhelper' or ALL_TRANSACTIONS_ELIGIBLE_FOR_FEE:
#                         total_revenue_in_sats+=amount_in_sats
#                         if is_topup_invoice(invoice):
#                             topup_revenue+=amount_in_sats
#                         if is_bb_topup_invoice(invoice):
#                             bb_topup_revenue+=amount_in_sats
#                         if fee_start_datetime:
#                             if payment_date.date() < fee_start_datetime.date():
#                                 ineligible_revenue_promo+=amount_in_sats
#             except Exception as e:
#                 logger.error(f"Error processing invoice in calc_fees: {e}:{invoice}")
#         if store['id'] in fees_paid_dict:
#             total_fees_paid_in_sats = payoutinfo.total_paid_in_sats_ln + payoutinfo.total_paid_in_sats_onchain
#             network_fees_in_sats += payoutinfo.total_network_fees_paid_ln + payoutinfo.total_network_fees_paid_onchain
#         else:
#             total_fees_paid_in_sats =0
#             network_fees_in_sats=0
#         my_calc_fees=InvoiceStats(store['id'],
#                                   total_revenue_in_sats,
#                                   total_fees_paid_in_sats=total_fees_paid_in_sats,
#                                   ineligible_revenue_because_of_promo=ineligible_revenue_promo,
#                                   ineligible_revenue_because_of_topups=topup_revenue,
#                                   ineligible_revenue_because_of_bb_topups=bb_topup_revenue,
#                                   network_fees_paid_in_payouts=network_fees_in_sats,
#                                   )
#         return_dict[store['id']]=my_calc_fees
#     return return_dict

def max_channel_size_from_sats(sats_we_have:int)->int:
    """"
    Given an amount of sats, find the biggest channel we can open, starting with the total number of sats and working our way down to 1
    """
    for i in reversed(range(1,sats_we_have)):
        own_reserve = min(i * 0.015, 500)

        dust_limit_sat = 20001  # don't know what this actually is
        electrum_reserve = max(i // 100, dust_limit_sat)

        reserve = max(own_reserve, electrum_reserve)
        final_cost=i + reserve
        if final_cost<=sats_we_have:
            return i
    return 0
def channel_size_from_intended_sats(intended_sats: int) -> int:
    """
    Given an intended amount of liquidity, return the amount of sats it will take to open.
    If you instead have an amount of sats you have and want to know the highest channnel you can open
    with it, use max_channel_size_from_sats
    """
    own_reserve = min(intended_sats * 0.015, 500)

    dust_limit_sat=20001 # don't know what this actually is
    electrum_reserve=max(intended_sats // 100, dust_limit_sat)

    reserve=max(own_reserve,electrum_reserve)
    return int(intended_sats + reserve)


def remove_existing_channel_partners(
    partner_list: List[str], current_channels: List[dict]
) -> List[str]:
    """ "
    Given list of current channels, remove existing partners from partner_list
    """
    found_channel_pubkeys = set()
    for channel in current_channels:
        if channel["state"] != "REDEEMED":
            found_channel_pubkeys.add(channel["remote_pubkey"].lower())
    return_list = deepcopy(partner_list)
    for partner in partner_list:
        for found_channel in found_channel_pubkeys:
            if found_channel in partner:
                return_list.remove(partner)
    return return_list


async def pick_best_channel_partners(ln_cashout_address: Optional[str] = None) -> List[str]:
    """
    Pick best channel partners to try.
    """
    coinos_uri = "021294fff596e497ad2902cd5f19673e9020953d90625d68c22e91b51a45c032d3@51.79.52.200:9736"
    strike_uri = "03c8e5f583585cac1de2b7503a6ccd3c12ba477cfd139cd4905be504c2f48e86bd@34.73.189.183:9735"
    boltz_node = "026165850492521f4ac8abd9bd8088123446d126f648ca35e60f88177dc149ceb2@143.202.162.204:9735"
    return_list = []
    partner_list = []
    try:
        bb_partners = await run_every_x_seconds(
            my_func=get_channel_partners,
            seconds=1,
            url="https://getbarebits.com/default_channel_partners.json",
        )
    except Exception as e:
        logger.error(f"Error fetching channel partners from bb: {e}")
    else:
        # add found nodes to database if we don't already have them
        for found_partner in bb_partners:
            try:
                pubkey = found_partner["node_address"]
                existing_db_object = LightningNode.get_or_none(
                    LightningNode.node_address == pubkey
                )
                try:
                    new_object = node_database.dict_to_node(found_partner)
                except Exception as e:
                    logger.error(f"Error turning dict tonode: {e}:{found_partner}")
                    continue

                if existing_db_object:
                    node_db_update.update_existing_lightning_node(
                        existing_db_object, new_object
                    )
                else:
                    logger.debug("Adding new node from json, not in existing DB")
                    new_object.save(force_insert=True)
            except Exception as e:
                logger.error(
                    f"Error processing partner in partner list: {e}:{found_partner}"
                )

    # add known nodes for strike, coinos, if not blacklisted
    if ln_cashout_address:
        if "strike.me" in ln_cashout_address:
            strike_db_object = LightningNode.get_or_none(
                LightningNode.node_address == strike_uri.split("@")[0]
            )
            if strike_db_object:
                blacklist_response, reason = is_node_blacklisted(strike_db_object)
                if not blacklist_response:
                    return_list.insert(0, strike_uri)
        elif "coin.os" in ln_cashout_address:
            coinos_db_object = LightningNode.get_or_none(
                LightningNode.node_address == coinos_uri.split("@")[0]
            )
            if coinos_db_object:
                blacklist_response, reason = is_node_blacklisted(coinos_db_object)
                if not blacklist_response:
                    return_list.insert(0, coinos_uri)

    # pull nodes from node database
    ln_node_list: List[LightningNode] = LightningNode.select().order_by(
        LightningNode.oldest_known_date
    )
    for node in ln_node_list:
        blacklisted, reason = is_node_blacklisted(node)
        if not blacklisted:
            uri = node.get_ipv4_uri()
            if uri:
                return_list.append(uri)

    return return_list


async def move_onchain_to_ln(
    wallet_id: str, amount_in_btc: float, api: BitcartAPI
) -> bool:
    """
    Open channels.
    pubkey: open channel to specified node
    Returns True if successful, false otherwise
    """
    channel_partners = await pick_best_channel_partners(CASHOUT_LIGHTNING_ADDRESS)
    current_channels = await api.get_wallet_ln_channels(wallet_id)
    # don't open more than one channel to existing partners
    paired_list = remove_existing_channel_partners(channel_partners, current_channels)
    # keep trying until we successfully open a channel with somebody
    if len(paired_list) == 0:
        logger.error(
            "In move_onchain_to_ln No potential partners found for creating channels."
        )
        return False
    for partner in paired_list:
        try:
            partner_pubkey = partner.lower().split("@")[0]
        except Exception as e:
            logger.error(f"Error getting pubkey from partner: {e}:{partner}")
            continue
        if DRY_RUN_FUNDS:
            logger.info(
                f"DRY RUN: Skipping LN channel open {amount_in_btc}BTC to {partner} from wallet {wallet_id}"
            )
        else:
            ln_node: Optional[LightningNode] = LightningNode.get_or_none(
                LightningNode.node_address == partner_pubkey
            )
            if not ln_node:
                logger.warning(
                    "In move_onchain_to_ln, no ln_node found"
                )  # TODO this shouldn't be happening
                continue
            blacklist_result, blacklist_reason = is_node_blacklisted(ln_node)
            if blacklist_reason == "REMOTE_CLOSE_COUNT":
                continue
            if not ln_node.ipv4_address and ln_node.magma_queries >= 2:
                continue
            if ln_node.needs_magma_update(30):
                node_db_update.update_node(ln_node)
                ln_node: Optional[LightningNode] = LightningNode.get_or_none(
                    LightningNode.node_address == partner_pubkey
                )
            blacklist_result, blacklist_reason = is_node_blacklisted(ln_node)
            if blacklist_result:
                logger.debug(
                    f"In move_onchain_to_ln after magma fetch, node is {ln_node.node_address} blacklisted for reason {blacklist_reason}"
                )
                continue
            logger.info(f"Attempting channel open to {partner} w {amount_in_btc} BTC")
            move_response = await api.open_ln_channel(wallet_id, partner, amount_in_btc)
            if move_response:  # channel opened successfully
                logger.info(f"New channel opened: {move_response}")
                return True
    return False


async def notused_wallet_has_channel_open_to_megalithic(
    wallet_id: str, api: BitcartAPI
) -> bool:
    """
    Returns True if a channel is open to Zeus, false otherwise
    """
    if LSP_DEV_MODE:
        pubkey = megalithic.MUTINY_PUBKEY
    else:
        pubkey = megalithic.MAINNET_PUBKEY
    all_channels = await api.get_wallet_ln_channels(wallet_id)
    pubkey = zeus.pubkey_from_uri(pubkey)
    for channel in all_channels:
        if channel["type"] == "BACKUP":
            continue
        if channel["remote_pubkey"] == pubkey:
            return True
    return False


async def notused_wallet_has_channel_open_to_zeus(
    wallet_id: str, api: BitcartAPI
) -> bool:
    """
    Returns True if a channel is open to Zeus, false otherwise
    """
    if LSP_DEV_MODE:
        pubkey = zeus.TESTNET_PUBKEY
    else:
        pubkey = zeus.MAINNET_PUBKEY
    all_channels = await api.get_wallet_ln_channels(wallet_id)
    pubkey = zeus.pubkey_from_uri(pubkey)
    for channel in all_channels:
        if channel["remote_pubkey"] == pubkey:
            return True
    return False


def is_valid_zeus_info_response(info: Dict[str, Union[str, int, float]]) -> bool:
    """
    Returns True if valid
    """
    for key in [
        "min_channel_balance_sat",
        "max_channel_balance_sat",
        "min_initial_lsp_balance_sat",
        "max_initial_lsp_balance_sat",
        "min_initial_client_balance_sat",
        "max_initial_client_balance_sat",
        "max_channel_expiry_blocks",
        "min_funding_confirms_within_blocks",
        "min_required_channel_confirmations",
    ]:
        if key not in info:
            return False
    return True


async def attempt_create_channels(
    wallet_id: str,
    api: BitcartAPI,
    target_channel_sizes: List[int],
) -> bool:
    """
    Function to create new channels. Returns True if ANY channels
    created successfully
    """
    return_value = False
    for i in target_channel_sizes:
        if DRY_RUN_FUNDS:
            logger.info(f"DRY RUN: Would have opened channel w {i} sats")
            continue
        result = await move_onchain_to_ln(wallet_id, sats_to_btc(i), api)
        if result:
            return_value = True
    return return_value


async def notused_attempt_request_liquidity(
    store_id: str, wallet_id: str, api: BitcartAPI, force_ln_node: Optional[str] = None
) -> None:
    """
    See https://github.com/spesmilo/electrum/issues/10221
    Option A: User is able to use LSPS7 (this is not possible in electrum right now)
    1. User opens channel A to zeus.
    2. User uses LSPS7 to create an order to "renew" the lease on channel A. This order/invoice can then be paid w on-chain or LN funds, including funds already in channel A.

    Option B: User is unable to use LSPS7 (this is how this function works)
    1. User opens channel A to zeus
    2. User requests channel B order via /api/v1/create_order.
    3. User pays invoice from order. Payment for this order MUST be made via lightning (channel A). If we don't have enough in channel A, make a new channel, wait for confirmation, then pay
    4. Channel B gets opened from zeus to User. Channel A still exists

    force_ln_node: force opening a channel to specified LN node like 02ca1ebab4d1003b30d989047252535660821d3dd14cc3c322b56f9a86c7818604@mynode.com:9735
    """
    # make sure a pending order doesn't exist
    cutoff_date = datetime.datetime.now() - datetime.timedelta(days=1)
    old_orders = LOrder.select().where(LOrder.date > cutoff_date)
    old_order_list = None
    if old_orders:
        old_order_list = list(old_orders)
    if old_order_list:
        if len(old_order_list) > 0:
            logger.info(
                f"Skipping liquidity request because one is already pending: {old_orders}"
            )
            return
    # figure out available on-chain funds
    my_pubkey = await api.get_wallet_ln_node_id(wallet_id)
    if FORCE_EXTERNAL_IP_AND_PORT_LN and not force_ln_node:
        my_pubkey = my_pubkey.split("@")[0] + "@" + FORCE_EXTERNAL_IP_AND_PORT_LN
    if force_ln_node:
        my_pubkey = force_ln_node
    full_wallet = await api.get_wallet(wallet_id)
    total_onchain_funds_in_sats = btc_to_sats(float(full_wallet["balance"]))
    total_outbound_ln_in_sats = await api.get_outbound_liquidity(wallet_id)
    # open initial channel to zeus
    zeus_channel_response = await notused_wallet_has_channel_open_to_zeus(
        wallet_id, api
    )
    if not zeus_channel_response:
        logger.info(
            "Skipping liquidity request bc no channel open to Zeus. Opening one now..."
        )
        target = max(MIN_ONCHAIN_TO_LN_MOVEMENT, INITIAL_CHANNEL_SIZE)
        actual_request_size = channel_size_from_intended_sats(target)
        if total_onchain_funds_in_sats < actual_request_size:
            logger.info(
                "Unable to open initial channel to zeus bc total_onchain_funds_in_sats<actual_request_size"
            )
            return
        else:
            logger.info("Creating first initial channel to zeus")
            await move_onchain_to_ln(wallet_id, sats_to_btc(actual_request_size), api)
            return

    # Initialize client
    if LSP_DEV_MODE:
        client = ZeusLSPS1Client(network=zeus.Network.TESTNET)
        cache_field_name = "ZEUSCACHE_GETINFO-TESTNET"
    else:
        client = ZeusLSPS1Client(network=zeus.Network.MAINNET)
        cache_field_name = "ZEUSCACHE_GETINFO-MAINNET"
    # Get service information from zeus or cache
    info = None
    try:
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=7)
        found_cache_field = SimpleCacheField.get(
            (SimpleCacheField.name == cache_field_name)
            & (SimpleCacheField.date > cutoff_date)
        )
        info = json.loads(found_cache_field.content)
    except DoesNotExist:
        logger.error("Getting service info...")
        info = client.get_info()
        if not is_valid_zeus_info_response(info):
            logger.error(
                "Received invalid info response from zeus, not proceeding w liquidity request"
            )
            return
        new_cache_field = SimpleCacheField(name=cache_field_name)
        new_cache_field.date = datetime.datetime.now()
        new_cache_field.expiry_in_seconds = 86400
        new_cache_field.content = json.dumps(info)
        new_cache_field.save()

    logger.debug(
        f"LSP supports channels from {info['min_channel_balance_sat']} to {info['max_channel_balance_sat']} sats"
    )

    # pick target liquidity that is < maximum offered by LSP, > minimum amount offered by LSP
    target_liquidity_request_amount = min(
        TARGET_INBOUND_LIQUIDITY, int(info["max_channel_balance_sat"])
    )
    target_liquidity_request_amount = max(
        target_liquidity_request_amount,
        int(info["min_initial_lsp_balance_sat"]),
        int(info["min_channel_balance_sat"]),
    )

    # create orders in descending amounts until we find one we can afford
    current_order_target_in_sats = target_liquidity_request_amount
    minimum_order = max(
        MIN_INBOUND_LIQUIDITY_PER_CHANNEL,
        MIN_INBOUND_LIQUIDITY_REQUEST_AMOUNT,
        int(info["min_channel_balance_sat"]),
    )
    while current_order_target_in_sats > minimum_order:
        # Create a channel order
        try:
            order_info = client.create_order(
                public_key=my_pubkey,
                lsp_balance_sat=current_order_target_in_sats,
                client_balance_sat=0,
                channel_expiry_blocks=min(
                    int(info["max_channel_expiry_blocks"]), 52560
                ),
            )
        except Exception as e:
            logger.error(f"Error creating order w Zeus: {e}")
            return
        ln_invoice = order_info["payment"]["bolt11"]["invoice"]
        price = int(order_info["payment"]["bolt11"]["order_total_sat"])
        if not await notused_wallet_has_channel_open_to_zeus(wallet_id, api):
            logger.info(
                "Skipping liquidity request bc no channel open to Zeus. Opening one now..."
            )
            actual_request_size = channel_size_from_intended_sats(price)
            await move_onchain_to_ln(wallet_id, sats_to_btc(actual_request_size), api)
            return
        if price > total_outbound_ln_in_sats:
            remainder = max(
                price - total_outbound_ln_in_sats, MIN_ONCHAIN_TO_LN_MOVEMENT
            )
            if total_onchain_funds_in_sats > remainder:
                logger.info(
                    f"Creating new channel to increase amount of outbound liquidity to buy inbound liquidity. Total onchain was: {total_onchain_funds_in_sats}, total outbound ln was {total_outbound_ln_in_sats}, price was {price}"
                )
                intended_sats = int(remainder)
                request_amount = sats_to_btc(
                    channel_size_from_intended_sats(intended_sats)
                )
                if btc_to_sats(request_amount) < total_onchain_funds_in_sats:
                    onchain_result = await move_onchain_to_ln(
                        wallet_id, request_amount, api
                    )
                    return
            logger.debug(
                f"Attempted to buy {current_order_target_in_sats} of liquidity but it costs {price} (more than we have which is {total_outbound_ln_in_sats}), trying lower amount..."
            )
            current_order_target_in_sats -= 1000
            continue
        payment_result = None
        if not isinstance(payment_result, dict):
            logger.error(
                f"Error paying ln invoice in attempt_request_liquidity: {payment_result}"
            )
            return
        if not payment_result:
            logger.error(
                f"Error paying ln invoice in attempt_request_liquidity, no payment result: {payment_result}"
            )
            return
        if payment_result.get("success", False) != "True":
            logger.error(
                f"Error paying ln invoice in attempt_request_liquidity, success!=True: {payment_result}"
            )
            return
        database.create_order(order_info["order_id"])
        logger.info(f"Paid liquidity order {order_info['order_id']}")


async def notused_attempt_request_megalithic(
    store_id: str, wallet_id: str, api: BitcartAPI, force_ln_node: Optional[str] = None
) -> None:
    """
    See https://github.com/spesmilo/electrum/issues/10221
    Option A: User is able to use LSPS7 (this is not possible in electrum right now)
    1. User opens channel A to zeus.
    2. User uses LSPS7 to create an order to "renew" the lease on channel A. This order/invoice can then be paid w on-chain or LN funds, including funds already in channel A.

    Option B: User is unable to use LSPS7 (this is how this function works)
    1. User opens channel A to zeus
    2. User requests channel B order via /api/v1/create_order.
    3. User pays invoice from order. Payment for this order MUST be made via lightning (channel A). If we don't have enough in channel A, make a new channel, wait for confirmation, then pay
    4. Channel B gets opened from zeus to User. Channel A still exists

    force_ln_node: force opening a channel to specified LN node instead of bitcart like 02ca1ebab4d1003b30d989047252535660821d3dd14cc3c322b56f9a86c7818604@mynode.com:9735
    """
    # make sure a pending order doesn't exist
    cutoff_date = datetime.datetime.now() - datetime.timedelta(days=1)
    old_orders = LOrder.select().where(LOrder.date > cutoff_date)
    old_order_list = None
    if old_orders:
        old_order_list = list(old_orders)
    if old_order_list:
        if len(old_order_list) > 0:
            logger.info(
                f"Skipping liquidity request because one is already pending: {old_orders}"
            )
            return

    # figure out available on-chain funds
    my_pubkey = await api.get_wallet_ln_node_id(wallet_id)
    if FORCE_EXTERNAL_IP_AND_PORT_LN and not force_ln_node:
        my_pubkey = my_pubkey.split("@")[0] + "@" + FORCE_EXTERNAL_IP_AND_PORT_LN
    if force_ln_node:
        my_pubkey = force_ln_node
    full_wallet = await api.get_wallet(wallet_id)
    total_onchain_funds_in_sats = btc_to_sats(float(full_wallet["balance"]))
    total_outbound_ln_in_sats = await api.get_outbound_liquidity(wallet_id)

    # open initial channel to megalithic
    zeus_channel_response = await notused_wallet_has_channel_open_to_megalithic(
        wallet_id, api
    )
    if (
        not zeus_channel_response and False
    ):  # disabled for now since we're using add_peer instead
        logger.info(
            "Skipping liquidity request bc no channel open to Zeus. Opening one now..."
        )
        target = max(MIN_ONCHAIN_TO_LN_MOVEMENT, INITIAL_CHANNEL_SIZE)
        actual_request_size = channel_size_from_intended_sats(target)
        if total_onchain_funds_in_sats < actual_request_size:
            logger.info(
                "Unable to open initial channel to zeus bc total_onchain_funds_in_sats<actual_request_size"
            )
            return
        else:
            logger.info("Creating first initial channel to zeus")
            if LSP_DEV_MODE:
                await move_onchain_to_ln(
                    wallet_id, sats_to_btc(actual_request_size), api
                )
            else:
                await move_onchain_to_ln(
                    wallet_id, sats_to_btc(actual_request_size), api
                )
            return

    # Initialize client
    if LSP_DEV_MODE:
        client = MegalithicLSPClient(network=megalithic.Network.MUTINYNET)
        cache_field_name = "MEGALITHIC_GETINFO-TESTNET"
    else:
        client = MegalithicLSPClient(network=megalithic.Network.MAINNET)
        cache_field_name = "MEGALITHIC_GETINFO-MAINNET"
    # Get service information from zeus or cache
    info = None
    try:
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=7)
        found_cache_field = SimpleCacheField.get(
            (SimpleCacheField.name == cache_field_name)
            & (SimpleCacheField.date > cutoff_date)
        )
        info = json.loads(found_cache_field.content)
    except DoesNotExist:
        logger.error("Getting service info...")
        info = client.get_info()
        if not is_valid_zeus_info_response(info):
            logger.error(
                "Received invalid info response from meg, not proceeding w liquidity request"
            )
            return
        new_cache_field = SimpleCacheField(name=cache_field_name)
        new_cache_field.date = datetime.datetime.now()
        new_cache_field.expiry_in_seconds = 86400
        new_cache_field.content = json.dumps(info)
        new_cache_field.save()

    logger.debug(
        f"LSP supports channels from {info['min_channel_balance_sat']} to {info['max_channel_balance_sat']} sats"
    )

    # pick target liquidity that is < maximum offered by LSP, > minimum amount offered by LSP
    target_liquidity_request_amount = min(
        TARGET_INBOUND_LIQUIDITY, int(info["max_channel_balance_sat"])
    )
    target_liquidity_request_amount = max(
        target_liquidity_request_amount,
        int(info["min_initial_lsp_balance_sat"]),
        int(info["min_channel_balance_sat"]),
    )

    # create orders in descending amounts until we find one we can afford
    current_order_target_in_sats = target_liquidity_request_amount
    minimum_order = max(
        MIN_INBOUND_LIQUIDITY_PER_CHANNEL,
        MIN_INBOUND_LIQUIDITY_REQUEST_AMOUNT,
        int(info["min_channel_balance_sat"]),
    )
    while current_order_target_in_sats > minimum_order:
        # Create a channel order
        try:
            order_info = client.create_order(
                public_key=my_pubkey,
                lsp_balance_sat=current_order_target_in_sats,
                client_balance_sat=0,
                channel_expiry_blocks=min(
                    int(info["max_channel_expiry_blocks"]), 52560
                ),
            )
        except Exception as e:
            logger.error(f"Error creating order w Megalithic: {e}")
            return
        ln_invoice = order_info["payment"]["bolt11"]["invoice"]
        price = int(order_info["payment"]["bolt11"]["order_total_sat"])
        zeus_channel_response = await notused_wallet_has_channel_open_to_megalithic(
            wallet_id, api
        )
        if not zeus_channel_response:
            logger.info(
                "Skipping liquidity request bc no channel open to mega. Opening one now..."
            )
            actual_request_size = channel_size_from_intended_sats(price)
            if LSP_DEV_MODE:
                await move_onchain_to_ln(
                    wallet_id, sats_to_btc(actual_request_size), api
                )
            else:
                await move_onchain_to_ln(
                    wallet_id, sats_to_btc(actual_request_size), api
                )
            return
        if price > total_outbound_ln_in_sats:
            remainder = max(
                price - total_outbound_ln_in_sats, MIN_ONCHAIN_TO_LN_MOVEMENT
            )
            if total_onchain_funds_in_sats > remainder:
                logger.info(
                    f"Creating new channel to increase amount of outbound liquidity to buy inbound liquidity. Total onchain was: {total_onchain_funds_in_sats}, total outbound ln was {total_outbound_ln_in_sats}, price was {price}"
                )
                intended_sats = int(remainder)
                request_amount = sats_to_btc(
                    channel_size_from_intended_sats(intended_sats)
                )
                if btc_to_sats(request_amount) < total_onchain_funds_in_sats:
                    if LSP_DEV_MODE:
                        onchain_result = await move_onchain_to_ln(
                            wallet_id, request_amount, api
                        )
                    else:
                        onchain_result = await move_onchain_to_ln(
                            wallet_id, request_amount, api
                        )
                    return
            logger.debug(
                f"Attempted to buy {current_order_target_in_sats} of liquidity but it costs {price} (more than we have which is {total_outbound_ln_in_sats}), trying lower amount..."
            )
            current_order_target_in_sats -= 1000
            continue
        payment_result = None
        if not isinstance(payment_result, dict):
            logger.error(
                f"Error paying ln invoice in attempt_request_liquidity: {payment_result}"
            )
            return
        if payment_result.get("success", False) != "True":
            logger.error(
                f"Error paying ln invoice in attempt_request_liquidity, success!=True: {payment_result}"
            )
            return
        database.create_order(order_info["order_id"])
        logger.info(f"Paid liquidity order {order_info['order_id']}")


async def our_wallet_exists(api: BitcartAPI, store: dict) -> Optional[dict]:
    """
    Get liquidityhelper wallet for store, return None if not found
    """
    found_wallet = None
    try:
        found_wallet = await api.get_best_ln_wallet_for_store(store)
    except Exception as e:
        logger.error(f"Error in our_wallet_exists: {e}:{found_wallet}")
        return None
    else:
        return found_wallet


async def first_wallet_check_create(api: BitcartAPI) -> bool:
    """
    Check for first wallet, create it if it doesn't exist. Return True if went well, None if error
    """
    wallet_list = await api.get_wallets()
    if wallet_list:
        if len(wallet_list) > 0:
            return True
    logger.info("No wallets found, creating first wallet..")
    mywallet_seed_response = await api.create_wallet_seed()
    if not isinstance(mywallet_seed_response, dict):
        logger.error(f"Err making wallet seed response: {mywallet_seed_response}")
        return False
    if "seed" not in mywallet_seed_response:
        logger.error(f"2Err making wallet seed response: {mywallet_seed_response}")
        return False
    mywallet_seed = mywallet_seed_response["seed"]
    if isinstance(mywallet_seed, str):
        print("=================================")
        print(
            "A new wallet has been created, your seed phrase is below. Store this seed phrase somewhere securely"
        )
        print(
            "If you lose your seed phrase, you will lose access to any funds stored in Bitcart!"
        )
        print(mywallet_seed)
    else:
        logger.error("Err generating wallet seed, will try again later")
        return False
    mywallet = await api.create_wallet(seed=mywallet_seed)
    if not mywallet:
        logger.error("Err generating wallet, will try again later")
        return False
    return True


def should_close_channel(
    failed_checks: int,
    total_checks: int,
    last_online: datetime.datetime,
    check_interval_in_seconds: int,
) -> Tuple[str, bool]:
    if failed_checks < 5:
        return "", False
    check_period_duration = check_interval_in_seconds * total_checks
    failed_check_duration = check_interval_in_seconds * failed_checks
    failed_check_ratio = failed_checks / total_checks
    # monitoring less than one hour
    if check_period_duration < 3600:
        return "", False
    # down longer than 5 hours per month over a > 2 month period
    if failed_check_duration / (86400 * 30) > 18000 and check_period_duration > (
        86400 * 60
    ):
        return "LONG_TERM_UNRELIABLE", True
    # down > 10% of the time over a > 1 week period
    if failed_check_ratio > 0.10 and check_period_duration > (86400 * 7):
        return "SHORT_TERM_UNRELIABLE", True
    # dropped offline and has been down for 48 hours
    hours_ago_48 = datetime.timedelta(hours=-48)
    if last_online < datetime.datetime.now() + hours_ago_48:
        return "OFFLINE_RECENTLY", True
    return "", False


async def attempt_cooperative_close(channel_point: str, xpub: str) -> Optional[dict]:
    close_result = await electrum_rpc(
        "close_channel", myxpub=xpub, params={"channel_point": channel_point}
    )
    return close_result


async def wallet_creation(
    api: BitcartAPI,
) -> Optional[bool]:
    store_list=await api.get_stores()
    for store in store_list:
        store_id = store["id"]
        mywallet = None
        # get our best LN wallet
        found_wallet = await our_wallet_exists(api, store)
        if found_wallet:
            # print(f"best wallet info id: {found_wallet['id']} balance: {found_wallet['balance']}")
            continue

        logger.info(
            f"No liquidity helper wallet found for store : {store['name']}, creating.."
        )
        mywallet_seed_response = await api.create_wallet_seed()
        if not isinstance(mywallet_seed_response, dict):
            logger.error(
                f"Err making wallet seed response: {mywallet_seed_response}"
            )
            continue
        if "seed" not in mywallet_seed_response:
            logger.error(
                f"2Err making wallet seed response: {mywallet_seed_response}"
            )
            continue
        mywallet_seed = mywallet_seed_response["seed"]
        if isinstance(mywallet_seed, str):
            print("=================================")
            print(
                "A new wallet has been created, your seed phrase is below. Store this seed phrase somewhere securely"
            )
            print(
                "If you lose your seed phrase, you will lose access to any funds stored in Bitcart!"
            )
            print(mywallet_seed)
        else:
            logger.error("Err generating wallet seed, will try again later")
            continue
        mywallet = await api.create_wallet(seed=mywallet_seed)
        if not mywallet:
            logger.error("Err generating wallet, will try again later")
            continue
        # add wallet to store
        new_wallet_list = store["wallets"]
        new_wallet_list.append(mywallet["id"])
        await api.add_wallet_to_store(new_wallet_list, store_id)
    return True


async def find_channel_closings(xpub: str) -> Dict[str, int]:
    result = await electrum_rpc("list_channels", xpub)
    channel_closings = {}
    for channel in result["result"]:  # DEBUG: VERIFY USAGE OF RESULT
        state = channel["state"]
        pubkey = channel["remote_pubkey"].lower()
        if state == "REDEEMED":
            if pubkey in channel_closings:
                channel_closings[pubkey] += 1
            else:
                channel_closings[pubkey] = 1
    return channel_closings
async def store_needs_liquidity(store_id:str,api:BitcartAPI,min_sats_liquidity:int=MIN_INBOUND_LIQUIDITY,min_channel_count:int=MIN_CHANNEL_COUNT,assume_zero:bool=False)->Optional[Tuple[int,int]]:
    """
    Checks wallet for a store, returns None if store does not need liquidity, otherwise returns amount needed in sats, followed by the # of channels that need to be created
    Assumes any balance in LN is "inbound" since it will be converted to inbound next time cashout is run

    min_channel_count: minimum number of channels this store should have
    min_sats_liquidity: minimum amount of liquidity we want this store to have
    assume_zero: if true, assume we have ZERO onchain funds, ZERO inbound liquidity, and ZERO channels. this is used to caculate topup amount/reserve amount
    """
    full_store=await api.get_store_by_id(store_id)
    found_inbound_liquidity=0
    found_channels=0
    if assume_zero:
        found_channels=0
    else:
        best_wallet = await api.get_best_ln_wallet_for_store(full_store)
        wallet_id = best_wallet['id']
        open_channels=await api.get_wallet_ln_channels(wallet_id,active_only=True,online_only=True)
        if open_channels:
            found_channels+=len(open_channels)
        for channel in open_channels:
            found_inbound_liquidity+=float(channel['remote_balance'])
            found_inbound_liquidity += float(channel['local_balance'])
    if found_inbound_liquidity>min_sats_liquidity and found_channels>min_channel_count and not assume_zero:
        return None
    liquidity_needed=max(min_sats_liquidity-found_inbound_liquidity,0)
    channels_needed=max(min_channel_count-found_channels,0)
    # If this would result in channels that are too small, increase the amount of liquidity needed
    while min(common_functions.distribute_sats_over_channels(liquidity_needed,channels_needed))<MIN_CHANNEL_SIZE_IN_SATS:
        liquidity_needed+=1
    return liquidity_needed,channels_needed

async def update_channel_closings(api:BitcartAPI) -> None:
    store_list=await api.get_stores()
    for store in store_list:
        best_wallet=await api.get_best_ln_wallet_for_store(store)
        closing_stats=await find_channel_closings(best_wallet['xpub'])
        for pubkey, count in closing_stats.items():
            found_node: LightningNode = LightningNode.get_or_none(
                LightningNode.node_address == pubkey.lower()
            )
            if not found_node:
                found_node=LightningNode(node_address=pubkey.lower())
                found_node.remote_close_count = count
                found_node.save(force_insert=True)
            else:
                found_node.remote_close_count = count
                found_node.save()

async def get_most_recent_channel_close(api:BitcartAPI,wallet_id:str)->Optional[datetime.datetime]:
    """
    Returns the most recent channel closing attempt date.
    This is the date of the first attempt to close said channel, subsequent dates are not tracked
    """
    channels=await api.get_wallet_ln_channels(wallet_id)
    found_closes=[]
    for channel in channels:
        state=channel['state']
        if state in ['OPEN','REDEEMED','CLOSED']:
            continue
        elif state in ['CLOSING']:
            channel_point=channel['channel_point']
            channel_object=LightningChannel.get_or_none(LightningChannel.channel_point==channel_point)
            if channel_object:
                if channel_object.cooperative_close_requested:
                    found_closes.append(found_closes)
        else:
            logger.warning(f'In get_most_recent_channel_close, found unknown state {state}')
    if len(found_closes)>0:
        return max(found_closes)
    return None


async def liquidity_check(
    api: BitcartAPI) -> Optional[bool]:
    """
    Find and make inbound liquidity.
    """
    list_of_stores=await api.get_stores()
    for store in list_of_stores:
        store_id=store['id']
        store_name=store['name']
        store_liquidity_result=await store_needs_liquidity(store_id,api,MIN_INBOUND_LIQUIDITY,MIN_CHANNEL_COUNT)
        if not store_liquidity_result:
            logger.debug(f'Store has enough liquidity: {store_id}')
            continue
        liquidity_needed=store_liquidity_result[0]
        channels_needed = store_liquidity_result[1]
        store_total_liquidity=await api.get_store_total_liquidity(store_id)
        #current_inbound_liquidity=await api.get_store_inbound_liquidity(store_id)
        best_wallet=await api.get_best_ln_wallet_for_store(store)
        best_wallet_balance_in_sats=btc_to_sats(float(best_wallet['balance']))
        wallet_id=best_wallet['id']
        # attempt to close any bad channels
        await find_offline_channels(
            best_wallet["xpub"]
        )
        # don't continue if there are pending channel opens.
        open_pending_response = await api.is_channel_open_pending(best_wallet["id"])
        if open_pending_response:
            logger.info(
                f"Not opening channel/getting more liquidity due to pending channel open on wallet {best_wallet['id']}"
            )
            continue
        # or recent channel closes
        close_pending_response = await get_most_recent_channel_close(api,best_wallet['id'])
        if close_pending_response:
            two_hours_ago=datetime.datetime.now()-datetime.timedelta(minutes=61)
            if close_pending_response>two_hours_ago:
                logger.info(
                    f"Not opening channel/getting more liquidity due to recent pending channel close on wallet {best_wallet['id']}"
                )
                continue
        # or we're due for a topup
        topup_result = await store_needs_topup(api,store_id)
        if topup_result:
            logger.warning(f'Skipping adding liquidity bc store needs topup before doing so: {store_name}')
            continue
        # or we don't have enough sats to open the channels we need
        needed_channel_liquidity_sizes = common_functions.distribute_sats_over_channels(liquidity_needed,
                                                                                        channels_needed)
        channel_sizes = [common_functions.liquidity_to_channel_size(item) for item in needed_channel_liquidity_sizes]

        amount_needed_including_onchain_reserves = sum(
            [common_functions.onchain_reserves_to_keep_for_channel(item) for item in channel_sizes])
        if amount_needed_including_onchain_reserves>best_wallet_balance_in_sats:
            logger.error(f'in liquidity_check this shouldnt happen (amount_needed_including_onchain_reserves>best_wallet_balance_in_sats)')
            continue

        if min(channel_sizes) < MIN_CHANNEL_SIZE_IN_SATS:
            logger.error(
                f'in liquidity_check this shouldnt happen (min(channel_sizes) < MIN_CHANNEL_SIZE_IN_SATS)')
            continue

        if sum(needed_channel_liquidity_sizes)+store_total_liquidity<MIN_INBOUND_LIQUIDITY:
            logger.error(
                f'in liquidity_check this shouldnt happen sum(needed_channel_liquidity_sizes)+store_total_liquidity<MIN_INBOUND_LIQUIDITY')
            continue
        if DRY_RUN_FUNDS:
            logger.info(
                f"DRY RUN: Would try to open channel due to total_in_ln_channels<MIN_INBOUND_LIQUIDITY on wallet {best_wallet['id']}"
            )
        else:
            logger.info(
                f"Opening channel due to total_in_ln_channels<MIN_INBOUND_LIQUIDITY on wallet {best_wallet['id']}"
            )
        channel_open_successful = await attempt_create_channels(
            best_wallet["id"], api, channel_sizes
        )
        if channel_open_successful:
            continue
        # Get new lightning nodes, try again with all available funds
        logger.info("Still failed, trying to fetch new nodes from magma, then trying to open channels again")
        node_db_update.main(force_homepage_fetch=True, skip_node_refresh=True)
        channel_open_result = await attempt_create_channels(
            best_wallet["id"], api, channel_sizes
        )
        if channel_open_result:
            continue
async def lnurl_to_invoice(lnurl:str,payment_amount_in_sats:int)->Optional[str]:
    """
    Given LNURL and payment amount, produce invoice
    """
    result = await get_lightning_invoice(lnurl, payment_amount_in_sats)
    if result.get("success"):
        # print(f"Got lightning invoice from LNURL")
        # print(f"Amount: {result['amount_sats']} sats")
        # print(f"Invoice: {result['invoice']}")
        invoice = result["invoice"]
        invoice_amount_in_sats = int(result["amount_sats"])
        assert invoice_amount_in_sats==payment_amount_in_sats
        return invoice
    else:
        logger.error(
            f"Error getting LN invoice from LNURL: {result['error']}"
        )
        if "details" in result:
            logger.error(f"Details: {result['details']}")
        return None

async def calculate_fees(api: BitcartAPI) -> Optional[bool]:
    logger.info("Calculating fees...")
    fees_due = await new_calc_invoice_stats(api)
    if not ENABLE_FEE_SENDING:
        logger.info("SKIPPING FEE SENDING DUE TO ENABLE_FEE_SENDING=False")
        return True
    combined_cashout_made = False
    for store_id, calculated_cashout in fees_due.items():
        if combined_cashout_made:
            logger.debug("Skipping fee calc/pay bc combined cashout made")
            continue
        eligible_revenue = calculated_cashout.calc_total_eligible_revenue_in_sats()
        fees_already_paid = (
            calculated_cashout.calc_total_bb_fees_paid_in_sats(include_onchain_network_fees=FEES_PAID_INCLUDES_ONCHAIN_NETWORK_FEES,include_ln_network_fees=FEES_PAID_INCLUDES_LN_NETWORK_FEES)
        )
        total_fees_due = eligible_revenue * FEE_AMOUNT
        remaining_fees_due = total_fees_due - fees_already_paid
        if FORCE_FEE_AMOUNT:
            remaining_fees_due = FORCE_FEE_AMOUNT
        if remaining_fees_due == 0:
            logger.debug(f"no fee due for store {store_id}")
            continue
        if remaining_fees_due < 0:
            logger.warning(
                f"Reported negative fee due for store {store_id}, fee amount {remaining_fees_due}"
            )
            continue

        if LN_FEE_DEST and not FORCE_FEE_ONCHAIN_INSTEAD_OF_LN:
            if not ENABLE_FEE_SENDING_LN:
                logger.warning(
                    "Skipping LN fee sending due to not ENABLE_FEE_SENDING_LN"
                )
                continue
            logger.info(
                f"For store {store_id} Attempting to pay fee via LN {remaining_fees_due}"
            )
            full_store=await api.get_store_by_id(store_id)
            wallet_to_use=await api.get_best_ln_wallet_for_store(full_store)
            wallet_xpub = wallet_to_use["xpub"]
            wallet_max_payout=int(await api.get_outbound_liquidity(wallet_to_use['id']))
            assert wallet_max_payout
            if wallet_max_payout<MIN_FEE_OUT:
                logger.warning(f'Unable to send fee due to wallet_max_payout {wallet_max_payout}, will try later')
                continue
            if DRY_RUN_FUNDS:
                logger.warning(
                    f"Skipping fee cashout due to DRY_RUN_FUNDS, would have sent {wallet_max_payout}"
                )
                continue
            updated_counter = SimpleDateTimeField.replace(
                name="LAST_LN_FEE_PAYMENT_ATTEMPT", date=datetime.datetime.now()
            ).execute()
            if FORCE_FEE_INVOICE:
                invoice = FORCE_FEE_INVOICE
            else:
                invoice=await lnurl_to_invoice(LN_FEE_DEST,remaining_fees_due)
                if not invoice:
                    continue

            ln_invoice_payment_result = await electrum_pay_ln_invoice(wallet_xpub,invoice,FEE_PAYOUT_REASON)
            if ln_invoice_payment_result:
                logger.info('Fee payment successful!')
                updated_counter = SimpleDateTimeField.replace(
                    name="LAST_SUCCESSFUL_LN_FEE_PAYMENT",
                    date=datetime.datetime.now(),
                ).execute()
            else:
                logger.error('Failed to pay fee!')
    return True

async def do_onchain_cashouts(api:BitcartAPI,
                              wallet_id: str, cashout_amount_avail_onchain: int
                              ):
    if not CASHOUT_ONCHAIN:
        logger.error('In do_onchain_cashouts, no CASHOUT_ONCHAIN (address), not cashing out')
        return
    if FORCE_CASHOUT_AMOUNT_ONCHAIN:
        cashout_amount_avail_onchain = FORCE_CASHOUT_AMOUNT_ONCHAIN
    if cashout_amount_avail_onchain < MIN_ONCHAIN_CASHOUT:
        logger.info(
            f"Unable to run onchain cashout due to MIN_ONCHAIN_CASHOUT {cashout_amount_avail_onchain}<{MIN_ONCHAIN_CASHOUT}"
        )
        return
    full_wallet = await api.get_wallet(wallet_id)
    xpub=full_wallet['xpub']
    if DRY_RUN_FUNDS:
        logger.info(
            f"DRY RUN: For wallet {wallet_id} would attempt to cashout via onchain {cashout_amount_avail_onchain}"
        )
    else:
        logger.info(
            f"For wallet {wallet_id} Attempting to cashout via onchain {cashout_amount_avail_onchain}"
        )

    actual_cashout_amount=cashout_amount_avail_onchain
    if DRY_RUN_FUNDS:
        logger.info(f"Skipping onchain cashout wallet id {wallet_id} bc DRY_RUN_FUNDS")
        return
    transaction_result=await electrum_pay_onchain(xpub,CASHOUT_ONCHAIN,amount=sats_to_btc(actual_cashout_amount),label=CASHOUT_REASON)
    if transaction_result:
        updated_counter = SimpleDateTimeField.replace(
            name="LAST_SUCCESSFUL_ONCHAIN_CASHOUT_PAYMENT", date=datetime.datetime.now()
        ).execute()
        return
async def do_ln_cashouts(api:BitcartAPI,
    wallet_id: str, cashout_amount_avail_ln: int
):
    if not CASHOUT_LIGHTNING_ADDRESS:
        logger.error('In do_ln_cashouts, no CASHOUT_LIGHTNING_ADDRESS, not cashing out')
        return
    if FORCE_CASHOUT_AMOUNT_LN:
        cashout_amount_avail_ln = FORCE_CASHOUT_AMOUNT_LN
    if cashout_amount_avail_ln < MIN_LN_CASHOUT_IN_SATS:
        logger.info(
            f"Unable to run LN cashout due to MIN_LN_CASHOUT_IN_SATS {cashout_amount_avail_ln}<{MIN_LN_CASHOUT_IN_SATS}"
        )
        return
    full_wallet = await api.get_wallet(wallet_id)
    xpub=full_wallet['xpub']
    if DRY_RUN_FUNDS:
        logger.info(
            f"DRY RUN: For wallet {wallet_id} would attempt to cashout via LN {cashout_amount_avail_ln}"
        )
    else:
        logger.info(
            f"For wallet {wallet_id} Attempting to cashout via LN {cashout_amount_avail_ln}"
        )

    actual_cashout_amount=cashout_amount_avail_ln
    # keep trying smaller cashouts until one goes through
    while actual_cashout_amount<=1000:
        updated_counter = SimpleDateTimeField.replace(
            name="LAST_LN_CASHOUT_ATTEMPT", date=datetime.datetime.now()
        ).execute()
        if FORCE_CASHOUT_INVOICE:
            invoice = FORCE_CASHOUT_INVOICE
        else:
            invoice=await lnurl_to_invoice(CASHOUT_LIGHTNING_ADDRESS,cashout_amount_avail_ln)
            if not invoice:
                logger.error('Error turning LNURL to invoice, not making cashout')
                return
        if DRY_RUN_FUNDS:
            logger.info(f"Skipping LN payout invoice {invoice} wallet id {wallet_id}")
            return
        ln_transaction_result=await electrum_pay_ln_invoice(xpub,invoice,label=CASHOUT_REASON)
        if ln_transaction_result:
            updated_counter = SimpleDateTimeField.replace(
                name="LAST_SUCCESSFUL_LN_CASHOUT_PAYMENT", date=datetime.datetime.now()
            ).execute()
            return
        actual_cashout_amount=int(actual_cashout_amount/2)



async def notused_do_onchain_cashouts(
    wallet_id: str, cashout_amount_avail_onchain: int, store: dict, api: BitcartAPI
):
    if not CASHOUT_ONCHAIN:
        return
    if not ENABLE_CASHOUT_ONCHAIN:
        logger.warning(
            f"Skipping actual onchain cashout due to ENABLE_CASHOUT_ONCHAIN. Would have sent {cashout_amount_avail_onchain} from {wallet_id} to {ONCHAIN_FEE_DEST}"
        )
        return
    if cashout_amount_avail_onchain < MIN_ONCHAIN_CASHOUT:
        logger.info("Unable to run cashout due to MIN_ONCHAIN_CASHOUT")
        return
    if FORCE_CASHOUT_AMOUNT_ONCHAIN:
        cashout_amount_avail_onchain = FORCE_CASHOUT_AMOUNT_ONCHAIN
    if (
        cashout_amount_avail_onchain < MIN_ONCHAIN_CASHOUT
        and not FORCE_CASHOUT_AMOUNT_ONCHAIN
    ):
        logger.info(
            f"Skipping bc fee_due_in_sats<MIN_ONCHAIN_CASHOUT {cashout_amount_avail_onchain}:{MIN_ONCHAIN_CASHOUT}"
        )
        return
    if DRY_RUN_FUNDS:
        logger.info(
            f"DRY RUN: Onchain cashout would be created: store {store} wallet {wallet_id} amount {cashout_amount_avail_onchain}"
        )
        return
    # do on-chain cashout
    cashout_result = await api.create_payout_onchain(
        store["id"], wallet_id, cashout_amount_avail_onchain, CASHOUT_ONCHAIN
    )
    cashout_approval_result = None
    cashout_send_result = None
    logger.info(
        f"Onchain cashout created: store {store} wallet {wallet_id} amount {cashout_amount_avail_onchain} payout id {cashout_result.get('id')}"
    )


async def do_cashouts(api: BitcartAPI) -> Optional[bool]:
    """
    Make appropriate LN cashouts, return False/None if some kind of error
    """
    logger.info("Calculating cashouts...")
    if not ENABLE_CASHOUT_ONCHAIN and not ENABLE_CASHOUT_LN:
        logger.info("SKIPPING CASHOUTS DUE TO ENABLE_CASHOUT_*")
        return None
    store_list=await api.get_stores()
    used_wallets=set()
    for store in store_list:
        store_id=store['id']
        best_wallet=await api.get_best_ln_wallet_for_store(store)
        wallet_id=best_wallet['id']
        if wallet_id in used_wallets:
            continue
        used_wallets.add(wallet_id)

        if PREFER_CASHOUT_ONCHAIN:
            available_onchain_sats = btc_to_sats(float(best_wallet['balance']))
            if FORCE_CASHOUT_AMOUNT_ONCHAIN:
                available_onchain_sats=FORCE_CASHOUT_AMOUNT_ONCHAIN
            if available_onchain_sats < 0:
                logger.warning(
                    f"Reported negative onchain cashout due for wallet {wallet_id}"
                )
                continue
            if available_onchain_sats==0:
                logger.debug(f'No cashout available for wallet {wallet_id}')
            try:
                onchain_answer = await do_onchain_cashouts(api, wallet_id, available_onchain_sats)
            except Exception as e:
                logger.error(f'Exception in do_onchain_cashouts: {e} {traceback.print_exc()}')
                return False
        # do LN cashouts
        else:
            available_ln = 0
            for channel in await api.get_wallet_ln_channels(wallet_id,active_only=True,online_only=True):
                available_ln+=int(channel['local_balance'])
            if FORCE_CASHOUT_AMOUNT_LN:
                available_ln=FORCE_CASHOUT_AMOUNT_LN
            if available_ln < 0:
                logger.warning(
                    f"Reported negative LN cashout due for wallet {wallet_id}"
                )
                continue
            if available_ln==0:
                logger.debug(f'No cashout available for wallet {wallet_id}')
            try:
                ln_answer = await do_ln_cashouts(api, wallet_id, available_ln)
            except Exception as e:
                logger.error(f'Exception in do_ln_cashouts: {e} {traceback.print_exc()}')
                return False
    return True

async def topup_goal_amount(api:BitcartAPI,store_id:str)->Optional[int]:
    """
        Calculate how much a store should reserve on-chain to buy liquidity. Retuns None if failure.
    """
    liquidity_result = await store_needs_liquidity(store_id, api, MIN_INBOUND_LIQUIDITY, MIN_CHANNEL_COUNT,assume_zero=True)
    if not liquidity_result:
        logger.error(f'Topup_goal_amount reports zero goal, this should not happen: {store_id}')
        return None
    liquidity_needed = liquidity_result[0]
    channels_needed = liquidity_result[1]
    needed_channel_liquidity_sizes = common_functions.distribute_sats_over_channels(liquidity_needed, channels_needed)
    channel_sizes = [common_functions.liquidity_to_channel_size(item) for item in needed_channel_liquidity_sizes]
    addl_onchain_reserves_needed = sum(
        [common_functions.onchain_reserves_to_keep_for_channel(item) for item in channel_sizes])
    final_amount = addl_onchain_reserves_needed + sum(channel_sizes)
    if final_amount <= 0:
        logger.error(f'2Topup_goal_amount reports zero goal, this should not happen: {store_id}')
        return None
    return final_amount
async def store_needs_topup(api: BitcartAPI, store_id: str) -> Optional[int]:
    """
    If store needs top-up, returns amount needed (int, sats) for the top-up, None otherwise.
    """
    topup_goal=await topup_goal_amount(api,store_id)
    store_full = await api.get_store_by_id(store_id)
    wallet_full = await api.get_best_ln_wallet_for_store(store_full)
    current_onchain_balance=btc_to_sats(float(wallet_full['balance']))
    final_amount=topup_goal-current_onchain_balance
    if final_amount<=0:
        return None
    return final_amount


def btc_address_from_invoice(invoice: dict)->Optional[str]:
    for payment in invoice["payments"]:
        if payment["lightning"]:
            continue
        return payment["payment_url"]


async def calculate_topups(
    api: BitcartAPI
) -> Optional[Tuple[Union[str,None],Union[str,None]]]:
    """
    Create topup invoices for stores that need it. Returns URLs for own,bb topups
    """
    list_of_stores=await api.get_stores()
    for store in list_of_stores:
        try:
            store_needs_topup_result = await store_needs_topup(api, store["id"])
            if not store_needs_topup_result:
                continue
            fetched_wallet = await api.get_best_ln_wallet_for_store(store)
            amount_remaining = store_needs_topup_result+1000 #(add 1000 sats as a buffer)
            found_own_invoice = await api.get_invoice_by_note(note=TOPUP_NAME,require_unlimited=True)
            if not found_own_invoice:
                found_own_invoice = await api.create_invoice(
                    price_in_btc=0,
                    store_id=store["id"],
                    currency="BTC",
                    notes=TOPUP_NAME,
                    expiration_in_seconds=2628000,
                )
            found_barebits_invoice = await api.get_invoice_by_note(
                note=TOPUP_BAREBITS,
                require_unlimited=True,
            )
            if not found_barebits_invoice:
                found_barebits_invoice = await api.create_invoice(
                    price_in_btc=0,
                    store_id=store["id"],
                    currency="BTC",
                    notes=TOPUP_BAREBITS,
                    expiration_in_seconds=2628000,
                )
            if not found_own_invoice or not found_barebits_invoice:
                logger.error("Error in calculate_topups, invoice missing!")
                return None
            logger.info(
                "Warning: on-chain funds are low, you must top-up your wallet or wait for incoming on-chain payments. To send funds, see invoices"
            )



            own_invoice = btc_address_from_invoice(found_own_invoice)
            bb_invoice = btc_address_from_invoice(found_barebits_invoice)
            logger.info("Payment information:")
            logger.info(
                f"Amount needed: {sats_to_btc(amount_remaining)} BTC / {amount_remaining} sats"
            )
            logger.info(f"If Bitcart Admin is paying: {own_invoice}")
            logger.info(f"If BareBits is paying: {bb_invoice}")
            for notifier in NOTIFICATION_PROVIDERS:
                body = f"""
                Warning: your wallet on store {store['name']} (wallet id {fetched_wallet['id']}) does not have sufficient inbound liquidity. This does not need to be immediately remedied, but we suggest doing so for the best performance from your Bitcart installation.
                
                Insufficient inbound liquidity can result in your customers not being able to make payments with Bitcoin lightning (higher fees, slower payments). On-chain payments will continue to work. Additionally, while liquidity is too low, your Bitcart installation will hold onto on-chain payments until additional liquidity can be created, which delays the time between when you receive payments and when they are delivered to you.

                To remedy this, send {sats_to_btc(amount_remaining)} BTC to this address {own_invoice}

                Once your funds have been received by your Bitcart installation, they will be sent right back to you at {CASHOUT_LIGHTNING_ADDRESS} minus some minor transaction fees for channel creation.

                If you do not deposit more funds for liquidity, on-chain payments from customers will accumulate until sufficient liquidity is re-established. Once that has happened, those on-chain payments will be delivered to {CASHOUT_LIGHTNING_ADDRESS} minus fees.
                """
                subject=f"Warning: low liquidity on your Bitcart store {store['name']}"
                await run_every_x_days(my_func=notifier.notify,days=30,body=body,subject=subject)
            return own_invoice, bb_invoice
        except Exception as e:
            logger.error(f"Error calculating topups: {e} {store}")
    return None


async def safe_to_spend(api:BitcartAPI,store_id:str)->int:
    """
    Given store id, return amount of sats that are safe to spend. Assumes we have already met liquidity goals and a topup is not pending
    This is asking about sats that are safe to spend/move into new channels once all channels are created.
    """
    full_store=await api.get_store_by_id(store_id)
    full_wallet=await api.get_best_ln_wallet_for_store(full_store)
    wallet_id=full_wallet['id']
    wallet_balance=btc_to_sats(float(full_wallet['balance']))
    channel_count_result=api.get_wallet_ln_channels(wallet_id,active_only=True)
    channel_sat_list=[]
    if channel_count_result:
        current_channel_count=len(channel_count_result)
        for channel in channel_count_result:
            local_balance=int(float(channel['local_balance']))
            remote_balance=int(float(channel['remote_balance']))
            channel_size=local_balance+remote_balance
            channel_sat_list.append(channel_size)
        electrum_onchain_reserves_required = [common_functions.onchain_reserves_to_keep_for_channel(item) for item in
                                              channel_sat_list]
    else:
        current_channel_count = 0
        electrum_onchain_reserves_required=0

    max_reserve_requirement_found=max(MIN_RESERVE_ONCHAIN,electrum_onchain_reserves_required)
    sats_remaining=max(0,wallet_balance-max_reserve_requirement_found)
    return sats_remaining

async def decide_onchain_to_ln(api:BitcartAPI):
    '''
    Figure out what on-chain funds are safe to spend making channels, make new channels if appropriate
    '''
    # Don't move funds to LN if we prefer cashout via on-chain
    if PREFER_CASHOUT_ONCHAIN:
        return
    store_list=await api.get_stores()
    for store in store_list:
        store_id=store['id']
        best_wallet=await api.get_best_ln_wallet_for_store(store)
        topup_result = await store_needs_topup(api,store_id)
        # Don't move onchain to ln if store needs topup
        if topup_result:
            logger.debug(f'Not moving addl onchain funds to LN because store needs topup {store_id}')
            continue
        # or needs liquidity
        liquidity_check_result=await store_needs_liquidity(store_id,api,MIN_INBOUND_LIQUIDITY,MIN_CHANNEL_COUNT)
        if liquidity_check_result:
            logger.debug(f'Not moving addl onchain funds to LN because store needs liquidity {store_id}')
            continue
        # or has recently closed channels
        close_pending_response = await get_most_recent_channel_close(api,best_wallet['id'])
        if close_pending_response:
            hours_ago = datetime.datetime.now() - datetime.timedelta(hours=12)
            if close_pending_response > hours_ago:
                logger.info(
                    f"Not moving addl onchain funds to LN bc pending channel closes store {store_id}"
                )
                continue
        # find out how much is safe to spend
        safe_to_spend_amount=await safe_to_spend(api,store_id)
        if safe_to_spend_amount<MIN_CHANNEL_SIZE_IN_SATS:
            logger.debug(f'Not moving addl onchain funds to LN because safe_to_spend_amount ({safe_to_spend_amount})<MIN_CHANNEL_SIZE_IN_SATS ({MIN_CHANNEL_SIZE_IN_SATS}) store: {store_id}')
            continue
        max_channel_size=common_functions.sats_to_max_channel_size(safe_to_spend_amount)
        if max_channel_size<MIN_CHANNEL_SIZE_IN_SATS:
            logger.warning(f'in decide_onchain_to_ln, max_channel_size<MIN_CHANNEL_SIZE_IN_SATS')
            continue
        best_wallet=await api.get_best_ln_wallet_for_store(store)
        logger.debug(f'Moving leftover onchain funds to ln... {max_channel_size} sats {sats_to_btc(max_channel_size)} BTC, store {store_id}')
        onchain_move_result=await move_onchain_to_ln(wallet_id=best_wallet['id'],amount_in_btc=sats_to_btc(max_channel_size),api=api)

async def recover_reserves_from_ln(api:BitcartAPI):
    """
    Returns True if successful, false otherwise
    """
    store_list=await api.get_stores()
    for store in store_list:
        store_id=store['id']
        best_wallet=await api.get_best_ln_wallet_for_store(store)
        onchain_funds_in_sats=btc_to_sats(float(best_wallet['balance']))
        topup_goal=await topup_goal_amount(api,store_id)
        if onchain_funds_in_sats>=topup_goal:
            return True
        addl_needed_onchain=topup_goal-onchain_funds_in_sats


def setup_notifiers()->List[NotificationProvider]:
    return_list=[]
    if SMTP_USERNAME and SMTP_TO_EMAIL and SMTP_PASSWORD and SMTP_FROM_EMAIL and SMTP_FROM_NAME and SMTP_PORT and SMTP_SERVER:
        my_notifier=notifications.EmailNotificationProvider(name='mymail',from_email=SMTP_FROM_EMAIL,from_name=SMTP_FROM_NAME,password=SMTP_PASSWORD,username=SMTP_USERNAME,smtp_server=SMTP_SERVER,smtp_port=SMTP_PORT,tls_enabled=SMTP_TLS,ssl_enabled=SMTP_SSL,to_email=SMTP_TO_EMAIL)
        try:
            my_notifier.test_connection()
        except Exception as e:
            logger.error(f'Error connecting to SMTP server: {e}')
        else:
            return_list.append(my_notifier)
    else:
        logger.warning('No SMTP notification provider config found')
    return return_list

async def main():
    global LAST_FEE_CHECK
    global START_TIME
    global AUTH_TOKEN
    global NOTIFICATION_PROVIDERS
    BITCART_URL = "http://127.0.0.1/api"
    # delete old cache entries
    try:
        SimpleCacheField.delete_expired()
    except Exception as e:
        logger.error(f'Error deleting expired cache entries {e} {traceback.print_exc()}')
    # init notifications
    try:
        if len(NOTIFICATION_PROVIDERS)==0:
            NOTIFICATION_PROVIDERS=await run_every_x_hours(my_func=setup_notifiers,hours=6)
    except Exception as e:
        logger.error(f'Not able to setup notifications, please see logs! {e}')
    # init Bitcart API
    try:
        logger.info("Initializing bitcart API....")
        api = BitcartAPI(BITCART_URL, AUTH_TOKEN)
        if not AUTH_TOKEN:
            token_object=SimpleVariable.get_or_none(
                SimpleVariable.name == "BITCARTAPITOKEN"
            )
            if token_object:
                AUTH_TOKEN = token_object.value
        api = BitcartAPI(BITCART_URL, AUTH_TOKEN)
        if not AUTH_TOKEN:
            logger.info(
                "🔎 Detected first run, attempting to create account for Bitcart API.."
            )

            AUTH_TOKEN = await api.setup_first_user(ADMIN_EMAIL, ADMIN_PASSWORD)
            if AUTH_TOKEN:
                new_object = SimpleVariable(
                    name="BITCARTAPITOKEN", value=AUTH_TOKEN
                )
                new_object.save(force_insert=True)
            else:
                logger.critical(
                    "Critical error: no auth token for bitcart API. Maybe bitcart isnt started yet? Sleeping for 30 seconds"
                )
                sleep(30)
                return
        # Check authentication
        api = BitcartAPI(BITCART_URL, AUTH_TOKEN)
        auth_result = await api.is_authenticated()
        if not auth_result:
            logger.error(
                "⚠️ Bitcart Authentication failed..."
            )
            sleep(60)
            return
    except Exception as e:
        logger.error(
            f"⚠️ Bitcart api auth error {e}, sleeping... {traceback.print_exc()}"
        )
        sleep(60)
        return
    # create first wallet if it doesn't exist, must be done before creating first store
    first_wallet_response = None
    try:
        first_wallet_response = await first_wallet_check_create(api)
    except Exception as e:
        logger.error(f"Error in wallet creation stage1 {e} {traceback.print_exc()}")
        traceback.print_exc()
        sleep(60)
        return
    if not first_wallet_response:
        logger.error(f"Error in wallet creation stage2")
        sleep(60)
        return

    # create our wallet for each store if it doesn't exist
    wallet_creation_response = None
    try:
        wallet_creation_response = await wallet_creation(api)
    except Exception as e:
        logger.error(f"Error in wallet creation stage {e} {traceback.print_exc()}")
        traceback.print_exc()
        sleep(60)
        return
    if not wallet_creation_response:
        logger.error(f"2Error in wallet creation stage")
        sleep(60)
        return
    # calculate top-ups
    try:
        if DEBUG_STEPS:
            breakpoint()
        topup_answer = await calculate_topups(api)
    except Exception as e:
        logger.error("Error calculating top-ups")
    # check available inbound liquidity
    liquidity_check_response = None
    if START_TIME > (datetime.datetime.now() - datetime.timedelta(seconds=30)) and not SKIP_WALLET_DELAY:
        logger.info(
            "Sleeping 30 seconds before requesting liquidity status so wallet has a chance to come online..."
        )
        sleep(30)
    # update closed channel stats
    try:
        channel_closings_result=await update_channel_closings(api)
    except Exception as e:
        logger.error(f'Error in updating channel closings: {e}:{traceback.print_exc()}')
    # add more liquidity as needed
    try:
        if DEBUG_STEPS:
            breakpoint()
        liquidity_check_response = await liquidity_check(api)
    except Exception as e:
        logger.error(
            f"Error in checking available inbound liquidity: {e}:{traceback.print_exc()}"
        )
    # calculate onchain -> LN moves
    # this is only done if liquidity/reserves are sufficient
    try:
        if DEBUG_STEPS:
            breakpoint()
        onchain_to_ln_result = await run_every_x_seconds(my_func=decide_onchain_to_ln, seconds=90, api=api)
    except Exception as e:
        logger.error(f'Error calling onchain_to_ln_result: {e}:{traceback.print_exc()}')
    # Calculate and send fees, this check is heavy and shouldn't be run more than once per day
    fee_response = None
    try:
        if DEBUG_STEPS:
            breakpoint()
        if FORCE_FEE_CHECK:
            fee_response= await run_every_x_seconds(my_func=calculate_fees,seconds=1,api=api)
        else:
            fee_response= await run_every_x_days(my_func=calculate_fees,days=1, api=api)
    except Exception as e:
        logger.error(f"Error in calculating fees: {e} {traceback.print_exc()}")

    # run any needed swaps, disabled for now
    #swap_response = None
    #try:
    #    if DEBUG_STEPS:
    #        breakpoint()
    #    swap_response = await recover_reserves_from_ln(api)
    #except Exception as e:
    #    logger.error(f"Error in doing swaps: {e} {traceback.print_exc()}")
    #if not swap_response:
    #    logger.error(f"2Error in in doing swaps: no swap_response")

    # Calculate and send cashouts, should basically be the same code as calculating fees
    cashout_response = None
    try:
        if DEBUG_STEPS:
            breakpoint()
        cashout_response = await do_cashouts(api)
    except Exception as e:
        logger.error(f"Error in calculating cashouts: {e} {traceback.print_exc()}")
    if not cashout_response:
        logger.error(f"2Error in calculating cashouts")


if __name__ == "__main__":
    while True:
        asyncio.run(main())
        if SINGLE_RUN:
            break
