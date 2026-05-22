<template>
  <v-card outlined class="mb-4 store-card">
    <v-card-title v-if="!isSummary">Fee breakdown</v-card-title>
    <v-card-text>
      <v-row>
        <v-col cols="12" md="7">
          <!-- Revenue + sales -->
          <div class="kv-row">
            <span class="kv-label">Total revenue:</span>
            <span class="kv-value">
              {{ formatBtcSats(store.revenue) }} / {{ formatUsd(store.revenue) }}
            </span>
          </div>
          <div class="kv-row">
            <span class="kv-label">Total paid invoices:</span>
            <span class="kv-value">{{ store.paid_invoice_count }}</span>
          </div>

          <!-- Developer fee. Showing both the ACTUAL paid percentage
               and the CONFIGURED rate (from settings) so the operator
               can see whether the plugin's collection lines up with
               what they've set the rate to. -->
          <div class="kv-row">
            <span class="kv-label">Developer fees paid:</span>
            <span class="kv-value">
              {{ formatBtcSats(store.developer_fees_paid) }} /
              {{ formatUsd(store.developer_fees_paid) }}
              <span class="kv-meta">
                ({{ formatPct(store.developer_fee_pct) }} of revenue<span
                  v-if="developerRateConfigured !== null">,
                  configured rate {{ formatPct(developerRateConfigured) }}</span>)
              </span>
            </span>
          </div>

          <!-- Hosting/referral fee -->
          <div class="kv-row">
            <span class="kv-label">Hosting / setup fees paid:</span>
            <span class="kv-value">
              {{ formatBtcSats(store.hosting_fees_paid) }} /
              {{ formatUsd(store.hosting_fees_paid) }}
              <span class="kv-meta">
                ({{ formatPct(store.hosting_fee_pct) }} of revenue<span
                  v-if="hostingRateConfigured !== null">,
                  configured rate {{ formatPct(hostingRateConfigured) }}</span>)
              </span>
            </span>
          </div>

          <!-- Network fees -->
          <div class="kv-row">
            <span class="kv-label">Network fees (total):</span>
            <span class="kv-value">
              {{ formatBtcSats(store.network_fees_total) }} /
              {{ formatUsd(store.network_fees_total) }}
            </span>
          </div>
          <!-- Indented breakdown — only shown for non-zero rows. -->
          <div v-if="feeRows.length" class="fee-breakdown">
            <div v-for="row in feeRows" :key="row.key" class="kv-row indented">
              <span class="kv-label">{{ row.label }}:</span>
              <span class="kv-value">
                {{ formatBtcSats({ sats: row.sats, btc: row.btc }) }} /
                <span v-if="row.usd !== null">${{ formatNumber(row.usd, 2) }}</span>
                <span v-else>$— (rate unavailable)</span>
              </span>
            </div>
          </div>

          <!-- Net fees paid — bolded to draw the eye to the summary
               line under the breakdown. -->
          <div class="kv-row total net-fees">
            <span class="kv-label">Net fees paid (dev + hosting + network):</span>
            <span class="kv-value">
              {{ formatBtcSats(store.net_fees_paid) }} / {{ formatUsd(store.net_fees_paid) }}
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
                {{ formatBtcSats(savingsAtSelectedPct) }} /
                {{ formatUsd(savingsAtSelectedPct) }}
              </span>
            </div>
          </div>

          <!-- Inbound liquidity — per-store only, omitted in summary.
               Same font size as savings but in the BareBits brand color
               (#F9A410). -->
          <div v-if="includeInbound" class="liquidity-row">
            <span class="liquidity-label">Inbound liquidity:</span>
            <span class="liquidity-value">
              {{ formatBtcSats(store.inbound_liquidity) }} /
              {{ formatUsd(store.inbound_liquidity) }}
              <span class="liquidity-meta">
                ({{ store.active_channel_count }} active channel{{ store.active_channel_count === 1 ? "" : "s" }})
              </span>
            </span>
          </div>
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
  </v-card>
</template>

<script>
import { Chart, ArcElement, Tooltip, Legend, DoughnutController } from "chart.js"
import { formatBtcSats, formatUsd, formatPct, formatNumber } from "./format.js"

// Chart.register has to happen once before any chart renders. Doing it at
// module load (not inside mounted) means chart.js sees the same registry
// regardless of which StoreCard instance renders first. Safe to call
// multiple times — chart.js dedupes.
Chart.register(ArcElement, Tooltip, Legend, DoughnutController)

export default {
  name: "StoreCard",
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
  },
  data() {
    return {
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
    feeRows() {
      const b = this.store.network_fee_breakdown || {}
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
        ["ln_payouts",                 "Lightning payout fees"],
        ["ln_fee_payments",            "Lightning dev-fee payment fees"],
        ["ln_referral_payments",       "Lightning hosting-fee payment fees"],
        ["ln_misc",                    "Lightning misc routing fees"],
      ]
      const rate = this.usdPerSat
      return labels.map(([key, label]) => {
        const sats = Number(b[key] || 0)
        return {
          key, label,
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
    formatBtcSats, formatUsd, formatPct, formatNumber,
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
</style>
