// Thin uPlot wrapper: dark-theme axis defaults + container-width resize.
import uPlot from 'uplot'
import 'uplot/dist/uPlot.min.css'

const AXIS = {
  stroke: '#8b94a7',
  grid: { stroke: '#232b36', width: 1 },
  ticks: { stroke: '#232b36', width: 1 },
}

export function makeChart(el, opts) {
  const height = opts.height ?? 220
  const u = new uPlot({
    width: el.clientWidth || 600,
    height,
    cursor: { drag: { x: true, y: false } },
    legend: { show: opts.legend ?? false },
    scales: { x: { time: false }, ...(opts.scales ?? {}) },
    axes: (opts.axes ?? [{}, {}]).map((a) => ({ ...AXIS, ...a })),
    series: opts.series,
    hooks: opts.hooks ?? {},
  }, opts.data ?? [[], []], el)
  const ro = new ResizeObserver(() => {
    if (el.clientWidth) u.setSize({ width: el.clientWidth, height })
  })
  ro.observe(el)
  return {
    u,
    setData: (d) => u.setData(d),
    destroy: () => { ro.disconnect(); u.destroy() },
  }
}

// Canvas-space dashed vertical marker (e.g. K_opt) as a uPlot draw hook.
export function vline(getX, color) {
  return (u) => {
    const xv = getX()
    if (xv == null) return
    const x = u.valToPos(xv, 'x', true)
    const ctx = u.ctx
    ctx.save()
    ctx.strokeStyle = color
    ctx.setLineDash([4, 4])
    ctx.beginPath()
    ctx.moveTo(x, u.bbox.top)
    ctx.lineTo(x, u.bbox.top + u.bbox.height)
    ctx.stroke()
    ctx.restore()
  }
}
