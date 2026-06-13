<script>
  import { onMount } from 'svelte'
  import { makeChart } from '../lib/chart.js'
  import { fetchCaptureDetail } from '../lib/bridge.js'
  import { conn } from '../lib/stores.svelte.js'

  let { a, b } = $props()

  let da = $state(null)
  let db = $state(null)
  let error = $state('')
  let el = $state(null)
  let chart = null

  let fetched = $state(false)
  $effect(() => {
    if (conn.bridge && !fetched) {
      fetched = true
      Promise.all([fetchCaptureDetail(a), fetchCaptureDetail(b)])
        .then(([ra, rb]) => { da = ra; db = rb })
        .catch((e) => (error = e.message))
    }
  })

  let sameKind = $derived(da && db && da.meta.kind === db.meta.kind)
  let kind = $derived(da?.meta.kind)

  // Linear interpolation of (xs, ys) onto grid x — display-only resampling so
  // two decay folds (separately binned grids) can share one uPlot x-axis.
  function interp(x, xs, ys) {
    let j = 0
    return x.map((xv) => {
      if (xv < xs[0] || xv > xs[xs.length - 1]) return null
      while (j < xs.length - 2 && xs[j + 1] < xv) j++
      const f = (xv - xs[j]) / (xs[j + 1] - xs[j] || 1)
      return ys[j] + f * (ys[j + 1] - ys[j])
    })
  }

  function overlay() {
    if (kind === 'decay' && da.plot?.fold_t && db.plot?.fold_t) {
      const x = da.plot.fold_t
      return {
        data: [x, da.plot.fold_f, interp(x, db.plot.fold_t, db.plot.fold_f)],
        ylabel: 'folded decay',
      }
    }
    if (kind === 'sweep' && da.plot?.per_k && db.plot?.per_k) {
      const ks = [...new Set([...da.plot.per_k.map((r) => r.k),
                              ...db.plot.per_k.map((r) => r.k)])].sort(
        (p, q) => p - q)
      const pick = (rows) => {
        const m = new Map(rows.map((r) => [r.k, r.overshoot]))
        return ks.map((k) => m.get(k) ?? null)
      }
      return {
        data: [ks, pick(da.plot.per_k), pick(db.plot.per_k)],
        ylabel: 'overshoot',
      }
    }
    return null
  }

  $effect(() => {
    const o = sameKind && el ? overlay() : null
    if (o && !chart) {
      chart = makeChart(el, {
        height: 220,
        legend: true,
        series: [
          {},
          { label: `A · ${da.file}`, stroke: '#4cc38a', width: 2,
            spanGaps: true, points: { show: true, size: 3 } },
          { label: `B · ${db.file}`, stroke: '#e3b341', width: 2,
            spanGaps: true, points: { show: true, size: 3 } },
        ],
        data: o.data,
      })
    }
  })
  onMount(() => () => chart?.destroy())

  const RESULT = {
    decay: (d) => ({ label: 'τ (= PA)', value: d.stats.tau }),
    sweep: (d) => ({ label: 'K_opt', value: d.stats.k_opt }),
    capture: (d) => ({ label: 'SNR', value: d.stats.snr }),
  }
  function rows(d) {
    if (!d) return []
    const r = RESULT[d.meta.kind]?.(d)
    return [
      ['kind', d.meta.kind],
      [r?.label ?? 'result', r?.value?.toFixed?.(4) ?? '—'],
      ['confidence', d.stats.conf ?? '—'],
      ['material', d.meta.material ?? '—'],
      ['hotend °C', d.meta.hotend_target ?? '—'],
      ['SNR', d.stats.snr?.toFixed?.(1) ?? '—'],
    ]
  }
</script>

<p><a href="#runs">← all runs</a></p>

{#if error}
  <section class="card"><p class="error">{error}</p></section>
{:else if !da || !db}
  <section class="card"><p class="dim">Loading…</p></section>
{:else}
  <section class="card">
    <h2>Compare</h2>
    <table>
      <thead>
        <tr><th></th>
          <th class="a"><a href="#captures/{da.file}">{da.file}</a></th>
          <th class="b"><a href="#captures/{db.file}">{db.file}</a></th></tr>
      </thead>
      <tbody>
        {#each rows(da) as [label], i}
          <tr>
            <td class="dim">{label}</td>
            <td>{rows(da)[i][1]}</td>
            <td>{rows(db)[i][1]}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </section>
  {#if sameKind}
    <section class="card">
      <h2>{kind === 'decay' ? 'Folded decays' : 'Overshoot vs K'}</h2>
      <div bind:this={el}></div>
    </section>
  {:else}
    <section class="card">
      <p class="dim">Different run kinds ({da.meta.kind} vs {db.meta.kind}) —
        no overlay, values only.</p>
    </section>
  {/if}
{/if}

<style>
  a { color: var(--accent); text-decoration: none; }
  .error { color: var(--bad); }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    text-align: left;
    padding: var(--sp-1) var(--sp-2);
    border-bottom: 1px solid var(--border);
  }
  th.a { color: var(--accent); }
  th.b { color: var(--warn); }
</style>
