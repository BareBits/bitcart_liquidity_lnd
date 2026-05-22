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
2. Provide config either via `user_config.py` (copy `config.py` as the starting template) or via environment variables — env wins over both files. At minimum set `CASHOUT_LIGHTNING_ADDRESS` and `AUTH_TOKEN`. Get your auth token from Bitcart at User Profile → API keys.
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
- You will occasionally have to "top up" your Bitcart wallet to re-open channels when existing channels get closed

## Privacy
This script runs locally only and does not report your transaction data or other private information to any external place. The script queries our server for a list of lightning nodes (and queries Magma for information about those nodes) and manages its node list autonomously.

## Contributing
Contributions in the form of PRs are welcome, please see `DESIGN.md` for our design principles and `ROADMAP.md` for planned/desired features.

## License
You are free to use and modify this script as you wish provided you do not remove the fee component. See LICENSE & USE_POLICY and for full terms and details.

BareBits is self-hosted payment processing software. You may download, deploy, and modify it on your own infrastructure (subject to applicable open-source and third-party licenses). You are solely responsible for configuration, security hardening, key custody, compliance, and any transactions processed through your instance. To the maximum extent permitted by law, BareBits disclaims liability for your deployment, modifications, integrations, and downstream use, and provides the Software “as is” with no warranties. BareBits does not provide a hosted service unless you have a separate written Service Agreement.

