import { defineConfig } from 'vite'

// Dev: the phone loads this Vite server (QR sideload); /ws and /skills are proxied to the bluebook server.
export default defineConfig({
  server: {
    allowedHosts: ['.ts.net'], // sideload over the tailnet by MagicDNS name
    proxy: {
      '/ws': { target: 'ws://127.0.0.1:8787', ws: true },
      '/skills': 'http://127.0.0.1:8787',
    },
  },
})
