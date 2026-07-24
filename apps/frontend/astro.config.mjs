import { defineConfig } from 'astro/config';

// Where the backend API lives when running `astro dev` individually. In Docker the
// static build is served by server.mjs instead, which proxies to the `backend` service.
const API_TARGET = process.env.API_INTERNAL_URL || 'http://localhost:3009';

export default defineConfig({
  server: {
    port: 4321,
    host: true,
  },
  vite: {
    server: {
      // Same-origin proxy so the browser only ever talks to the frontend origin:
      //   /api/*      -> backend (path prefix stripped)
      //   /socket.io  -> backend (WebSocket upgrade proxied)
      proxy: {
        '/api': {
          target: API_TARGET,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
        '/socket.io': {
          target: API_TARGET,
          changeOrigin: true,
          ws: true,
        },
      },
    },
  },
});
