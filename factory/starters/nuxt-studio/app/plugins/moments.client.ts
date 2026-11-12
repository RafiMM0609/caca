/**
 * Mount the page's dealt moment. Runs on every route, on the client only.
 *
 * A worker never calls this. It writes `<section data-moment="photo-chapters">` and the moment
 * mounts itself, which is the point: the design decision was made by the dealer, and the
 * worker should not have to remember a wiring detail on top of it.
 *
 * Three measured facts shape this file:
 *
 * 1. **`moments.js` must not be bundled.** It resolves each moment with
 *    `new URL('<id>/moment.js', import.meta.url)`. When Vite bundles it into `/_nuxt/`, that
 *    base is a chunk directory, so every moment requests `/_nuxt/photo-chapters/moment.js` and
 *    gets a 404 served as `application/json`. Measured: both moments failed with
 *    `Failed to fetch dynamically imported module`, console errors on every load. Importing it
 *    from `public/` with `@vite-ignore` keeps `import.meta.url` on the real folder.
 *
 * 2. **Its CSS is already in the document** (`/assets/moments/bundle.css`, linked in
 *    `nuxt.config.ts`), because the loader injects a moment's stylesheet only after the module
 *    resolves, and until then `photo-chapters` stacks its photographs full-height on top of one
 *    another. That is a layout fault, not a missing animation.
 *
 * 3. **Nuxt does not reload the document**, so `moments.js`'s own load-time mount only covers
 *    the first route. Every navigation needs `mountAll` again; the loader itself is imported
 *    once and cached on `globalThis` so a second visit cannot add a second scroll listener.
 */

interface MomentApi {
  mountAll: (root?: ParentNode) => Promise<Element[]>
  ready: Promise<Element[]>
}

const KEY = '__factoryMoments'

function loader(): Promise<MomentApi | null> {
  const store = globalThis as unknown as Record<string, Promise<MomentApi | null> | undefined>
  if (!store[KEY]) {
    store[KEY] = import(/* @vite-ignore */ '/assets/moments/moments.js')
      .then((mod: MomentApi) => mod)
      .catch((error: Error) => {
        // A page with no moment must still render, so this does not throw. It is logged
        // rather than swallowed: a silently missing moment is the defect this change exists
        // to remove, and the checks read the DOM rather than this log.
        console.warn('[moments] the vendored moment layer did not load:', error.message)
        return null
      })
  }
  return store[KEY] ?? null
}

async function mount(): Promise<void> {
  const only = document.querySelector('[data-moment]')
  // Nothing on this page declares a moment: skip the import entirely, so a page that was not
  // dealt one pays no JavaScript for the layer.
  if (!only) return
  const api = await loader()
  if (!api) return
  await api.ready
  await api.mountAll(document)
}

export default defineNuxtPlugin((nuxtApp) => {
  nuxtApp.hook('app:mounted', () => { void mount() })
  nuxtApp.hook('page:finish', () => { void mount() })
})
