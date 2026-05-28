<template>
  <!-- Native browser `title` attribute = lightweight tooltip, no
       Vuetify v-tooltip wrapper needed. Hovering the abbreviated text
       reveals the un-abbreviated digits + USD; tooltip is identical
       on amounts below the 1M-sat threshold (the visible text already
       shows the full digit count). -->
  <span :title="fullText">{{ displayText }}</span>
</template>

<script>
import {
  formatAmount,
  formatAmountFull,
  formatAmountFromSats,
  formatAmountFromSatsFull,
} from "./format.js"

// Drop-in replacement for `{{ formatAmount(money, unit) }}` and
// `{{ formatAmountFromSats(sats, usd, unit) }}` in dashboard templates.
//
// Why a component instead of just a helper: the user-visible behavior
// is now `<abbreviated text>` + `<tooltip with full text>`, which
// requires a DOM element with a `title` attribute. Templates that
// just interpolate via `{{ ... }}` can't carry a hover tooltip.
// Wrapping every call site in `<span :title="fullFn(...)">{{ ... }}</span>`
// was the alternative (~30 sites) — a single shared component is
// less duplication and gets the abbreviation-threshold and tooltip
// behavior in one place.
//
// Accepts either:
//   :money="{sats, btc, usd}"             // _Money object
// or:
//   :sats="123456" :usd="...nullable..."  // synthesized _Money
//
// `unit` follows the dashboard's BTC/sats toggle (default sats).
// USD is always shown in parentheses regardless of unit selection.
export default {
  name: "MoneyDisplay",
  props: {
    money: { type: Object, default: null },
    sats: { type: Number, default: null },
    usd: { type: Number, default: null },
    unit: { type: String, default: "sats" },
  },
  computed: {
    displayText() {
      if (this.money) return formatAmount(this.money, this.unit)
      if (this.sats !== null && this.sats !== undefined) {
        return formatAmountFromSats(this.sats, this.usd, this.unit)
      }
      return "—"
    },
    fullText() {
      if (this.money) return formatAmountFull(this.money, this.unit)
      if (this.sats !== null && this.sats !== undefined) {
        return formatAmountFromSatsFull(this.sats, this.usd, this.unit)
      }
      return "—"
    },
  },
}
</script>
