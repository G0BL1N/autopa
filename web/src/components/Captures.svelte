<script>
  import { status } from '../lib/stores.svelte.js'
  import { sendGcode } from '../lib/moonraker.js'
  import { annotate, deleteCapture } from '../lib/gcode.js'
  import CaptureDetail from './CaptureDetail.svelte'
  import Compare from './Compare.svelte'

  // route: 'captures' | 'captures/<file>' | 'compare/<a>/<b>'
  let { route } = $props()
  let parts = $derived(route.split('/'))
  let captures = $derived(status.autopa?.captures ?? [])

  let selected = $state([])
  let msg = $state('')
  function toggle(file) {
    selected = selected.includes(file)
      ? selected.filter((f) => f !== file)
      : [...selected, file]
  }
  let allSelected = $derived(captures.length > 0 && selected.length === captures.length)
  function toggleAll() {
    selected = allSelected ? [] : captures.map((r) => r.file)
  }

  // bulk labeling → AUTOPA_ANNOTATE per selected capture. Fields are prefilled
  // with the value shared across the selection (blank + a "mixed" hint when they
  // differ), and only fields the operator actually changes from that baseline
  // are sent — so re-saving never clobbers labels that were already consistent.
  let bulkOpen = $state(false)
  let bMaterial = $state('')
  let bBrand = $state('')
  let bHotend = $state('')
  let bNotes = $state('')
  let baseline = $state({ material: '', brand: '', hotend: '', notes: '' })
  let mixed = $state({ material: false, brand: false, hotend: false, notes: false })

  // shared value across the selection, or '' when they disagree (mixed)
  function common(field) {
    const vals = selected.map((f) =>
      captures.find((c) => c.file === f)?.[field] ?? '')
    const same = vals.every((v) => v === vals[0])
    return { value: same ? (vals[0] ?? '') : '', mixed: vals.length > 1 && !same }
  }
  // snapshot the selection's current labels into the form (called on open, so a
  // live status refresh of `captures` never clobbers in-progress edits)
  function openBulk() {
    bulkOpen = !bulkOpen
    if (!bulkOpen) return
    const m = common('material'), b = common('brand')
    const h = common('hotend'), n = common('notes')
    baseline = { material: m.value, brand: b.value, hotend: h.value, notes: n.value }
    mixed = { material: m.mixed, brand: b.mixed, hotend: h.mixed, notes: n.mixed }
    bMaterial = m.value; bBrand = b.value; bHotend = h.value; bNotes = n.value
  }
  async function bulkLabel(e) {
    e.preventDefault()
    const fields = {}
    if (bMaterial !== baseline.material) fields.MATERIAL = bMaterial
    if (bBrand !== baseline.brand) fields.BRAND = bBrand
    if (bHotend !== baseline.hotend) fields.HOTEND = bHotend
    if (bNotes !== baseline.notes) fields.NOTES = bNotes
    if (!Object.keys(fields).length) {
      msg = 'nothing changed'
      return
    }
    msg = 'labeling…'
    try {
      for (const f of selected) await sendGcode(annotate(f, fields))
      msg = `labeled ${selected.length} captures`
      bulkOpen = false
      selected = []
    } catch (err) {
      msg = err.message
    }
  }

  async function deleteSelected() {
    if (!confirm(`Delete ${selected.length} capture(s) permanently?\n\n`
                 + selected.join('\n')))
      return
    msg = 'deleting…'
    try {
      for (const f of selected) await sendGcode(deleteCapture(f))
      msg = `deleted ${selected.length} captures`
      selected = []
    } catch (err) {
      msg = err.message
    }
  }

  async function downloadSelected() {
    for (const f of selected) {
      const a = document.createElement('a')
      a.href = `${import.meta.env.BASE_URL}captures/${f}`
      a.download = f
      document.body.appendChild(a)
      a.click()
      a.remove()
      await new Promise((r) => setTimeout(r, 400))
    }
  }

  const KIND_RESULT = { decay: 'τ', sweep: 'K', capture: 'SNR' }
  function fmtResult(r) {
    if (r.result == null) return '—'
    const label = KIND_RESULT[r.kind] ?? ''
    const digits = r.kind === 'capture' ? 1 : 4
    return `${label} ${r.result.toFixed(digits)}`
  }
  const fmtMaterial = (r) =>
    r.material ? (r.brand ? `${r.material} ${r.brand}` : r.material) : '—'
  const fmtTime = (t) =>
    new Date(t * 1000).toLocaleString([], {
      dateStyle: 'short', timeStyle: 'short' })
</script>

{#if parts[0] === 'compare' && parts.length === 3}
  <Compare a={parts[1]} b={parts[2]} />
{:else if parts[0] === 'captures' && parts[1]}
  <CaptureDetail file={parts.slice(1).join('/')} />
{:else}
  <section class="card">
    <div class="toolbar">
      <h2>Saved captures <span class="dim count">· {captures.length}</span></h2>
      <span class="spacer"></span>
      <span class="dim">{selected.length
        ? `${selected.length} selected`
        : 'click a capture for its curves'}</span>
      <span class="actions">
        <button disabled={!selected.length}
                title={selected.length ? '' : 'select at least one capture'}
                onclick={openBulk}>label</button>
        <button disabled={!selected.length}
                title={selected.length ? '' : 'select at least one capture'}
                onclick={downloadSelected}>download</button>
        <button disabled={!selected.length}
                title={selected.length ? '' : 'select at least one capture'}
                onclick={deleteSelected}>delete</button>
        <button disabled={selected.length !== 2}
                title="select exactly two captures"
                onclick={() => (location.hash = `#compare/${selected[0]}/${selected[1]}`)}>
          compare
        </button>
      </span>
    </div>
    {#if bulkOpen && selected.length}
      <form class="bulk" onsubmit={bulkLabel}>
        <input placeholder={mixed.material ? 'mixed — leave to keep' : 'material (PLA…)'}
               size="12" bind:value={bMaterial} />
        <input placeholder={mixed.brand ? 'mixed — leave to keep' : 'brand (optional)'}
               size="14" bind:value={bBrand} />
        <input placeholder={mixed.hotend ? 'mixed — leave to keep' : 'hotend'}
               size="14" bind:value={bHotend} />
        <input placeholder={mixed.notes ? 'mixed — leave to keep' : 'notes'}
               size="20" bind:value={bNotes} />
        <button class="primary">apply to {selected.length}</button>
        <span class="dim">only changed fields are written</span>
      </form>
    {/if}
    {#if msg}<p class="dim toolmsg">{msg}</p>{/if}
    {#if captures.length === 0}
      <p class="dim">No recorded captures yet — every calibration saves one.</p>
    {:else}
      <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th><input type="checkbox" title="select all"
                       checked={allSelected} onchange={toggleAll} /></th>
            <th>when</th><th>kind</th><th>result</th>
            <th>material</th><th>temp</th><th>notes</th></tr>
        </thead>
        <tbody>
          {#each captures as r (r.file)}
            <tr onclick={() => (location.hash = `#captures/${r.file}`)}>
              <td onclick={(e) => e.stopPropagation()}>
                <input type="checkbox" title="select"
                       checked={selected.includes(r.file)}
                       onchange={() => toggle(r.file)} />
              </td>
              <td class="when">{fmtTime(r.time)}</td>
              <td>{r.kind}
                {#if r.confidence}
                  <span class="chip {r.confidence.toLowerCase()}">{r.confidence}</span>
                {/if}
              </td>
              <td class="num">{fmtResult(r)}</td>
              <td>{fmtMaterial(r)}</td>
              <td class="num">{r.hotend_target != null ? `${Math.round(r.hotend_target)}°` : '—'}</td>
              <td class="dim notes">{r.notes ?? ''}</td>
            </tr>
          {/each}
        </tbody>
      </table>
      </div>
    {/if}
  </section>
{/if}

<style>
  .toolbar {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    margin-bottom: var(--sp-2);
    flex-wrap: wrap;
  }
  .toolbar h2 { margin: 0; }
  .toolbar .dim { font-size: 0.85rem; }
  .spacer { flex: 1; }
  .actions { display: flex; gap: var(--sp-2); flex-wrap: nowrap; }
  .bulk {
    display: flex;
    gap: var(--sp-2);
    flex-wrap: wrap;
    align-items: center;
    padding: var(--sp-2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--bg-inset);
    margin-bottom: var(--sp-2);
  }
  .toolmsg { margin: 0 0 var(--sp-2); }
  .count { font-weight: 400; font-size: 0.9rem; }
  .tablewrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    text-align: left;
    padding: var(--sp-1) var(--sp-2);
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  th { color: var(--text-dim); font-weight: 500; font-size: 0.85rem; }
  tbody tr { cursor: pointer; }
  tbody tr:hover { background: var(--bg-inset); }
  .when { color: var(--accent); }
  .num { font-variant-numeric: tabular-nums; }
  .notes {
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .chip {
    font-size: 0.72rem;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: 999px;
    background: var(--bg-inset);
  }
  .chip.high { color: var(--accent); }
  .chip.med { color: var(--warn); }
  .chip.low { color: var(--bad); }
</style>
