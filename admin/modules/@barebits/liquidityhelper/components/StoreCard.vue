<template>
  <v-card outlined class="mb-4 store-card">
    <!-- Click the title to collapse/expand. Only the per-store
         variant has a v-card-title (`isSummary=true` cards have
         no title — they're already wrapped by an outer v-card in
         index.vue that owns the collapse toggle). When this title
         is absent, the body stays unconditionally visible so the
         outer wrapper's collapse fully controls visibility. -->
    <v-card-title
      v-if="!isSummary"
      @click="collapsed = !collapsed"
      class="section-toggle"
    >
      <v-icon class="mr-2">
        {{ collapsed ? 'mdi-chevron-right' : 'mdi-chevron-down' }}
      </v-icon>
      Fee breakdown
    </v-card-title>
    <v-expand-transition>
    <v-card-text v-show="!collapsed || isSummary">
      <v-row>
        <v-col cols="12" md="7">
          <!-- Revenue + sales -->
          <div class="kv-row">
            <span class="kv-label">Total revenue:</span>
            <span class="kv-value">
              <MoneyDisplay :money="store.revenue" :unit="displayUnit" />
            </span>
          </div>
          <div class="kv-row">
            <span class="kv-label">Total paid invoices:</span>
            <span class="kv-value">{{ store.paid_invoice_count }}</span>
          </div>

          <!-- Developer fee. Three numbers:
                 paid (delivered to LN_FEE_DEST/ONCHAIN_FEE_DEST),
                 of due (eligible_revenue × FEE_AMOUNT, cumulative),
                 balance (due − paid; >0 means owed, ≤0 means caught up).
               Balance is computed client-side so a future change to the
               engine's network-fee credit policy doesn't need a backend
               change to keep the math obvious. -->
          <div class="kv-row">
            <span class="kv-label">Developer fees paid:</span>
            <span class="kv-value">
              <MoneyDisplay :money="store.developer_fees_paid" :unit="displayUnit" />
              <span class="kv-meta">
                of <MoneyDisplay :money="store.developer_fees_due" :unit="displayUnit" /> due
                ({{ formatPct(store.developer_fee_pct) }} of revenue<span
                  v-if="developerRateConfigured !== null">,
                  configured rate {{ formatPct(developerRateConfigured) }}</span>)
              </span>
              <span v-if="developerBalanceSats > 0" class="kv-balance owed">
                — <MoneyDisplay :money="developerBalance" :unit="displayUnit" /> owed
              </span>
              <span v-else-if="developerBalanceSats < 0" class="kv-balance overpaid">
                — overpaid by <MoneyDisplay :money="developerOverpayment" :unit="displayUnit" />
              </span>
            </span>
          </div>

          <!-- Hosting/referral fee — same pattern as developer fee. -->
          <div class="kv-row">
            <span class="kv-label">Hosting / setup fees paid:</span>
            <span class="kv-value">
              <MoneyDisplay :money="store.hosting_fees_paid" :unit="displayUnit" />
              <span class="kv-meta">
                of <MoneyDisplay :money="store.hosting_fees_due" :unit="displayUnit" /> due
                ({{ formatPct(store.hosting_fee_pct) }} of revenue<span
                  v-if="hostingRateConfigured !== null">,
                  configured rate {{ formatPct(hostingRateConfigured) }}</span>)
              </span>
              <span v-if="hostingBalanceSats > 0" class="kv-balance owed">
                — <MoneyDisplay :money="hostingBalance" :unit="displayUnit" /> owed
              </span>
              <span v-else-if="hostingBalanceSats < 0" class="kv-balance overpaid">
                — overpaid by <MoneyDisplay :money="hostingOverpayment" :unit="displayUnit" />
              </span>
            </span>
          </div>

          <!-- Network fees -->
          <div class="kv-row">
            <span class="kv-label">Network fees (total):</span>
            <span class="kv-value">
              <MoneyDisplay :money="store.network_fees_total" :unit="displayUnit" />
            </span>
          </div>
          <!-- Indented breakdown — only shown for non-zero rows.
               Rows can optionally carry a `tooltip` field; when
               present, a small info-circle icon renders next to the
               label with the tooltip text in its `title=` attribute. -->
          <div v-if="feeRows.length" class="fee-breakdown">
            <div v-for="row in feeRows" :key="row.key" class="kv-row indented">
              <span class="kv-label">{{ row.label
                }}<v-icon
                  v-if="row.tooltip"
                  x-small
                  class="ml-1 fee-info-icon"
                  :title="row.tooltip"
                >mdi-information-outline</v-icon>:</span>
              <span class="kv-value">
                <MoneyDisplay :money="{ sats: row.sats, btc: row.btc, usd: row.usd }" :unit="displayUnit" />
              </span>
            </div>
          </div>

          <!-- Net fees paid — bolded to draw the eye to the summary
               line under the breakdown. The pct-of-revenue annotation
               is what operators glance at to decide whether the fee
               policy is in the right ballpark for their volume. -->
          <div class="kv-row total net-fees">
            <span class="kv-label">Net fees paid (dev + hosting + network):</span>
            <span class="kv-value">
              <MoneyDisplay :money="store.net_fees_paid" :unit="displayUnit" />
              <span class="kv-meta">
                ({{ formatPct(store.net_fees_pct) }} of revenue)
              </span>
            </span>
          </div>

          <!-- Savings vs credit-card baseline — larger green text under
               the breakdown. Dropdown lets the operator try different
               baseline percentages without a backend round-trip; the
               savings figure recomputes live via JS using the same
               formula the backend uses (revenue_usd × cc_pct −
               net_fees_paid_usd, clamped to >= 0). -->
          <div class="savings-row">
            <div class="savings-line">
              <span class="savings-label">Amount saved vs</span>
              <v-select
                v-model.number="ccPctSelected"
                :items="ccPctOptions"
                hide-details dense
                class="cc-pct-select mx-2"
                style="max-width: 90px;"
              />
              <span class="savings-label">credit-card baseline:</span>
              <span class="savings-value">
                <MoneyDisplay :money="savingsAtSelectedPct" :unit="displayUnit" />
              </span>
            </div>
            <!-- Sub-line: the would-be CC fee at the selected baseline.
                 Helps the operator sanity-check the savings number — if
                 the expected fee is small, big savings would look
                 suspicious. Same MoneyDisplay so sats/btc/usd track the
                 unit toggle at the top of the page. -->
            <div class="savings-meta">
              (expected credit-card fee:
              <MoneyDisplay :money="expectedCcFeeAtSelectedPct" :unit="displayUnit" />)
            </div>
          </div>

          <!-- Inbound liquidity moved out of per-store cards into the
               new Liquidity stats section (one card with per-wallet
               rows + totals). Two stores sharing a wallet used to
               each show the same number on their card, which was
               confusing — the new section is per-wallet, so each
               figure appears exactly once. -->
          <!-- (Intentionally empty — see Liquidity stats card.) -->
        </v-col>

        <!-- Pie chart column -->
        <v-col cols="12" md="5" class="d-flex flex-column align-center">
          <div class="pie-wrapper">
            <canvas ref="pieCanvas"></canvas>
          </div>
          <div v-if="(store.pie_slices.developer + store.pie_slices.hosting + store.pie_slices.network) === 0" class="text-caption grey--text mt-2">
            (no fees paid yet)
          </div>
        </v-col>
      </v-row>
    </v-card-text>
    </v-expand-transition>
  </v-card>
</template>

<script>
import { Chart, ArcElement, Tooltip, Legend, DoughnutController } from "chart.js"
import { formatBtcSats, formatUsd, formatPct, formatNumber, formatAmount } from "./format.js"
import MoneyDisplay from "./MoneyDisplay.vue"

// Chart.register has to happen once before any chart renders. Doing it at
// module load (not inside mounted) means chart.js sees the same registry
// regardless of which StoreCard instance renders first. Safe to call
// multiple times — chart.js dedupes.
Chart.register(ArcElement, Tooltip, Legend, DoughnutController)

export default {
  name: "StoreCard",
  components: { MoneyDisplay },
  props: {
    store: { type: Object, required: true },
    includeInbound: { type: Boolean, default: true },
    isSummary: { type: Boolean, default: false },
    // The plugin's CURRENT settings (from /api/plugins/settings/
    // liquidityhelper). The fee-rate display needs `FEE_AMOUNT`
    // (developer) and `REFERRAL_FEE_AMOUNT` (hosting). Optional —
    // the card renders without configured-rate annotations if not
    // provided (e.g. in unit tests).
    settings: { type: Object, default: () => ({}) },
    // CC baseline percentage from the dashboard response (e.g. 0.05
    // for 5%). Used as the initial value for the dropdown; the
    // dropdown can then override it without a backend round-trip.
    initialCcPct: { type: Number, default: 0.05 },
    // Display unit for monetary amounts: "sats" (default) or "btc".
    // The USD equivalent is always shown in parentheses regardless.
    // Controlled by the toggle at the top of the dashboard page in
    // index.vue.
    displayUnit: { type: String, default: "sats" },
  },
  data() {
    return {
      // Collapsed flag for the Fee-breakdown card. Defaults to false
      // so the section is open on first load. Per-instance — each
      // store has its own collapse state. The summary variant
      // (isSummary=true) is rendered without a title, so this flag
      // is ignored there (the v-show in the template gates on
      // `!collapsed || isSummary`).
      collapsed: false,
      // Persistent ref to the Chart instance so we can destroy it on
      // store-data changes. Chart.js leaks canvases otherwise.
      chartInstance: null,
      // Selected credit-card baseline percentage. Drives the savings
      // recompute. We initialize from the prop and let the user
      // change it freely; the change is purely local (does not POST
      // back to settings).
      ccPctSelected: this.initialCcPct,
      ccPctOptions: [
        { text: "3%", value: 0.03 },
        { text: "4%", value: 0.04 },
        { text: "5%", value: 0.05 },
        { text: "10%", value: 0.10 },
        { text: "15%", value: 0.15 },
      ],
    }
  },
  computed: {
    // Configured fee rates pulled from the plugin's settings, exposed
    // as decimals (matches the `developer_fee_pct` / `hosting_fee_pct`
    // shape so formatPct() handles both uniformly). Returns null when
    // the setting is missing or non-numeric so the template can omit
    // the "configured rate" annotation cleanly.
    developerRateConfigured() {
      const v = Number(this.settings.FEE_AMOUNT)
      return Number.isFinite(v) ? v : null
    },
    hostingRateConfigured() {
      const v = Number(this.settings.REFERRAL_FEE_AMOUNT)
      return Number.isFinite(v) ? v : null
    },
    // Balance = due − paid in sats. Positive = engine will try to
    // charge this much next tick (possibly minus network-fee credit
    // depending on FEES_PAID_INCLUDES_*_NETWORK_FEES). Negative =
    // operator has over-delivered (e.g. via FORCE_FEE_AMOUNT) and
    // the engine will pay nothing until eligible revenue catches up.
    developerBalanceSats() {
      const paid = Number(this.store?.developer_fees_paid?.sats || 0)
      const due = Number(this.store?.developer_fees_due?.sats || 0)
      return due - paid
    },
    hostingBalanceSats() {
      const paid = Number(this.store?.hosting_fees_paid?.sats || 0)
      const due = Number(this.store?.hosting_fees_due?.sats || 0)
      return due - paid
    },
    developerBalance() {
      return this._asMoney(Math.max(0, this.developerBalanceSats))
    },
    developerOverpayment() {
      return this._asMoney(Math.max(0, -this.developerBalanceSats))
    },
    hostingBalance() {
      return this._asMoney(Math.max(0, this.hostingBalanceSats))
    },
    hostingOverpayment() {
      return this._asMoney(Math.max(0, -this.hostingBalanceSats))
    },
    // Live-recomputed savings. Mirrors what the backend does in
    // `compute_dashboard`: revenue × cc_pct − net_fees_paid, clamped
    // to >= 0 so a high net-fee period doesn't show "negative
    // savings". Computed in sats first so we keep precision; the USD
    // amount is derived via the same per-sat rate the rest of the
    // card uses (so all three formats stay in lockstep).
    savingsAtSelectedPct() {
      const revenueSats = Number(this.store?.revenue?.sats || 0)
      const netFeesSats = Number(this.store?.net_fees_paid?.sats || 0)
      const ccBaselineSats = Math.round(revenueSats * this.ccPctSelected)
      const savedSats = Math.max(0, ccBaselineSats - netFeesSats)
      const rate = this.usdPerSat
      return {
        sats: savedSats,
        btc: savedSats / 100000000,
        usd: rate !== null ? savedSats * rate : null,
      }
    },
    // Sibling of savingsAtSelectedPct: the would-be credit-card fee at
    // the selected baseline (revenue × cc_pct, NO net-fees subtraction
    // or clamping). Surfaced under the savings line as context — the
    // operator wants to see what the card processor would have skimmed,
    // not just the net savings. Same {sats, btc, usd} shape as the
    // other money values so MoneyDisplay renders it identically.
    expectedCcFeeAtSelectedPct() {
      const revenueSats = Number(this.store?.revenue?.sats || 0)
      const ccBaselineSats = Math.round(revenueSats * this.ccPctSelected)
      const rate = this.usdPerSat
      return {
        sats: ccBaselineSats,
        btc: ccBaselineSats / 100000000,
        usd: rate !== null ? ccBaselineSats * rate : null,
      }
    },
    feeRows() {
      const b = this.store.network_fee_breakdown || {}
      // [key, label, optional tooltip]. Tooltip is rendered as an
      // info-circle icon next to the label.
      const labels = [
        ["onchain_payouts",            "On-chain payout miner fees"],
        ["onchain_fee_payments",       "On-chain dev-fee payment miner fees"],
        ["onchain_referral_payments",  "On-chain hosting-fee payment miner fees"],
        ["onchain_topup_returns",      "On-chain topup return miner fees"],
        ["onchain_channel_opens",      "On-chain channel-open miner fees"],
        ["onchain_channel_closes",     "On-chain channel-close miner fees"],
        ["onchain_swaps",              "On-chain swap miner fees"],
        ["onchain_lsp_orders",         "On-chain LSP order miner fees"],
        ["lsp_service_fees",           "LSP service fees (channel rental)"],
        ["onchain_external",           "On-chain external / manual send miner fees"],
        ["ln_payouts",                 "Lightning payout fees"],
        ["ln_fee_payments",            "Lightning dev-fee payment fees"],
        ["ln_referral_payments",       "Lightning hosting-fee payment fees"],
        ["ln_rebalances",              "Lightning rebalance routing fees",
          "Routing fees paid to other LN nodes when the engine performs " +
          "small circular rebalances. Rebalances quietly cycle sats between " +
          "your channels to keep them in use, discouraging peers from closing " +
          "them and maintaining inbound capacity for customer payments. The " +
          "yearly budget is configurable on the Settings tab under Channel " +
          "rebalancing."],
        ["ln_misc",                    "Lightning misc routing fees"],
      ]
      const rate = this.usdPerSat
      return labels.map(([key, label, tooltip]) => {
        const sats = Number(b[key] || 0)
        return {
          key, label, tooltip: tooltip || null,
          sats,
          btc: sats / 100000000,
          usd: rate !== null ? sats * rate : null,
        }
      }).filter((row) => row.sats > 0)
    },
    usdPerSat() {
      const m = this.store.revenue || this.store.net_fees_paid
      if (!m || m.usd === null || m.usd === undefined || !m.sats) return null
      return m.usd / m.sats
    },
  },
  watch: {
    "store.pie_slices": {
      deep: true,
      handler() { this.renderChart() },
    },
  },
  mounted() {
    this.renderChart()
  },
  beforeDestroy() {
    if (this.chartInstance) {
      this.chartInstance.destroy()
      this.chartInstance = null
    }
  },
  methods: {
    formatBtcSats, formatUsd, formatPct, formatNumber, formatAmount,
    // Synthesize a _Money-shaped object from a sat amount so the
    // existing formatters work uniformly. USD per sat is borrowed from
    // the same rate-source the rest of the card uses; null when the
    // dashboard couldn't fetch a rate.
    _asMoney(sats) {
      const s = Math.max(0, Math.round(Number(sats) || 0))
      const rate = this.usdPerSat
      return {
        sats: s,
        btc: s / 100000000,
        usd: rate !== null ? s * rate : null,
      }
    },
    renderChart() {
      const canvas = this.$refs.pieCanvas
      if (!canvas) return
      const slices = this.store.pie_slices || {}
      const total = (slices.developer || 0) + (slices.hosting || 0) + (slices.network || 0)
      if (this.chartInstance) {
        this.chartInstance.destroy()
        this.chartInstance = null
      }
      if (total === 0) return
      this.chartInstance = new Chart(canvas, {
        type: "doughnut",
        data: {
          labels: ["Developer fees", "Hosting/setup fees", "Network fees"],
          datasets: [{
            data: [
              slices.developer || 0,
              slices.hosting || 0,
              slices.network || 0,
            ],
            backgroundColor: ["#1976D2", "#FFB300", "#43A047"],
            borderWidth: 1,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { position: "bottom" },
            tooltip: {
              callbacks: {
                label: (ctx) => {
                  const v = ctx.parsed
                  const pct = total > 0 ? (v / total * 100).toFixed(1) : "0"
                  return `${ctx.label}: ${v.toLocaleString()} sats (${pct}%)`
                },
              },
            },
          },
        },
      })
    },
  },
}
</script>

<style scoped>
/* Bolded summary row: net fees paid (dev + hosting + network). */
.net-fees {
  font-weight: 600;
}

/* Savings vs CC baseline — visually emphasized: larger green text.
   Sits beneath the breakdown in the same card. The dropdown is
   inline so the operator can flip baselines without breaking
   reading flow. */
.savings-row {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}
.savings-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  font-size: 1.05rem;
  color: #4caf50;       /* green */
  font-weight: 600;
}
.savings-label { white-space: nowrap; }
.savings-value { margin-left: 6px; }
/* Sub-line under .savings-line showing the would-be CC fee at the
   selected baseline. Smaller and muted — informational context, not
   the headline. Sits inline with the embedded MoneyDisplay span. */
.savings-meta {
  font-size: 0.85em;
  font-weight: 400;
  color: rgba(0, 0, 0, 0.6);
  margin-top: 2px;
  line-height: 1.3;
}
.theme--dark .savings-meta {
  color: rgba(255, 255, 255, 0.6);
}
/* Make the inline v-select compact so it doesn't dwarf the row. */
.cc-pct-select :deep(.v-input__control) {
  min-height: 28px;
}
.cc-pct-select :deep(.v-input__slot) {
  min-height: 28px;
}

/* Inbound liquidity — same size as savings, but in BareBits brand
   orange (#F9A410) so the two key bottom-of-card lines are clearly
   distinct ("money saved" vs "money you can still receive"). */
.liquidity-row {
  margin-top: 6px;
  font-size: 1.05rem;
  color: #F9A410;
  font-weight: 600;
}
.liquidity-label { margin-right: 6px; }
.liquidity-meta { font-weight: 400; font-size: 0.9em; opacity: 0.9; }

/* Balance pill following the "paid of due" annotation. `owed` is
   amber so it reads as a soft warning (not an error — paying a few
   sats short is normal between ticks); `overpaid` is green because
   overpayment is fine. */
.kv-balance {
  margin-left: 6px;
  font-weight: 600;
  font-size: 0.95em;
}
.kv-balance.owed { color: #FFB300; }
.kv-balance.overpaid { color: #4caf50; }

/* Clickable v-card-title used to collapse/expand the card. Mirror
   of the same selector in pages/index.vue — restated here since
   that file's <style scoped> doesn't reach this component's
   template (Vue scoped CSS scopes to the parent's template only). */
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

/* Info-circle icon next to a fee-breakdown row label. Hover gives a
   native tooltip via the title attribute. Muted color so the icon
   doesn't compete with the actual fee values. */
.fee-info-icon {
  opacity: 0.6;
  cursor: help;
  vertical-align: baseline;
}
.fee-info-icon:hover { opacity: 1; }

/* ---------------------------------------------------------------
   Key/value row styling — label on the left, value on the right,
   with an optional muted "meta" inline annotation. These styles
   used to live in pages/index.vue, but that file's <style scoped>
   block scopes selectors to its own template — the elements they
   target are rendered HERE in StoreCard, so the index.vue rules
   never actually applied. Result: in dark mode, the indented
   fee-breakdown rows fell back to whatever Vuetify defaulted to,
   which blended against the card background. Moving the rules
   into this file (where the elements actually live) makes them
   take effect, and the .theme--dark variants below give each
   element a dark-mode-appropriate color.
   ---------------------------------------------------------------*/
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

/* Card backgrounds. Light-mode defaults; .theme--dark overrides
   below give Vuetify-aligned dark surfaces. */
.store-card {
  background-color: #fafafa;
}
.summary-card {
  border: 2px solid #1976D2;
  background-color: #f5f9fc;
}

/* ---------------------------------------------------------------
   Dark-mode overrides. Bitcart toggles Vuetify's $vuetify.theme.dark
   which puts `.theme--dark` on the v-application root + every v-card
   etc. We piggyback on that class to swap our hardcoded light
   values for dark-surface-appropriate ones. Kept here (not in a
   global stylesheet) so the rules are co-located with their
   light-mode siblings and scoped to this component's elements.
   ---------------------------------------------------------------*/
.theme--dark .store-card {
  /* Slightly lighter than the page surface so the card still
     reads as a distinct panel on a dark page background. */
  background-color: #1e1e1e;
}
.theme--dark .summary-card {
  background-color: #1a2632;
  border-color: #1976D2; /* keep the brand-blue outline */
}

/* Row separators: subtle white on dark, instead of #eee/#ccc. */
.theme--dark .kv-row {
  border-bottom-color: rgba(255, 255, 255, 0.08);
}
.theme--dark .kv-row.total {
  border-top-color: rgba(255, 255, 255, 0.16);
}

/* Text contrast. Vuetify's dark-mode default body text is
   rgba(255,255,255,0.87) — but our explicit color: #555 / #999
   from above would override that to a near-invisible dark grey
   on a dark card. Restore high-contrast variants. */
.theme--dark .kv-label {
  color: rgba(255, 255, 255, 0.87);
}
.theme--dark .kv-row.indented {
  color: rgba(255, 255, 255, 0.87);
}
.theme--dark .kv-meta {
  /* Meta stays subordinate to the primary value — slightly muted
     but still clearly readable against the dark card. */
  color: rgba(255, 255, 255, 0.6);
}

/* The fee breakdown's light-grey background panel becomes a
   subtle lighter shade on dark mode so it still reads as a
   distinct subsection (rather than the same color as the card). */
.theme--dark .fee-breakdown {
  background-color: rgba(255, 255, 255, 0.04);
}
</style>
