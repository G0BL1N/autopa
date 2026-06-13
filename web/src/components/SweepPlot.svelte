<script>
  import { onMount } from 'svelte'
  import { makeChart, vline } from '../lib/chart.js'

  // per_k: [{k, segs_inc, segs_tot, overshoot, undershoot}], k_opt: number|null
  let { per_k, k_opt = null } = $props()
  let el
  let chart

  function data(rows) {
    return [
      rows.map((r) => r.k),
      rows.map((r) => r.overshoot),
      rows.map((r) => r.undershoot),
    ]
  }

  onMount(() => {
    chart = makeChart(el, {
      height: 220,
      legend: true,
      series: [
        {},
        { label: 'overshoot', stroke: '#e5615e', width: 2,
          points: { show: true, size: 4 } },
        { label: 'undershoot', stroke: '#8b94a7', width: 2,
          points: { show: true, size: 4 } },
      ],
      axes: [{}, {}],
      hooks: { draw: [vline(() => k_opt, '#4cc38a')] },
      data: data(per_k),
    })
    return () => chart.destroy()
  })
  $effect(() => {
    if (per_k) chart?.setData(data(per_k))
  })
</script>

<div bind:this={el}></div>
<p class="dim caption">
  per-K step-response medians vs swept K
  {#if k_opt != null}· K_opt={k_opt.toFixed(4)} (dashed){/if}
  — overshoot should sit near 0 up to K_opt, then climb
</p>

<style>
  .caption { font-size: 0.8rem; margin: var(--sp-1) 0 0; }
</style>
