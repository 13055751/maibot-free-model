/**
 * Sidecar bootstrap for the MaiBot plugin.
 *
 * Starts the vendored standalone service on a loopback port, then prints one
 * JSON line (`OFM_READY …`) that the Python host parses: base url, api key,
 * management url, upstream version. No stdout prose is parsed — the contract is
 * this line and the exit status.
 *
 * Signals: the child is expected to be managed by the host plugin. SIGTERM and
 * SIGINT close the service (releasing service.lock) before exit.
 */
import fs from 'node:fs'
import path from 'node:path'

const args = process.argv.slice(2)
const flag = name => {
  const i = args.indexOf(name)
  return i === -1 ? undefined : args[i + 1]
}

const dataDir = path.resolve(flag('--data-dir') ?? process.env.OFM_HOME ?? path.join(process.env.HOME ?? os_home(), '.our-free-model-maibot'))
const port = flag('--port') === undefined ? undefined : Number(flag('--port'))

function os_home() {
  return process.env.HOME ?? process.env.USERPROFILE ?? '.'
}

const { startStandalone } = await import(new URL('./vendor/packages/standalone/service.mjs', import.meta.url).href)

const logger = {
  info: message => process.stderr.write(`[info] ${message}\n`),
  warn: message => process.stderr.write(`[warn] ${message}\n`),
  error: message => process.stderr.write(`[error] ${message}\n`),
  debug: () => {},
}

const service = await startStandalone({
  dataDir,
  host: '127.0.0.1',
  port,
  logger,
  refresh: flag('--no-refresh') !== 'true',
})

/**
 * `startStandalone()` returns `ready` as a promise it never awaits, so the
 * listener answers before the catalog refresh and the channel worker boot are
 * done. A human opening the management page sees a sync spinner; a plugin
 * treating this process as a backend has no such luxury — its first turn would
 * be served against a half-built catalog and fail `model not found`.
 *
 * So the handshake line means "servable": wait for `ready` before printing it.
 * Failures are recorded rather than fatal — an unreachable upstream leaves the
 * seeded catalog standing, and refusing to answer at all would hide the one
 * lane that still works. The host's own ready timeout bounds this wait.
 */
let catalogReady = true
try {
  await service.ready
} catch (error) {
  catalogReady = false
  logger.warn(`startup sequence failed (${error?.message ?? error}); answering with the catalog on hand`)
}

const settings = JSON.parse(fs.readFileSync(path.join(dataDir, 'settings.json'), 'utf8'))
process.stdout.write(`${'OFM_READY'} ${JSON.stringify({
  url: service.url,
  port: service.port,
  apiKey: settings.forwardKey,
  managementUrl: service.managementUrl,
  version: service.version,
  dataDir: service.dataDir,
  catalogReady,
  models: service.runtime?.publicModelRows?.().length ?? 0,
})}\n`)

let closing = false
const shutdown = () => {
  if (closing) return
  closing = true
  service.close().then(() => process.exit(0), () => process.exit(1))
}
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
