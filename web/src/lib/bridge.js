// Raw Klippy API access via Moonraker's /klippysocket bridge — used for the
// load_cell/dump_force stream and the autopa/capture_detail endpoint. nginx does
// not proxy this path, so production connects to Moonraker's port directly;
// the dev server proxies it (vite.config.js).
import { conn } from './stores.svelte.js'

let ws = null
let nextId = 1
const pending = new Map()
const subs = new Map() // response_template key -> callback
let backoff = 1000

function url() {
  if (import.meta.env.DEV) return `ws://${location.host}/klippysocket`
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.hostname}:7125/klippysocket`
}

export function bridgeCall(method, params = {}) {
  return new Promise((resolve, reject) => {
    if (!ws || ws.readyState !== WebSocket.OPEN)
      return reject(new Error('bridge: not connected'))
    const id = nextId++
    pending.set(id, { resolve, reject })
    ws.send(JSON.stringify({ id, method, params }))
  })
}

// Subscribe to the load-cell force dump; cb receives each batch's params
// ({data: [[time, force_g, counts, tare], ...], errors, overflows}).
// Dump subscriptions live until the socket closes, so subscribe once and
// let consumers ignore batches while hidden.
export function subscribeForce(loadCell, cb) {
  const key = `force:${loadCell}`
  subs.set(key, cb)
  return bridgeCall('load_cell/dump_force', {
    load_cell: loadCell,
    response_template: { key },
  })
}

export function fetchCaptureDetail(capture) {
  return bridgeCall('autopa/capture_detail', { capture })
}

export function startBridge() {
  ws = new WebSocket(url())
  ws.onopen = () => {
    backoff = 1000
    conn.bridge = true
  }
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.id != null && pending.has(msg.id)) {
      const p = pending.get(msg.id)
      pending.delete(msg.id)
      if (msg.error) p.reject(new Error(msg.error.message))
      else p.resolve(msg.result)
    } else if (msg.key != null && subs.has(msg.key)) {
      subs.get(msg.key)(msg.params)
    }
  }
  ws.onclose = () => {
    conn.bridge = false
    for (const p of pending.values()) p.reject(new Error('bridge: closed'))
    pending.clear()
    subs.clear() // dump subscriptions died with the socket; resubscribe on use
    setTimeout(startBridge, backoff)
    backoff = Math.min(backoff * 2, 15000)
  }
}
