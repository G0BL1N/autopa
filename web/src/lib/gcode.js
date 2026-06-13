// Builders for the frozen AUTOPA_* command surface. The UI invents no verbs:
// every mutation is one of these commands sent via printer.gcode.script.

// Quote a value for g-code: keep simple tokens bare, quote anything spaced.
function arg(name, value) {
  if (value == null || value === '') return null
  const s = String(value)
  return /[\s"]/.test(s) ? `${name}="${s.replace(/"/g, "'")}"` : `${name}=${s}`
}

function cmd(name, params) {
  const parts = [name]
  for (const [k, v] of Object.entries(params)) {
    const a = arg(k, v)
    if (a) parts.push(a)
  }
  return parts.join(' ')
}

export const decay = (p = {}) => cmd('AUTOPA_DECAY', p)
export const sweep = (p = {}) => cmd('AUTOPA_SWEEP', p)
export const annotate = (capture, fields) =>
  cmd('AUTOPA_ANNOTATE', { CAPTURE: capture, ...fields })
export const deleteCapture = (capture) => cmd('AUTOPA_DELETE', { CAPTURE: capture })
export const profileApply = (material, temp, brand) =>
  cmd('AUTOPA_APPLY', { MATERIAL: material, BRAND: brand, TEMP: temp })
export const profileSet = (material, temp, pa, brand) =>
  cmd('AUTOPA_SET', { MATERIAL: material, BRAND: brand, TEMP: temp, PA: pa })
export const profileForget = (material, temp, brand) =>
  cmd('AUTOPA_FORGET', { MATERIAL: material, BRAND: brand, TEMP: temp })
export const setPa = (pa) =>
  `SET_PRESSURE_ADVANCE ADVANCE=${Number(pa).toFixed(4)}`

// calibration prep (plain printer verbs, not AUTOPA commands)
export const setHotend = (t) => `M104 S${Math.round(Number(t))}`
export const home = () => 'G28'
// travel order is always safe: raise Z first (never below current), move XY,
// then descend to the target Z only at the destination
export const moveTo = (x, y, z, zNow) => {
  const lines = [
    'G90',
    `G1 Z${Math.max(z, zNow).toFixed(1)} F600`,
    `G1 X${x.toFixed(1)} Y${y.toFixed(1)} F3000`,
  ]
  if (z < zNow) lines.push(`G1 Z${z.toFixed(1)} F600`)
  return lines.join('\n')
}
