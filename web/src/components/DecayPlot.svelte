<script>
  import { onMount } from 'svelte'
  import { makeChart, vline } from '../lib/chart.js'

  // plot: {fold_t, fold_f, tau, amp, c, window} from last.decay / capture_detail.
  let { plot } = $props()
  let el
  let chart

  // Fit overlay A·exp(-t/τ)+C, drawn only inside the fitted window — purely
  // display math from the backend's coefficients, nothing re-estimated here.
  function data(p) {
    const fit = p.fold_t.map((t) =>
      t <= p.window ? p.amp * Math.exp(-t / p.tau) + p.c : null)
    return [p.fold_t, p.fold_f, fit]
  }

  onMount(() => {
    chart = makeChart(el, {
      height: 220,
      legend: true,
      series: [
        {},
        { label: 'folded decay', stroke: '#8b94a7', width: 1,
          points: { show: true, size: 4 } },
        { label: 'fit', stroke: '#4cc38a', width: 2,
          points: { show: false } },
      ],
      axes: [{}, {}],
      hooks: { draw: [vline(() => plot?.window, '#e3b341')] },
      data: data(plot),
    })
    return () => chart.destroy()
  })
  $effect(() => {
    if (plot) chart?.setData(data(plot))
  })
</script>

<div bind:this={el}></div>
<p class="dim caption">
  post-stop force fold · fit τ={plot.tau.toFixed(4)}s inside the
  {plot.window.toFixed(2)}s window (dashed)
</p>

<style>
  .caption { font-size: 0.8rem; margin: var(--sp-1) 0 0; }
</style>
