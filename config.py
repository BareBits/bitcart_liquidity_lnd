from typing import Optional
import os
from os import environ

from common_functions import is_integer, is_float
#***********************************************************
#              DO NOT MODIFY THIS FILE                     *
#***********************************************************
# User-adjusted config values can be set in a custom user_config.py or declared via environment variables
# Environment-declared variables override variables from user_config and config
# If no environment variable or user_config variable is declared, ones found in this file will be used by default

# Unless otherwise stated, all settings are applied on a per-wallet basis. BTC amounts are in sats.
MIN_CHANNEL_COUNT:int=2 # always try to maintain this # of channels w inbound liquidity, minimum
MIN_INBOUND_LIQUIDITY:int=100000 # minimum amount of inbound liquidity, in sats, we should always have. Request more if we don't have it
MIN_LN_CASHOUT_IN_SATS:int=150 # 100 is the minimum for Strike
CASHOUT_LIGHTNING_ADDRESS:Optional[str]='cashout@getbarebits.com' # lightning address to cash out to for example myname@strike.me
MIN_RESERVE_TOTAL:int=20000 # keep this amount of sats in reserve to open new channels for inbound liquidity
MIN_RESERVE_ONCHAIN:int=10000 # keep this amount of sats on-chain in reserve to open new channels for inbound liquidity
MIN_FEE_OUT:int=150 # send fees when amount due > X
CHANNEL_ONCHAIN_BUFFER:int=500 # how many sats to keep per channel so we can close a channel if need be
AUTH_TOKEN:Optional[str] = None  # Replace with your API token
LOG_LEVEL:str='WARNING' # DEBUG|WARNING|ERROR|INFO
# Notification settings
SMTP_SERVER:Optional[str]=None
SMTP_PORT:Optional[int]=None
SMTP_TLS:bool=False
SMTP_SSL:bool=False
SMTP_FROM_EMAIL:Optional[str]=None
SMTP_FROM_NAME:Optional[str]='LiquidityHelper'
SMTP_TO_EMAIL:Optional[str]=None
SMTP_USERNAME:Optional[str]=None
SMTP_PASSWORD:Optional[str]=None

# Only used at first run if you are starting with a fresh bitcart install
STORE_NAME='mystore'
ADMIN_EMAIL:Optional[str]=None
ADMIN_PASSWORD:Optional[str]=None

#Variables which aren't used for anything at this point in time. Some remain uncommented as live code references them even if it doesn't use them
CASHOUT_ONCHAIN:Optional[str]=None # on-chain address to cash out to, not currently used
#NOTUSED_CASHOUT_SWITCH_TO_ONCHAIN_AFTER_X_DAYS:int=30 # switch to on-chain cashouts if LN not successful for x days
#NOTUSED_ONCHAIN_CASHOUT_CHANNEL_THRESHOLD_IN_LOCALSATS:int=50000 # if doing on-chain cashouts, close LN channels that have this many sats or more on local side
#NOTUSED_ONCHAIN_CASHOUT_CHANNEL_THRESHOLD_IN_REMOTESATS:int=20000 # if doing on-chain cashouts, close LN channels that have this many sats or less on remote side. This should generally be the smallest order you can expect to receive
#NOTUSED_COMBINE_STORE_CASHOUTS:bool=False # let stores share a cashout procedure, saves fees for on-chain cashouts. Does nothing currently.
MIN_ONCHAIN_CASHOUT:int=25000 # minimum to cash-out on-chain
#ON_CHAIN_FEE_DELAY:int=15 # only send fees on-chain is last successful LN fee payment > x days ago, not used
#ENABLE_FEE_SENDING_ONCHAIN:bool=False
FORCE_CASHOUT_AMOUNT_ONCHAIN:Optional[int]=None #whenever sending onchain cashouts, use this amount instead of the actual amount due
MIN_INBOUND_LIQUIDITY_PER_CHANNEL:int=50000 # we should always have one channel with at least this much inbound liquidity, in sats. Prevent situation where MIN_INBOUND_LIQUIDITY is met via a bunch of small channels. Request more if we don't have it
#MIN_RESERVE_LN:int=0 # keep this amount of sats in LN in reserve to open new channels for inbound liquidity

#LSP stuff currently not used
MIN_INBOUND_LIQUIDITY_REQUEST_AMOUNT:int=50000 # never request a liquidity lease smaller than this amount
MIN_ONCHAIN_TO_LN_MOVEMENT:int=20000 # when moving funds from on-chain to LN, never move less than this amount
INITIAL_CHANNEL_SIZE:int=20000 # when creating your initial channel w an LSP to buy liquidity with, use max(INITIAL_CHANNEL_SIZE, MIN_ONCHAIN_TO_LN_MOVEMENT)
TARGET_INBOUND_LIQUIDITY:int=500000 # how much liquidity to request when requesting it, in sats
LSP_DEV_MODE:bool=False # run in dev mode, use TESTNET (applicable to LSP stuff only)

# GLOBAL VARIABLES THAT SHOULD NEVER NEED TO BE CHANGED
FEE_PAYOUT_REASON='lnhelper_fee'
CASHOUT_REASON='lnhelper_cashout'
TOPUP_NAME='topupself'
TOPUP_BAREBITS='topupbarebits'
MIN_CHANNEL_SIZE_IN_SATS=60000 # never open a channel smaller than this amount, electrum minimum as set by bitcart is 60000 https://github.com/bitcart/bitcart/blob/master/daemons/btc.py

# Some DEBUG flags
DRY_RUN_FUNDS:bool=False # if true, don't actually move any funds anywhere, but run as if we are moving funds
SINGLE_RUN:bool=False # if true, run once then exit instead of looping
ENABLE_FEE_SENDING:bool=True
ENABLE_FEE_SENDING_LN:bool=True
ENABLE_CASHOUT_LN:bool=True
ENABLE_CASHOUT_ONCHAIN:bool=False # Disabled for now, onchain funds automatically get moved into LN channels
PREFER_CASHOUT_ONCHAIN:bool=False # prefer cashing out on-chain, when possible (stops funds from being moved into LN)
DEBUG_STEPS:bool=False # if True, stop between each major step (liquidity calculations, fee payment, etc) in debugger
## Debug flags for cashouts and fees
FORCE_FEE_AMOUNT:Optional[int]=None # whenever sending fees, use this amount instead of the actual amount due
FORCE_FEE_CHECK:bool=False # force fee checks even if they've been done recently
# NOTE: Invoice INCLUDES a fee amount. This overrides FORCE_FEE_AMOUNT
FORCE_FEE_INVOICE:Optional[str]=None # invoice to send fees to, instead of LN address.
FORCE_FEE_ONCHAIN_INSTEAD_OF_LN:Optional[bool]=False # also causes fee to be submitted even if below minimum, does nothing curently
FORCE_CASHOUT_AMOUNT_LN:Optional[int]=None #whenever sending LN cashouts, use this amount instead of the actual amount due
 # NOTE: Invoice INCLUDES a fee amount. This overrides FORCE_CASHOUT_AMOUNT
FORCE_CASHOUT_INVOICE:Optional[str]=None # invoice to send cashout to, instead of LN address.
FORCE_EXTERNAL_IP_AND_PORT_LN:Optional[str]=None # use if this script can't correctly detect your IP address and port, for example, if you are behind a reverse proxy of some kind.
SKIP_WALLET_DELAY:bool=False # assume wallets are online at start of script instead of waiting 30 seconds

# Criteria for selecting lightning nodes to connect to
NODE_CRITERIA_MINIMUM_CAPACITY:int=1000000 # in sats
NODE_CRITERIA_MINIMUM_CHANNELCOUNT:int=10
NODE_CRITERIA_MINIMUM_AGE:int=365 # in days

# How often to run various routines, in seconds. These aren't totally respected yet.
RUN_FREQUENCY_LIQUIDITYCHECK:int=1 # every time the script runs, as often as possible
RUN_FREQUENCY_FEE_CALCULATION:int=86400 # every day
RUN_FREQUENCY_PULL_DEV_NODES:int=86400 # every day (pull list of LN nodes from dev website)
RUN_FREQUENCY_FEE_PAYMENT:int=46400 # every half day
LN_FEE_DEST:str= 'fees@getbarebits.com' # where to send fees
FEE_START_DATE:Optional[str]='1999/11/30' # date in format '2020/11/30'
FEE_START_REVENUE:int=0 # dont charge a fee for first x sats in revenue
FEES_PAID_INCLUDES_ONCHAIN_NETWORK_FEES:bool=True # count BTC network fees in total amount of fees paid
FEES_PAID_INCLUDES_LN_NETWORK_FEES:bool=True # count BTC LN network fees in total amount of fees paid
ONCHAIN_FEE_DEST:str= 'bc1q586um24k7zr6swxqny5qqgqn8xt43pk4xeeg9g' # where to send fees on chain
CHARGE_FEE_FOR_LN_TRANSACTIONS:bool=True
CHARGE_FEE_FOR_ONCHAIN_TRANSACTIONS:bool=True
FEE_AMOUNT:float=.02 # fee amount

if os.path.exists('user_config.py'):
    from user_config import *

# If environment variables exist, over-write local values
for entry in list(locals().items()):
    name=entry[0]
    value=entry[1]
    if name.startswith("_"):
        continue
    if name in {'environ'}:
        continue
    if name in environ:
        environ_var = environ.get(name)
        if environ_var.upper()=='NONE':
            locals()[name]=None
            continue
        if environ_var.upper()=='TRUE':
            locals()[name]=True
            continue
        if environ_var.upper()=='FALSE':
            locals()[name]=False
            continue
        if environ_var!='':
            if is_integer(environ_var):
                locals()[name] = int(environ_var)
            elif is_float(environ_var):
                locals()[name] = float(environ_var)
            else:
                locals()[name]=environ_var
            continue
