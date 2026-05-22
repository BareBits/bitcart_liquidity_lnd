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
