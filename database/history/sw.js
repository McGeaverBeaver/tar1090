// tar1090 reports — minimal service worker (installability + fast static assets).
// Deliberately conservative: it never intercepts navigations, /api, /oidc or cross-origin
// requests, so server-side OIDC auth stays fully in control.
const CACHE = 'tar1090-reports-v10';
const ASSETS = ['/acicons.js', '/groundview.js', '/auth.js', '/manifest.webmanifest', '/icon.svg', '/icon-maskable.svg'];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS).catch(() => {})));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;                 // leave CDN / IdP alone
  if (req.mode === 'navigate') return;                        // navigations -> network (auth redirects)
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/oidc/')) return;
  if (!/\.(js|css|svg|png|ico|webmanifest)$/.test(url.pathname)) return;
  // auth.js drives the role/logout UI -> always network-first so it can't go stale
  if (url.pathname === '/auth.js') {
    e.respondWith(fetch(req).then((r) => {
      const copy = r.ok ? r.clone() : null;            // clone BEFORE the body is read by the page
      if (copy) caches.open(CACHE).then((c) => c.put(req, copy));
      return r;
    }).catch(() => caches.match(req)));
    return;
  }
  // cache-first for our static shell, refreshed in the background
  e.respondWith(caches.open(CACHE).then(async (c) => {
    const hit = await c.match(req);
    const net = fetch(req).then((r) => { if (r.ok) c.put(req, r.clone()); return r; }).catch(() => hit);
    return hit || net;
  }));
});
