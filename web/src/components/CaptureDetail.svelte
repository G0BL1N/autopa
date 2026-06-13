<script>
  import { onMount } from 'svelte'
  import { makeChart } from '../lib/chart.js'
  import { fetchCaptureDetail } from '../lib/bridge.js'
  import { conn } from '../lib/stores.svelte.js'
  import { sendGcode } from '../lib/moonraker.js'
  import { annotate, deleteCapture } from '../lib/gcode.js'
  import DecayPlot from './DecayPlot.svelte'
  import SweepPlot from './SweepPlot.svelte'

  let { file } = $props()

  let detail = $state(null)
  let error = $state('')
  let traceEl = $state(null)
  let traceChart = null

  async function load() {
    error = ''
    try {
      detail = await fetchCaptureDetail(file)
      // prefill the metadata editor from the loaded labels
      material = detail.meta?.material ?? ''
      brand = detail.meta?.brand ?? ''
      hotend = detail.meta?.hotend ?? ''
      notes = detail.meta?.notes ?? ''
    } catch (e) {
      error = e.message
    }
  }
  async function remove() {
    if (!confirm(`Delete ${detail.file} permanently?`)) return
    try {
      await sendGcode(deleteCapture(detail.file))
      location.hash = '#runs'
    } catch (e) {
      error = e.message
    }
  }

  // fetch when the bridge is up (it may still be connecting on first render)
  let fetched = $state(false)
  $effect(() => {
    if (conn.bridge && !fetched) {
      fetched = true
      load()
    }
  })

  // zoomable raw-force trace (uPlot drag-zoom; double-click resets)
  $effect(() => {
    if (traceEl && detail?.trace && !traceChart) {
      traceChart = makeChart(traceEl, {
        height: 200,
        series: [{}, { stroke: '#4cc38a', width: 1, points: { show: false } }],
        data: [detail.trace.t, detail.trace.force],
      })
    }
  })
  onMount(() => () => traceChart?.destroy())

  // Skip bulky/internal meta in the table; shown elsewhere or not useful.
  const META_SKIP = new Set(['stops', 'windows', 'transitions', 'ks', 'errs',
                             'material', 'brand', 'hotend', 'notes', 'kind',
                             'schema_version'])
  let metaRows = $derived(detail
    ? Object.entries(detail.meta).filter(([k, v]) =>
        !META_SKIP.has(k) && v != null && typeof v !== 'object')
    : [])
  let statRows = $derived(detail
    ? Object.entries(detail.stats).filter(([, v]) => typeof v !== 'object')
    : [])
  const fmt = (v) => typeof v === 'number'
    ? (Number.isInteger(v) ? v : v.toFixed(4)) : String(v)

  // metadata editor → AUTOPA_ANNOTATE (the only mutation; samples untouched)
  let material = $state('')
  let brand = $state('')
  let hotend = $state('')
  let notes = $state('')
  let saveMsg = $state('')
  async function saveMeta(e) {
    e.preventDefault()
    saveMsg = ''
    const fields = {}
    if (material !== (detail.meta?.material ?? '')) fields.MATERIAL = material
    if (brand !== (detail.meta?.brand ?? '')) fields.BRAND = brand
    if (hotend !== (detail.meta?.hotend ?? '')) fields.HOTEND = hotend
    if (notes !== (detail.meta?.notes ?? '')) fields.NOTES = notes
    if (!Object.keys(fields).length) {
      saveMsg = 'nothing changed'
      return
    }
    try {
      await sendGcode(annotate(detail.file, fields))
      saveMsg = 'saved ✓'
      fetched = false // re-fetch to confirm the labels persisted
    } catch (e2) {
      saveMsg = e2.message
    }
  }
</script>

<p><a href="#runs">← all runs</a></p>

{#if error}
  <section class="card"><p class="error">{error}</p></section>
{:else if !detail}
  <section class="card"><p class="dim">Loading {file}…</p></section>
{:else}
  <div class="titlebar">
    <h2 class="title"><code>{detail.file}</code> <span class="dim">· {detail.meta.kind}</span></h2>
    <span class="spacer"></span>
    <a href="{import.meta.env.BASE_URL}captures/{detail.file}" download={detail.file}>
      <button>download .npz</button>
    </a>
    <button onclick={remove}>delete</button>
  </div>
  {#if !detail.meta.material}
    <p class="sharewarn">Unlabeled capture — set the material before sharing.</p>
  {/if}

  {#if detail.plot && detail.meta.kind === 'decay'}
    <section class="card">
      <h2>Decay fit</h2>
      <DecayPlot plot={detail.plot} />
    </section>
  {:else if detail.plot && detail.meta.kind === 'sweep'}
    <section class="card">
      <h2>Sweep response</h2>
      <SweepPlot per_k={detail.plot.per_k} k_opt={detail.plot.k_opt} />
    </section>
  {/if}

  {#if detail.trace}
    <section class="card">
      <h2>Raw force <span class="dim">(drag to zoom, double-click to reset)</span></h2>
      <div bind:this={traceEl}></div>
    </section>
  {/if}

  <section class="card">
    <h2>Labels</h2>
    <form onsubmit={saveMeta}>
      <div class="row">
        <label><span class="dim">material</span>
          <input bind:value={material} placeholder="PLA, PETG…" /></label>
        <label><span class="dim">brand (optional)</span>
          <input bind:value={brand} placeholder="Prusament…" /></label>
        <label><span class="dim">hotend</span>
          <input bind:value={hotend} placeholder="Volcano + CHC steel…" /></label>
      </div>
      <label class="wide"><span class="dim">notes</span>
        <textarea rows="3" bind:value={notes}
                  placeholder="conditions, filament state, anything future-you needs"></textarea>
      </label>
      <div class="row">
        <button class="primary">save labels</button>
        {#if saveMsg}<span class="dim">{saveMsg}</span>{/if}
      </div>
    </form>
  </section>

  <section class="card">
    <div class="panels">
      <div>
        <h3 class="dim">capture</h3>
        <dl>
          {#each metaRows as [k, v]}
            <dt>{k}</dt><dd>{fmt(v)}</dd>
          {/each}
        </dl>
      </div>
      <div>
        <h3 class="dim">result</h3>
        <dl>
          {#each statRows as [k, v]}
            <dt>{k}</dt><dd>{fmt(v)}</dd>
          {/each}
        </dl>
      </div>
    </div>
  </section>
{/if}

<style>
  a { color: var(--accent); text-decoration: none; }
  .error { color: var(--bad); }
  .titlebar { display: flex; align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-2); }
  .title { margin: 0; }
  .spacer { flex: 1; }
  .sharewarn { color: var(--warn); font-size: 0.9rem; margin: 0 0 var(--sp-2); }
  h3 { font-size: 0.85rem; font-weight: 600; margin: 0 0 var(--sp-1); text-transform: uppercase; }
  .panels { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
  @media (max-width: 600px) { .panels { grid-template-columns: 1fr; } }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 2px var(--sp-2);
    margin: 0;
    font-size: 0.9rem;
  }
  dt { color: var(--text-dim); }
  dd { margin: 0; font-variant-numeric: tabular-nums; }
  form { display: flex; flex-direction: column; gap: var(--sp-2); }
  .row { display: flex; gap: var(--sp-2); flex-wrap: wrap; align-items: center; }
  form label { display: flex; flex-direction: column; gap: 2px; font-size: 0.9rem; }
  .row label { flex: 1; min-width: 180px; }
  .row input { width: 100%; }
  .wide textarea { width: 100%; resize: vertical; }
</style>
