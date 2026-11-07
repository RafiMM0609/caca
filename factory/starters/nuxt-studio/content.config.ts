import { defineCollection, defineContentConfig, z } from '@nuxt/content'
export default defineContentConfig({
  collections: {
    site: defineCollection({
      type: 'data', source: 'site.json',
      schema: z.object({
        brand: z.string(), eyebrow: z.string(), title: z.string(), description: z.string(),
        buttonLabel: z.string(), accent: z.enum(['sage', 'blue', 'rose']),
        features: z.array(z.object({ title: z.string(), description: z.string() }))
      })
    })
  }
})
