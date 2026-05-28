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

// Threshold above which a sats amount renders as e.g. "1.25M" instead
// of "1,250,000". The full digit version is still available via
// formatAmountFull() for tooltip use — the MoneyDisplay component
// pairs the abbreviated render with a `title` attribute carrying the
// full text. 1 million chosen because below that the comma-formatted
// number is still a reasonable column width.
export const SATS_ABBREVIATE_THRESHOLD = 1_000_000

// Format a sats integer in abbreviated form for amounts above the
// threshold, or comma-formatted otherwise.
//   abbreviateSats(100_000)    → "100,000 sats"
//   abbreviateSats(1_500_000)  → "1.50M sats"
//   abbreviateSats(125_000_000)→ "125.00M sats"
function abbreviateSats(sats) {
  const s = Math.round(Number(sats) || 0)
  if (Math.abs(s) >= SATS_ABBREVIATE_THRESHOLD) {
    return `${formatNumber(s / 1_000_000, 2)}M sats`
  }
  return `${formatNumber(s, 0)} sats`
}

// Format a _Money triple in the operator's selected unit, with the
// USD equivalent always shown in parentheses. The unit toggle lives
// at the top of the dashboard and propagates via the `displayUnit`
// prop (StoreCard) / data field (index.vue).
//
//   formatAmount({sats:100000, btc:0.001, usd:100.0}, "sats")
//     → "100,000 sats ($100.00)"
//   formatAmount({sats:1_500_000, btc:0.015, usd:1500.0}, "sats")
//     → "1.50M sats ($1,500.00)"
//   formatAmount({sats:100000, btc:0.001, usd:100.0}, "btc")
//     → "0.00100000 BTC ($100.00)"
//   formatAmount({sats:100000, btc:0.001, usd:null}, "sats")
//     → "100,000 sats ($—)"
//
// Defaults to sats when an unrecognized unit is passed — safer than
// rendering raw BTC by accident on a startup-state miss.
//
// Use the MoneyDisplay component (not raw {{ formatAmount(...) }})
// at every dashboard render site so the abbreviated form gets a
// hover tooltip showing the un-abbreviated digits.
export function formatAmount(money, unit) {
  if (!money) return "—"
  const main = unit === "btc"
    ? `${formatNumber(money.btc, 8)} BTC`
    : abbreviateSats(money.sats)
  const usd = (money.usd === null || money.usd === undefined)
    ? "$—"
    : `$${formatNumber(money.usd, 2)}`
  return `${main} (${usd})`
}

// Un-abbreviated version of formatAmount, used by MoneyDisplay as the
// tooltip text so operators who hover see the full digit count for
// amounts >= 1M sats. Always uses comma-formatted sats regardless of
// the configured threshold.
export function formatAmountFull(money, unit) {
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
  return formatAmount(_synthesizeMoney(sats, usd), unit)
}

// Un-abbreviated counterpart of formatAmountFromSats — same role as
// formatAmountFull but for the (sats, usd) shape.
export function formatAmountFromSatsFull(sats, usd, unit) {
  return formatAmountFull(_synthesizeMoney(sats, usd), unit)
}

function _synthesizeMoney(sats, usd) {
  const s = Number(sats) || 0
  return {
    sats: s,
    btc: s / 100000000,
    usd: (usd === null || usd === undefined) ? null : usd,
  }
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
