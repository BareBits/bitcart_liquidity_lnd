// Vuems module config. Bitcart reads this at admin build time and
// uses it to wire aliases, Nuxt plugins, and route generation.
//
// plugins: declared as client-only — they need access to `$auth`
// state, which is hydrated on the client. The vuems helper resolves
// `src` relative to this module's root (no .js suffix), so
// "plugins/nav-injector" → modules/@barebits/liquidityhelper/plugins/nav-injector.js.
export default {
  name: "@barebits/liquidityhelper",
  aliases: {
    "@LiquidityHelper": "/",
  },
  plugins: [
    { src: "plugins/nav-injector", ssr: false },
  ],
}
