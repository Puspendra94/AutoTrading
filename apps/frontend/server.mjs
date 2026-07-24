// Production static server + reverse proxy for the Dockerized frontend.
//
// Serves the built Astro site from ./dist and proxies:
//   /api/*     -> backend (the leading /api is stripped)
//   /socket.io -> backend (HTTP + WebSocket upgrade)
//
// This is what lets the frontend be the single browser-facing origin: the backend keeps a
// fixed internal port (BACKEND_INTERNAL_URL, default http://backend:3009) reachable only on
// the compose network, while the frontend's own host port is the one published (randomly).
import http from 'node:http';
import httpProxy from 'http-proxy';
import sirv from 'sirv';

const PORT = parseInt(process.env.PORT || '4321', 10);
const BACKEND = process.env.BACKEND_INTERNAL_URL || 'http://backend:3009';

const serveStatic = sirv('./dist', { single: true, gzip: true });

const proxy = httpProxy.createProxyServer({ changeOrigin: true });
proxy.on('error', (err, _req, res) => {
  if (res && !res.headersSent && res.writeHead) {
    res.writeHead(502, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ message: `Upstream error: ${err.message}` }));
  }
});

const server = http.createServer((req, res) => {
  if (req.url === '/api' || req.url.startsWith('/api/')) {
    req.url = req.url.replace(/^\/api/, '') || '/';
    return proxy.web(req, res, { target: BACKEND });
  }
  if (req.url.startsWith('/socket.io')) {
    return proxy.web(req, res, { target: BACKEND });
  }
  serveStatic(req, res, () => {
    res.statusCode = 404;
    res.end('Not found');
  });
});

// Proxy the WebSocket upgrade for socket.io.
server.on('upgrade', (req, socket, head) => {
  if (req.url.startsWith('/socket.io')) {
    proxy.ws(req, socket, head, { target: BACKEND });
  } else {
    socket.destroy();
  }
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Frontend serving ./dist on :${PORT}, proxying /api + /socket.io -> ${BACKEND}`);
});
