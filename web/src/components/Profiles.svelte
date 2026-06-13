<script>
  import { status } from '../lib/stores.svelte.js'
  import { sendGcode } from '../lib/moonraker.js'
  import { profileApply, profileSet, profileForget } from '../lib/gcode.js'

  // profiles: {"PLA@220": {pa, material, temp, updated, source}, ...}
  let profiles = $derived(status.autopa?.profiles ?? {})
  let keys = $derived(Object.keys(profiles).sort())
  let msg = $state('')

  let selected = $state([])
  function toggle(key) {
    selected = selected.includes(key)
      ? selected.filter((k) => k !== key)
      : [...selected, key]
  }

  async function exec(script, okMsg) {
    msg = ''
    try {
      await sendGcode(script)
      msg = okMsg
    } catch (e) {
      msg = e.message
    }
  }

  async function forgetSelected() {
    if (!confirm(`Forget ${selected.length} profile(s)?\n${selected.join(', ')}`))
      return
    msg = ''
    try {
      for (const key of selected) {
        const p = profiles[key]
        if (p) await sendGcode(profileForget(p.material, p.temp, p.brand))
      }
      msg = `forgot ${selected.length} profile(s)`
      selected = []
    } catch (e) {
      msg = e.message
    }
  }

  let material = $state('')
  let brand = $state('')
  let temp = $state('')
  let pa = $state('')
  function add(e) {
    e.preventDefault()
    const name = [material.trim(), brand.trim()].filter(Boolean)
      .join(' ').toUpperCase()
    exec(profileSet(material, temp, pa, brand),
         `stored ${name}@${Math.round(temp)}`)
  }
</script>

<section class="card">
  <div class="toolbar">
    <h2>Profiles</h2>
    <span class="spacer"></span>
    {#if selected.length}
      <button onclick={forgetSelected}>forget {selected.length} selected</button>
    {/if}
  </div>
  <p class="dim">Calibrated pressure advance per (material, temperature) —
    apply one before a print, no recalibration needed.</p>
  {#if keys.length === 0}
    <p class="dim">No saved profiles yet — run a calibration and save the
      result, or add one below.</p>
  {:else}
    <table>
      <thead>
        <tr><th></th><th>profile</th><th>PA</th><th>source</th><th>updated</th><th></th></tr>
      </thead>
      <tbody>
        {#each keys as key}
          {@const p = profiles[key]}
          <tr>
            <td><input type="checkbox" checked={selected.includes(key)}
                       onchange={() => toggle(key)} /></td>
            <td><code>{key}</code></td>
            <td class="num">{p.pa?.toFixed(4)}</td>
            <td class="dim">{p.source ?? '—'}</td>
            <td class="dim">{p.updated ?? '—'}</td>
            <td class="actions">
              <button class="primary"
                onclick={() => exec(profileApply(p.material, p.temp, p.brand),
                                    `applied ${key} → PA ${p.pa.toFixed(4)}`)}>
                apply
              </button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
  {#if msg}<p class="dim">{msg}</p>{/if}

  <details class="recall">
    <summary>How to apply in a print</summary>
    <p class="dim hint">Recall a stored value (errors if none is stored):</p>
    <pre class="dim example">AUTOPA_APPLY MATERIAL=PLA BRAND=Prusament TEMP=220</pre>
    <p class="dim hint">Calibrate once, reuse after — <code>ELSE</code> runs
      <em>your</em> macro (move to a bin, calibrate, clean) only when no profile
      exists yet, then it's recalled on every later print:</p>
    <pre class="dim example">AUTOPA_APPLY MATERIAL=PLA TEMP=220 ELSE=AUTOPA_CALIBRATE</pre>
    <p class="dim hint">See <code>autopa.cfg</code> for the
      <code>AUTOPA_CALIBRATE</code> macro stub.</p>
    <p class="dim hint">Let the slicer fill material + temp — PrusaSlicer
      <em>Start G-code</em>:</p>
    <pre class="dim example">{'AUTOPA_APPLY MATERIAL={filament_type[0]} TEMP={temperature[0]}'}</pre>
    <p class="dim hint">OrcaSlicer / Bambu Studio:</p>
    <pre class="dim example">{'AUTOPA_APPLY MATERIAL={filament_type[0]} TEMP={nozzle_temperature[0]}'}</pre>
  </details>
</section>

<section class="card">
  <h2>Add manually</h2>
  <form onsubmit={add}>
    <input placeholder="material (PLA…)" required size="11" bind:value={material} />
    <input placeholder="brand (optional)" size="13" bind:value={brand} />
    <input type="number" placeholder="temp °C" required min="120" max="400" bind:value={temp} />
    <input type="number" placeholder="PA" required min="0" max="2" step="0.0001" bind:value={pa} />
    <button class="primary">save</button>
  </form>
  <p class="dim hint">Brand is optional — it narrows a material when spools
    differ (PLA + Prusament → <code>PLA&nbsp;PRUSAMENT@220</code>).</p>
</section>

<style>
  .toolbar { display: flex; align-items: center; gap: var(--sp-2); }
  .toolbar h2 { margin: 0; }
  .spacer { flex: 1; }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    text-align: left;
    padding: var(--sp-1) var(--sp-2);
    border-bottom: 1px solid var(--border);
  }
  th { color: var(--text-dim); font-weight: 500; font-size: 0.85rem; }
  .num { font-variant-numeric: tabular-nums; }
  .actions { text-align: right; }
  form { display: flex; gap: var(--sp-2); flex-wrap: wrap; }
  .hint { font-size: 0.85rem; margin: var(--sp-2) 0 0; }
  .recall { margin-top: var(--sp-3); }
  .recall summary { cursor: pointer; color: var(--text-dim); font-size: 0.9rem; }
  .example {
    font-size: 0.85rem;
    margin: var(--sp-1) 0 0;
    padding: var(--sp-1) var(--sp-2);
    background: var(--bg-inset);
    border-radius: var(--radius);
    overflow-x: auto;
    width: fit-content;
    max-width: 100%;
  }
</style>
