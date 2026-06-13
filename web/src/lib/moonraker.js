// Minimal Moonraker JSON-RPC websocket client. Same-origin /websocket works
// in production (kiauh nginx proxies it to Moonraker) and in dev (vite proxy).
import { conn, mergeStatus, pushGcode } from './stores.svelte.js'

const OBJECTS = {
  autopa: null,
  extruder: ['temperature', 'target', 'pressure_advance', 'smooth_time'],
  print_stats: ['state', 'filename'],
  toolhead: ['homed_axes', 'axis_minimum', 'axis_maximum', 'position'],
}

let ws = null
let nextId = 1
const pending = new Map()
let backoff = 1000

export function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    if (!ws || ws.readyState !== WebSocket.OPEN)
      return reject(new Error('moonraker: not connected'))
    const id = nextId++
    pending.set(id, { resolve, reject })
    ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }))
  })
}

export function sendGcode(script) {
  return call('printer.gcode.script', { script })
}

export function start() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/websocket`)
  ws.onopen = async () => {
    backoff = 1000
    conn.moonraker = true
    try {
      await call('server.connection.identify', {
        client_name: 'autopa', version: '0.1.0',
        type: 'web', url: location.origin,
      })
    } catch (e) { /* older moonraker: identify is optional */ }
    subscribe()
  }
  ws.onmessage = (ev) => handle(JSON.parse(ev.data))
  ws.onclose = () => {
    conn.moonraker = false
    for (const p of pending.values()) p.reject(new Error('moonraker: closed'))
    pending.clear()
    setTimeout(start, backoff)
    backoff = Math.min(backoff * 2, 15000)
  }
}

async function subscribe() {
  try {
    const res = await call('printer.objects.subscribe', { objects: OBJECTS })
    mergeStatus(res.status)
    conn.klippy = 'ready'
  } catch (e) {
    conn.klippy = 'error' // klippy not ready yet; notify_klippy_ready retries
  }
}

function handle(msg) {
  if (msg.id != null && pending.has(msg.id)) {
    const p = pending.get(msg.id)
    pending.delete(msg.id)
    if (msg.error) p.reject(new Error(msg.error.message))
    else p.resolve(msg.result)
    return
  }
  switch (msg.method) {
    case 'notify_status_update':
      mergeStatus(msg.params[0])
      break
    case 'notify_gcode_response':
      pushGcode(msg.params[0])
      break
    case 'notify_klippy_ready':
      subscribe()
      break
    case 'notify_klippy_shutdown':
      conn.klippy = 'shutdown'
      break
    case 'notify_klippy_disconnected':
      conn.klippy = 'disconnected'
      break
  }
}
