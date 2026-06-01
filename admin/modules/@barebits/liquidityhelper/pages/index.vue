<template>
  <!-- Defer all real rendering until the client mounts. Two reasons:
       1. vue-client-only's SSR placeholder calls `slots()` which
          forces evaluation of our children before returning empty
          comment-node placeholders. Somewhere deep in the v-tabs +
          chart.js tree, that evaluation hits an anonymous-component
          path and crashes SSR with the unhelpful
          "render function or template not defined in component:
          anonymous". Gating the entire content tree on a data flag
          that starts `false` and flips to `true` in mounted() means
          SSR only ever sees the tiny placeholder branch.
       2. The page is interactive admin tooling — there is no SEO
          loss from skipping SSR, the auth-gated `v-if` couldn't
          render anything meaningful server-side anyway, and any
          client-side runtime error now surfaces with a readable
          stack instead of being mangled by Vue's SSR. -->
  <v-container v-if="!clientReady">
    <v-progress-circular indeterminate color="primary" />
  </v-container>
  <v-container v-else>
    <h1 class="text-h4 mb-4">Liquidity Helper</h1>

    <!-- Non-admins see only this forbidden alert. The page route
         doesn't redirect — we want a clear message instead of a
         silent bounce to "/". Backend endpoints enforce the same
         check, so even if a non-admin reached this template via a
         dev shortcut they couldn't get data through the API. -->
    <v-alert
      v-if="!isAdmin"
      type="error"
      prominent
      icon="mdi-lock-alert"
      class="mt-4"
    >
      <h3 class="text-h6 mb-2">Admin access required</h3>
      <p class="mb-0">
        The Liquidity Helper dashboard, settings, and logs are
        available to Bitcart superusers only. If you believe you
        should have access, ask your server administrator to grant
        you superuser status on the
        <a href="/manage" class="white--text"><u>user management</u></a>
        page.
      </p>
    </v-alert>

    <!-- Wrapper div is intentional, NOT <template v-if>. vue-client-only's
         SSR placeholder maps each default-slot child to `h(false)`. A
         <template v-if> compiles to an inline array of VNodes (a fragment);
         the map callback hands the array to h(), which Vue treats as an
         anonymous component lookup and crashes with "render function or
         template not defined in component: anonymous". Keeping the
         conditional on a single real element makes each branch one VNode,
         which the placeholder mapper can handle. -->
    <div v-if="isAdmin"><v-tabs v-model="tab" background-color="primary" dark>
      <v-tab>Dashboard</v-tab>
      <v-tab>Settings</v-tab>
      <v-tab>Debug</v-tab>
      <v-tab>Logs</v-tab>
    </v-tabs>

    <v-tabs-items v-model="tab" class="mt-4">
      <!-- ──────────── Dashboard tab ──────────── -->
      <v-tab-item>
        <v-card flat>
          <v-card-text>
            <!-- Mode + preferred-cashout header. Two-line summary
                 showing the operator at a glance what management mode
                 the plugin is in (LSP vs automatic channel mgmt) and
                 which cashout rail the engine will try first (with
                 the other enabled rail as fallback in parens). The
                 on-chain destination address is shown truncated
                 (first4…last4) with the full value on hover and a
                 click-through to mempool.space when the network is
                 known. Backed by dashboard.cashout_summary; when
                 neither rail is configured the whole line is omitted. -->
            <div v-if="dashboard" class="dashboard-header mb-4">
              <div class="text-h6 dashboard-mode-title">
                {{ dashboard.liquidity_stats && dashboard.liquidity_stats.mode || "Liquidity helper" }}
              </div>
              <div
                v-if="dashboard.cashout_summary && dashboard.cashout_summary.primary"
                class="text-body-2 mt-1"
              >
                Preferred cashout:
                <v-icon small class="mr-1">{{ dashboard.cashout_summary.primary.method === 'lightning' ? 'mdi-flash' : 'mdi-link' }}</v-icon>
                {{ dashboard.cashout_summary.primary.method === 'lightning' ? 'Lightning' : 'On-chain' }}
                to
                <component
                  :is="cashoutDestComponent(dashboard.cashout_summary.primary)"
                  v-bind="cashoutDestProps(dashboard.cashout_summary.primary)"
                >{{ cashoutDestDisplay(dashboard.cashout_summary.primary) }}</component>
                <span v-if="dashboard.cashout_summary.fallback">
                  (fallback:
                  <v-icon small class="mr-1">{{ dashboard.cashout_summary.fallback.method === 'lightning' ? 'mdi-flash' : 'mdi-link' }}</v-icon>
                  {{ dashboard.cashout_summary.fallback.method === 'lightning' ? 'Lightning' : 'on-chain' }}
                  to
                  <component
                    :is="cashoutDestComponent(dashboard.cashout_summary.fallback)"
                    v-bind="cashoutDestProps(dashboard.cashout_summary.fallback)"
                  >{{ cashoutDestDisplay(dashboard.cashout_summary.fallback) }}</component>)
                </span>
              </div>
            </div>

            <!-- Top-up warning. Renders ONLY when the backend
                 returns a non-empty topup_warning.rows list. Each
                 row names the store + wallet that's below its
                 reserve floor along with the unlimited TOPUP_NAME
                 invoice address the operator can pay to refill the
                 wallet on demand. The BareBits-pays address is
                 surfaced only when debug_mode is on. -->
            <v-alert
              v-if="dashboard && dashboard.topup_warning && dashboard.topup_warning.rows.length"
              color="amber lighten-4"
              icon="mdi-cash-refund"
              class="mb-4 topup-warning"
            >
              <strong>
                Wallets {{ topupWalletNames }} for stores
                {{ topupStoreNames }} need a top-up so they can
                continue managing your liquidity.
              </strong>
              <div class="text-body-2 mt-2">
                You can top-up manually or wait for your next on-chain
                payment to come in.
              </div>
              <ul class="topup-address-list mt-2 mb-2">
                <li
                  v-for="row in dashboard.topup_warning.rows"
                  :key="row.store_id"
                  class="text-body-2"
                >
                  <strong>{{ row.store_name || row.store_id }}</strong>
                  ({{ row.wallet_name || row.wallet_id || "wallet" }}):
                  send
                  <strong><MoneyDisplay :sats="row.amount_sats" :usd="topupSatsToUsd(row.amount_sats)" :unit="displayUnit" /></strong>
                  to:
                  <div class="topup-address-line">
                    <component
                      :is="topupAddrComponent(row.own_address)"
                      v-bind="topupAddrProps(row.own_address)"
                    >{{ bareAddr(row.own_address) }}</component>
                  </div>
                  <div v-if="row.barebits_address" class="topup-address-line">
                    <span class="topup-address-label">debug BareBits address:</span>
                    <component
                      :is="topupAddrComponent(row.barebits_address)"
                      v-bind="topupAddrProps(row.barebits_address)"
                    >{{ bareAddr(row.barebits_address) }}</component>
                  </div>
                </li>
              </ul>
              <div class="text-body-2">
                Check below for liquidity stats — your wallets can
                continue to receive Lightning payments so long as
                each wallet has some inbound liquidity (at least one
                channel open).
              </div>
            </v-alert>

            <!-- Yellow shared-wallet warning. Renders ONLY when the
                 backend flips shared_wallet_warning=true. The
                 spec calls for a yellow background + ⚠️ emoji. -->
            <v-alert
              v-if="dashboard && dashboard.shared_wallet_warning"
              color="amber lighten-4"
              icon="mdi-alert"
              class="mb-4 shared-wallet-warning"
            >
              <strong>⚠️ Multiple stores share the same wallet.</strong>
              This can confuse the liquidity helper, produce inaccurate
              numbers on this dashboard, or result in higher paid network
              fees. We recommend giving each store its own dedicated
              <code>liquidityhelper</code> wallet.
            </v-alert>

            <!-- Health warnings (config sanity + LN-cashout staleness).
                 Backed by dashboard.health_warnings; each entry has
                 severity HIGH (red) or MEDIUM (yellow). Same conditions
                 are also emitted to decisions.log via log_decision so
                 operators can grep history of when warnings appeared
                 and cleared. -->
            <v-alert
              v-for="w in (dashboard && dashboard.health_warnings) || []"
              :key="w.id"
              :type="w.severity === 'HIGH' ? 'error' : 'warning'"
              :color="w.severity === 'HIGH' ? 'red lighten-4' : 'amber lighten-4'"
              :icon="w.severity === 'HIGH' ? 'mdi-alert-octagon' : 'mdi-alert'"
              text
              class="mb-2 health-warning"
            >
              <strong>{{ w.title }}</strong>
              <div class="text-body-2 mt-1">{{ w.message }}</div>
              <div class="text-caption mt-1 grey--text text--darken-1">
                category: {{ w.category }} · id: {{ w.id }}
              </div>
            </v-alert>

            <!-- Range selector + unit toggle + refresh -->
            <v-row align="center" class="mb-3">
              <v-col cols="12" sm="3">
                <v-select
                  v-model="dashboardRange"
                  :items="dashboardRangeOptions"
                  label="Time range"
                  hide-details
                  outlined
                  dense
                  @change="reloadDashboard"
                />
              </v-col>
              <v-col cols="12" sm="3">
                <!-- Unit toggle. Persists in localStorage so the
                     operator's preference survives reload. Default is
                     sats; USD continues to render in parentheses
                     regardless of the selected unit. -->
                <v-btn-toggle
                  v-model="displayUnit"
                  mandatory
                  dense
                  color="primary"
                  @change="persistDisplayUnit"
                >
                  <v-btn small value="sats">sats</v-btn>
                  <v-btn small value="btc">BTC</v-btn>
                </v-btn-toggle>
              </v-col>
              <v-col cols="12" sm="2">
                <v-btn
                  block
                  color="primary"
                  :loading="loadingDashboard"
                  @click="reloadDashboard(true)"
                >
                  <v-icon left>mdi-refresh</v-icon> Refresh
                </v-btn>
              </v-col>
              <v-col cols="12" sm="4" class="text-right text-caption">
                <span v-if="dashboard && dashboard.btc_usd_rate">
                  BTC/USD rate: ${{ formatNumber(dashboard.btc_usd_rate, 0) }}
                </span>
                <span v-else>BTC/USD rate: — (unavailable)</span>
              </v-col>
            </v-row>

            <v-alert v-if="dashboardError" type="error" text dense class="mb-2">
              {{ dashboardError }}
            </v-alert>

            <!-- LND-not-ready banner. Shown when at least one
                 btclnd-backed liquidityhelper wallet's LND daemon
                 isn't responding yet (typical for the first ~5-10s
                 after a bitcart container restart). Auto-refreshes
                 every 5s — the interval is set up in
                 startLndReadyPollIfNeeded / cleared in
                 stopLndReadyPoll, both wired to the dashboard
                 watcher below. -->
            <v-alert
              v-if="dashboard && !dashboard.lnd_ready"
              type="info"
              prominent
              text
              class="mb-2"
            >
              <div class="d-flex align-center">
                <v-progress-circular
                  indeterminate
                  size="20"
                  width="2"
                  class="mr-3"
                />
                <div>
                  <strong>Waiting for LND wallets to come online…</strong>
                  <div class="text-caption mt-1">
                    The dashboard will refresh automatically every 5
                    seconds until ready. Wallet IDs still spinning up:
                    <code
                      v-for="wid in dashboard.lnd_not_ready_wallets"
                      :key="wid"
                      class="mx-1"
                    >{{ wid }}</code>
                  </div>
                </div>
              </div>
            </v-alert>

            <!-- Empty-state when no liquidityhelper wallets are
                 configured. Renders BEFORE the loading spinner check
                 only when we've successfully fetched and got nothing.
                 Suppressed during lnd-not-ready since the stores list
                 is empty as a side effect of the skeleton response. -->
            <v-alert
              v-if="dashboard && dashboard.lnd_ready && dashboard.stores.length === 0 && !loadingDashboard"
              type="info"
              text
              dense
              class="mb-2"
            >
              No stores using a wallet named <code>liquidityhelper</code> were
              found. Create a wallet with that exact name to start seeing
              data here.
            </v-alert>

            <v-progress-circular
              v-if="loadingDashboard && !dashboard"
              indeterminate
            />

            <!-- Per-store cards. Suppressed during lnd-not-ready
                 (the skeleton response has empty stores and would
                 just render a row of empty cards behind the banner). -->
            <div v-if="dashboard && dashboard.lnd_ready">
              <StoreCard
                v-for="store in dashboard.stores"
                :key="store.store_id"
                :store="store"
                :include-inbound="true"
                :settings="settings"
                :initial-cc-pct="dashboard.cc_baseline_pct"
                :display-unit="displayUnit"
              />

              <!-- Summary section: only if more than one store. -->
              <v-card v-if="dashboard.summary" outlined class="mb-4 summary-card">
                <v-card-title @click="toggleSection('summary')" class="section-toggle">
                  <v-icon class="mr-2">
                    {{ isExpanded('summary') ? 'mdi-chevron-down' : 'mdi-chevron-right' }}
                  </v-icon>
                  Summary — all stores combined
                </v-card-title>
                <v-expand-transition>
                  <v-card-text v-show="isExpanded('summary')">
                    <StoreCard
                      :store="dashboard.summary"
                      :include-inbound="false"
                      :is-summary="true"
                      :settings="settings"
                      :initial-cc-pct="dashboard.cc_baseline_pct"
                      :display-unit="displayUnit"
                    />
                  </v-card-text>
                </v-expand-transition>
              </v-card>

              <!-- ─── Liquidity stats ─── -->
              <!-- One row per liquidityhelper-named wallet with its
                   inbound + outbound balance and active channel count,
                   plus a totals row at the bottom. Title shows which
                   liquidity-management mode is configured (LSP-managed
                   vs Automatic) so the operator instantly knows whether
                   new-channel acquisition is automatic or operator-
                   driven. Replaces the per-store inbound-liquidity row
                   that used to live on each StoreCard. -->
              <v-card v-if="dashboard.liquidity_stats" outlined class="mb-4">
                <v-card-title @click="toggleSection('liquidity_stats')" class="section-toggle">
                  <v-icon class="mr-2">
                    {{ isExpanded('liquidity_stats') ? 'mdi-chevron-down' : 'mdi-chevron-right' }}
                  </v-icon>
                  Liquidity stats
                  <v-chip
                    class="ml-3"
                    small
                    :color="dashboard.liquidity_stats.mode === 'Automatic channel management' ? 'warning' : 'info'"
                    outlined
                  >
                    {{ dashboard.liquidity_stats.mode }}
                  </v-chip>
                </v-card-title>
                <v-expand-transition>
                <v-card-text v-show="isExpanded('liquidity_stats')">
                  <p class="text-caption mb-2">
                    Per-wallet inbound and outbound liquidity (active
                    channels only). Only wallets named
                    <code>liquidityhelper</code> are counted — these are
                    the wallets the engine manages. The split bar shows
                    outbound (right, ability to send funds) vs inbound
                    (left, ability to receive funds) proportions; hover
                    any segment for exact sats.
                  </p>

                  <!-- Empty-state -->
                  <p
                    v-if="!dashboard.liquidity_stats.wallets.length"
                    class="text-caption grey--text mb-0"
                  >
                    No <code>liquidityhelper</code> wallets configured.
                  </p>

                  <!-- One sub-card per wallet. Within each: stores
                       label, aggregate split bar, then the indented
                       list of channels with their own per-channel bars
                       and peer labels. -->
                  <div
                    v-for="w in dashboard.liquidity_stats.wallets"
                    :key="w.wallet_id"
                    class="wallet-block mb-3"
                  >
                    <!-- Per-wallet header is the collapse toggle.
                         section-toggle gives the same hover affordance
                         used by every other expand/collapse on this
                         page; isExpanded keyed on "wallet_<id>" so
                         each wallet remembers its own open/closed
                         state independently. Default state is OPEN
                         (isExpanded returns true for any key not
                         explicitly toggled to false). -->
                    <div
                      class="d-flex align-center flex-wrap mb-1 section-toggle"
                      @click="toggleSection('wallet_' + w.wallet_id)"
                    >
                      <v-icon class="mr-1" small>
                        {{ isExpanded('wallet_' + w.wallet_id) ? 'mdi-chevron-down' : 'mdi-chevron-right' }}
                      </v-icon>
                      <strong>{{ w.wallet_name }}</strong>
                      <span class="text-caption grey--text ml-1">({{ w.wallet_short }})</span>
                      <span class="text-caption ml-3">
                        <span class="grey--text">Stores:</span>
                        <span v-if="w.store_names && w.store_names.length">
                          {{ w.store_names.join(", ") }}
                        </span>
                        <span v-else class="grey--text">— (no store currently uses this wallet)</span>
                      </span>
                      <v-spacer />
                      <span class="text-caption">
                        {{ w.active_channel_count }} active
                        channel{{ w.active_channel_count === 1 ? "" : "s" }}
                      </span>
                    </div>

                    <v-expand-transition>
                    <div v-show="isExpanded('wallet_' + w.wallet_id)">

                    <!-- Wallet aggregate split bar.
                         Two background segments side-by-side: the left
                         segment represents outbound (operator can send),
                         the right represents inbound (operator can
                         receive). Widths are proportional to the
                         outbound:inbound sat ratio. Native title
                         attributes give precise hover readouts.
                         A zero-balance wallet renders an empty grey bar. -->
                    <div
                      class="balance-bar"
                      :title="balanceBarTitle(w.outbound.sats, w.inbound.sats)"
                    >
                      <div
                        class="balance-bar-inbound"
                        :style="{ width: (100 - balanceBarPct(w.outbound.sats, w.inbound.sats)) + '%' }"
                      ></div>
                      <div
                        class="balance-bar-outbound"
                        :style="{ width: balanceBarPct(w.outbound.sats, w.inbound.sats) + '%' }"
                      ></div>
                    </div>
                    <div class="d-flex text-caption mt-1">
                      <span>
                        <v-icon x-small color="success">mdi-arrow-down</v-icon>
                        Inbound (receive):
                        <MoneyDisplay :money="w.inbound" :unit="displayUnit" />
                      </span>
                      <v-spacer />
                      <span>
                        <v-icon x-small style="color: #f7931a">mdi-arrow-up</v-icon>
                        Outbound (send):
                        <MoneyDisplay :money="w.outbound" :unit="displayUnit" />
                      </span>
                    </div>

                    <!-- Indented per-channel list. One v-simple-table
                         row per channel: peer (alias + truncated pubkey
                         + mempool LN-node link), then the split bar
                         column, then the channel point. Reuses the same
                         alias/pubkey UX as the closures table. -->
                    <div v-if="w.channels && w.channels.length" class="channel-list">
                      <v-simple-table dense class="elevation-0 channel-table">
                        <template #default>
                          <thead>
                            <tr>
                              <th>Peer</th>
                              <th>Balance</th>
                              <th class="text-right">Capacity</th>
                              <th>Channel point</th>
                            </tr>
                          </thead>
                          <tbody>
                            <tr v-for="ch in w.channels" :key="ch.channel_point">
                              <td>
                                <span v-if="!ch.peer_pubkey" class="grey--text">—</span>
                                <template v-else>
                                  {{ ch.peer_alias || "no name" }}
                                  <span
                                    v-if="channelLspName(ch)"
                                    class="grey--text text--darken-1"
                                  >({{ channelLspName(ch) }})</span>
                                  (<component
                                    :is="lnNodeComponent(ch.peer_pubkey)"
                                    v-bind="lnNodeProps(ch.peer_pubkey)"
                                  >{{ shortAddr(ch.peer_pubkey) }}</component>)
                                </template>
                              </td>
                              <td style="min-width: 220px;">
                                <div
                                  class="balance-bar balance-bar-sm"
                                  :title="balanceBarTitle(ch.local_balance, ch.remote_balance)"
                                >
                                  <div
                                    class="balance-bar-inbound"
                                    :style="{ width: (100 - balanceBarPct(ch.local_balance, ch.remote_balance)) + '%' }"
                                  ></div>
                                  <div
                                    class="balance-bar-outbound"
                                    :style="{ width: balanceBarPct(ch.local_balance, ch.remote_balance) + '%' }"
                                  ></div>
                                </div>
                                <div class="text-caption d-flex">
                                  <span><MoneyDisplay :sats="ch.remote_balance" :usd="null" :unit="displayUnit" /></span>
                                  <v-spacer />
                                  <span><MoneyDisplay :sats="ch.local_balance" :usd="null" :unit="displayUnit" /></span>
                                </div>
                              </td>
                              <td class="text-right text-caption">
                                <MoneyDisplay :sats="ch.capacity" :usd="null" :unit="displayUnit" />
                              </td>
                              <td class="text-caption">
                                <a
                                  v-if="channelPointUrl(ch.channel_point)"
                                  :href="channelPointUrl(ch.channel_point)"
                                  :title="ch.channel_point"
                                  target="_blank"
                                  rel="noopener noreferrer"
                                >{{ shortTxid(ch.channel_point) }}</a>
                                <span v-else :title="ch.channel_point">
                                  {{ shortTxid(ch.channel_point) }}
                                </span>
                              </td>
                            </tr>
                          </tbody>
                        </template>
                      </v-simple-table>
                    </div>
                    <p
                      v-else
                      class="text-caption grey--text mt-1 mb-0 channel-list"
                    >
                      No active channels.
                    </p>
                    </div>
                    </v-expand-transition>
                  </div>

                  <!-- Aggregate totals across every wallet. Kept
                       simple — just the totals line; the per-wallet
                       bars above already cover the visual story. -->
                  <div
                    v-if="dashboard.liquidity_stats.wallets.length"
                    class="totals-row pt-2 mt-2 d-flex flex-wrap text-body-2"
                  >
                    <strong>Total across wallets:</strong>
                    <v-spacer />
                    <span class="ml-3">
                      <v-icon x-small color="success">mdi-arrow-down</v-icon>
                      Inbound:
                      <strong><MoneyDisplay :money="dashboard.liquidity_stats.total_inbound" :unit="displayUnit" /></strong>
                    </span>
                    <span class="ml-3">
                      <v-icon x-small style="color: #f7931a">mdi-arrow-up</v-icon>
                      Outbound:
                      <strong><MoneyDisplay :money="dashboard.liquidity_stats.total_outbound" :unit="displayUnit" /></strong>
                    </span>
                    <span class="ml-3">
                      Channels: <strong>{{ dashboard.liquidity_stats.total_channel_count }}</strong>
                    </span>
                  </div>
                </v-card-text>
                </v-expand-transition>
              </v-card>

              <!-- ─── Recent activity tables ─── -->

              <v-card outlined class="mb-4">
                <v-card-title @click="toggleSection('recent_fee_payments')" class="section-toggle">
                  <v-icon class="mr-2">
                    {{ isExpanded('recent_fee_payments') ? 'mdi-chevron-down' : 'mdi-chevron-right' }}
                  </v-icon>
                  Recent fee payments
                </v-card-title>
                <v-expand-transition>
                <v-card-text v-show="isExpanded('recent_fee_payments')">
                  <p class="text-caption mb-2">
                    Developer and hosting/setup fee payments across all
                    <code>liquidityhelper</code> wallets, newest first
                    (capped at 100 entries).
                    <em>Destination shown is the CURRENT configured destination
                    and may differ from where the payment actually went historically.</em>
                  </p>
                  <v-data-table
                    :headers="feePaymentHeaders"
                    :items="dashboard.recent_fee_payments"
                    :items-per-page="10"
                    :no-data-text="'No fee payments yet.'"
                    dense
                    class="elevation-0"
                  >
                    <template #item.iso_date="{ item }">
                      <span class="text-caption">{{ item.iso_date }}</span>
                    </template>
                    <template #item.amount="{ item }">
                      <MoneyDisplay :sats="item.amount_sats" :usd="item.amount_usd" :unit="displayUnit" />
                    </template>
                    <template #item.fee_sats="{ item }">
                      <MoneyDisplay :sats="item.fee_sats" :usd="item.fee_usd" :unit="displayUnit" />
                    </template>
                    <template #item.fee_type="{ item }">
                      <v-chip x-small :color="feeTypeColor(item.fee_type)" outlined>
                        {{ item.fee_type }}
                      </v-chip>
                    </template>
                    <template #item.method="{ item }">
                      <v-icon small>{{ item.method === 'lightning' ? 'mdi-flash' : 'mdi-link' }}</v-icon>
                      {{ item.method === 'lightning' ? 'LN' : 'on-chain' }}
                    </template>
                    <template #item.destination="{ item }">
                      <a
                        v-if="isBitcoinAddress(item.destination) && mempoolAddrUrl(item.destination)"
                        :href="mempoolAddrUrl(item.destination)"
                        :title="item.destination"
                        target="_blank" rel="noopener noreferrer"
                        class="text-caption"
                      >{{ shortAddr(item.destination) }}</a>
                      <span
                        v-else-if="isBitcoinAddress(item.destination)"
                        :title="item.destination"
                        class="text-caption"
                      >
                        {{ shortAddr(item.destination) }}
                      </span>
                      <span v-else class="text-caption" :title="item.destination">{{ item.destination }}</span>
                    </template>
                    <template #item.txid="{ item }">
                      <a
                        v-if="item.txid && mempoolTxUrl(item.txid)"
                        :href="mempoolTxUrl(item.txid)"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="text-caption"
                      >{{ shortTxid(item.txid) }}</a>
                      <span v-else-if="item.txid" class="text-caption">
                        {{ shortTxid(item.txid) }}
                      </span>
                      <span v-else-if="item.payment_hash" class="text-caption grey--text">
                        {{ shortTxid(item.payment_hash) }} (LN)
                      </span>
                      <span v-else>—</span>
                    </template>
                    <!-- Totals row, rendered inside the table BELOW the
                         data rows but ABOVE the rows-per-page footer.
                         #body.append is the right slot for this — putting
                         the total div outside the v-data-table (the prior
                         layout) placed it after the footer instead of
                         before it. Suppressed when the table is empty;
                         otherwise an unattached "Total fees paid: 0"
                         appears below an empty "No fee payments yet." row. -->
                    <template v-if="dashboard.recent_fee_payments.length" #body.append>
                      <tr class="totals-row">
                        <td :colspan="feePaymentHeaders.length" class="text-left text-caption">
                          Total fees paid:
                          <strong><MoneyDisplay :sats="feePaymentsTotal.sats" :usd="feePaymentsTotal.usd" :unit="displayUnit" /></strong>
                        </td>
                      </tr>
                    </template>
                  </v-data-table>
                </v-card-text>
                </v-expand-transition>
              </v-card>

              <v-card outlined class="mb-4">
                <v-card-title @click="toggleSection('recent_cashouts')" class="section-toggle">
                  <v-icon class="mr-2">
                    {{ isExpanded('recent_cashouts') ? 'mdi-chevron-down' : 'mdi-chevron-right' }}
                  </v-icon>
                  Recent cashouts
                </v-card-title>
                <v-expand-transition>
                <v-card-text v-show="isExpanded('recent_cashouts')">
                  <p class="text-caption mb-2">
                    Cashout payments across all <code>liquidityhelper</code>
                    wallets, newest first (capped at 100 entries).
                  </p>
                  <v-data-table
                    :headers="paymentHeaders"
                    :items="dashboard.recent_cashouts"
                    :items-per-page="10"
                    :no-data-text="'No cashouts yet.'"
                    dense
                    class="elevation-0"
                  >
                    <template #item.iso_date="{ item }">
                      <span class="text-caption">{{ item.iso_date }}</span>
                    </template>
                    <template #item.amount="{ item }">
                      <MoneyDisplay :sats="item.amount_sats" :usd="item.amount_usd" :unit="displayUnit" />
                    </template>
                    <template #item.fee_sats="{ item }">
                      <MoneyDisplay :sats="item.fee_sats" :usd="item.fee_usd" :unit="displayUnit" />
                    </template>
                    <template #item.fee_type="{ item }">
                      <v-chip x-small color="primary" outlined>
                        {{ item.fee_type }}
                      </v-chip>
                    </template>
                    <template #item.method="{ item }">
                      <v-icon small>{{ item.method === 'lightning' ? 'mdi-flash' : 'mdi-link' }}</v-icon>
                      {{ item.method === 'lightning' ? 'LN' : 'on-chain' }}
                    </template>
                    <template #item.destination="{ item }">
                      <a
                        v-if="isBitcoinAddress(item.destination) && mempoolAddrUrl(item.destination)"
                        :href="mempoolAddrUrl(item.destination)"
                        :title="item.destination"
                        target="_blank" rel="noopener noreferrer"
                        class="text-caption"
                      >{{ shortAddr(item.destination) }}</a>
                      <span
                        v-else-if="isBitcoinAddress(item.destination)"
                        :title="item.destination"
                        class="text-caption"
                      >
                        {{ shortAddr(item.destination) }}
                      </span>
                      <span v-else class="text-caption" :title="item.destination">{{ item.destination }}</span>
                    </template>
                    <template #item.txid="{ item }">
                      <a
                        v-if="item.txid && mempoolTxUrl(item.txid)"
                        :href="mempoolTxUrl(item.txid)"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="text-caption"
                      >{{ shortTxid(item.txid) }}</a>
                      <span v-else-if="item.txid" class="text-caption">
                        {{ shortTxid(item.txid) }}
                      </span>
                      <span v-else-if="item.payment_hash" class="text-caption grey--text">
                        {{ shortTxid(item.payment_hash) }} (LN)
                      </span>
                      <span v-else>—</span>
                    </template>
                    <template v-if="dashboard.recent_cashouts.length" #body.append>
                      <tr class="totals-row">
                        <td :colspan="paymentHeaders.length" class="text-left text-caption">
                          Total cashouts:
                          <strong><MoneyDisplay :sats="cashoutsTotal.sats" :usd="cashoutsTotal.usd" :unit="displayUnit" /></strong>
                        </td>
                      </tr>
                    </template>
                  </v-data-table>
                </v-card-text>
                </v-expand-transition>
              </v-card>

              <v-card outlined class="mb-4">
                <v-card-title @click="toggleSection('recent_channel_closures')" class="section-toggle">
                  <v-icon class="mr-2">
                    {{ isExpanded('recent_channel_closures') ? 'mdi-chevron-down' : 'mdi-chevron-right' }}
                  </v-icon>
                  Recent channel closures
                </v-card-title>
                <v-expand-transition>
                <v-card-text v-show="isExpanded('recent_channel_closures')">
                  <p class="text-caption mb-2">
                    Channels the liquidity helper has closed (or initiated
                    closure of), newest first. The reason column explains why —
                    e.g. <code>AUDIT_FAILURE</code> includes the failing audit
                    criteria (<code>HIGH_FEE_RATE</code>,
                    <code>LOW_EFFECTIVE_DEGREE</code>,
                    <code>LONG_OUTAGE</code>, …);
                    <code>FORCE_CLOSE_AFTER_COOP_TIMEOUT</code> means the
                    cooperative close was escalated to a unilateral close
                    after the peer remained unresponsive.
                  </p>
                  <v-data-table
                    :headers="closureHeaders"
                    :items="dashboard.recent_channel_closures"
                    :items-per-page="10"
                    :no-data-text="'No channel closures recorded yet.'"
                    dense
                    class="elevation-0"
                  >
                    <template #item.iso_date="{ item }">
                      <span class="text-caption">{{ item.iso_date }}</span>
                    </template>
                    <template #item.peer="{ item }">
                      <!-- Peer column: human-readable alias (if LND's
                           gossip still knows the node) followed by the
                           truncated pubkey in parens. Hover reveals the
                           full pubkey; click links to the mempool.space
                           Lightning node page for the current network.
                           Closed-channel peers can vanish from gossip,
                           in which case alias is null and we render
                           "no name". -->
                      <span v-if="!item.peer_pubkey" class="text-caption grey--text">—</span>
                      <template v-else>
                        <span class="text-caption">
                          {{ item.peer_alias || "no name" }}
                          (<component
                            :is="lnNodeComponent(item.peer_pubkey)"
                            v-bind="lnNodeProps(item.peer_pubkey)"
                          >{{ shortAddr(item.peer_pubkey) }}</component>)
                        </span>
                      </template>
                    </template>
                    <template #item.channel_point="{ item }">
                      <span class="text-caption">{{ shortTxid(item.channel_point) }}</span>
                    </template>
                    <template #item.force_close_initiated="{ item }">
                      <v-chip
                        v-if="item.force_close_initiated"
                        x-small color="error" outlined
                      >
                        Force-closed
                      </v-chip>
                      <v-chip v-else x-small color="success" outlined>
                        Coop close
                      </v-chip>
                    </template>
                    <template #item.close_reason="{ item }">
                      <!-- Audit-failure reasons are stored as
                           "AUDIT_FAILURE: criterion1,criterion2,…"
                           by the engine (see liquidityhelper.py's
                           AUDIT_FAILURE close path). Split that into
                           a labeled category + a bullet list so the
                           operator can see at a glance WHY the audit
                           failed (low local balance vs. peer offline
                           vs. fee too high, etc.) instead of having
                           to read a comma-blob. Non-AUDIT_FAILURE
                           reasons fall through to a plain caption. -->
                      <template v-if="parseCloseReason(item.close_reason).category === 'AUDIT_FAILURE'">
                        <div class="text-caption">
                          <strong>AUDIT_FAILURE</strong>
                          <ul class="audit-reason-list mt-1 mb-0">
                            <li
                              v-for="(reason, idx) in parseCloseReason(item.close_reason).reasons"
                              :key="idx"
                            >
                              {{ humanizeAuditReason(reason) }}
                            </li>
                          </ul>
                        </div>
                      </template>
                      <span v-else class="text-caption">{{ item.close_reason }}</span>
                    </template>
                  </v-data-table>
                </v-card-text>
                </v-expand-transition>
              </v-card>

              <v-card outlined class="mb-4">
                <v-card-title @click="toggleSection('recent_lsp_orders')" class="section-toggle">
                  <v-icon class="mr-2">
                    {{ isExpanded('recent_lsp_orders') ? 'mdi-chevron-down' : 'mdi-chevron-right' }}
                  </v-icon>
                  Recent LSP orders
                </v-card-title>
                <v-expand-transition>
                <v-card-text v-show="isExpanded('recent_lsp_orders')">
                  <p class="text-caption mb-2">
                    LSP channel-order lifecycle, newest first.
                    <strong>State</strong>:
                    <code>ORDERED</code> → row created;
                    <code>PAID</code> → on-chain payment broadcast,
                    waiting for the LSP;
                    <code>COMPLETED</code> → LSP opened the channel
                    (see the Funding tx);
                    <code>FAILED</code> → LSP couldn't deliver and
                    (per LSPS1) refunded the payment.
                    <strong>Net cost</strong> = Paid − Refund; only
                    counts toward fee accounting once the refund tx
                    is confirmed on-chain.
                  </p>
                  <v-data-table
                    :headers="lspOrderHeaders"
                    :items="dashboard.recent_lsp_orders"
                    :items-per-page="10"
                    :no-data-text="'No LSP orders yet.'"
                    dense
                    class="elevation-0"
                  >
                    <template #item.iso_date="{ item }">
                      <span class="text-caption">{{ item.iso_date }}</span>
                    </template>
                    <template #item.state="{ item }">
                      <v-chip
                        v-if="item.state === 'COMPLETED'"
                        x-small color="success" outlined
                      >COMPLETED</v-chip>
                      <v-chip
                        v-else-if="item.state === 'FAILED'"
                        x-small color="error" outlined
                      >FAILED</v-chip>
                      <v-chip
                        v-else-if="item.state === 'PAID'"
                        x-small color="warning" outlined
                      >PAID</v-chip>
                      <v-chip
                        v-else x-small color="grey" outlined
                      >{{ item.state }}</v-chip>
                    </template>
                    <template #item.short_order_id="{ item }">
                      <span class="text-caption" :title="item.order_id">
                        {{ item.short_order_id }}…
                      </span>
                    </template>
                    <template #item.paid_sats="{ item }">
                      <span class="text-caption">
                        <MoneyDisplay :sats="item.paid_sats" :usd="item.paid_usd" :unit="displayUnit" />
                      </span>
                    </template>
                    <template #item.refund_sats="{ item }">
                      <span class="text-caption">
                        <MoneyDisplay :sats="item.refund_sats" :usd="item.refund_usd" :unit="displayUnit" />
                        <v-icon
                          v-if="item.state === 'FAILED' && !item.refund_observed_onchain"
                          x-small color="warning" class="ml-1"
                          :title="'LSP claimed a refund but it has not been confirmed on-chain yet — not yet credited in fee accounting.'"
                        >mdi-alert-circle-outline</v-icon>
                      </span>
                    </template>
                    <template #item.net_cost_sats="{ item }">
                      <span class="text-caption">
                        <MoneyDisplay :sats="item.net_cost_sats" :usd="item.net_cost_usd" :unit="displayUnit" />
                      </span>
                    </template>
                    <template #item.channel_funding_txid="{ item }">
                      <a
                        v-if="item.channel_funding_txid && mempoolTxUrl(item.channel_funding_txid)"
                        :href="mempoolTxUrl(item.channel_funding_txid)"
                        target="_blank" rel="noopener"
                        class="text-caption"
                      >{{ shortTxid(item.channel_funding_txid) }}</a>
                      <span v-else-if="item.channel_funding_txid" class="text-caption">
                        {{ shortTxid(item.channel_funding_txid) }}
                      </span>
                      <span v-else class="text-caption text--disabled">—</span>
                    </template>
                    <template #item.refund_txid="{ item }">
                      <a
                        v-if="item.refund_txid && mempoolTxUrl(item.refund_txid)"
                        :href="mempoolTxUrl(item.refund_txid)"
                        target="_blank" rel="noopener"
                        class="text-caption"
                      >{{ shortTxid(item.refund_txid) }}</a>
                      <span v-else-if="item.refund_txid" class="text-caption">
                        {{ shortTxid(item.refund_txid) }}
                      </span>
                      <span v-else class="text-caption text--disabled">—</span>
                    </template>
                  </v-data-table>
                </v-card-text>
                </v-expand-transition>
              </v-card>

              <v-card outlined class="mb-4">
                <v-card-title @click="toggleSection('recent_network_fees')" class="section-toggle">
                  <v-icon class="mr-2">
                    {{ isExpanded('recent_network_fees') ? 'mdi-chevron-down' : 'mdi-chevron-right' }}
                  </v-icon>
                  Recent network fees
                </v-card-title>
                <v-expand-transition>
                <v-card-text v-show="isExpanded('recent_network_fees')">
                  <p class="text-caption mb-2">
                    Every transaction across all <code>liquidityhelper</code>
                    wallets that paid an on-chain miner fee or a Lightning
                    routing fee, newest first (capped at 100 entries).
                    Includes developer/hosting fee payments, cashouts,
                    channel opens/closes, and LSP-order on-chain payments —
                    anything with a non-zero fee.
                  </p>
                  <v-data-table
                    :headers="networkFeeHeaders"
                    :items="dashboard.recent_network_fees"
                    :items-per-page="10"
                    :no-data-text="'No network fees yet.'"
                    dense
                    class="elevation-0"
                  >
                    <template #item.iso_date="{ item }">
                      <span class="text-caption">{{ item.iso_date }}</span>
                    </template>
                    <template #item.fee_sats="{ item }">
                      <MoneyDisplay :sats="item.fee_sats" :usd="item.fee_usd" :unit="displayUnit" />
                    </template>
                    <template #item.fee_rate_sat_per_vbyte="{ item }">
                      <span
                        v-if="item.fee_rate_sat_per_vbyte != null"
                        class="text-caption"
                      >{{ item.fee_rate_sat_per_vbyte.toFixed(2) }}</span>
                      <span v-else class="text-caption grey--text">—</span>
                    </template>
                    <template #item.amount_sats="{ item }">
                      <MoneyDisplay :sats="item.amount_sats" :usd="item.amount_usd" :unit="displayUnit" />
                    </template>
                    <template #item.category="{ item }">
                      <v-chip x-small :color="networkFeeCategoryColor(item.category)" outlined>
                        {{ item.category }}
                      </v-chip>
                    </template>
                    <template #item.method="{ item }">
                      <v-icon small>{{ item.method === 'lightning' ? 'mdi-flash' : 'mdi-link' }}</v-icon>
                      {{ item.method === 'lightning' ? 'LN' : 'on-chain' }}
                    </template>
                    <template #item.destination="{ item }">
                      <a
                        v-if="isBitcoinAddress(item.destination) && mempoolAddrUrl(item.destination)"
                        :href="mempoolAddrUrl(item.destination)"
                        :title="item.destination"
                        target="_blank" rel="noopener noreferrer"
                        class="text-caption"
                      >{{ shortAddr(item.destination) }}</a>
                      <span
                        v-else-if="isBitcoinAddress(item.destination)"
                        :title="item.destination"
                        class="text-caption"
                      >
                        {{ shortAddr(item.destination) }}
                      </span>
                      <span v-else class="text-caption" :title="item.destination">{{ item.destination }}</span>
                    </template>
                    <template #item.txid="{ item }">
                      <a
                        v-if="item.txid && mempoolTxUrl(item.txid)"
                        :href="mempoolTxUrl(item.txid)"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="text-caption"
                      >{{ shortTxid(item.txid) }}</a>
                      <span v-else-if="item.txid" class="text-caption">
                        {{ shortTxid(item.txid) }}
                      </span>
                      <span v-else-if="item.payment_hash" class="text-caption grey--text">
                        {{ shortTxid(item.payment_hash) }} (LN)
                      </span>
                      <span v-else>—</span>
                    </template>
                    <template v-if="dashboard.recent_network_fees.length" #body.append>
                      <tr class="totals-row">
                        <td :colspan="networkFeeHeaders.length" class="text-left text-caption">
                          Total network fees:
                          <strong><MoneyDisplay :sats="networkFeesTotal.sats" :usd="networkFeesTotal.usd" :unit="displayUnit" /></strong>
                        </td>
                      </tr>
                    </template>
                  </v-data-table>
                </v-card-text>
                </v-expand-transition>
              </v-card>
            </div>
          </v-card-text>
        </v-card>
      </v-tab-item>

      <!-- ──────────── Settings tab ──────────── -->
      <v-tab-item>
        <v-card flat>
          <v-card-text>
            <p class="text-body-2 mb-4">
              These knobs override the plugin's <code>config.py</code> defaults.
              Changes are applied live on the next tick — no Bitcart restart
              needed. Precedence (highest wins): this page &gt; environment
              variables &gt; <code>user_config.py</code> &gt; <code>config.py</code>.
              Hover the question mark next to each setting for a description
              pulled directly from <code>config.py</code>.
            </p>

            <!-- ──────────── Liquidity management mode ────────────
                 Top-level dropdown that drives the underlying
                 LIQUIDITY_DISABLED + AUTOMATIC_CHANNEL_CREATION_ENABLED
                 flags in one click. Those flags are excluded from the
                 expansion-panel list below so this dropdown is the
                 single authoritative entry point. -->
            <v-card v-if="settingsLoaded" outlined class="mb-4 mode-card">
              <v-card-text class="pa-3">
                <div class="d-flex align-center flex-wrap">
                  <span class="text-subtitle-2 mr-3">
                    Liquidity management mode
                  </span>
                  <v-select
                    v-model="liquidityModeUi"
                    :items="liquidityModeOptions"
                    :loading="liquidityModeSaving"
                    :disabled="liquidityModeSaving"
                    dense
                    outlined
                    hide-details
                    style="max-width: 240px;"
                    @change="onLiquidityModeChange"
                  />
                  <v-tooltip bottom max-width="420">
                    <template #activator="{ on }">
                      <v-icon
                        small
                        color="grey"
                        class="ml-2"
                        v-on="on"
                      >mdi-help-circle-outline</v-icon>
                    </template>
                    <span>
                      <strong>LSP</strong>: channel acquisition is delegated to
                      Zeus or Megalithic over LSPS1 (default).
                      <br>
                      <strong>Automatic</strong>: the plugin opens channels
                      directly to peers selected from a locally curated
                      database (no LSP intermediary).
                      <br>
                      <strong>Disabled</strong>: pause the tick loop entirely.
                      Cashouts, fee payments, and channel creation all stop
                      until the mode is changed back. Dashboard endpoints
                      keep serving.
                    </span>
                  </v-tooltip>
                  <v-spacer />
                  <span
                    v-if="liquidityModeSaveError"
                    class="text-caption error--text ml-2"
                  >
                    {{ liquidityModeSaveError }}
                  </span>
                </div>
              </v-card-text>
            </v-card>

            <div v-if="settingsLoaded && schemaGroups.length">
              <!-- One v-expansion-panel per config.py group.
                   v-model="openSettingsPanels" is an ARRAY (multiple
                   panels can be open at once); accordion isn't set so
                   opening one doesn't close the others — the operator
                   often wants to see two adjacent groups at once when
                   tuning related knobs. Initial state: all closed,
                   keeping the page compact on first open. -->
              <v-expansion-panels
                v-model="openSettingsPanels"
                multiple
                accordion
                class="settings-panels"
              >
                <v-expansion-panel
                  v-for="group in schemaGroups"
                  :key="group.group"
                >
                  <v-expansion-panel-header>
                    <span class="text-h6 d-flex align-center">
                      <!-- Warning icon when ANY setting in this group
                           is referenced by an active dashboard health
                           warning. Operator sees this from a glance at
                           the closed list and knows which group to
                           expand. The tooltip lists the offending
                           setting names so we don't make the operator
                           hunt inside the group for the one that's
                           broken. Color follows the dashboard banner
                           convention: error for HIGH, warning for
                           MEDIUM, error wins when both present. -->
                      <v-tooltip v-if="groupWarningInfo(group).count > 0" bottom max-width="420">
                        <template #activator="{ on }">
                          <v-icon
                            :color="groupWarningInfo(group).color"
                            class="mr-2"
                            v-on="on"
                          >mdi-alert</v-icon>
                        </template>
                        <span>
                          {{ groupWarningInfo(group).count }} active
                          warning{{ groupWarningInfo(group).count === 1 ? "" : "s" }}
                          in this group:
                          <br>
                          {{ groupWarningInfo(group).settings.join(", ") }}
                        </span>
                      </v-tooltip>
                      {{ group.group }}
                    </span>
                    <template #actions>
                      <span class="text-caption grey--text mr-2">
                        {{ group.settings.length }} setting{{ group.settings.length === 1 ? "" : "s" }}
                      </span>
                      <v-icon>mdi-chevron-down</v-icon>
                    </template>
                  </v-expansion-panel-header>
                  <v-expansion-panel-content>
                    <div
                      v-for="entry in group.settings"
                      :key="entry.name"
                      class="setting-row d-flex align-start mb-2"
                    >
                      <!-- The question-mark tooltip. The text comes
                           straight from the description block above the
                           setting in config.py via the /settings/schema
                           endpoint. -->
                      <v-tooltip bottom max-width="500">
                        <template #activator="{ on }">
                          <v-icon
                            small
                            class="mr-2 mt-2"
                            color="grey"
                            v-on="on"
                          >mdi-help-circle-outline</v-icon>
                        </template>
                        <span>{{ entry.description || "(no description)" }}</span>
                      </v-tooltip>
                      <div class="flex-grow-1">
                        <PolicySetting
                          :title="entry.name"
                          :detail="''"
                          :type="guessType(settings[entry.name])"
                          :what="entry.name"
                          policy-url="/plugins/settings/liquidityhelper"
                          :initial-value="settings[entry.name]"
                        />
                      </div>
                    </div>
                  </v-expansion-panel-content>
                </v-expansion-panel>
              </v-expansion-panels>
            </div>
            <v-progress-circular v-else indeterminate />
          </v-card-text>
        </v-card>
      </v-tab-item>

      <!-- ──────────── Debug tab ──────────── -->
      <!-- Wallet-by-wallet diagnostic surface: lists every
           liquidityhelper-named wallet with its store associations,
           the timestamp of the latest tx, and per-wallet Export-CSV
           and Backup action buttons. Both actions trigger a
           confirmation modal first — the backup zip in particular
           contains the seed phrase in plaintext, so we want the
           operator to explicitly acknowledge before downloading. -->
      <v-tab-item>
        <v-card flat>
          <v-card-text>
            <p class="text-body-2 mb-3">
              Per-wallet diagnostics for every wallet named
              <code>liquidityhelper</code>. Export CSV produces a
              complete transaction history (on-chain + Lightning);
              Backup produces a zip containing the seed phrase and
              the channel-backup files required for disaster recovery.
            </p>

            <v-alert
              v-if="debugError"
              type="error"
              text
              dense
              class="mb-3"
            >
              {{ debugError }}
            </v-alert>

            <v-data-table
              :headers="debugWalletHeaders"
              :items="debugWallets"
              :items-per-page="20"
              :no-data-text="loadingDebugWallets ? 'Loading…' : 'No liquidityhelper wallets configured.'"
              :loading="loadingDebugWallets"
              dense
              class="elevation-0"
            >
              <template #item.wallet_id="{ item }">
                <span class="text-caption">
                  {{ item.wallet_name }}
                  <span class="grey--text">({{ item.wallet_short }})</span>
                </span>
              </template>
              <template #item.currency="{ item }">
                <v-chip x-small outlined :color="item.currency === 'btclnd' ? 'info' : 'success'">
                  {{ item.currency }}
                </v-chip>
              </template>
              <template #item.stores="{ item }">
                <span class="text-caption">
                  <span v-if="item.stores.length === 0" class="grey--text">— (no stores)</span>
                  <span v-else>{{ item.stores.join(', ') }}</span>
                </span>
              </template>
              <template #item.last_tx_iso="{ item }">
                <span class="text-caption">{{ item.last_tx_iso }}</span>
              </template>
              <template #item.actions="{ item }">
                <v-btn
                  x-small
                  outlined
                  color="primary"
                  class="mr-1"
                  :disabled="debugActionInFlight"
                  @click="confirmDebugAction('csv', item)"
                >
                  <v-icon left x-small>mdi-download</v-icon>
                  Export CSV
                </v-btn>
                <v-btn
                  x-small
                  outlined
                  color="error"
                  :disabled="debugActionInFlight"
                  @click="confirmDebugAction('backup', item)"
                >
                  <v-icon left x-small>mdi-shield-key</v-icon>
                  Backup
                </v-btn>
              </template>
            </v-data-table>

            <v-btn
              small
              outlined
              class="mt-3"
              :loading="loadingDebugWallets"
              @click="loadDebugWallets"
            >
              <v-icon left small>mdi-refresh</v-icon>
              Refresh
            </v-btn>
          </v-card-text>
        </v-card>
      </v-tab-item>

      <!-- ──────────── Logs tab ──────────── -->
      <v-tab-item>
        <v-card flat>
          <v-card-text>
            <!-- Log export buttons. Sit above the stream selector so
                 they are visible even before the operator picks a
                 stream. Both buttons go through the warning modal
                 because the zip's contents are operator-sensitive. -->
            <v-row align="center" class="mb-3" no-gutters>
              <v-col cols="12" sm="6" md="4" class="pr-sm-2 mb-2 mb-sm-0">
                <v-btn
                  block
                  outlined
                  color="primary"
                  :loading="debugActionInFlight && debugPendingAction && debugPendingAction.kind === 'logs_engine'"
                  @click="confirmLogExport('engine')"
                >
                  <v-icon left>mdi-download-outline</v-icon>
                  Export liquidityhelper log
                </v-btn>
              </v-col>
              <v-col cols="12" sm="6" md="4">
                <v-btn
                  block
                  outlined
                  color="primary"
                  :loading="debugActionInFlight && debugPendingAction && debugPendingAction.kind === 'logs_all'"
                  @click="confirmLogExport('all')"
                >
                  <v-icon left>mdi-download-multiple-outline</v-icon>
                  Export all logs
                </v-btn>
              </v-col>
            </v-row>

            <v-row align="center" class="mb-3">
              <v-col cols="12" sm="4">
                <v-select
                  v-model="selectedStream"
                  :items="streamItems"
                  label="Log stream"
                  hide-details
                  outlined
                  dense
                  @change="reloadLogs"
                />
              </v-col>
              <v-col cols="12" sm="3">
                <v-select
                  v-model="tailSize"
                  :items="tailSizeOptions"
                  label="Lines"
                  hide-details
                  outlined
                  dense
                  @change="reloadLogs"
                />
              </v-col>
              <v-col cols="12" sm="3">
                <v-btn
                  block
                  color="primary"
                  :loading="loadingLogs"
                  @click="reloadLogs"
                >
                  <v-icon left>mdi-refresh</v-icon> Refresh
                </v-btn>
              </v-col>
              <v-col cols="12" sm="2">
                <v-switch
                  v-model="autoRefresh"
                  label="Auto"
                  hide-details
                  dense
                />
              </v-col>
            </v-row>

            <!-- Log-level filter. Multi-select chip group so the
                 operator can toggle individual levels on/off. Default
                 set EXCLUDES Debug — engine logs are noisy at DEBUG
                 and operators rarely want to scroll through them on
                 a routine check. Filter is applied client-side over
                 the most recent `tailSize` lines pulled by reloadLogs
                 (no backend round-trip when toggling a level). -->
            <v-row align="center" class="mb-3" no-gutters>
              <v-col cols="auto" class="mr-3 text-caption grey--text">
                Show levels:
              </v-col>
              <v-col>
                <v-chip-group
                  v-model="enabledLogLevels"
                  multiple
                  column
                  active-class="primary--text"
                >
                  <v-chip
                    v-for="lvl in allLogLevels"
                    :key="lvl"
                    :value="lvl"
                    small
                    outlined
                    filter
                  >
                    {{ lvl }}
                  </v-chip>
                </v-chip-group>
              </v-col>
            </v-row>

            <v-alert
              v-if="logTruncated"
              type="info"
              text
              dense
              class="mb-2"
            >
              Showing only the last {{ logLines.length }} lines — earlier
              entries are still on disk in
              <code>{{ selectedStreamPath }}</code>.
            </v-alert>

            <v-alert
              v-if="logError"
              type="error"
              text
              dense
              class="mb-2"
            >
              {{ logError }}
            </v-alert>

            <!-- Debug-mode controls. Only visible when DEBUG_MODE is
                 enabled in settings — when off, the tick loop cycles
                 continuously and a "Run one tick" button has nothing
                 useful to do. -->
            <v-alert
              v-if="settings && settings.DEBUG_MODE"
              color="indigo lighten-5"
              dense
              class="mb-2 debug-mode-alert"
            >
              <div class="d-flex align-center justify-space-between">
                <div>
                  <strong>🛠 DEBUG_MODE is ON.</strong>
                  The tick loop is paused; it will only run one iteration
                  each time you click the button.
                </div>
                <v-btn
                  small
                  color="primary"
                  :loading="loadingDebugTick"
                  @click="triggerDebugTick"
                >
                  <v-icon left small>mdi-play-circle-outline</v-icon>
                  Run one tick
                </v-btn>
              </div>
              <v-alert
                v-if="debugTickStatus"
                :type="debugTickStatus.triggered ? 'success' : 'warning'"
                dense
                text
                class="mt-2 mb-0"
              >
                {{ debugTickStatus.message }}
              </v-alert>
            </v-alert>

            <pre class="log-view">{{ logText || "(no log entries yet)" }}</pre>
          </v-card-text>
        </v-card>
      </v-tab-item>
    </v-tabs-items>

    <!-- Confirmation modal shared between Export CSV and Backup
         actions. Backup in particular contains a plaintext seed
         phrase, so we want a deliberate confirmation step every
         time — accidental clicks shouldn't leak the seed via the
         operator's browser history / downloads folder. -->
    <v-dialog v-model="debugDialogOpen" max-width="540">
      <v-card>
        <v-card-title class="text-h6">
          {{ debugDialogConfig.title }}
        </v-card-title>
        <v-card-text>
          <p>{{ debugDialogConfig.body }}</p>
          <v-alert
            v-if="debugDialogConfig.warning"
            type="warning"
            dense
            text
            class="mb-0 mt-3"
          >
            {{ debugDialogConfig.warning }}
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn text @click="debugDialogOpen = false">Cancel</v-btn>
          <v-btn
            :color="debugDialogConfig.confirmColor || 'primary'"
            :loading="debugActionInFlight"
            @click="executeDebugAction"
          >
            {{ debugDialogConfig.confirmLabel || 'Continue' }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
    </div>
  </v-container>
</template>

<script>
import PolicySetting from "@/components/PolicySetting.vue"
import StoreCard from "../components/StoreCard.vue"
import MoneyDisplay from "../components/MoneyDisplay.vue"
import {
  formatNumber,
  formatBtcSats,
  formatUsd,
  formatPct,
  formatAmount,
  formatAmountFromSats,
  guessType,
} from "../components/format.js"

// Auto-refresh cadence for the Logs tab. 3s is a comfortable balance
// between "feels live" and "doesn't hammer the backend with no-op tails".
const AUTO_REFRESH_INTERVAL_MS = 3000

// localStorage key for the dashboard's BTC-vs-sats unit preference.
// Survives reload but is per-browser per-user — no server-side persist.
const DISPLAY_UNIT_STORAGE_KEY = "liquidityhelper.dashboard.displayUnit"

function _loadDisplayUnit() {
  try {
    const v = window.localStorage.getItem(DISPLAY_UNIT_STORAGE_KEY)
    if (v === "btc" || v === "sats") return v
  } catch (_) {
    // Storage disabled / privacy mode — fall through to default.
  }
  return "sats"
}


export default {
  components: { PolicySetting, StoreCard, MoneyDisplay },
  // Use the default layout (drawer + app bar). The "admin" layout
  // wraps every page in bitcart's server-management nav-toolbar,
  // which is for User Management / Server Logs / etc. — not the
  // right grouping for our plugin's dashboard. The default layout
  // is what's used for /, /stores, /invoices, etc.
  // Note: no `middleware: "superuserOnly"` here. We could enable it
  // for a silent redirect to "/", but the in-component guard below
  // gives users a clear "you are not an admin" message instead of
  // dropping them on the dashboard with no explanation. The global
  // `auth` middleware (registered in nuxt.config.js) still runs and
  // bounces unauthenticated users to /login, so by the time this
  // component mounts the user is logged in — we just need to check
  // is_superuser. The backend's plugin endpoints (/api/plugins/
  // liquidityhelper/*) enforce auth independently, so this guard is
  // UX, not the security boundary.
  data() {
    return {
      tab: 0,

      // Dashboard tab state.
      dashboard: null,         // last fetched DashboardResponse payload
      dashboardRange: "all",
      dashboardRangeOptions: [
        { text: "All time", value: "all" },
        { text: "Last 30 days", value: "30" },
        { text: "Last 90 days", value: "90" },
        { text: "Last 365 days", value: "365" },
      ],
      loadingDashboard: false,
      dashboardError: null,

      // Collapsible-section state for the Dashboard tab. Each key
      // identifies one section (Summary, Liquidity stats, the
      // Recent-* tables, and per-store cards keyed by store_id).
      // Defaults to {} = no key set; isExpanded() treats absence-of-
      // key as "expanded" so newly-added sections automatically
      // start expanded without the dashboard needing to enumerate
      // every key. Per-tab UI preference — NOT persisted to
      // localStorage; the operator's collapse choices reset on each
      // page reload, which keeps the default-expanded contract
      // unambiguous for first-time-after-deploy operators.
      expandedSections: {},
      // Display-unit toggle. "sats" (default) or "btc". Initialized
      // from localStorage so the operator's preference survives a
      // reload. USD continues to render in parentheses regardless of
      // the selected unit — the toggle only controls the main unit.
      displayUnit: _loadDisplayUnit(),
      // setInterval handle for the LND-not-ready auto-refresh poll.
      // Set when the dashboard returns lnd_ready=false, cleared as
      // soon as it flips true (or on unmount). 5s cadence per the
      // banner copy — long enough to avoid hammering the backend
      // while LND finishes spinning up, short enough that the
      // operator's first refresh after LND comes online is fast.
      lndReadyPollHandle: null,
      // Static column definitions for the recent-activity v-data-tables.
      // Defined in data() (not as a computed) so Vuetify gets a stable
      // reference and doesn't re-create header cells on every re-render.
      // Used by the Recent cashouts table — keeps the "Fee paid"
      // column because for cashouts the fee is a meaningful overhead
      // distinct from the cashout amount.
      paymentHeaders: [
        { text: "Date", value: "iso_date", width: 160 },
        { text: "Amount", value: "amount" },
        { text: "Fee paid", value: "fee_sats" },
        { text: "Type", value: "fee_type", width: 110 },
        { text: "Method", value: "method", width: 110 },
        { text: "Destination", value: "destination" },
        { text: "Tx / hash", value: "txid" },
      ],
      // Used by the Recent fee payments table — no "Fee paid" column
      // because for dev/hosting-fee sends the operator's focus is the
      // amount delivered to the destination; the network fee for
      // sending it is a small overhead that already appears in the
      // Recent network fees table.
      feePaymentHeaders: [
        { text: "Date", value: "iso_date", width: 160 },
        { text: "Amount", value: "amount" },
        { text: "Type", value: "fee_type", width: 110 },
        { text: "Method", value: "method", width: 110 },
        { text: "Destination", value: "destination" },
        { text: "Tx / hash", value: "txid" },
      ],
      closureHeaders: [
        { text: "Date", value: "iso_date", width: 160 },
        { text: "Peer", value: "peer", sortable: false },
        { text: "Channel point", value: "channel_point" },
        { text: "Outcome", value: "force_close_initiated", width: 130 },
        { text: "Attempts", value: "cooperative_close_attempts", width: 90 },
        { text: "Reason", value: "close_reason" },
      ],
      lspOrderHeaders: [
        { text: "Date", value: "iso_date", width: 160 },
        { text: "Provider", value: "provider", width: 110 },
        { text: "State", value: "state", width: 110 },
        { text: "Order ID", value: "short_order_id", width: 120 },
        { text: "Paid", value: "paid_sats", width: 110 },
        { text: "Refund", value: "refund_sats", width: 110 },
        { text: "Net cost", value: "net_cost_sats", width: 110 },
        { text: "Funding tx", value: "channel_funding_txid" },
        { text: "Refund tx", value: "refund_txid" },
      ],
      networkFeeHeaders: [
        { text: "Date", value: "iso_date", width: 160 },
        { text: "Category", value: "category", width: 130 },
        { text: "Fee paid", value: "fee_sats" },
        // sat/vbyte — populated server-side for on-chain LND-source
        // rows via the BIP141 vsize of raw_tx_hex. Blank for LN
        // (no concept) and Electrum-source on-chain rows (raw_tx_hex
        // isn't returned by Electrum's onchain_history endpoint).
        { text: "Sat/vbyte", value: "fee_rate_sat_per_vbyte", width: 110, align: "end" },
        { text: "Method", value: "method", width: 110 },
        { text: "Destination", value: "destination" },
        { text: "Tx / hash", value: "txid" },
      ],

      // Debug tab — per-wallet diagnostic table.
      debugWalletHeaders: [
        { text: "Wallet", value: "wallet_id" },
        { text: "Currency", value: "currency", width: 110 },
        { text: "Store(s)", value: "stores" },
        { text: "Last tx", value: "last_tx_iso", width: 200 },
        { text: "Actions", value: "actions", width: 240, sortable: false },
      ],
      debugWallets: [],
      loadingDebugWallets: false,
      debugError: null,
      // Confirmation-modal state. `debugDialogConfig` is populated
      // by confirmDebugAction with the title/body/warning text for
      // the chosen action; `debugPendingAction` carries the action
      // type + target wallet so executeDebugAction can dispatch.
      debugDialogOpen: false,
      debugDialogConfig: {
        title: "", body: "", warning: "",
        confirmLabel: "", confirmColor: "primary",
      },
      debugPendingAction: null,  // { kind: 'csv'|'backup', wallet: { ... } }
      debugActionInFlight: false,

      // Settings tab state.
      settings: {},
      settingsLoaded: false,
      // Mode-dropdown UI state. liquidityModeUi is bound to the
      // v-select; the read direction is computed from `settings` via
      // syncLiquidityModeUi (called after every load and successful
      // save). liquidityModeSaving gates the input + spinner during
      // the round-trip; liquidityModeSaveError surfaces the last
      // failure inline so the operator isn't left guessing why their
      // change didn't stick.
      liquidityModeUi: "lsp",
      liquidityModeSaving: false,
      liquidityModeSaveError: "",
      liquidityModeOptions: [
        { text: "LSP", value: "lsp" },
        { text: "Automatic", value: "automatic" },
        { text: "Disabled", value: "disabled" },
      ],
      // schemaGroups comes from /api/plugins/liquidityhelper/settings/schema
      // and carries the parsed group + description for each setting.
      // Shape: [{group, settings: [{name, description, default}]}]
      schemaGroups: [],
      // v-model for the settings-tab expansion panels. Array of open
      // panel indices. Empty = all closed (the default we want — the
      // page used to scroll for ages, so collapsed-by-default is what
      // most operators benefit from).
      openSettingsPanels: [],

      // Logs tab state.
      streams: [],          // [{ name, path, size_bytes, exists }]
      selectedStream: "operational",
      tailSize: 500,
      tailSizeOptions: [100, 250, 500, 1000, 2500, 5000],
      logLines: [],
      logTruncated: false,
      logError: null,
      loadingLogs: false,
      // Default ON. Operators glancing at the Logs tab almost always
      // want it to keep updating without having to click Refresh —
      // and the explicit toggle still lets them pause it for analysis.
      autoRefresh: true,
      autoRefreshTimer: null,
      // Available log levels (in increasing severity). The set is
      // what Python's logging module emits in our formatter
      // ("%(levelname)s") — see liquidityhelper.py's logger setup.
      // DEBUG is intentionally omitted from the default selection;
      // operators can add it back via the chip group.
      allLogLevels: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
      enabledLogLevels: ["INFO", "WARNING", "ERROR", "CRITICAL"],
      // Debug-mode (operator single-step) state.
      loadingDebugTick: false,
      debugTickStatus: null,    // {triggered, message} from POST /debug/run_once

      // Gate for the SSR skip. Stays false on the server, flips true
      // once we hit mounted() on the client. See the wrapping
      // v-if/v-else comment in the template for why this exists.
      clientReady: false,
    }
  },
  computed: {
    // Gates the entire page UI and the initial API fetches.
    // bitcart's global auth middleware ensures `$auth.user` is
    // populated by the time we render, so a missing user object
    // here is treated the same as a non-superuser (forbidden).
    isAdmin() {
      return Boolean(this.$auth.user && this.$auth.user.is_superuser)
    },
    // Per-table totals shown under each recent-activity v-data-table.
    // Totals are over ALL rows in the response (capped at 100 by the
    // backend), not just the visible 10-per-page page — that's the
    // "total over this time range" semantic operators expect.
    //
    // USD is summed only over rows where amount_usd / fee_usd is
    // non-null. When ANY row has a usd value we surface the partial
    // sum so the operator sees the dollar magnitude; when ALL are
    // null we propagate null so formatAmountFromSats renders "$—".
    feePaymentsTotal() {
      return this._sumRowsField(this.dashboard?.recent_fee_payments || [], "amount")
    },
    cashoutsTotal() {
      return this._sumRowsField(this.dashboard?.recent_cashouts || [], "amount")
    },
    networkFeesTotal() {
      return this._sumRowsField(this.dashboard?.recent_network_fees || [], "fee")
    },
    // Comma-joined store / wallet names for the top-up warning's
    // headline sentence. Empty when there are no rows (the v-alert is
    // already hidden in that case via v-if).
    topupWalletNames() {
      const rows = (this.dashboard && this.dashboard.topup_warning && this.dashboard.topup_warning.rows) || []
      return rows.map(r => r.wallet_name || r.wallet_id || "wallet").join(", ")
    },
    topupStoreNames() {
      const rows = (this.dashboard && this.dashboard.topup_warning && this.dashboard.topup_warning.rows) || []
      return rows.map(r => r.store_name || r.store_id || "store").join(", ")
    },
    streamItems() {
      // Vuetify select items: [{ text, value }]. The text shows the
      // friendly name plus a hint about empty state so the operator
      // knows up front whether a stream has anything to show.
      return this.streams.map((s) => ({
        text:
          s.name === "operational" ? "Operational (full firehose)"
          : s.name === "info" ? "Events (INFO+ only, longer retention)"
          : s.name === "decisions" ? "Decisions (audit log)"
          : s.name,
        value: s.name,
        disabled: !s.exists,
      }))
    },
    selectedStreamPath() {
      const s = this.streams.find((s) => s.name === this.selectedStream)
      return s ? s.path : ""
    },
    logText() {
      // Filter on the level token in our log format:
      // "YYYY-MM-DD HH:MM:SS - logger.name - LEVEL - message ...".
      // If a line doesn't match the expected shape (e.g. multi-line
      // tracebacks where only the first line has the level), we
      // include it unconditionally — dropping it would orphan the
      // attached lines and make tracebacks unreadable.
      const enabled = new Set(this.enabledLogLevels)
      // Fast path: if all levels are selected, no filtering needed.
      if (this.allLogLevels.every(l => enabled.has(l))) {
        return this.logLines.join("\n")
      }
      const out = []
      let includeContinuation = true
      const lineRe = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[^ ]* - [^ ]+ - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - /
      for (const line of this.logLines) {
        const m = lineRe.exec(line)
        if (m) {
          // This line has a level token. Decide whether to include
          // it AND any follow-on continuation lines (tracebacks etc).
          includeContinuation = enabled.has(m[1])
          if (includeContinuation) out.push(line)
        } else {
          // Continuation of the previous log record (no header):
          // include if and only if the parent line was included.
          if (includeContinuation) out.push(line)
        }
      }
      return out.join("\n")
    },
  },
  watch: {
    autoRefresh(on) {
      if (on) this.startAutoRefresh()
      else this.stopAutoRefresh()
    },
    // Bind the LND-readiness auto-refresh poll to the dashboard's
    // lnd_ready flag. Each new dashboard payload triggers this; we
    // start the 5s poll on the first not-ready response and stop it
    // as soon as the engine reports ready (or the dashboard reloads
    // without a payload).
    "dashboard.lnd_ready": {
      immediate: true,
      handler(ready) {
        if (this.dashboard && ready === false) {
          this.startLndReadyPoll()
        } else {
          this.stopLndReadyPoll()
        }
      },
    },
    // Tab-change side effects:
    //   - Tab 0 = Dashboard: force-refresh whenever the operator
    //     returns to it. The most common reason to leave Dashboard
    //     is "I saw a warning, let me go fix it in Settings"; coming
    //     back without a refresh would show the stale pre-save
    //     health_warnings and net_fees rows. Pass `true` to bypass
    //     the dashboard endpoint's 60s cache so a setting saved
    //     seconds ago is reflected immediately. Skip the very-first
    //     transition into Dashboard since mounted() already loaded
    //     it; we'd be issuing a double-fetch in that case.
    //   - Tab 2 = Debug: lazy-load on first open only. Loading on
    //     every switch would be redundant for this slow-changing
    //     surface; the tab has its own Refresh button.
    tab(newTab, oldTab) {
      if (newTab === 0 && oldTab !== undefined && !this.loadingDashboard) {
        this.reloadDashboard(true)
      }
      if (newTab === 2 && this.debugWallets.length === 0 && !this.loadingDebugWallets) {
        this.loadDebugWallets()
      }
    },
  },
  async mounted() {
    // Flip the SSR-skip flag so the real template branch renders.
    // Done in mounted() (not beforeMount) because mounted is the
    // first Vue lifecycle hook that is guaranteed to be client-only
    // — beforeMount can fire on the server in some Nuxt code paths.
    this.clientReady = true

    // Skip all initial data loads for non-admins. The template gates
    // rendering with v-if="isAdmin"; firing these fetches would just
    // generate noise in the console (the backend would 401/403) and
    // briefly populate state that we never render. Cleaner to bail.
    if (!this.isAdmin) return

    // Load settings (matches the per-plugin block on /manage/policies)
    // AND the schema (groups + tooltips) in parallel — they're
    // independent and both required to render the Settings tab.
    try {
      const [settingsResp, schemaResp] = await Promise.all([
        this.$axios.get("/plugins/settings/liquidityhelper"),
        this.$axios.get("/plugins/liquidityhelper/settings/schema"),
      ])
      this.settings = settingsResp.data || {}
      this.schemaGroups = (schemaResp.data && schemaResp.data.groups) || []
    } catch (e) {
      console.error("failed to load liquidityhelper settings/schema", e)
      this.settings = {}
      this.schemaGroups = []
    }
    this.syncLiquidityModeUi()
    this.settingsLoaded = true

    // Load list of streams up front so the Logs tab is ready when
    // selected — feels instant.
    await this.loadStreams()
    await this.reloadLogs()

    // Kick off auto-refresh if the default has it on. The watcher
    // only fires on value CHANGE, so we have to start the timer
    // explicitly to honor the initial-true setting.
    if (this.autoRefresh) this.startAutoRefresh()

    // Load the dashboard last — it's the default tab and the data
    // load is the slowest of the three (walks invoices).
    await this.reloadDashboard()
  },
  beforeDestroy() {
    this.stopAutoRefresh()
    this.stopLndReadyPoll()
  },
  methods: {
    guessType, formatNumber, formatBtcSats, formatUsd, formatPct,
    formatAmount, formatAmountFromSats,
    persistDisplayUnit() {
      // Best-effort — privacy-mode browsers throw on setItem.
      try {
        window.localStorage.setItem(DISPLAY_UNIT_STORAGE_KEY, this.displayUnit)
      } catch (_) { /* ignore */ }
    },
    // Sum a row collection across the `<field>_sats` and `<field>_usd`
    // columns. `field` is either "amount" or "fee" — payment-row
    // tables surface both. Returns {sats, usd}. USD is null only when
    // every row's <field>_usd is null/undefined.
    _sumRowsField(rows, field) {
      let sats = 0
      let usd = 0
      let anyUsd = false
      const satsKey = `${field}_sats`
      const usdKey = `${field}_usd`
      for (const r of rows) {
        sats += Number(r[satsKey]) || 0
        const u = r[usdKey]
        if (u !== null && u !== undefined) {
          usd += Number(u)
          anyUsd = true
        }
      }
      return { sats, usd: anyUsd ? usd : null }
    },
    // LND-not-ready auto-refresh. Polls reloadDashboard(force=true)
    // every 5 seconds until the backend reports lnd_ready=true.
    // Idempotent — calling start when already running is a no-op.
    startLndReadyPoll() {
      if (this.lndReadyPollHandle) return
      this.lndReadyPollHandle = setInterval(() => {
        // force-refresh to bypass the 60s cache; the backend also
        // refuses to cache not-ready responses but a stale cached
        // not-ready from before this check is no longer possible.
        this.reloadDashboard(true)
      }, 5000)
    },
    stopLndReadyPoll() {
      if (this.lndReadyPollHandle) {
        clearInterval(this.lndReadyPollHandle)
        this.lndReadyPollHandle = null
      }
    },

    // ─── Debug tab ─────────────────────────────────────────────────
    async loadDebugWallets() {
      this.loadingDebugWallets = true
      this.debugError = null
      try {
        const resp = await this.$axios.get(
          "/plugins/liquidityhelper/wallet_debug/wallets",
        )
        this.debugWallets = (resp.data && resp.data.wallets) || []
      } catch (e) {
        console.error("failed to load debug wallets", e)
        this.debugError = "Failed to load wallets: " + (e?.message || e)
        this.debugWallets = []
      } finally {
        this.loadingDebugWallets = false
      }
    },
    // Open the warning modal for a log-export action. Same modal
    // surface as confirmDebugAction (CSV / backup) but without a
    // wallet — the export applies plugin-wide. The warning copy is
    // unambiguous about the fund-theft risk because logs CAN contain
    // operationally-sensitive details even after seed scrubbing.
    confirmLogExport(scope) {
      const isAll = scope === "all"
      this.debugPendingAction = { kind: isAll ? "logs_all" : "logs_engine", wallet: null }
      this.debugDialogConfig = {
        title: isAll
          ? "Export all logs?"
          : "Export liquidityhelper log?",
        body: isAll
          ? "This will download a zip containing the liquidityhelper plugin logs AND the Bitcart application logs (/datadir/logs). Other Docker container logs (LND, postgres, etc.) are not reachable from inside the backend container and are not included."
          : "This will download a zip containing the liquidityhelper plugin's own logs: liquidityhelper.log (plus rotated archives) and decisions.log.",
        warning:
          "Logs may contain SENSITIVE INFORMATION that could be used to steal funds from your Bitcart wallets. " +
          "Wallet seed phrases are scrubbed before download, but channel state, addresses, payment hashes, and error messages may still reveal operational details. " +
          "Do NOT share this file with anyone you do not fully trust.",
        confirmLabel: "I understand — download",
        confirmColor: "error",
      }
      this.debugDialogOpen = true
    },
    // Open the confirmation modal with action-appropriate copy.
    // The modal's Continue button calls executeDebugAction below.
    confirmDebugAction(kind, wallet) {
      this.debugPendingAction = { kind, wallet }
      if (kind === "csv") {
        this.debugDialogConfig = {
          title: `Export CSV for ${wallet.wallet_name} (${wallet.wallet_short})?`,
          body:
            "This will download a CSV containing every on-chain and Lightning transaction for this wallet. " +
            "The file may be large for high-activity wallets.",
          warning: "",
          confirmLabel: "Download CSV",
          confirmColor: "primary",
        }
      } else if (kind === "backup") {
        const isLnd = wallet.currency === "btclnd"
        this.debugDialogConfig = {
          title: `Back up ${wallet.wallet_name} (${wallet.wallet_short})?`,
          body: isLnd
            ? "This will download a zip containing the wallet seed (seed.txt) AND the LND Static Channel Backup (channel.backup). Together they are sufficient to recover both on-chain funds and Lightning channels."
            : "This will download a zip containing the wallet seed (seed.txt), per-channel Electrum SCB entries (channel_backups.json), and wallet metadata (wallet_info.json).",
          warning:
            "The zip contains your seed phrase in PLAINTEXT. Anyone with the file can spend your funds. " +
            "Save it to encrypted storage immediately and delete the original download.",
          confirmLabel: "I understand — download backup",
          confirmColor: "error",
        }
      }
      this.debugDialogOpen = true
    },
    async executeDebugAction() {
      if (!this.debugPendingAction) return
      const { kind, wallet } = this.debugPendingAction
      this.debugActionInFlight = true
      try {
        let path
        let defaultFilename
        if (kind === "csv") {
          path = `/plugins/liquidityhelper/wallet_debug/wallet/${wallet.wallet_id}/csv`
          defaultFilename = `liquidityhelper-${wallet.wallet_short}-transactions.csv`
        } else if (kind === "backup") {
          path = `/plugins/liquidityhelper/wallet_debug/wallet/${wallet.wallet_id}/backup`
          defaultFilename = `liquidityhelper-${wallet.wallet_short}-backup.zip`
        } else if (kind === "logs_engine") {
          path = `/plugins/liquidityhelper/wallet_debug/logs/engine`
          defaultFilename = `liquidityhelper-logs-engine.zip`
        } else if (kind === "logs_all") {
          path = `/plugins/liquidityhelper/wallet_debug/logs/all`
          defaultFilename = `liquidityhelper-logs-all.zip`
        } else {
          throw new Error(`unknown debug action kind: ${kind}`)
        }
        // Fetch as a Blob so we can pass auth via $axios (a plain
        // <a href> wouldn't include the bearer token), then trigger
        // a download via a synthetic <a> click.
        const resp = await this.$axios.get(path, { responseType: "blob" })
        const blob = resp.data
        // Filename comes from Content-Disposition when the server set
        // one — otherwise fall back to a sensible default.
        let filename = defaultFilename
        const cd = resp.headers && (resp.headers["content-disposition"] || resp.headers["Content-Disposition"])
        if (cd) {
          const m = /filename="?([^"]+)"?/.exec(cd)
          if (m) filename = m[1]
        }
        const url = window.URL.createObjectURL(blob)
        const a = document.createElement("a")
        a.href = url
        a.download = filename
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        // Free the blob URL after the click — browsers won't reuse
        // it but releasing avoids holding a reference to a large
        // CSV/zip in memory longer than needed.
        window.URL.revokeObjectURL(url)
        this.debugDialogOpen = false
      } catch (e) {
        const target = wallet ? `wallet ${wallet.wallet_id}` : "log export"
        console.error(`failed to ${kind} for ${target}`, e)
        this.debugError = `Failed to ${kind}: ` + (e?.message || e)
        this.debugDialogOpen = false
      } finally {
        this.debugActionInFlight = false
        this.debugPendingAction = null
      }
    },
    // Abbreviate long txid/payment_hash strings for table display.
    // First 8 + last 8 chars with an ellipsis in the middle is enough
    // to recognize / click the mempool link without breaking the row.
    shortTxid(id) {
      if (!id) return ""
      if (id.length <= 20) return id
      return `${id.slice(0, 8)}…${id.slice(-8)}`
    },
    // Bitcoin-address truncation. First 4 + … + last 4 — shorter than
    // shortTxid because addresses are themselves shorter and we want
    // them to fit inline next to the chip in the destination column.
    shortAddr(addr) {
      if (!addr) return ""
      if (addr.length <= 12) return addr
      return `${addr.slice(0, 4)}…${addr.slice(-4)}`
    },
    // Strip the bitcoin: prefix and any BIP21 query string so the
    // operator sees the bare address ready to copy-paste. The dashboard
    // backend returns the full BIP21 URI (so a click can launch the
    // operator's wallet with the right amount prefilled), but the
    // displayed text shouldn't include those extras.
    bareAddr(uri) {
      if (!uri) return ""
      const stripped = uri.replace(/^bitcoin:/i, "")
      const queryIdx = stripped.indexOf("?")
      return queryIdx >= 0 ? stripped.slice(0, queryIdx) : stripped
    },
    // Convert a sat amount to USD using the dashboard's BTC/USD rate.
    // Returns null when the rate is missing (network failed, plugin
    // hasn't ticked yet) so MoneyDisplay shows just the sat/BTC value
    // without a misleading $0 alongside it.
    topupSatsToUsd(sats) {
      const rate = this.dashboard && this.dashboard.btc_usd_rate
      if (!rate || sats == null) return null
      return (sats / 1e8) * rate
    },
    // Render-decision helpers for the dashboard header's cashout
    // destination block. The header treats LN addresses (which
    // contain "@") as opaque strings (no truncation, no mempool
    // link) and on-chain addresses as Bitcoin-style values with
    // hover-tooltip + mempool click-through when the network is
    // recognized. Returned as <component :is="..." v-bind="..."/>
    // triples to keep the template free of duplicated v-if branches.
    cashoutDestComponent(dest) {
      if (!dest || !dest.destination) return "span"
      if (dest.method !== "onchain") return "span"
      return this.mempoolAddrUrl(dest.destination) ? "a" : "span"
    },
    cashoutDestProps(dest) {
      if (!dest || !dest.destination) return { class: "text-body-2" }
      if (dest.method !== "onchain") {
        return { class: "text-body-2 font-weight-medium" }
      }
      const url = this.mempoolAddrUrl(dest.destination)
      const base = { class: "text-body-2 font-weight-medium", title: dest.destination }
      return url ? { ...base, href: url, target: "_blank", rel: "noopener noreferrer" } : base
    },
    cashoutDestDisplay(dest) {
      if (!dest || !dest.destination) return "(not configured)"
      if (dest.method === "onchain") return this.shortAddr(dest.destination)
      return dest.destination
    },
    // Same idea as cashoutDestComponent/Props but for the top-up
    // warning's addresses — always on-chain, so the LN-vs-on-chain
    // branch is dropped.
    topupAddrComponent(addr) {
      if (!addr) return "span"
      return this.mempoolAddrUrl(addr) ? "a" : "span"
    },
    topupAddrProps(addr) {
      if (!addr) return { class: "text-body-2" }
      const url = this.mempoolAddrUrl(addr)
      const base = { class: "text-body-2 font-weight-medium topup-address", title: addr }
      return url ? { ...base, href: url, target: "_blank", rel: "noopener noreferrer" } : base
    },
    // mempool.space subdomain selector for the current network.
    // mainnet uses the root domain; every other network uses a path
    // prefix. regtest has no public explorer — we return null so
    // callers render plain text.
    //   "mainnet"  → "https://mempool.space"
    //   "testnet"  → "https://mempool.space/testnet"
    //   "testnet4" → "https://mempool.space/testnet4"
    //   "signet"   → "https://mempool.space/signet"
    //   "regtest"  → null
    //   ""         → null (unknown network; render plain)
    mempoolBase() {
      const n = this.dashboard && this.dashboard.bitcoin_network
      if (!n) return null
      if (n === "mainnet") return "https://mempool.space"
      if (n === "testnet") return "https://mempool.space/testnet"
      if (n === "testnet4") return "https://mempool.space/testnet4"
      if (n === "signet") return "https://mempool.space/signet"
      // regtest and anything else: no public explorer.
      return null
    },
    mempoolTxUrl(txid) {
      const base = this.mempoolBase()
      if (!base || !txid) return null
      return `${base}/tx/${txid}`
    },
    // Channel point is "txid:vout"; mempool.space's /tx page shows the
    // funding tx with all outputs visible, so we don't need the vout in
    // the URL. Returns null when the network has no public explorer
    // (regtest) or the channel_point is malformed.
    channelPointUrl(channelPoint) {
      if (!channelPoint) return null
      const txid = String(channelPoint).split(":")[0]
      return this.mempoolTxUrl(txid)
    },
    // Resolve the LSP provider name for a channel by joining its
    // peer_pubkey against the lsp_provider_pubkeys map the backend
    // ships in liquidity_stats. Empty string when there's no match
    // (or the map is missing — happens on regtest / unknown network).
    channelLspName(ch) {
      if (!ch || !ch.peer_pubkey) return ""
      const stats = this.dashboard && this.dashboard.liquidity_stats
      const map = stats && stats.lsp_provider_pubkeys
      if (!map) return ""
      return map[String(ch.peer_pubkey).toLowerCase()] || ""
    },
    // Project the two underlying flags (LIQUIDITY_DISABLED +
    // AUTOMATIC_CHANNEL_CREATION_ENABLED) onto the three dropdown values.
    // Disabled wins regardless of the automatic flag — when paused, the
    // engine isn't running either path anyway. Called after settings
    // load + after each successful save.
    syncLiquidityModeUi() {
      const s = this.settings || {}
      if (s.LIQUIDITY_DISABLED) {
        this.liquidityModeUi = "disabled"
      } else if (s.AUTOMATIC_CHANNEL_CREATION_ENABLED) {
        this.liquidityModeUi = "automatic"
      } else {
        this.liquidityModeUi = "lsp"
      }
    },
    // Inverse projection. Returns the partial settings dict to POST
    // for a given dropdown value. Disabled preserves the operator's
    // existing LSP/Automatic choice on the other flag so flipping
    // Disabled → LSP/Automatic is a one-step revert.
    liquidityModePayload(mode) {
      if (mode === "disabled") {
        return { LIQUIDITY_DISABLED: true }
      }
      if (mode === "automatic") {
        return {
          LIQUIDITY_DISABLED: false,
          AUTOMATIC_CHANNEL_CREATION_ENABLED: true,
        }
      }
      return {
        LIQUIDITY_DISABLED: false,
        AUTOMATIC_CHANNEL_CREATION_ENABLED: false,
      }
    },
    async onLiquidityModeChange(mode) {
      const payload = this.liquidityModePayload(mode)
      this.liquidityModeSaving = true
      this.liquidityModeSaveError = ""
      try {
        await this.$axios.post("/plugins/settings/liquidityhelper", payload)
        // Reflect into local state so a subsequent re-render of the
        // mode card (or another setting save that re-syncs) sees the
        // change without a full reload.
        this.settings = { ...this.settings, ...payload }
        this.syncLiquidityModeUi()
      } catch (e) {
        console.error("liquidity mode save failed", e)
        this.liquidityModeSaveError = "Save failed — reload and retry."
        // Revert the dropdown to whatever the stored state actually is.
        this.syncLiquidityModeUi()
      } finally {
        this.liquidityModeSaving = false
      }
    },
    mempoolAddrUrl(addr) {
      const base = this.mempoolBase()
      if (!base || !addr) return null
      return `${base}/address/${addr}`
    },
    // mempool.space's Lightning explorer URL for a node pubkey. Same
    // network-prefix rules as mempoolBase() apply, so regtest (and any
    // unknown network) returns null and the UI falls back to a plain
    // truncated pubkey with no link.
    mempoolNodeUrl(pubkey) {
      const base = this.mempoolBase()
      if (!base || !pubkey) return null
      return `${base}/lightning/node/${pubkey}`
    },
    // Render-decision helpers for LN node pubkey columns (currently
    // the Recent channel closures peer column). Same component/props
    // pattern as cashoutDest* — keeps the template free of duplicated
    // v-if branches.
    lnNodeComponent(pubkey) {
      if (!pubkey) return "span"
      return this.mempoolNodeUrl(pubkey) ? "a" : "span"
    },
    lnNodeProps(pubkey) {
      if (!pubkey) return { class: "text-caption" }
      const url = this.mempoolNodeUrl(pubkey)
      const base = { class: "text-caption", title: pubkey }
      return url ? { ...base, href: url, target: "_blank", rel: "noopener noreferrer" } : base
    },
    // Outbound/inbound split-bar helpers used by the Liquidity stats
    // panel (per-wallet aggregate AND per-channel). Returns the
    // outbound segment's width as a 0-100 percentage; the inbound
    // segment uses the complement. A zero-capacity channel (rare —
    // would mean both sides empty) returns 50/50 so the bar still
    // renders as a recognizable shape rather than collapsing.
    balanceBarPct(localSats, remoteSats) {
      const total = (Number(localSats) || 0) + (Number(remoteSats) || 0)
      if (total <= 0) return 50
      return Math.max(0, Math.min(100, (Number(localSats) / total) * 100))
    },
    balanceBarTitle(localSats, remoteSats) {
      const total = (Number(localSats) || 0) + (Number(remoteSats) || 0)
      const outPct = total > 0 ? ((Number(localSats) / total) * 100).toFixed(1) : "—"
      return `Inbound (receive) ${this.formatNumber(remoteSats, 0)} sat / Outbound (send) ${this.formatNumber(localSats, 0)} sat (outbound ${outPct}%)`
    },
    // Heuristic: is `s` a Bitcoin address (vs an LN address / pubkey /
    // empty)? LN addresses contain '@' (user@domain), LN pubkeys are
    // 66-char hex, on-chain addresses start with one of the standard
    // prefixes. We're conservative — anything that doesn't clearly
    // look like a Bitcoin address is rendered plain rather than risk
    // building a bogus mempool URL.
    isBitcoinAddress(s) {
      if (!s || typeof s !== "string") return false
      if (s.includes("@")) return false
      if (/^[0-9a-f]{66}$/i.test(s)) return false   // LN pubkey
      // Mainnet: 1.../3.../bc1...; testnet/signet: m.../n.../2.../tb1...;
      // regtest: bcrt1... Taproot adds bc1p/tb1p/bcrt1p; we accept those
      // via the prefix-segwit match.
      return /^(bc1|tb1|bcrt1|[123mn])/.test(s)
    },
    // Color for the fee_type chip in the payments tables.
    feeTypeColor(type) {
      if (type === "developer") return "primary"
      if (type === "hosting") return "warning"
      if (type === "cashout") return "success"
      return "default"
    },
    // Dashboard-tab collapsible sections. `isExpanded` returns true
    // when a section's key has never been toggled OR was last
    // toggled to expanded — sections default to OPEN. `toggleSection`
    // flips one section's state. Vue 2's reactivity needs $set for
    // a NEW key on the expandedSections object; once a key exists,
    // direct assignment is reactive — so we use $set unconditionally
    // (cheap, no harm if the key was already present).
    isExpanded(key) {
      return this.expandedSections[key] !== false
    },
    toggleSection(key) {
      this.$set(this.expandedSections, key, !this.isExpanded(key))
    },
    // Settings-tab support: return {count, settings, color} for the
    // given schema group. `count` is the number of distinct settings
    // in this group referenced by active dashboard.health_warnings.
    // `settings` is the sorted list of those names (for the tooltip).
    // `color` is "error" if any contributing warning is HIGH severity,
    // "warning" otherwise — matches the dashboard banner palette so
    // operators don't have to learn a second color mapping.
    //
    // Backed by HealthWarning.settings (list of related setting
    // names per warning, populated server-side by every _check_*
    // helper in liquidityhelper.py). Warnings without settings (e.g.
    // ln-cashout-failing — a runtime warning not tied to one knob)
    // contribute to the banner but not to any group icon.
    groupWarningInfo(group) {
      const warnings = (this.dashboard && this.dashboard.health_warnings) || []
      if (!warnings.length) return { count: 0, settings: [], color: "warning" }
      const groupNames = new Set((group.settings || []).map((s) => s.name))
      const matching = new Set()
      let highSeverity = false
      for (const w of warnings) {
        for (const name of (w.settings || [])) {
          if (groupNames.has(name)) {
            matching.add(name)
            if (w.severity === "HIGH") highSeverity = true
          }
        }
      }
      return {
        count: matching.size,
        settings: Array.from(matching).sort(),
        color: highSeverity ? "error" : "warning",
      }
    },
    // Color for the category chip in the Recent network fees table.
    // Reuses the fee-payment palette where categories overlap so a
    // glancing operator builds one mental color→meaning map.
    networkFeeCategoryColor(cat) {
      if (cat === "developer_fee") return "primary"
      if (cat === "hosting_fee") return "warning"
      if (cat === "cashout") return "success"
      if (cat === "channel_open") return "info"
      if (cat === "channel_close") return "error"
      if (cat === "lsp_order") return "purple"
      // external_send = outgoing tx not initiated by an engine-labeled
      // path (operator manual send, anchor sweep, etc.) — neutral grey
      // so it doesn't pull the eye like an engine-decision category.
      if (cat === "external_send") return "grey"
      return "default"
    },
    // Parse close_reason into {category, reasons[]}. Engine format
    // for audit-driven closures: "AUDIT_FAILURE: r1,r2,r3". Anything
    // not matching that shape returns category=raw string,
    // reasons=[].
    parseCloseReason(raw) {
      if (!raw) return { category: "", reasons: [] }
      const m = /^AUDIT_FAILURE\s*:\s*(.*)$/.exec(raw)
      if (!m) return { category: raw, reasons: [] }
      const reasons = m[1].split(",").map(s => s.trim()).filter(Boolean)
      return { category: "AUDIT_FAILURE", reasons }
    },
    // Make audit-reason tokens (UPPER_SNAKE_CASE constants set by
    // node_database.audit_existing_peer / is_node_blacklisted)
    // human-readable. Unknown tokens fall back to the generic
    // underscore-stripping titlecase logic.
    humanizeAuditReason(token) {
      const map = {
        // Tokens emitted by audit_existing_peer
        HIGH_FEE_RATE: "Routing fee above the configured ceiling",
        LOW_EFFECTIVE_DEGREE: "Peer has too few effective channels",
        LOW_TWO_HOP_REACH: "Peer's 2-hop reach is too small",
        LOW_CAPACITY: "Peer's total capacity is below the threshold",
        LOW_OUTBOUND_CAPACITY: "Peer's outbound capacity is too low",
        HIGH_MIN_HTLC: "Peer's min_htlc would block small payments",
        LOW_MAX_HTLC: "Peer's max_htlc is below the threshold",
        LONG_OUTAGE: "Peer has been offline beyond the allowed window",
        HIGH_FAILURE_RATIO: "Peer's payment failure rate is too high",
        // Additional tokens emitted by is_node_blacklisted (pre-open
        // gate). Audit re-uses some of these via _evaluate_uptime_signals
        // and the shared capacity/HTLC checks; the rest only appear when
        // surfacing pre-open rejection reasons.
        FORCE_CLOSE_BLACKLISTED: "Peer is serving a force-close blacklist",
        AUDIT_BLACKLISTED: "Peer is serving an audit-failure blacklist",
        NO_IPV4: "Peer has no IPv4 address advertised",
        REMOTE_CLOSE_COUNT: "Peer has remote-closed too many of our channels",
        UNKNOWN_CHANNEL_COUNT: "Peer's channel count is not yet known",
        MIN_CHANNEL_COUNT: "Peer has fewer channels than the minimum",
        UNKNOWN_CAPACITY: "Peer's total capacity is not yet known",
        NO_OLDEST_KNOWN_DATE: "Peer's first-seen date is not yet known",
        NOT_OLD_ENOUGH: "Peer is newer than the minimum age",
        UNKNOWN_FEE_RATE: "Peer's median fee rate is not yet known",
        UNKNOWN_HTLC_LIMITS: "Peer's HTLC limits are not yet known",
        UNKNOWN_CONNECTEDNESS: "Peer's connectedness metrics are not yet known",
      }
      if (map[token]) return map[token]
      return token.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())
    },
    async reloadDashboard(forceRefresh = false) {
      this.loadingDashboard = true
      this.dashboardError = null
      try {
        const resp = await this.$axios.get(
          "/plugins/liquidityhelper/dashboard",
          {
            params: {
              range: this.dashboardRange,
              ...(forceRefresh ? { force_refresh: true } : {}),
            },
          }
        )
        this.dashboard = resp.data
      } catch (e) {
        this.dashboardError = (e.response && e.response.data && e.response.data.detail)
          || (e.message || String(e))
      } finally {
        this.loadingDashboard = false
      }
    },
    async triggerDebugTick() {
      // Fires one tick when DEBUG_MODE is on. Refreshes logs
      // immediately so the operator sees the tick's output without
      // having to click Refresh too.
      this.loadingDebugTick = true
      this.debugTickStatus = null
      try {
        const resp = await this.$axios.post(
          "/plugins/liquidityhelper/debug/run_once"
        )
        this.debugTickStatus = resp.data
      } catch (e) {
        this.debugTickStatus = {
          triggered: false,
          message:
            (e.response && e.response.data && e.response.data.detail)
            || (e.message || String(e)),
        }
      } finally {
        this.loadingDebugTick = false
      }
      // Give the tick a brief moment to start writing log entries,
      // then refresh the log view. The tick itself may take much
      // longer than this; the operator sees that via continued log
      // updates via the auto-refresh switch.
      setTimeout(() => this.reloadLogs(), 500)
    },
    async loadStreams() {
      try {
        const resp = await this.$axios.get(
          "/plugins/liquidityhelper/logs/streams"
        )
        this.streams = resp.data || []
      } catch (e) {
        console.error("failed to load log streams", e)
        this.streams = []
      }
    },
    async reloadLogs() {
      if (!this.selectedStream) return
      this.loadingLogs = true
      this.logError = null
      try {
        const resp = await this.$axios.get(
          `/plugins/liquidityhelper/logs/${this.selectedStream}`,
          { params: { tail: this.tailSize } }
        )
        this.logLines = resp.data.lines || []
        this.logTruncated = resp.data.truncated || false
      } catch (e) {
        // Surface the server's detail field if present, otherwise the
        // raw error. Don't blow up the whole page on a transient 5xx.
        this.logError = (e.response && e.response.data && e.response.data.detail)
          || (e.message || String(e))
      } finally {
        this.loadingLogs = false
      }
      // Refresh the sidebar size badges so a freshly-rotated file
      // shows its new size without a manual reload.
      this.loadStreams()
    },
    startAutoRefresh() {
      this.stopAutoRefresh()
      this.autoRefreshTimer = setInterval(
        this.reloadLogs, AUTO_REFRESH_INTERVAL_MS
      )
    },
    stopAutoRefresh() {
      if (this.autoRefreshTimer != null) {
        clearInterval(this.autoRefreshTimer)
        this.autoRefreshTimer = null
      }
    },
  },
}
</script>

<style scoped>
.log-view {
  background-color: #1e1e1e;
  color: #d4d4d4;
  font-family: "Source Code Pro", "Menlo", "Consolas", monospace;
  font-size: 12px;
  line-height: 1.45;
  padding: 12px 16px;
  border-radius: 4px;
  max-height: 70vh;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.setting-row {
  /* Compact rows so a dense settings page is still scannable. */
  padding: 4px 0;
}

/* ── Dashboard styles ── */

.shared-wallet-warning {
  /* The yellow alert color is already provided by Vuetify's
     amber lighten-4 palette — this just nudges the typography
     so the warning feels more emphatic. */
  border-left: 4px solid #f57c00;
}

/* The amber lighten-4 background is the same in both themes, so the
   text color should be the same dark value in both. The body-text
   override only fires in dark mode (light mode is already dark text);
   the icon override fires in BOTH modes because Vuetify also tints
   the icon with the alert's foreground color in light mode and we
   need a single rule that always wins. Vuetify puts .theme--dark
   directly on the v-icon element (not on an ancestor), so the
   selector must match the combined class on the icon itself rather
   than rely on descendant matching. */
.theme--dark .topup-warning {
  color: rgba(0, 0, 0, 0.87);
}
.topup-warning .v-icon,
.topup-warning .v-icon.theme--dark,
.topup-warning .v-icon.theme--light,
.topup-warning .v-alert__icon,
.topup-warning .v-alert__icon.v-icon,
.topup-warning .v-alert__icon.v-icon.theme--dark,
.topup-warning .v-alert__icon.v-icon.theme--light {
  color: rgba(0, 0, 0, 0.87) !important;
  caret-color: rgba(0, 0, 0, 0.87) !important;
}

/* Light mode: every warning banner (top-up, shared-wallet, and the
   health/config warnings — including the "no SMTP credentials" one) sits
   on a pale amber/red fill. The health warnings use Vuetify's `text`
   variant, which tints their text with the warning/error FOREGROUND
   colour — washed out and hard to read on the pale fill in the light
   theme. Force dark text + icons so warnings stay legible in light mode.
   (The dark-mode top-up case is handled by the block above.) */
.theme--light .topup-warning,
.theme--light .shared-wallet-warning,
.theme--light .health-warning,
.theme--light .topup-warning .v-alert__content,
.theme--light .shared-wallet-warning .v-alert__content,
.theme--light .health-warning .v-alert__content,
.theme--light .topup-warning .v-icon,
.theme--light .shared-wallet-warning .v-icon,
.theme--light .health-warning .v-icon,
.theme--light .topup-warning .v-alert__icon,
.theme--light .shared-wallet-warning .v-alert__icon,
.theme--light .health-warning .v-alert__icon {
  color: rgba(0, 0, 0, 0.87) !important;
  caret-color: rgba(0, 0, 0, 0.87) !important;
}

/* Each top-up address gets its own line. Bare full address — no
   truncation — so the operator can copy-paste straight into a
   wallet. The label sits inline to the left of the address. */
.topup-address-line {
  margin-top: 4px;
  word-break: break-all;
}
.topup-address-label {
  margin-right: 4px;
}

.store-card {
  background-color: #fafafa;
}

.summary-card {
  border: 2px solid #1976D2;
  background-color: #f5f9fc;
}

/* The kv- (key/value) rows are the dashboard's information-density
   workhorse: a label on the left, value on the right, with optional
   meta (percentage / channel count) in muted text. Indented variant
   used by the fee breakdown subsection. */

.kv-row {
  display: flex;
  justify-content: space-between;
  padding: 4px 0;
  border-bottom: 1px solid #eee;
  font-size: 14px;
}

.kv-row:last-child { border-bottom: none; }

.kv-row.indented {
  padding-left: 24px;
  font-size: 13px;
  color: #555;
}

.kv-row.total {
  font-weight: bold;
  border-top: 1px solid #ccc;
  margin-top: 4px;
  padding-top: 8px;
}

.kv-row.liquidity {
  border-top: 1px solid #ccc;
  margin-top: 8px;
  padding-top: 8px;
  color: #1976D2;
}

.kv-label { color: #555; }
.kv-value { text-align: right; }
.kv-meta {
  color: #999;
  font-weight: normal;
  font-size: 12px;
  margin-left: 8px;
}

.fee-breakdown {
  background-color: #f5f5f5;
  border-radius: 4px;
  padding: 4px 8px;
  margin: 4px 0;
}

.pie-wrapper {
  position: relative;
  width: 100%;
  max-width: 280px;
  height: 280px;
}

/* Audit-failure reason bullets inside the Recent channel closures
   table. Compact list with no left padding so it sits cleanly under
   the AUDIT_FAILURE category label. */
.audit-reason-list {
  padding-left: 18px;
  line-height: 1.3;
}
.audit-reason-list li {
  font-size: 0.85em;
}

/* Clickable v-card-title used to toggle a section's collapse
   state. Cursor + subtle hover-tint signal interactivity without
   competing with the section title's visual weight. The chevron
   icon inside flips between mdi-chevron-down (expanded) and
   mdi-chevron-right (collapsed) via the template. */
.section-toggle {
  cursor: pointer;
  user-select: none;
}
.section-toggle:hover {
  background-color: rgba(0, 0, 0, 0.04);
}
.theme--dark .section-toggle:hover {
  background-color: rgba(255, 255, 255, 0.06);
}

/* Totals row at the bottom of the Liquidity stats table. Bolded
   `<th>` cells inside `<tfoot>` get a top border so the row is
   visually separated from the per-wallet rows. */
.totals-row th {
  border-top: 1px solid rgba(255, 255, 255, 0.12);
  font-weight: 600;
}

/* Totals row injected into each recent-activity v-data-table via the
   #body.append slot. Renders as the LAST row of the table body so it
   sits BETWEEN the data rows and the rows-per-page pagination footer.
   Visually distinguished from data rows by a top border and no hover
   highlight (defeats Vuetify's row-hover background, which would
   otherwise make this look clickable). */
.totals-row td {
  border-top: 2px solid rgba(0, 0, 0, 0.12);
  /* Disable Vuetify's row-hover background — totals aren't clickable. */
  background: transparent !important;
}
.theme--dark .totals-row td {
  border-top-color: rgba(255, 255, 255, 0.16);
}

/* Per-table totals line under each recent-activity v-data-table.
   Right-aligned, slight top border to separate from the data rows.
   Note: superseded by the .totals-row pattern above (the new layout
   uses #body.append to keep the totals INSIDE the table, above the
   pagination footer); kept here briefly until any out-of-tree
   templates referencing .totals-line are migrated. Safe to delete
   after grepping confirms no other consumers. */
.totals-line {
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  padding-top: 6px;
}

/* Outbound/inbound split bar shown per-wallet and per-channel in the
   Liquidity stats panel. Two segments flex side-by-side; their widths
   come from inline styles (computed in balanceBarPct). The container
   gets a thin border so even a zero-balance bar renders as a visible
   shape rather than collapsing. */
.balance-bar {
  display: flex;
  width: 100%;
  height: 14px;
  border: 1px solid rgba(0, 0, 0, 0.18);
  border-radius: 3px;
  overflow: hidden;
  background: rgba(0, 0, 0, 0.04);
}
.theme--dark .balance-bar {
  border-color: rgba(255, 255, 255, 0.18);
  background: rgba(255, 255, 255, 0.04);
}
.balance-bar-sm {
  height: 10px;
}
/* Outbound = ability to send. Rendered in Bitcoin orange so the
   bar's two halves are immediately distinguishable from each other
   without consulting a legend. Inbound (ability to receive) stays
   on Vuetify's success-green. */
.balance-bar-outbound {
  background: #f7931a;
  height: 100%;
}
.balance-bar-inbound {
  background: var(--v-success-base, #4caf50);
  height: 100%;
}

/* Indented channel list under each wallet block. Uses left padding to
   visually establish the parent-child relationship without nesting a
   second v-card inside. */
.wallet-block {
  border-left: 2px solid rgba(0, 0, 0, 0.08);
  padding-left: 10px;
}
.theme--dark .wallet-block {
  border-left-color: rgba(255, 255, 255, 0.12);
}
.channel-list {
  margin-left: 16px;
  margin-top: 6px;
}
.channel-table th, .channel-table td {
  font-size: 0.78rem;
}
</style>
