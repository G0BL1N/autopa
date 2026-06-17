<script>
  import { status, conn, progress, gcodeLog, pushGcode } from '../lib/stores.svelte.js'
  import { sendGcode } from '../lib/moonraker.js'
  import * as g from '../lib/gcode.js'
  import LiveChart from './LiveChart.svelte'
  import ResultCard from './ResultCard.svelte'
  import DecayPlot from './DecayPlot.svelte'
  import SweepPlot from './SweepPlot.svelte'

  // Volumetric conversion for the SLOW/FAST inputs: people think in mm³/s, the
  // AUTOPA_SWEEP command takes mm/s filament feed. Assumes 1.75 mm filament
  // (the target audience); a 2.85 mm setup would read proportionally off.
  const FIL_AREA = Math.PI * (1.75 / 2) ** 2 // 2.405 mm²
  const vol2lin = (mm3) => Number(mm3) / FIL_AREA

  // params: { k, def, hint, tier, vol? }. tier 'common' shows in the advanced
  // grid; 'expert' is tucked into a nested, rarely-needed group. vol fields are
  // entered as mm³/s and converted to the command's mm/s on send.
  const METHODS = {
    sweep: {
      label: 'Sweep',
      tags: ['recommended', 'slower'],
      desc: 'K-sweep force tracking (PrusaPATuner port): square-wave extrusion '
          + 'across a K grid, picks the K that best tracks commanded flow. '
          + 'Needs homed axes (PA-gate wobble).',
      params: [
        { k: 'SLOW', def: 2, hint: 'slow flow (mm³/s)', tier: 'common', vol: true },
        { k: 'FAST', def: 18, hint: 'fast flow (mm³/s)', tier: 'common', vol: true },
        { k: 'KSTART', def: 0.01, hint: 'K grid start', tier: 'common' },
        { k: 'KEND', def: 0.08, hint: 'K grid end', tier: 'common' },
        { k: 'KSTEP', def: 0.01, hint: 'K grid step', tier: 'common' },
        { k: 'CYCLES', def: 8, hint: 'cycles per K', tier: 'common' },
        { k: 'TSLOW', def: 1, hint: 'slow leg time (s)', tier: 'expert' },
        { k: 'TFAST', def: 0.25, hint: 'fast leg time (s)', tier: 'expert' },
        { k: 'WARMUP', def: 4, hint: 'first-leg warmup ×', tier: 'expert' },
        { k: 'WOBBLE', def: 0.05, hint: 'PA-gate wobble (mm)', tier: 'expert' },
        { k: 'WOBBLEAXIS', def: 'Y', hint: 'wobble axis (X or Y)', tier: 'expert' },
        { k: 'ACCEL', def: 1000, hint: 'sweep accel (mm/s²)', tier: 'expert' },
        { k: 'MAXFILAMENT', def: 400, hint: 'max filament (mm)', tier: 'expert' },
      ],
      build: g.sweep,
    },
    decay: {
      label: 'Decay',
      tags: ['fast', 'alternative'],
      desc: "Pulse-and-stop melt relaxation: fits the post-stop force decay; "
          + 'τ is the optimal pressure advance. autopa\'s own method — faster '
          + 'than Sweep and, at the default settings, just as well tested.',
      params: [
        { k: 'FLOW', def: 18, hint: 'flow (mm³/s)', tier: 'common', vol: true },
        { k: 'PULSE', def: 2, hint: 'filament per pulse (mm)', tier: 'common' },
        { k: 'OFF', def: 0.5, hint: 'pause between pulses (s)', tier: 'common' },
        { k: 'PULSES', def: 20, hint: 'number of pulses', tier: 'common' },
        { k: 'PRIME', def: 20, hint: 'bulk melt prime (mm)', tier: 'expert' },
        { k: 'WARMUP', def: 10, hint: 'warmup pulses', tier: 'expert' },
        { k: 'WINDOW', def: 0.14, hint: 'fit window (s)', tier: 'expert' },
        { k: 'SNRMIN', def: 4, hint: 'min SNR', tier: 'expert' },
        { k: 'PA', def: 0, hint: 'PA during measure', tier: 'expert' },
        { k: 'MAXFILAMENT', def: 250, hint: 'max filament (mm)', tier: 'expert' },
      ],
      build: g.decay,
    },
  }

  // canonical defaults, regenerated fresh each call (never aliased into state)
  const defaults = () => Object.fromEntries(Object.entries(METHODS).map(
    ([id, m]) => [id, Object.fromEntries(m.params.map((p) => [p.k, p.def]))]))

  // Persist the operator's choices across reloads. Saved params are merged onto
  // current defaults key-by-key, so a renamed/removed param is dropped and a
  // newly added one still gets its default rather than going undefined.
  const STORE_KEY = 'autopa.calibrator'
  function restore() {
    const out = { method: 'sweep', apply: true, params: defaults() }
    try {
      const saved = JSON.parse(localStorage.getItem(STORE_KEY) || '{}')
      if (saved.method && METHODS[saved.method]) out.method = saved.method
      if (typeof saved.apply === 'boolean') out.apply = saved.apply
      if (saved.params)
        for (const id of Object.keys(out.params))
          for (const k of Object.keys(out.params[id]))
            if (saved.params[id] && k in saved.params[id])
              out.params[id][k] = saved.params[id][k]
    } catch { /* corrupt/blocked storage → fall back to defaults */ }
    return out
  }
  const init = restore()

  let method = $state(init.method)
  let apply = $state(init.apply)
  let error = $state('')
  let params = $state(init.params)
  let commonParams = $derived(METHODS[method].params.filter((p) => p.tier === 'common'))
  let expertParams = $derived(METHODS[method].params.filter((p) => p.tier === 'expert'))

  // write back on any change (deep-tracks params via JSON serialization)
  $effect(() => {
    const snapshot = JSON.stringify({ method, apply, params })
    try { localStorage.setItem(STORE_KEY, snapshot) } catch { /* ignore */ }
  })

  // reset just the visible method's params to their canonical defaults
  let atDefaults = $derived(METHODS[method].params.every(
    (p) => String(params[method][p.k]) === String(p.def)))
  function resetDefaults() {
    for (const p of METHODS[method].params) params[method][p.k] = p.def
  }

  let activity = $derived(status.autopa?.activity)
  let running = $derived(activity?.state === 'running')
  let last = $derived(status.autopa?.last)
  let pct = $derived(Math.round((progress() ?? 0) * 100))

  // preflight
  let lcOk = $derived(status.autopa?.has_load_cell ?? false)
  let lcName = $derived(status.autopa?.load_cell_name)
  let temp = $derived(status.extruder?.temperature)
  let target = $derived(status.extruder?.target)
  let hotOk = $derived((temp ?? 0) >= 170)
  let heating = $derived((target ?? 0) > 0)
  let printing = $derived(status.print_stats?.state === 'printing')
  let homed = $derived(status.toolhead?.homed_axes ?? '')
  let homedXYZ = $derived(['x', 'y', 'z'].every((a) => homed.includes(a)))

  // Sweep's PA-gate wobble is a high extrude-ratio move; warn when the
  // extruder's max_extrude_cross_section (surfaced by autopa) is below the
  // headroom Sweep needs. Default-Sweep params need ~170 mm²; 200 covers them
  // with margin. null ⇒ couldn't read it ⇒ warn to be safe.
  const SWEEP_XSEC_MIN = 200
  let mecs = $derived(status.autopa?.max_extrude_cross_section)
  let sweepGuardLow = $derived(mecs == null || mecs < SWEEP_XSEC_MIN)

  // Soft, non-blocking position check on the LIVE toolhead position (the purge
  // happens wherever the nozzle currently is — not the move-target inputs). Only
  // one situation is actually risky: far from every edge (no waste bin to catch
  // the purge) AND low enough to touch the bed. Near an edge a bin is probably
  // there; high above the table is safe to purge into the air regardless.
  const EDGE_MARGIN_MM = 50 // closer than this to an edge ⇒ a bin is likely
  const SAFE_HEIGHT_MM = 40 // at/above this we're clearly above the table
  let posWarn = $derived.by(() => {
    const mn = status.toolhead?.axis_minimum
    const mx = status.toolhead?.axis_maximum
    if (!mn || !mx || !pos || pos.length < 3) return null
    const [x, y, z] = pos
    const edge = Math.min(x - mn[0], mx[0] - x, y - mn[1], mx[1] - y)
    if (edge > EDGE_MARGIN_MM && z < SAFE_HEIGHT_MM)
      return `nozzle is ${Math.round(edge)} mm from any edge and only Z=`
           + `${z.toFixed(1)} mm up — likely no bin here and it may squish into the bed`
    return null
  })

  // prepare: heat / home / move without leaving for fluidd. Feedback is
  // in-place (busy button labels + live position), not a status log; only
  // errors get a line.
  let setTemp = $state(210)
  let posX = $state('')
  let posY = $state('')
  let posZ = $state(50)
  let prepBusy = $state('') // 'heat' | 'home' | 'move' while awaiting klipper
  let prepErr = $state('')
  let pos = $derived(status.toolhead?.position)

  // default the target inputs to a sane calibration spot: bed center, Z=50
  let posInit = false
  $effect(() => {
    const th = status.toolhead
    if (!posInit && th?.axis_minimum && th?.axis_maximum) {
      posInit = true
      posX = Math.round((th.axis_minimum[0] + th.axis_maximum[0]) / 2)
      posY = Math.round((th.axis_minimum[1] + th.axis_maximum[1]) / 2)
    }
  })

  async function prep(kind, script) {
    prepBusy = kind
    prepErr = ''
    try {
      await sendGcode(script) // resolves when klipper finished the move
    } catch (e) {
      prepErr = e.message
    } finally {
      prepBusy = ''
    }
  }
  // apply the setpoint (starts heating, or retargets while already hot) and
  // turn-off are separate actions, so the temperature can be changed without
  // first switching the heater off
  const applyHeat = () => prep('heat', g.setHotend(setTemp))
  const heatOff = () => prep('heat', g.setHotend(0))
  const doHome = () => prep('home', g.home())
  const doMove = () =>
    prep('move', g.moveTo(Number(posX), Number(posY), Number(posZ),
                          pos?.[2] ?? 0))

  // live force: user-controlled, auto-opens when a calibration starts
  let liveOpen = $state(false)
  let wasRunning = false
  $effect(() => {
    if (running && !wasRunning) liveOpen = true // unfold when a run starts
    if (!running && wasRunning) liveOpen = false // fold back once it's done
    wasRunning = running
  })

  // interactive console: send arbitrary g-code, echo it into the shared log,
  // keep a small command history (↑/↓), and pin the scroll to the latest line
  let cmd = $state('')
  let consoleEl = $state(null)
  let history = $state([])
  let histIdx = $state(-1)
  async function sendCmd(e) {
    e.preventDefault()
    const c = cmd.trim()
    if (!c) return
    pushGcode(`$ ${c}`) // echo what we sent, distinct from printer responses
    history = [...history.filter((h) => h !== c), c]
    histIdx = -1
    cmd = ''
    try {
      await sendGcode(c)
    } catch (err) {
      pushGcode(`!! ${err.message}`)
    }
  }
  function cmdKey(e) {
    if (e.key === 'ArrowUp') {
      if (!history.length) return
      e.preventDefault()
      histIdx = histIdx < 0 ? history.length - 1 : Math.max(0, histIdx - 1)
      cmd = history[histIdx]
    } else if (e.key === 'ArrowDown') {
      if (histIdx < 0) return
      e.preventDefault()
      histIdx += 1
      if (histIdx >= history.length) { histIdx = -1; cmd = '' }
      else cmd = history[histIdx]
    }
  }
  $effect(() => {
    gcodeLog.length // re-run as the log grows
    if (consoleEl) consoleEl.scrollTop = consoleEl.scrollHeight
  })

  async function run() {
    error = ''
    const m = METHODS[method]
    const p = {}
    for (const pd of m.params) {
      const v = params[method][pd.k]
      if (v === '') continue
      if (pd.vol) { p[pd.k] = vol2lin(v).toFixed(4); continue } // always send converted
      if (String(v) === String(pd.def)) continue               // unchanged → firmware default
      p[pd.k] = v
    }
    if (!apply) p.APPLY = 0
    try {
      await sendGcode(m.build(p)) // resolves when the calibration finishes
    } catch (e) {
      error = e.message
    }
  }
</script>

<div class="badges">
  <span class="badge" class:ok={lcOk} class:bad={!lcOk}>
    {lcOk ? `load cell: ${lcName ?? 'found'}` : 'no load cell'}
  </span>
  {#if printing}<span class="badge warn-b">printing — result applies live</span>{/if}
</div>

<section class="card prep">
  <div class="groups">
    <div class="group">
      <span class="glabel">hotend</span>
      <span class="temps" class:ok={hotOk}>
        {temp?.toFixed(0) ?? '—'}° <small class="dim">/ {target?.toFixed(0) ?? '—'}°</small>
      </span>
      <input type="number" min="0" max="400" title="target temperature, °C"
             bind:value={setTemp} disabled={running} />
      <button class="primary heatbtn" onclick={applyHeat}
              disabled={running || prepBusy === 'heat'}
              title={heating ? 're-apply this setpoint' : 'start heating'}>
        {heating ? 'set' : 'heat'}
      </button>
      {#if heating}
        <button class="heatbtn" onclick={heatOff}
                disabled={running || prepBusy === 'heat'} title="turn the heater off">
          off
        </button>
      {/if}
      {#if !hotOk}<span class="cold">too cold to extrude</span>{/if}
    </div>
    <div class="group">
      <span class="glabel">motion</span>
      <button onclick={doHome} class="homebtn"
              disabled={running || printing || prepBusy !== ''}>
        {prepBusy === 'home' ? 'homing…' : 'home'}
      </button>
      <span class="hstatus"
            class:on={homedXYZ && prepBusy !== 'home'}
            class:busy={prepBusy === 'home'}>
        {prepBusy === 'home' ? 'homing…' : homedXYZ ? 'homed' : 'not homed'}
      </span>
      <span class="sep"></span>
      <label class="ax">X <input type="number" bind:value={posX} disabled={running} /></label>
      <label class="ax">Y <input type="number" bind:value={posY} disabled={running} /></label>
      <label class="ax">Z <input type="number" min="0" bind:value={posZ} disabled={running} /></label>
      <button class="movebtn" onclick={doMove}
              disabled={running || printing || !homedXYZ || prepBusy !== ''}
              title={homedXYZ
                ? 'raise Z first, travel, then descend to target Z'
                : 'home first'}>
        {prepBusy === 'move' ? 'moving…' : 'move'}
      </button>
      {#if pos}
        <span class="dim posnow" title="current toolhead position">
          at {pos[0].toFixed(0)} / {pos[1].toFixed(0)} / {pos[2].toFixed(1)}
        </span>
      {/if}
    </div>
  </div>
  {#if posWarn}<p class="warnline">⚠ {posWarn}</p>{/if}
  {#if prepErr}<p class="error">{prepErr}</p>{/if}
</section>

<div class="methods">
  {#each Object.entries(METHODS) as [id, m]}
    <button class="method" class:selected={method === id}
            onclick={() => (method = id)} disabled={running}>
      <span class="mlabel">{m.label}</span>
      <span class="tags">
        {#each m.tags as t}<span class="tag" class:warn-t={t === 'needs tuning'}>{t}</span>{/each}
      </span>
      <span class="mdesc dim">{m.desc}</span>
    </button>
  {/each}
</div>

{#if method === 'sweep' && sweepGuardLow}
  <p class="sweep-note">
    <strong>⚠ Sweep needs <code>max_extrude_cross_section</code> raised</strong>
    {#if mecs != null}(currently <code>{mecs.toFixed(2)}</code> mm²){/if}.
    Its PA-gate wobble makes a high extrude-ratio move that Klipper blocks by
    default. Add <code>max_extrude_cross_section: 200</code> (or more) to your
    <code>[extruder]</code> in <code>printer.cfg</code> and restart — it only
    relaxes a safety check, not your prints (0 isn't allowed; Klipper requires
    &gt; 0). The run also aborts up front with the exact value if it's still too
    low. See <code>CONFIG.md</code>; Decay doesn't need this.
  </p>
{/if}

<section class="card">
  <div class="runrow">
    <button class="primary" onclick={run}
            title={hotOk ? '' : 'heat the hotend first'}
            disabled={running || !lcOk || !hotOk || !conn.moonraker || prepBusy !== ''}>
      {running ? 'running…' : `Run ${METHODS[method].label.toLowerCase()} calibration`}
    </button>
    <label class="apply">
      <input type="checkbox" bind:checked={apply} disabled={running} />
      apply result live
    </label>
  </div>

  {#if running}
    <div class="progress">
      <div class="bar"><div class="fill" style="width:{pct}%"></div></div>
      <span class="dim">{activity.method} · {pct}%</span>
    </div>
  {/if}
  {#if error}<p class="error">{error}</p>{/if}

  <details class="advanced" open={false}>
    <summary>advanced parameters</summary>
    <div class="grid">
      {#each commonParams as p}
        <label>
          <span class="dim">{p.k} <small>· {p.hint}</small></span>
          <input bind:value={params[method][p.k]} placeholder={String(p.def)}
                 disabled={running} />
        </label>
      {/each}
    </div>
    <p class="flow-note">
      <strong>ℹ Reading these flows.</strong> They are volumetric — mm³/s of
      plastic, assuming 1.75 mm filament. Rough scale: a 0.4 × 0.2 mm line is
      ≈ 0.4 mm³/s at 5 mm/s, ≈ 6 at 80 mm/s, ≈ 16 at 200 mm/s. Most stock
      hotends top out near 10–15 mm³/s; high-flow ones reach 25–30.</p>
    <details class="expert">
      <summary>expert · rarely needed</summary>
      <div class="grid">
        {#each expertParams as p}
          <label>
            <span class="dim">{p.k} <small>· {p.hint}</small></span>
            <input bind:value={params[method][p.k]} placeholder={String(p.def)}
                   disabled={running} />
          </label>
        {/each}
      </div>
    </details>
    <div class="resetrow">
      <button class="reset" onclick={resetDefaults} disabled={running || atDefaults}>
        reset {METHODS[method].label.toLowerCase()} to defaults
      </button>
      <span class="dim hint">Defaults shown are the canonical values; only changed
        fields are sent.</span>
    </div>
  </details>
</section>

{#if !running && last?.decay}
  <ResultCard kind="decay" result={last.decay} />
  {#if last.decay.plot}
    <section class="card"><DecayPlot plot={last.decay.plot} /></section>
  {/if}
{:else if !running && last?.sweep}
  <ResultCard kind="sweep" result={last.sweep} />
  {#if last.sweep.per_k?.length}
    <section class="card">
      <SweepPlot per_k={last.sweep.per_k} k_opt={last.sweep.k_opt} />
    </section>
  {/if}
{/if}

<details class="card" bind:open={liveOpen}>
  <summary>live force</summary>
  <LiveChart active={liveOpen} />
</details>

<details class="card console">
  <summary>console</summary>
  <pre bind:this={consoleEl}>{gcodeLog.length
    ? gcodeLog.slice(-200).join('\n')
    : 'no output yet — send a command below'}</pre>
  <form class="cmdline" onsubmit={sendCmd}>
    <input bind:value={cmd} onkeydown={cmdKey} spellcheck="false"
           autocomplete="off" placeholder="g-code command (e.g. M115, ↑ for history)"
           disabled={!conn.moonraker} />
    <button class="primary" disabled={!conn.moonraker || !cmd.trim()}>send</button>
  </form>
</details>

<style>
  .badges { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-3); }
  .badge {
    font-size: 0.85rem;
    padding: 2px 10px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--bg-raised);
  }
  .badge.ok { color: var(--accent); border-color: var(--accent-dim); }
  .badge.bad { color: var(--bad); }
  .warn-b { color: var(--warn); }

  .prep { padding: var(--sp-2) var(--sp-3); }
  .groups { display: flex; gap: var(--sp-3); flex-wrap: wrap; }
  .group {
    display: flex;
    gap: var(--sp-2);
    align-items: center;
    padding: var(--sp-1) var(--sp-2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--bg-inset);
  }
  .glabel {
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-dim);
  }
  .temps { font-variant-numeric: tabular-nums; }
  .temps.ok { color: var(--accent); }
  .cold { color: var(--warn); font-size: 0.85rem; }
  .group input { width: 4.2em; background: var(--bg); }
  .group button { background: var(--bg); }
  .group button.primary { background: var(--accent-dim); }
  .heatbtn { min-width: 4.2em; }
  .homebtn { min-width: 5.6em; }
  .movebtn { min-width: 5.6em; }
  .sep {
    width: 1px;
    align-self: stretch;
    background: var(--border);
    margin: 0 var(--sp-1);
  }
  .ax {
    display: flex;
    gap: 4px;
    align-items: center;
    color: var(--text-dim);
    font-size: 0.9rem;
  }
  .posnow { font-variant-numeric: tabular-nums; font-size: 0.85rem; }
  .hstatus {
    font-size: 0.75rem;
    font-weight: 600;
    line-height: 1;
    padding: 4px 8px;
    border-radius: 4px;
    border: 1px solid var(--bad);
    background: var(--bg);
    color: var(--bad);
  }
  .hstatus.on {
    color: var(--accent);
    border-color: var(--accent-dim);
    background: var(--accent-dim);
  }
  .hstatus.busy {
    color: var(--warn);
    border-color: var(--warn);
    animation: pulse 1s ease-in-out infinite;
  }
  @keyframes pulse { 50% { opacity: 0.4; } }
  .warnline { color: var(--warn); font-size: 0.85rem; margin: var(--sp-2) 0 0; }

  .methods { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-2); margin-bottom: var(--sp-3); }
  @media (max-width: 600px) { .methods { grid-template-columns: 1fr; } }
  .method {
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: var(--sp-1);
    padding: var(--sp-2) var(--sp-3);
    background: var(--bg-raised);
  }
  .method.selected { border-color: var(--accent); }
  .mlabel { font-weight: 600; }
  .tags { display: flex; gap: var(--sp-1); }
  .tag {
    font-size: 0.75rem;
    padding: 1px 8px;
    border-radius: 999px;
    background: var(--bg-inset);
    color: var(--accent);
  }
  .tag.warn-t { color: var(--warn); }
  .mdesc { font-size: 0.875rem; font-weight: 400; }

  .runrow { display: flex; gap: var(--sp-3); align-items: center; flex-wrap: wrap; }
  .apply { display: flex; gap: var(--sp-1); align-items: center; color: var(--text-dim); }
  .progress { display: flex; gap: var(--sp-2); align-items: center; margin-top: var(--sp-2); }
  .bar {
    flex: 1;
    height: 8px;
    border-radius: 999px;
    background: var(--bg-inset);
    overflow: hidden;
  }
  .fill { height: 100%; background: var(--accent); transition: width 0.3s linear; }
  .error { color: var(--bad); margin: var(--sp-2) 0 0; }

  .advanced { margin-top: var(--sp-3); }
  .expert { margin-top: var(--sp-2); padding-left: var(--sp-2);
            border-left: 2px solid var(--border); }
  .expert > summary { font-size: 0.85rem; }
  summary { cursor: pointer; color: var(--text-dim); }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
    gap: var(--sp-2);
    margin-top: var(--sp-2);
  }
  .grid label {
    display: flex;
    flex-direction: column;
    justify-content: flex-end; /* pin inputs to the bottom so rows line up */
    gap: 2px;
    font-size: 0.9rem;
  }
  /* reserve two lines for the caption so single- and double-line labels keep
     their inputs on the same baseline across the grid */
  .grid label > .dim { min-height: 2.5em; }
  .hint { font-size: 0.85rem; margin: var(--sp-2) 0 0; color: var(--text-dim); }
  .resetrow {
    display: flex;
    gap: var(--sp-3);
    align-items: center;
    flex-wrap: wrap;
    margin-top: var(--sp-3);
  }
  .resetrow .hint { margin: 0; }
  .flow-note {
    margin: var(--sp-2) 0 0;
    padding: var(--sp-2) var(--sp-3);
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--accent) 9%, var(--bg-raised));
    border-left: 3px solid var(--accent-dim);
    font-size: 0.85rem;
    line-height: 1.45;
  }
  .flow-note strong { color: var(--accent); font-weight: 600; }

  .sweep-note {
    margin: 0 0 var(--sp-3);
    padding: var(--sp-2) var(--sp-3);
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--warn) 12%, var(--bg-raised));
    border-left: 3px solid var(--warn);
    font-size: 0.85rem;
    line-height: 1.45;
  }
  .sweep-note strong { color: var(--warn); font-weight: 600; }
  .sweep-note code { font-size: 0.85em; }

  .console pre {
    margin: var(--sp-2) 0;
    padding: var(--sp-2);
    max-height: 320px;
    overflow: auto;
    font-size: 0.85rem;
    background: var(--bg-inset);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .cmdline { display: flex; gap: var(--sp-2); }
  .cmdline input { flex: 1; font-family: var(--mono); }
  pre { margin: var(--sp-2) 0 0; overflow-x: auto; font-size: 0.85rem; }
</style>
