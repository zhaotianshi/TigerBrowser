import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const entry = new URL('../src/diagnostics/collectors.ts', import.meta.url)
const source = (await readFile(entry, 'utf8')).replaceAll('\r\n', '\n')
const digest = createHash('sha256').update(source).digest('hex')
await build({
  entryPoints: [fileURLToPath(entry)],
  outfile: fileURLToPath(new URL('../../../pythonlib/camoufox/studio/team/probe.js', import.meta.url)),
  bundle: true,
  platform: 'browser',
  target: 'es2017',
  format: 'iife',
  globalName: 'CamoufoxProbe',
  minify: true,
  banner: { js: `// Collector source SHA256: ${digest}` },
})
