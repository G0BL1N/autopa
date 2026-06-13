<script>
  import { status } from '../lib/stores.svelte.js'
  import { sendGcode } from '../lib/moonraker.js'
  import { setPa, profileSet } from '../lib/gcode.js'

  // kind 'decay': result = {tau, snr, spread, slack, n_used, confidence}
  // kind 'sweep': result = {k_opt, sample_rate_hz, per_k}
  let { kind, result } = $props()

  let value = $derived(kind === 'decay' ? result.tau : result.k_opt)
  let conf = $derived(kind === 'decay' ? result.confidence : null)
  let paste = $derived(value != null ? setPa(value) : null)
  // "active now": live extruder PA matches the result (set by APPLY=1)
  let active = $derived(value != null && Math.abs(
    (status.extruder?.pressure_advance ?? -1) - value) < 5e-5)

  let copied = $state(false)
  async function copy() {
    await navigator.clipboard.writeText(paste)
    copied = true
    setTimeout(() => (copied = false), 1500)
  }

  let material = $state('')
  let brand = $state('')
  let temp = $state('')
  let saveMsg = $state('')
  $effect(() => {
    if (temp === '' && status.extruder?.target)
      temp = Math.round(status.extruder.target)
  })
  async function saveProfile() {
    saveMsg = ''
    try {
      await sendGcode(profileSet(material, temp, value.toFixed(4), brand))
      const name = [material.trim(), brand.trim()].filter(Boolean)
        .join(' ').toUpperCase()
      saveMsg = `saved ${name}@${Math.round(temp)}`
    } catch (e) {
      saveMsg = e.message
    }
  }
</script>

<section class="card">
  {#if value == null}
    <h2>No usable result</h2>
    <p class="dim">
      The sweep found no K passing the segment-quality gate — try a finer K
      grid through the optimum, or a stronger signal.
    </p>
  {:else}
    <div class="headline">
      <span class="value">{value.toFixed(4)}</span>
      <span class="unit">pressure advance</span>
      {#if conf}<span class="chip {conf.toLowerCase()}">{conf}</span>{/if}
      {#if active}<span class="chip ok">applied live</span>{/if}
    </div>
    {#if kind === 'decay'}
      <div class="metrics dim">
        <span>SNR <b>{result.snr?.toFixed(1)}</b></span>
        <span>spread <b>±{result.spread?.toFixed(4) ?? '—'}</b></span>
        <span>slack <b>{result.slack?.toFixed(2)}</b></span>
        <span>pulses <b>{result.n_used}</b></span>
      </div>
      {#if conf === 'LOW'}
        <p class="warn">LOW confidence — raise PULSES/OFF or check for a
          hanging blob before trusting this value</p>
      {/if}
    {/if}
    <div class="paste">
      <code>{paste}</code>
      <button onclick={copy}>{copied ? 'copied ✓' : 'copy'}</button>
    </div>
    <p class="dim hint">Paste into your slicer's filament start g-code to make
      it permanent — or save it as a profile:</p>
    <form class="save" onsubmit={(e) => { e.preventDefault(); saveProfile() }}>
      <input placeholder="material (PLA…)" required size="11"
             bind:value={material} />
      <input placeholder="brand (optional)" size="13" bind:value={brand} />
      <input type="number" placeholder="°C" required min="120" max="400"
             size="4" bind:value={temp} />
      <button class="primary">save profile</button>
      {#if saveMsg}<span class="dim">{saveMsg}</span>{/if}
    </form>
  {/if}
</section>

<style>
  .headline {
    display: flex;
    align-items: baseline;
    gap: var(--sp-2);
    flex-wrap: wrap;
  }
  .value {
    font-size: 2.2rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .unit { color: var(--text-dim); }
  .chip {
    font-size: 0.72rem;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--bg-inset);
    border: 1px solid var(--border);
    align-self: center;
  }
  .chip.high, .chip.ok { color: var(--accent); border-color: var(--accent-dim); }
  .chip.med { color: var(--warn); }
  .chip.low { color: var(--bad); }
  .metrics { display: flex; gap: var(--sp-3); flex-wrap: wrap; margin: var(--sp-2) 0; }
  .metrics b { color: var(--text); }
  .warn { color: var(--warn); }
  .paste {
    display: flex;
    gap: var(--sp-2);
    align-items: center;
    background: var(--bg-inset);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: var(--sp-1) var(--sp-2);
    margin-top: var(--sp-2);
  }
  .paste code { flex: 1; overflow-x: auto; white-space: nowrap; }
  .hint { font-size: 0.85rem; margin: var(--sp-2) 0 var(--sp-1); }
  .save { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; }
</style>
