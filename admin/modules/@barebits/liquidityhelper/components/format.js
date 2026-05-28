// Shared display formatters used by both pages/index.vue and StoreCard.vue.
// One place that defines how every monetary amount renders. The spec is
// "BTC (sats) / USD (approx)" — keep both forms visible because the
// dashboard is consumed by operators who think in sats AND owners who
// think in dollars.

export function formatNumber(n, decimals = 2) {
  // null/undefined → '—'. Operators expect a clean dash, not "NaN".
  if (n === null || n === undefined || Number.isNaN(n)) return "—"
  return Number(n).toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

export function formatBtcSats(money) {
  // money = {sats, btc, usd}. Each field is independently nullable.
  // Retained for any legacy template still using the dual-form render;
  // new templates should prefer formatAmount(money, unit) instead.
  if (!money) return "—"
  const btcStr = formatNumber(money.btc, 8)
  const satsStr = formatNumber(money.sats, 0)
  return `${btcStr} BTC (${satsStr} sats)`
}

export function formatUsd(money) {
  if (!money || money.usd === null || money.usd === undefined) {
    return "$— (rate unavailable)"
  }
  return `$${formatNumber(money.usd, 2)}`
}

// Format a _Money triple in the operator's selected unit, with the
// USD equivalent always shown in parentheses. The unit toggle lives
// at the top of the dashboard and propagates via the `displayUnit`
// prop (StoreCard) / data field (index.vue).
//
//   formatAmount({sats:100000, btc:0.001, usd:100.0}, "sats")
//     → "100,000 sats ($100.00)"
//   formatAmount({sats:100000, btc:0.001, usd:100.0}, "btc")
//     → "0.00100000 BTC ($100.00)"
//   formatAmount({sats:100000, btc:0.001, usd:null}, "sats")
//     → "100,000 sats ($—)"
//
// Defaults to sats when an unrecognized unit is passed — safer than
// rendering raw BTC by accident on a startup-state miss.
export function formatAmount(money, unit) {
  if (!money) return "—"
  const main = unit === "btc"
    ? `${formatNumber(money.btc, 8)} BTC`
    : `${formatNumber(money.sats, 0)} sats`
  const usd = (money.usd === null || money.usd === undefined)
    ? "$—"
    : `$${formatNumber(money.usd, 2)}`
  return `${main} (${usd})`
}

// Convenience for payment-row tables that store amounts as separate
// `sats` / `usd` columns (not a packed _Money object). Synthesizes a
// _Money triple and delegates to formatAmount.
export function formatAmountFromSats(sats, usd, unit) {
  const s = Number(sats) || 0
  return formatAmount({
    sats: s,
    btc: s / 100000000,
    usd: (usd === null || usd === undefined) ? null : usd,
  }, unit)
}

export function formatPct(pct) {
  // 0/0 ratios come back as null from the backend → render '—'.
  if (pct === null || pct === undefined) return "—"
  return `${formatNumber(pct * 100, 2)}%`
}

// Heuristic when the schema doesn't provide an explicit `type`. Mirrors
// what /manage/policies.vue does: anything non-boolean/number falls
// through to the default checkbox/string control depending on context.
export function guessType(value) {
  if (typeof value === "boolean") return "checkbox"
  if (typeof value === "number") return "number"
  return "string"
}
