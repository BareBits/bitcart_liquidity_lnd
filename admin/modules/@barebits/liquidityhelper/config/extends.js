// Hooks into bitcart's admin extension points.
//
// We declare `nav_items` as an EMPTY array here on purpose. Bitcart's
// dictionaries store merges this with every other plugin's contribution
// at admin build time (see /src/store/dictionaries/state.js) — so the
// array becomes a stable, Vuex-observed reference that the sidebar's
// computed property re-reads on every push/splice.
//
// The actual nav item gets pushed at runtime by
// plugins/nav-injector.client.js once we can tell whether the current
// user is a superuser. That's the only way to get superuser-only
// visibility for a plugin-contributed sidebar entry: bitcart's
// `getExtendSetting` concatenates extended dictionaries unconditionally
// and ignores any `superuser: true` flag that the built-in items use.
// Reactively populating the array at runtime sidesteps that limitation
// without patching bitcart's layout.
export default {
  dictionaries: {
    nav_items: [],
  },
}
