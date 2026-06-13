<script>
  import Header from './components/Header.svelte'
  import Calibrate from './components/Calibrate.svelte'
  import Profiles from './components/Profiles.svelte'
  import Captures from './components/Captures.svelte'
  import { start } from './lib/moonraker.js'
  import { startBridge } from './lib/bridge.js'

  const tabs = [
    ['calibrate', 'Calibrate'],
    ['profiles', 'Profiles'],
    ['captures', 'Captures'],
  ]
  // Hash routing: #captures, #captures/<file>, #compare/<a>/<b> — no router dep.
  let route = $state(location.hash.slice(1) || 'calibrate')
  window.addEventListener('hashchange', () => {
    route = location.hash.slice(1) || 'calibrate'
  })
  let tab = $derived(route.split('/')[0])

  start()
  startBridge()
</script>

<div class="page">
  <Header />
  <nav>
    {#each tabs as [id, label]}
      <a href="#{id}" class:active={tab === id || (id === 'captures' && tab === 'compare')}>{label}</a>
    {/each}
  </nav>
  <main>
    {#if tab === 'calibrate'}
      <Calibrate />
    {:else if tab === 'profiles'}
      <Profiles />
    {:else}
      <Captures {route} />
    {/if}
  </main>
</div>

<style>
  .page {
    max-width: 880px;
    margin: 0 auto;
    padding: 0 var(--sp-3) var(--sp-4);
  }
  nav {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border);
    margin-bottom: var(--sp-3);
  }
  nav a {
    padding: var(--sp-2) var(--sp-3);
    color: var(--text-dim);
    text-decoration: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    font-weight: 500;
  }
  nav a.active {
    color: var(--text);
    border-bottom-color: var(--accent);
  }
</style>
