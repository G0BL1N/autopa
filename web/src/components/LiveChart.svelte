<script>
  import { onMount, onDestroy } from 'svelte'
  import { makeChart } from '../lib/chart.js'
  import { subscribeForce, stopForce } from '../lib/bridge.js'
  import { conn, status } from '../lib/stores.svelte.js'

  // Stream only while the live panel is actually open. The force dump is a
  // continuous firehose; left running while nobody watches it loads the Klippy
  // reactor needlessly (a contributor to "Timer too close"). The parent passes
  // active=true only while the <details> panel is open.
  let { active = false } = $props()

  const WINDOW_S = 15
  const MIN_SPAN_G = 25 // don't let the y-axis zoom tighter than this (grams)
  const STAGES = 3 // cascaded EMA passes ⇒ steeper rolloff than a single pole
  let el
  let chart
  const buf = { t: [], f: [], s: [] } // time, raw grams, filtered grams
  let stages = null // running per-stage EMA state, kept across batches
  let raf = 0

  // client-side smoothing: a 3-stage cascaded exponential moving average over
  // the raw grams. A single pole left a "fat" residual at high noise; cascading
  // gives a much steeper rolloff. Display-only — the raw buffer is untouched and
  // calibration math runs server-side on the unsmoothed stream.
  let smooth = $state(true)
  let strength = $state(0.7) // 0 = light, 1 = heavy
  let showRaw = $state(true)

  // strength maps the per-stage EMA pole: alpha 0.3 (light) → 0.018 (heavy),
  // exponentially. Through three stages even the heavy end is very smooth.
  let alpha = $derived(smooth ? 0.3 * Math.pow(0.06, strength) : 1)

  // push one raw sample through the cascade, returning the smoothed value
  function step(x, a) {
    if (stages == null) stages = new Array(STAGES).fill(x)
    let v = x
    for (let k = 0; k < STAGES; k++) {
      stages[k] += a * (v - stages[k])
      v = stages[k]
    }
    return v
  }

  // Feed both series real arrays (uPlot freezes if a series gets null); the raw
  // underlay is shown/hidden via series visibility. Raw is series 1 (drawn
  // first/underneath), the filtered line is series 2 (drawn on top).
  function render() {
    if (!chart) return
    chart.u.setSeries(1, { show: smooth && showRaw })
    chart.setData([buf.t, buf.f, smooth ? buf.s : buf.f])
  }

  function onBatch(params) {
    if (document.hidden) return // keep streaming, skip the work
    const a = alpha
    for (const row of params?.data ?? []) {
      const f = row[1] // grams
      buf.t.push(row[0])
      buf.f.push(f)
      buf.s.push(step(f, a))
    }
    const cut = (buf.t[buf.t.length - 1] ?? 0) - WINDOW_S
    let i = 0
    while (i < buf.t.length && buf.t[i] < cut) i++
    if (i) {
      buf.t.splice(0, i)
      buf.f.splice(0, i)
      buf.s.splice(0, i)
    }
    if (!raf)
      raf = requestAnimationFrame(() => {
        raf = 0
        render()
      })
  }

  // Slider change ⇒ re-filter the visible history once (a static one-time
  // transient that scrolls off), then carry the running state forward.
  $effect(() => {
    const a = alpha
    stages = null
    for (let i = 0; i < buf.f.length; i++) buf.s[i] = step(buf.f[i], a)
    render()
  })

  // re-draw immediately when the raw underlay is toggled
  $effect(() => {
    showRaw; smooth
    render()
  })

  // Subscribe while the panel is open and the bridge is up; tear the stream
  // down (recycle the socket) when the panel closes. A bridge reconnect drops
  // all dump subscriptions, so re-subscribe when it returns.
  let subscribed = $state(false)
  $effect(() => {
    const name = status.autopa?.load_cell_name
    if (conn.bridge && name && active && !subscribed) {
      subscribed = true
      subscribeForce(name, onBatch).catch(() => (subscribed = false))
    } else if (subscribed && (!active || !conn.bridge)) {
      subscribed = false
      if (!active) stopForce() // panel closed -> stop the firehose at the source
    }
  })
  // leaving the view entirely (tab change) also stops the stream
  onDestroy(() => {
    if (subscribed) stopForce()
  })

  onMount(() => {
    chart = makeChart(el, {
      height: 180,
      series: [
        {},
        // raw underlay (faint, drawn first), then the filtered line on top
        { stroke: 'rgba(139,148,167,0.35)', width: 1, points: { show: false } },
        { stroke: '#4cc38a', width: 1.75, points: { show: false } },
      ],
      axes: [{}, {}],
      scales: {
        // Always scale to the RAW signal envelope, even when the raw line is
        // hidden (uPlot would otherwise drop it from auto-range and zoom into
        // the filtered residual, magnifying tiny wander into a fat wiggle). This
        // keeps the comfortable zoom whether or not raw is drawn; the filtered
        // line then reads flat against the real ±envelope. Floor at MIN_SPAN_G.
        y: {
          range: () => {
            const f = buf.f
            if (!f.length) return [-1, 1]
            let lo = Infinity, hi = -Infinity
            for (let i = 0; i < f.length; i++) {
              if (f[i] < lo) lo = f[i]
              if (f[i] > hi) hi = f[i]
            }
            const mid = (lo + hi) / 2
            const half = Math.max(hi - lo, MIN_SPAN_G) / 2 * 1.1
            return [mid - half, mid + half]
          },
        },
      },
    })
    return () => chart.destroy()
  })
</script>

<div class="ctl">
  <label class="opt">
    <input type="checkbox" bind:checked={smooth} />
    smooth
  </label>
  {#if smooth}
    <input class="rng" type="range" min="0" max="1" step="0.05"
           bind:value={strength} title="smoothing strength" />
    <label class="opt">
      <input type="checkbox" bind:checked={showRaw} />
      show raw
    </label>
  {/if}
</div>
<div bind:this={el}></div>
{#if !conn.bridge}
  <p class="dim">Waiting for the Klippy bridge (port 7125)…</p>
{/if}

<style>
  .ctl {
    display: flex;
    gap: var(--sp-3);
    align-items: center;
    margin-bottom: var(--sp-2);
  }
  .opt {
    display: flex;
    gap: var(--sp-1);
    align-items: center;
    color: var(--text-dim);
    font-size: 0.85rem;
  }
  .rng { flex: 1; max-width: 180px; }
</style>
