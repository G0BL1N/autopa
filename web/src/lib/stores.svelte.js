// Shared reactive state (Svelte 5 runes module).
export const conn = $state({
  moonraker: false, // websocket to Moonraker is open
  bridge: false,    // /klippysocket bridge is open
  klippy: '',       // '', 'ready', 'shutdown', 'disconnected', 'error'
})

// Mirrors of the subscribed printer objects; merged from notify_status_update.
export const status = $state({
  autopa: null,
  extruder: null,
  print_stats: null,
  toolhead: null,
})

// Rolling tail of console output (notify_gcode_response).
export const gcodeLog = $state([])
const GCODE_LOG_MAX = 200

export function mergeStatus(update) {
  for (const [obj, fields] of Object.entries(update)) {
    if (status[obj] == null) status[obj] = {}
    Object.assign(status[obj], fields)
  }
}

export function pushGcode(line) {
  gcodeLog.push(line)
  if (gcodeLog.length > GCODE_LOG_MAX)
    gcodeLog.splice(0, gcodeLog.length - GCODE_LOG_MAX)
}

// 0..1 while a calibration runs, else null. activity.now is Klipper's
// eventtime echoed by get_status, so no host clock sync is needed.
export function progress() {
  const a = status.autopa?.activity
  if (!a || a.state !== 'running' || !a.expected_s) return null
  return Math.min((a.now - a.started) / a.expected_s, 1)
}
