export default defineEventHandler(async (event) => {
  const body = await readBody(event)
  if (typeof body?.email !== 'string' || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(body.email) || body.email.length > 254) {
    throw createError({ statusCode: 400, statusMessage: 'A valid email is required' })
  }
  return { message: 'Thanks! Your demo enquiry is valid.' }
})
