<script>
  import { conn } from '../lib/stores.svelte.js'

  let ok = $derived(conn.moonraker && conn.klippy === 'ready')
  let state = $derived(
    !conn.moonraker ? 'connecting…'
    : conn.klippy === 'ready' ? 'connected'
    : `klippy ${conn.klippy || '…'}`)
</script>

<header>
  <h1>autopa</h1>
  <span class="dot" class:ok title={state}></span>
  <span class="state">{state}</span>
</header>

<style>
  header {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    padding: var(--sp-3) 0 var(--sp-2);
  }
  h1 {
    font-size: 1.2rem;
    margin: 0;
    letter-spacing: 0.02em;
  }
  .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--bad);
  }
  .dot.ok { background: var(--accent); }
  .state { color: var(--text-dim); font-size: 0.9rem; }
</style>
