# Bitcart Bitcoin Lightning Liquidity Helper
This script makes it easy to manage inbound Bitcoin Lightning liquidity for your Bitcart store. While Bitcart supports lightning, 
it leaves you to figure out how to get inbound liquidity. Without inbound liquidity, you can't
receive payments via lightning. 

This script supports having multiple stores and manages a wallet *liquidityhelper* for each store. **This script charges a 2% fee** which is assessed over all incoming payments to wallets the script manages. Network fees are included in this, so if this script manages your funds and spends 0.5% on network fees, it only "charges" you 1.5% so your net fee remains at 2%

## ⚠️ Warning
This is alpha software and should not be deployed on production systems. There are bugs which will probably cause you to lose money. We are not responsible for any lost funds.

## Requirements
In order to use this script, you must have:
- A server with Bitcart running
- The ability to execute commands/python scripts on that server
- Some on-chain funds to open lightning channels with
- A lightning address that can occasionally receive payments FROM your Bitcart server. Good, free custodial options include Strike and CoinOS. For non-custodial, check out Zeus wallet.

If deployed via docker, you must have the following environment variables set. You can set these like so and then re-run setup.sh in bitcart-docker to seamlessly upgrade:
```
export BITCART_CRYPTOS=btc
export BTC_LIGHTNING=True
export BTC_LIGHTNING_LISTEN=0.0.0.0:9735
export BITCART_ADDITIONAL_COMPONENTS=btc-ln
export BTC_LIGHTNING_GOSSIP=true 
export BITCART_BITCOIN_EXPOSE=true
export BTC_DEBUG=true
export ALLOW_INCOMING_CHANNELS=true
```


## How to use

You can run this either as a **standalone script** (existing path — talks to Bitcart over its public HTTP API) or as a **Bitcart plugin** (loaded into Bitcart's process; settings configurable through the Bitcart admin UI). Both paths use the same engine code; the difference is purely how the engine gets bootstrapped and where its settings come from.

### Option A — Standalone (external script)

1. `git clone` this repository onto the server.
2. Provide config either via `user_config.py` (copy `config.py` as the starting template) or via environment variables — env wins over both files. **Env vars require a `LIQUIDITYHELPER_` prefix** (`LIQUIDITYHELPER_CASHOUT_LIGHTNING_ADDRESS=…`, `LIQUIDITYHELPER_AUTH_TOKEN=…`). The prefix avoids collisions with unrelated env vars in the operator's environment; bare names are ignored with a stderr warning. At minimum set `LIQUIDITYHELPER_CASHOUT_LIGHTNING_ADDRESS` and `LIQUIDITYHELPER_AUTH_TOKEN`. Get your auth token from Bitcart at User Profile → API keys.
3. `pip install -r requirements.txt`
4. `python3 liquidityhelper.py`

### Option B — Bitcart plugin

1. Copy this entire directory into Bitcart's `modules/barebits/liquidityhelper/` directory (path is enforced by `manifest.json` — `author` = BareBits, `name` = liquidityhelper).
2. Restart Bitcart. The plugin's settings page becomes available under the admin UI; every value in `config.py` is exposed there as an editable field with the same name.
3. On first boot the plugin auto-acquires a long-lived bearer token tagged with `app_id=plugin:liquidityhelper`, bound to the first superuser. If you're installing into a brand-new Bitcart (no admin yet), set `ADMIN_EMAIL` and `ADMIN_PASSWORD` in the plugin settings — the plugin will create the first admin via the same `POST /users/` path the standalone script uses, then bind to it.

**Settings precedence** (highest wins): plugin UI > environment variables > `user_config.py` > `config.py` defaults. Edits in the plugin UI are applied live to the running engine — no Bitcart restart needed.

**Logs viewer** — after install, visit `/plugins/liquidityhelper` in the Bitcart admin to get a tabbed view with **Settings** and **Logs**. The Logs tab lets you switch between:
- **Operational** — the full firehose (`liquidityhelper.log`)
- **Decisions** — the audit log of what the script actually decided to do (`decisions.log`)
…with an adjustable tail size (100–5000 lines) and an optional 3-second auto-refresh. Logs are also still written to the engine's working directory in standalone mode.

**Standalone code path is unaffected by plugin mode**; the same `config.py` defaults still drive `python3 liquidityhelper.py` for users who don't want the in-process integration.

## How it works
- The script monitors the amount of available inbound liquidity on your server. If liquidity is below your set threshold, it will open new lightning channels using your on-chain funds, then empty those channels to your payout lightning address (so you now have inbound liquidity)
- Any time there are funds in lightning, it will instruct Bitcart to send those funds to your payout address
- You will occasionally have to "top up" your Bitcart wallet to keep enough on-chain reserve for the script to maintain inbound liquidity. A top-up is needed any time the wallet's on-chain balance drops below the per-mode reserve floor:
  - **LSP mode** (default — channel acquisition delegated to an LSP): floor = `max(MIN_RESERVE_ONCHAIN, recent 6-month LSP price peak)`, capped at `LSP_RESERVE_CAP_SAT`. A channel close, a customer-payment-driven channel open, or an LSP quote spike on the network can all push you below the floor.
  - **Automatic mode** (the engine opens channels itself): floor = `target_liquidity + (MIN_CHANNEL_COUNT × AUTOMATIC_CHANNEL_OPEN_FEE_ESTIMATE_SAT × 2) + (target_liquidity × AUTOMATIC_LIQUIDITY_LOOPOUT_FEE_PERCENT) + AUTOMATIC_RESERVE_SAFETY_SAT`. Same trigger sources as LSP mode; the larger formula reflects the script needing budget to open channels AND loop the local balance back out for inbound capacity.

  Channel closures are one cause of needing a top-up but aren't the only one — normal drain from channel opens, swap fees, LSP service fees, and cashouts can all bring the on-chain balance below the reserve floor over time.

## Fees
- **Developer fee** — 2% net, charged on all revenue received on a `liquidityhelper` wallet. Network fees the script incurs on your behalf (channel opens, LSP service fees, cashout miner fees, fee-payment routing fees, etc.) are deducted from the 2% so your *net* fee stays at 2% — if the script spends 0.5% on network fees, only 1.5% reaches the developer.
- **Referral fee** (optional, off by default) — a flat additional percentage on top of the 2%, paid to a third party (typically a whitelabel distributor or installer). Configured via `REFERRAL_FEE_AMOUNT` (e.g. `0.02` = 2% extra), `REFERRAL_FEE_DEST` (Lightning Address), and `REFERRAL_ONCHAIN_DEST_XPUB` (on-chain fallback when LN routing has been stale for `REFERRAL_SWITCH_TO_ONCHAIN_AFTER_X_DAYS` days). Unlike the developer fee, the referrer receives the **gross** percentage — the LN/on-chain network cost of delivering the referral payment comes out of the developer's 2%, not the referrer's share. Leave `REFERRAL_FEE_AMOUNT = 0.0` (the default) if you have no referral arrangement.
- **BareBits top-up returns** — when BareBits has paid a `topupbarebits` invoice into your wallet to lubricate a new installation with inbound liquidity, the principal is repaid back to BareBits via lightning over subsequent fee-payment ticks. The repayment is independent of the 2% developer fee: liquidity costs (channel opens, LSP service fees) acquired with those funds count against the 2% cap as normal; the full BareBits principal is returned over time as the wallet accumulates LN outbound. Repayment is gated on the store first reaching its inbound-liquidity target so the channels funded by the top-up have actually been provisioned before any funds flow back. LN-only; no on-chain fallback.

## On-chain destinations (xpub-derived addresses)

On-chain payments — cashouts, on-chain dev-fee fallbacks, on-chain referral fallbacks, and loop-out drains — all use xpub-derived addresses, not fixed addresses. Each send picks the next unused receive-chain address (`<xpub>/0/<N>`) from a counter stored locally, so the recipient receives a fresh address per transaction. This is standard Bitcoin wallet practice and significantly improves on-chain privacy on the receive side (blockchain explorers can no longer attribute the total of all your cashouts to a single accumulating address).

Three operator-controlled settings:

- `CASHOUT_ONCHAIN_XPUB` — operator's own xpub for on-chain cashouts. Required when `ENABLE_CASHOUT_ONCHAIN=True` OR `PREFER_CASHOUT_ONCHAIN=True`.
- `REFERRAL_ONCHAIN_DEST_XPUB` — operator's referral partner's xpub. Required when `REFERRAL_FEE_AMOUNT > 0` and the LN referral rail is unavailable / stale.
- BareBits's developer-fee xpubs (`BAREBITS_FEE_XPUB_MAINNET`, `BAREBITS_FEE_XPUB_TESTNET`) ship as defaults in `config.py`. Don't change unless you have an alternative arrangement.

Accepted xpub formats: any standard BIP-32 extended public key. Version bytes determine the address type that gets derived:

| Network | Legacy P2PKH | Wrapped segwit P2SH-P2WPKH | Native segwit P2WPKH |
|---|:---:|:---:|:---:|
| Mainnet | `xpub` (`1...`) | `ypub` (`3...`) | `zpub` (`bc1q...`) |
| Testnet / signet / Mutinynet / regtest | `tpub` (`m.../n...`) | `upub` (`2...`) | `vpub` (`tb1q.../bcrt1q...`) |

Most modern wallets export a `zpub`/`vpub` by default. Both depth-1 (Electrum native-segwit default — derived from `m/0'`) and depth-3 (strict BIP-44/49/84 — `m/84'/0'/0'` etc) xpubs are supported.

**Network validation**: the engine cross-checks each xpub's version-byte family against the deployment's detected Bitcoin network at startup AND on every payment. A mainnet `zpub` on a testnet deployment (or vice versa) is rejected with a clear error in the engine log and a red banner on the plugin dashboard — operator funds never silently land on the wrong network. Testnet `vpub` is automatically re-encoded with the regtest HRP (`bcrt1q...`) on regtest deployments, so one testnet xpub serves testnet3 / testnet4 / signet / Mutinynet / regtest.

**Migration from earlier versions**: prior versions used fixed-address settings (`CASHOUT_ONCHAIN`, `REFERRAL_ONCHAIN_DEST`, `ONCHAIN_FEE_DEST`). Those have been replaced by the `_XPUB` variants above. Export an xpub from your wallet (Electrum: Wallet → Information → Master Public Key; hardware wallets: account-level zpub/vpub via your wallet's exporter) and set it as the new value. The on-chain rail soft-fails (logs the error, skips the tx) until you complete the migration; LN cashouts and fee payments continue unaffected meanwhile.

## Privacy
This script runs locally only and does not report your transaction data or other private information to any external place. The script queries our server for a list of lightning nodes (and queries Magma for information about those nodes) and manages its node list autonomously.

## Contributing
Contributions in the form of PRs are welcome, please see `DESIGN.md` for our design principles and `ROADMAP.md` for planned/desired features.

## License
You are free to use and modify this script as you wish provided you do not remove the fee component. See LICENSE & USE_POLICY and for full terms and details.

BareBits is self-hosted payment processing software. You may download, deploy, and modify it on your own infrastructure (subject to applicable open-source and third-party licenses). You are solely responsible for configuration, security hardening, key custody, compliance, and any transactions processed through your instance. To the maximum extent permitted by law, BareBits disclaims liability for your deployment, modifications, integrations, and downstream use, and provides the Software “as is” with no warranties. BareBits does not provide a hosted service unless you have a separate written Service Agreement.

## Testnet support

Which Bitcoin networks each component will function on. ✅ = works against a public production endpoint, ⚠️ = works but requires extra configuration, ❌ = no support / will fail at startup.

| Component                              | mainnet | testnet3 | testnet4 | signet (official) | Mutinynet (signet variant) | regtest |
|----------------------------------------|:-------:|:--------:|:--------:|:-----------------:|:--------------------------:|:-------:|
| Automatic channel management (LND-only)¹|   ✅    |    ✅    |    ✅    |        ✅         |             ✅             |   ✅²   |
| Loop (Lightning Labs `loopd` LoopOut)  |   ✅    |    ✅    |    ❌    |        ✅         |             ❌             |   ⚠️³   |
| LSP mode — Zeus (`lnolymp.us`)         |   ✅    |    ✅    |    ❌⁴   |        ❌⁵        |             ✅             |   ❌    |
| LSP mode — Megalithic (`megalithic.me`)|   ✅    |    ❌    |    ❌    |        ❌⁵        |             ✅             |   ❌    |

Notes:

¹ Automatic channel management uses an LND-gossip-derived candidate DB to pick peers; Electrum wallets are not supported on this path. Whatever network your LND node speaks, this component speaks. (Electrum wallets can use LSP mode where available, but cannot purchase from LSPs today due to an Electrum bug paying LSP invoices.)

² On regtest you supply the peer set yourself — there is no public gossip to enumerate.

³ Lightning Labs runs no public swap server for regtest/simnet. Set `LOOPD_SERVER_HOST` (and `LOOPD_SERVER_NOTLS=true` if your local `loopserver` is plaintext) to point at your own instance. See `config.py` (`LOOPD_NETWORK`, `LOOPD_SERVER_HOST`, `LOOPD_SERVER_NOTLS`) for the knobs.

⁴ Zeus serves testnet3 at `testnet-lsps1.lnolymp.us`; there is no testnet4 endpoint. A wallet on testnet4 will receive chain-hash mismatches because the LSP is on testnet3. The engine emits a warning decision-log entry when it detects a testnet4 wallet attempting to use Zeus.

⁵ Mutinynet is a fast-block variant of signet, NOT the official slow-block Bitcoin signet. Both Zeus and Megalithic serve Mutinynet but neither runs a public endpoint on official signet. Wallets on official signet attempting to use these providers will get chain-hash mismatches at the LSP side.

Provider URLs in use (defined in `lsp_providers.py`; verified against [Zeus LSPS1 docs](https://docs.zeusln.app/lsp/services/lsps1) and [Megalithic LSP1 docs](https://docs.megalithic.me/lightning-services/lsp1-get-inbound-liquidity-for-mobile-clients/)):

- Zeus: `lsps1.lnolymp.us` (mainnet), `testnet-lsps1.lnolymp.us` (testnet3), `mutinynet-lsps1.lnolymp.us` (Mutinynet).
- Megalithic: `megalithic.me/api/lsps1/v1` (mainnet), `lsp1.mutiny.megalith-node.com/api/lsps1/v1` (Mutinynet).
- Loop: built-in Lightning Labs defaults per `LOOPD_NETWORK` — `swap.lightning.today:11010` (mainnet), `test.swap.lightning.today:11010` (testnet3), `signet.swap.lightning.today:11010` (signet).

