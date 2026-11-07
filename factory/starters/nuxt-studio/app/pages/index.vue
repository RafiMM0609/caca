<script setup lang="ts">
const { data: site } = await useAsyncData('site', () => queryCollection('site').first())
const email = ref('')
const feedback = ref('')
const busy = ref(false)
async function contact() {
  busy.value = true
  try {
    const result = await $fetch('/api/contact', { method: 'POST', body: { email: email.value } })
    feedback.value = result.message
  } catch { feedback.value = 'Please enter a valid email address.' }
  finally { busy.value = false }
}
useHead({ title: () => site.value?.brand || 'Studio preview' })
</script>
<template>
  <div v-if="site" class="site" :data-theme="site.accent">
    <header><a href="/" class="brand">{{ site.brand }}<span>®</span></a><a href="#contact">Start a conversation ↗</a></header>
    <main>
      <section class="hero"><div class="eyebrow">{{ site.eyebrow }}</div><h1 data-testid="hero-title">{{ site.title }}</h1><p>{{ site.description }}</p><a class="button" href="#contact">{{ site.buttonLabel }} <span>↗</span></a><div class="orb" aria-hidden="true"></div></section>
      <section class="features" aria-label="Our approach"><article v-for="(feature,index) in site.features" :key="index"><small>0{{ index + 1 }}</small><h2>{{ feature.title }}</h2><p>{{ feature.description }}</p></article></section>
      <section id="contact"><div><div class="eyebrow">THE NEXT CHAPTER</div><h2>Good things start<br>with a conversation.</h2><p>This local demo validates a form. It does not send emails.</p></div><form @submit.prevent="contact"><label for="email">Your email</label><input id="email" v-model="email" type="email" required placeholder="you@company.com"><button class="button" :disabled="busy" type="submit">Send enquiry ↗</button><p role="status" data-testid="contact-result">{{ feedback }}</p></form></section>
    </main><footer><span>{{ site.brand }} · Built with care</span><span>Make it your own in Studio</span></footer>
  </div>
</template>
<style>
*{box-sizing:border-box}body{margin:0;font-family:Inter,Arial,sans-serif;color:#182921;background:#f4f3ec}a{color:inherit;text-decoration:none}.site{--accent:#c9dfad;max-width:1400px;margin:auto;padding:0 7%}.site[data-theme=blue]{--accent:#b9d8ef}.site[data-theme=rose]{--accent:#ebc8c1}header,footer{display:flex;align-items:center;justify-content:space-between;padding:30px 0;border-bottom:1px solid #d5d9cb}header>a:last-child{font-size:13px}.brand{font-size:24px;font-weight:700;letter-spacing:-1px}.brand span{font-size:12px;vertical-align:top;margin-left:4px}.hero{padding:90px 0 80px;position:relative;overflow:hidden;min-height:560px}.eyebrow{font-size:10px;letter-spacing:2px;font-weight:700}h1{font-size:clamp(40px,6vw,78px);line-height:1.05;font-weight:500;letter-spacing:-3px;max-width:680px;margin:25px 0;position:relative;z-index:1}.hero p{max-width:470px;color:#58645b;font-size:16px;line-height:1.8;position:relative;z-index:1}.button{display:inline-flex;justify-content:space-between;gap:30px;background:#203b2d;color:white;padding:16px 22px;border:0;border-radius:6px;margin-top:20px;font:inherit;cursor:pointer;position:relative;z-index:1}.orb{height:300px;width:300px;border-radius:100%;background:var(--accent);box-shadow:inset -35px -25px 50px #0002;position:absolute;right:0;top:120px;z-index:0}.features{display:grid;grid-template-columns:repeat(3,1fr);gap:40px;border-top:1px solid #d5d9cb;padding:35px 0 70px}.features small{color:#637b56}.features h2{font-weight:500;font-size:20px}.features p{font-size:14px;line-height:1.8;color:#58645b}#contact{display:grid;grid-template-columns:1fr 1fr;gap:70px;background:var(--accent);padding:45px;border-radius:15px;margin-bottom:50px}#contact h2{font-size:35px;letter-spacing:-1px;font-weight:500}#contact p{font-size:13px;line-height:1.8}label{display:block;font-size:13px;margin:10px 0}input{width:100%;border:1px solid #91a17c;background:#ffffff80;padding:15px;border-radius:6px;font:inherit}footer{border-top:1px solid #d5d9cb;border-bottom:0;font-size:12px;color:#58645b}button:disabled{opacity:.5}@media(max-width:700px){.orb{opacity:.35;right:-80px}.hero{padding:60px 0}.features{grid-template-columns:1fr;gap:15px}#contact{grid-template-columns:1fr;padding:25px;gap:20px}footer{gap:20px}.site{padding:0 6%}}
</style>
