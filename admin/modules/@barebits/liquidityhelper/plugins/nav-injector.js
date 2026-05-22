// Adds a "Liquidity Helper" entry to the bitcart admin sidebar, but
// ONLY when the current user is a superuser.
//
// Why this can't live in `config/extends.js` directly:
//   Bitcart's `getExtendSetting("nav_items", defaults)` is just
//   `[...defaults, ...(extended || [])]` — it has zero awareness of
//   auth state, and the built-in `superuser: true` filter applies only
//   to the layout's hardcoded items (see /src/layouts/default.vue,
//   `availableItems` computed). So plugin-contributed nav items
//   unconditionally show to every logged-in user.
//
// Workaround:
//   `extends.js` declares an empty `nav_items: []` so the Vuex store
//   has the array observed at boot. This client-only Nuxt plugin
//   watches `state.auth.user.is_superuser`; when it flips, we push or
//   splice our item. Vuex's reactivity propagates the change through
//   the default layout's `availableItems` computed and Vuetify
//   re-renders the drawer.
//
// Defense in depth:
//   The page route itself is gated with `middleware: "superuserOnly"`
//   in pages/index.vue, so even if the entry leaked into the sidebar
//   for a non-superuser (e.g. via store debugging tools) the route
//   would still redirect them to "/".

const NAV_ITEM = Object.freeze({
  icon: "mdi-water-pump",
  text: "Liquidity Helper",
  to: "/plugins/liquidityhelper",
  order: 13,
})

export default ({ store }) => {
  // Hard client-only guard. vuems' setPlugins helper accepts an
  // { ssr: false } flag in config/index.js but actually passes
  // `mode` inside `options:` to Nuxt's addPlugin — which Nuxt
  // ignores, so the registration ends up as "mode: all" (runs on
  // both server and client). Bailing on process.server here makes
  // sure we don't mutate the SSR-rendered dictionaries store, which
  // ends up baked into the hydration payload and can interact badly
  // with downstream rendering. Pure client side from here.
  if (process.server) return

  // Defensive: if the dictionaries module hasn't initialized for any
  // reason (custom build, fork without that store), don't crash the
  // whole admin — just no-op. The route-level middleware still
  // protects access.
  const dict = store.state && store.state.dictionaries
  if (!dict || !Array.isArray(dict.nav_items)) return

  const items = dict.nav_items

  const sync = (isSuperuser) => {
    const idx = items.findIndex((i) => i && i.to === NAV_ITEM.to)
    if (isSuperuser) {
      if (idx === -1) items.push({ ...NAV_ITEM })
    } else if (idx !== -1) {
      items.splice(idx, 1)
    }
  }

  // `immediate: true` fires synchronously with the current value, so
  // page reloads where the user is already authenticated land with
  // the entry present from first paint. Subsequent login/logout flips
  // it on or off without a refresh.
  store.watch(
    (state) =>
      Boolean(
        state.auth &&
          state.auth.user &&
          state.auth.user.is_superuser
      ),
    sync,
    { immediate: true }
  )
}
