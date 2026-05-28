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
      <v-tab>Logs</v-tab>
    </v-tabs>

    <v-tabs-items v-model="tab" class="mt-4">
      <!-- ──────────── Dashboard tab ──────────── -->
      <v-tab-item>
        <v-card flat>
          <v-card-text>
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

            <!-- Empty-state when no liquidityhelper wallets are
                 configured. Renders BEFORE the loading spinner check
                 only when we've successfully fetched and got nothing. -->
            <v-alert
              v-if="dashboard && dashboard.stores.length === 0 && !loadingDashboard"
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

            <!-- Per-store cards -->
            <div v-if="dashboard">
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
                <v-card-title>
                  Summary — all stores combined
                </v-card-title>
                <v-card-text>
                  <StoreCard
                    :store="dashboard.summary"
                    :include-inbound="false"
                    :is-summary="true"
                    :settings="settings"
                    :initial-cc-pct="dashboard.cc_baseline_pct"
                    :display-unit="displayUnit"
                  />
                </v-card-text>
              </v-card>

              <!-- ─── Liquidity stats ─── -->
              <!-- One row per liquidityhelper-named wallet with its
                   inbound + outbound balance and active channel count,
                   plus a totals row at the bottom. Title shows which
                   liquidity-management mode is configured (LSP-managed
                   vs Manual) so the operator instantly knows whether
                   new-channel acquisition is automatic or operator-
                   driven. Replaces the per-store inbound-liquidity row
                   that used to live on each StoreCard. -->
              <v-card v-if="dashboard.liquidity_stats" outlined class="mb-4">
                <v-card-title>
                  Liquidity stats
                  <v-chip
                    class="ml-3"
                    small
                    :color="dashboard.liquidity_stats.mode === 'Manual channel management' ? 'warning' : 'info'"
                    outlined
                  >
                    {{ dashboard.liquidity_stats.mode }}
                  </v-chip>
                </v-card-title>
                <v-card-text>
                  <p class="text-caption mb-2">
                    Per-wallet inbound and outbound liquidity (active
                    channels only). Only wallets named
                    <code>liquidityhelper</code> are counted — these are
                    the wallets the engine manages.
                  </p>
                  <v-simple-table dense class="elevation-0">
                    <template #default>
                      <thead>
                        <tr>
                          <th>Wallet</th>
                          <th class="text-right">Inbound</th>
                          <th class="text-right">Outbound</th>
                          <th class="text-right" style="width: 110px;">Channels</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr
                          v-for="w in dashboard.liquidity_stats.wallets"
                          :key="w.wallet_id"
                        >
                          <td>
                            {{ w.wallet_name }}
                            <span class="text-caption grey--text">({{ w.wallet_short }})</span>
                          </td>
                          <td class="text-right">
                            {{ formatAmount(w.inbound, displayUnit) }}
                          </td>
                          <td class="text-right">
                            {{ formatAmount(w.outbound, displayUnit) }}
                          </td>
                          <td class="text-right">{{ w.active_channel_count }}</td>
                        </tr>
                        <tr v-if="!dashboard.liquidity_stats.wallets.length">
                          <td colspan="4" class="text-center grey--text">
                            No <code>liquidityhelper</code> wallets configured.
                          </td>
                        </tr>
                      </tbody>
                      <tfoot v-if="dashboard.liquidity_stats.wallets.length">
                        <tr class="totals-row">
                          <th>Total</th>
                          <th class="text-right">
                            {{ formatAmount(dashboard.liquidity_stats.total_inbound, displayUnit) }}
                          </th>
                          <th class="text-right">
                            {{ formatAmount(dashboard.liquidity_stats.total_outbound, displayUnit) }}
                          </th>
                          <th class="text-right">
                            {{ dashboard.liquidity_stats.total_channel_count }}
                          </th>
                        </tr>
                      </tfoot>
                    </template>
                  </v-simple-table>
                </v-card-text>
              </v-card>

              <!-- ─── Recent activity tables ─── -->

              <v-card outlined class="mb-4">
                <v-card-title>Recent fee payments</v-card-title>
                <v-card-text>
                  <p class="text-caption mb-2">
                    Developer and hosting/setup fee payments across all
                    <code>liquidityhelper</code> wallets, newest first
                    (capped at 100 entries).
                    <em>Destination shown is the CURRENT configured destination
                    and may differ from where the payment actually went historically.</em>
                  </p>
                  <v-data-table
                    :headers="paymentHeaders"
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
                      {{ formatAmountFromSats(item.amount_sats, item.amount_usd, displayUnit) }}
                    </template>
                    <template #item.fee_sats="{ item }">
                      {{ formatAmountFromSats(item.fee_sats, item.fee_usd, displayUnit) }}
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
                        target="_blank" rel="noopener noreferrer"
                        class="text-caption"
                      >{{ shortAddr(item.destination) }}</a>
                      <span v-else-if="isBitcoinAddress(item.destination)" class="text-caption">
                        {{ shortAddr(item.destination) }}
                      </span>
                      <span v-else class="text-caption">{{ item.destination }}</span>
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
                  </v-data-table>
                </v-card-text>
              </v-card>

              <v-card outlined class="mb-4">
                <v-card-title>Recent cashouts</v-card-title>
                <v-card-text>
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
                      {{ formatAmountFromSats(item.amount_sats, item.amount_usd, displayUnit) }}
                    </template>
                    <template #item.fee_sats="{ item }">
                      {{ formatAmountFromSats(item.fee_sats, item.fee_usd, displayUnit) }}
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
                        target="_blank" rel="noopener noreferrer"
                        class="text-caption"
                      >{{ shortAddr(item.destination) }}</a>
                      <span v-else-if="isBitcoinAddress(item.destination)" class="text-caption">
                        {{ shortAddr(item.destination) }}
                      </span>
                      <span v-else class="text-caption">{{ item.destination }}</span>
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
                  </v-data-table>
                </v-card-text>
              </v-card>

              <v-card outlined class="mb-4">
                <v-card-title>Recent channel closures</v-card-title>
                <v-card-text>
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
              </v-card>

              <v-card outlined class="mb-4">
                <v-card-title>Recent LSP orders</v-card-title>
                <v-card-text>
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
                        {{ formatAmountFromSats(item.paid_sats, item.paid_usd, displayUnit) }}
                      </span>
                    </template>
                    <template #item.refund_sats="{ item }">
                      <span class="text-caption">
                        {{ formatAmountFromSats(item.refund_sats, item.refund_usd, displayUnit) }}
                        <v-icon
                          v-if="item.state === 'FAILED' && !item.refund_observed_onchain"
                          x-small color="warning" class="ml-1"
                          :title="'LSP claimed a refund but it has not been confirmed on-chain yet — not yet credited in fee accounting.'"
                        >mdi-alert-circle-outline</v-icon>
                      </span>
                    </template>
                    <template #item.net_cost_sats="{ item }">
                      <span class="text-caption">
                        {{ formatAmountFromSats(item.net_cost_sats, item.net_cost_usd, displayUnit) }}
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
              </v-card>

              <v-card outlined class="mb-4">
                <v-card-title>Recent network fees</v-card-title>
                <v-card-text>
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
                      {{ formatAmountFromSats(item.fee_sats, item.fee_usd, displayUnit) }}
                    </template>
                    <template #item.amount_sats="{ item }">
                      {{ formatAmountFromSats(item.amount_sats, item.amount_usd, displayUnit) }}
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
                        target="_blank" rel="noopener noreferrer"
                        class="text-caption"
                      >{{ shortAddr(item.destination) }}</a>
                      <span v-else-if="isBitcoinAddress(item.destination)" class="text-caption">
                        {{ shortAddr(item.destination) }}
                      </span>
                      <span v-else class="text-caption">{{ item.destination }}</span>
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
                  </v-data-table>
                </v-card-text>
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
                    <span class="text-h6">{{ group.group }}</span>
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

      <!-- ──────────── Logs tab ──────────── -->
      <v-tab-item>
        <v-card flat>
          <v-card-text>
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
    </div>
  </v-container>
</template>

<script>
import PolicySetting from "@/components/PolicySetting.vue"
import StoreCard from "../components/StoreCard.vue"
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
  components: { PolicySetting, StoreCard },
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
      // Display-unit toggle. "sats" (default) or "btc". Initialized
      // from localStorage so the operator's preference survives a
      // reload. USD continues to render in parentheses regardless of
      // the selected unit — the toggle only controls the main unit.
      displayUnit: _loadDisplayUnit(),
      // Static column definitions for the recent-activity v-data-tables.
      // Defined in data() (not as a computed) so Vuetify gets a stable
      // reference and doesn't re-create header cells on every re-render.
      paymentHeaders: [
        { text: "Date", value: "iso_date", width: 160 },
        { text: "Amount", value: "amount" },
        { text: "Fee paid", value: "fee_sats" },
        { text: "Type", value: "fee_type", width: 110 },
        { text: "Method", value: "method", width: 110 },
        { text: "Destination", value: "destination" },
        { text: "Tx / hash", value: "txid" },
      ],
      closureHeaders: [
        { text: "Date", value: "iso_date", width: 160 },
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
        { text: "Amount", value: "amount_sats" },
        { text: "Method", value: "method", width: 110 },
        { text: "Destination", value: "destination" },
        { text: "Tx / hash", value: "txid" },
      ],

      // Settings tab state.
      settings: {},
      settingsLoaded: false,
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
      // Debug-mode (manual single-step) state.
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
    streamItems() {
      // Vuetify select items: [{ text, value }]. The text shows the
      // friendly name plus a hint about empty state so the operator
      // knows up front whether a stream has anything to show.
      return this.streams.map((s) => ({
        text:
          s.name === "operational" ? "Operational (full firehose)"
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
    mempoolAddrUrl(addr) {
      const base = this.mempoolBase()
      if (!base || !addr) return null
      return `${base}/address/${addr}`
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

/* Totals row at the bottom of the Liquidity stats table. Bolded
   `<th>` cells inside `<tfoot>` get a top border so the row is
   visually separated from the per-wallet rows. */
.totals-row th {
  border-top: 1px solid rgba(255, 255, 255, 0.12);
  font-weight: 600;
}
</style>
