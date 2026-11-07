// Materialize Nitro's internal links so the exported archive contains regular files only.
import fs from 'node:fs'
import path from 'node:path'
const root = fs.realpathSync('.output')
let bytes = 0, count = 0
function copy(source, target, parents = new Set()) {
  const real = fs.realpathSync(source)
  if (real !== root && !real.startsWith(root + path.sep)) throw new Error('Build link escapes output')
  const stat = fs.statSync(real)
  if (stat.isDirectory()) {
    if (parents.has(real)) throw new Error('Build contains a link cycle')
    const next = new Set(parents); next.add(real)
    fs.mkdirSync(target, { recursive: true })
    for (const name of fs.readdirSync(real)) copy(path.join(real, name), path.join(target, name), next)
  } else if (stat.isFile()) {
    bytes += stat.size; count++
    if (bytes > 20000000 || count > 10000) throw new Error('Build export exceeds factory limits')
    fs.copyFileSync(real, target)
  } else throw new Error('Unexpected build entry')
}
fs.rmSync('release', { recursive: true, force: true })
copy(root, 'release')
console.log(`Exported ${count} regular files, ${bytes} bytes`)
