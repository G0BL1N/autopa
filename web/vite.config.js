import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// Dev server proxies straight to the live printer so `bun run dev` exercises
// real Moonraker + the Klippy bridge. Override with PRINTER=<host>.
// /klippysocket lives on Moonraker's own port (nginx doesn't proxy it).
const printer = process.env.PRINTER || 'printer.local'

export default defineConfig({
  base: '/autopa/',
  plugins: [svelte()],
  server: {
    proxy: {
      // capture downloads are served by the printer's nginx (static alias);
      // must be listed before the dev server claims the /autopa/ base
      '/autopa/captures': `http://${printer}`,
      '/websocket': { target: `ws://${printer}`, ws: true },
      '/klippysocket': { target: `ws://${printer}:7125`, ws: true },
      '/printer': `http://${printer}`,
      '/server': `http://${printer}`,
    },
  },
})
