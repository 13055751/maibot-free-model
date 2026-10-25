/**
 * Vendoring script: copies the standalone service's import closure out of a
 * dsh-our-free-model checkout into this plugin's `vendor/` tree.
 *
 * Run from the plugin root:
 *   node sidecar/vendor.mjs ../dsh-our-free-model
 *
 * The closure is computed by walking relative imports from the entry points, so
 * a future upstream refactor that moves code between files is picked up
 * automatically. Absolute `@deepseek-ai/*` imports outside adapter/kernel.js are
 * a hard failure: the sidecar must not depend on a DSH host.
 */
import fs from 'node:fs'
import path from 'node:path'

const ROOT = path.resolve(import.meta.dirname, '..')
const SOURCE = path.resolve(process.argv[2] ?? '../dsh-our-free-model')
const OUT = path.join(ROOT, 'sidecar', 'vendor')

const ENTRIES = [
  'packages/standalone/service.mjs',
  'packages/standalone/management.mjs',
  'packages/standalone/channels/runtime.mjs',
  'packages/standalone/channels/worker.mjs',
  'packages/standalone/channels/business.mjs',
]

/**
 * Bundled assets loaded by path at runtime (never via import).
 *
 * `web/` is the built management UI, read through `./web/${name}` lookups in
 * management.mjs. `frontend/` is only that build's source and stays behind. The
 * wasm is loaded by path from the Qoder auth module, so the walk cannot see it.
 */
const ASSETS = [
  'packages/standalone/web',
  'vendor/channel-pack/qoder-auth-wasm.wasm',
]

/**
 * License and attribution files that must travel with the vendored code.
 *
 * Vendoring copies MIT-licensed sources, and MIT's only condition is that the
 * copyright notice and license text stay with the copy. Three layers apply:
 * the upstream plugin (Ebony-Vinyl), the absorbed channel-pack (Jet), and the
 * built UI's bundled runtime (React et al., listed in the third-party notice).
 * `web/` is a compiled React app even though `frontend/` was pruned, so its
 * third-party notice is still owed.
 */
const LICENSES = [
  // 上游插件的许可。落到 vendored 树里时改名，避免和插件自己的 LICENSE 混淆。
  ['LICENSE', 'UPSTREAM-LICENSE.txt'],
  'vendor/channel-pack/LICENSE',
  'vendor/channel-pack/NOTICE.md',
  // 独立包的第三方声明（React 等）。`web/` 是 React 的编译产物，
  // 即使 frontend/ 被裁掉，这份声明仍然欠着。
  ['scripts/standalone-third-party.txt', 'UPSTREAM-THIRD-PARTY.txt'],
]

/**
 * Data files read by path at module load, invisible to an import walk.
 *
 * `src/providers/registry.json` is parsed at import time by
 * `src/providers/index.js`; `packages/standalone/package.json` supplies the
 * service's own `version`. Both were found by grepping the closure for literal
 * path reads — re-run that grep after upstream refactors.
 */
const DATA = [
  'src/providers/registry.json',
  'packages/standalone/package.json',
]

if (!fs.existsSync(path.join(SOURCE, 'package.json'))) {
  throw new Error(`source checkout not found: ${SOURCE}`)
}

const seen = new Set()

/**
 * Collect the relative specifiers a module really loads.
 *
 * Line-based on purpose: bundled output (channels/business.mjs) keeps JSDoc
 * `import('../../types/…')` references to files that are absent from the release
 * tree, and a whole-source regex treats those as loadable. Comment lines are
 * skipped, so only static `from '…'` / `import('…')` statements are followed —
 * the closure sources use no other import form (verified by grep).
 */
function specifiersOf(abs) {
  const found = []
  for (const line of fs.readFileSync(abs, 'utf8').split('\n')) {
    const trimmed = line.trim()
    if (trimmed.startsWith('*') || trimmed.startsWith('//') || trimmed.startsWith('/*')) continue
    for (const match of line.matchAll(/\b(?:from|import)\s*\(?\s*['"]([^'"]+)['"]/g)) found.push(match[1])
  }
  return found
}

function walk(file) {
  const abs = path.resolve(SOURCE, file)
  if (seen.has(abs)) return
  seen.add(abs)
  if (!/\.(?:m?js|ts)$/.test(abs) || !fs.existsSync(abs)) return
  for (const specifier of specifiersOf(abs)) {
    if (specifier.startsWith('@deepseek-ai/')) {
      throw new Error(`host dependency leaked into the sidecar closure: ${file} -> ${specifier}`)
    }
    if (!specifier.startsWith('.')) continue
    let target = path.resolve(path.dirname(abs), specifier)
    if (fs.existsSync(target) && fs.statSync(target).isDirectory()) target = path.join(target, 'index.js')
    const hit = [target, `${target}.mjs`, `${target}.js`, `${target}.ts`]
      .find(candidate => fs.existsSync(candidate) && fs.statSync(candidate).isFile())
    // Bundled names that resolve to nothing are type-only or in-bundle aliases;
    // the runtime never loads them, so an unresolved name is not a failure here.
    if (hit !== undefined) walk(path.relative(SOURCE, hit))
  }
}

for (const entry of ENTRIES) walk(entry)

fs.rmSync(OUT, { recursive: true, force: true })
const files = [...seen].map(f => path.relative(SOURCE, f)).sort()
for (const rel of files) {
  const dest = path.join(OUT, rel)
  fs.mkdirSync(path.dirname(dest), { recursive: true })
  fs.copyFileSync(path.join(SOURCE, rel), dest)
}

function copyEntry(entry) {
  // 条目可以是 'path'，也可以是 ['源路径', '落点路径']（用于改名）。
  const [from, to] = Array.isArray(entry) ? entry : [entry, entry]
  const source = path.join(SOURCE, from)
  if (!fs.existsSync(source)) { console.warn(`skip missing asset: ${from}`); return }
  fs.cpSync(source, path.join(OUT, to), { recursive: true })
}

for (const entry of [...ASSETS, ...DATA, ...LICENSES]) copyEntry(entry)

const manifest = JSON.parse(fs.readFileSync(path.join(SOURCE, 'package.json'), 'utf8'))
/** Resolve the checkout's commit, following a `ref:` HEAD when needed. */
function revisionOf(source) {
  const git = path.join(source, '.git')
  try {
    const head = fs.readFileSync(path.join(git, 'HEAD'), 'utf8').trim()
    if (!head.startsWith('ref: ')) return head
    const ref = path.join(git, head.slice(5).trim())
    if (fs.existsSync(ref)) return fs.readFileSync(ref, 'utf8').trim()
    // Packed refs: one ` <name>` line per ref in `packed-refs`.
    for (const line of fs.readFileSync(path.join(git, 'packed-refs'), 'utf8').split('\n')) {
      const [sha, name] = line.trim().split(' ')
      if (name === head.slice(5).trim()) return sha
    }
  } catch { /* a tarball checkout has no .git: record what we can */ }
  return ''
}
const revision = revisionOf(SOURCE)
fs.writeFileSync(path.join(OUT, 'VENDORED.json'), `${JSON.stringify({
  source: 'https://github.com/Ebony-Vinyl/dsh-our-free-model',
  upstreamVersion: manifest.version,
  upstreamRevision: revision,
  vendoredAt: new Date().toISOString(),
  closureFiles: files.length,
  licenses: LICENSES.map(entry => (Array.isArray(entry) ? entry.join(' -> ') : entry)),
  note: 'Generated by sidecar/vendor.mjs. Do not hand-edit; re-run the script to refresh.',
}, null, 2)}\n`)

let bytes = 0
for (const rel of files) bytes += fs.statSync(path.join(OUT, rel)).size
console.log(`vendored ${files.length} modules + assets -> ${path.relative(ROOT, OUT)} (${Math.round(bytes / 1024)} KB of JS)`)
console.log(`upstream v${manifest.version} @ ${revision || 'unknown revision'}`)
