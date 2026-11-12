export default defineNuxtConfig({
  compatibilityDate: '2026-11-01',
  modules: ['@nuxt/content', 'nuxt-studio'],
  $production: { studio: false },
  devtools: { enabled: false },
  studio: { route: '/_studio' },
  content: { experimental: { sqliteConnector: 'native' } },

  app: {
    head: {
      // The self-hosted type families, the design-token bridge and the moment stylesheets.
      //
      // All three are static `<link>`s and not bundled CSS, on purpose:
      //  - `fonts.css` declares every family the dealer can pick (latin woff2, ~1.4 MB on
      //    disk); a page downloads only the family its own `@font-face` usage resolves to.
      //    Measured: a page using two families fetched exactly two files. This also removes
      //    the network dependency a Google Fonts `<link>` had at render time.
      //  - `design-tokens.css` maps the page's `--p-*` palette onto the `--bg`/`--ink`/...
      //    names the vendored moment and motion layers read. Without it a moment renders
      //    unstyled and silently.
      //  - `moments/bundle.css` lays a dealt moment out before its JavaScript runs, so a
      //    `photo-chapters` section is correct with scripts off.
      link: [
        { rel: 'stylesheet', href: '/assets/fonts/fonts.css' },
        { rel: 'stylesheet', href: '/assets/design-tokens.css' },
        { rel: 'stylesheet', href: '/assets/motion/motion.css' },
        { rel: 'stylesheet', href: '/assets/moments/bundle.css' },
      ],
    },
  },
})
