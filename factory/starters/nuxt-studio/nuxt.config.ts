export default defineNuxtConfig({
  compatibilityDate: '2026-11-01',
  modules: ['@nuxt/content', 'nuxt-studio'],
  $production: { studio: false },
  devtools: { enabled: false },
  studio: { route: '/_studio' },
  content: { experimental: { sqliteConnector: 'native' } }
})
