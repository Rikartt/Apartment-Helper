/* WGPendler service worker: caches the app shell only. API data is never cached. */
const SHELL = 'wgp-shell-v1';
const LIB = 'wgp-lib-v1';
const SHELL_URLS = ['/', '/manifest.json', '/icons/icon-192.png', '/icons/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== SHELL && k !== LIB).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function tell(msg) {
  for (const c of await self.clients.matchAll()) c.postMessage(msg);
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Versioned Leaflet assets (unpkg): cache-first, they are immutable.
  if (url.hostname === 'unpkg.com') {
    e.respondWith(
      caches.open(LIB).then(async (c) => {
        const hit = await c.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res.ok || res.type === 'opaque') c.put(req, res.clone());
        return res;
      })
    );
    return;
  }

  if (url.origin !== self.location.origin) return;   // map tiles etc.: leave to the network

  // App shell: network-first (so updates arrive), cache only as offline fallback.
  const isShell = req.mode === 'navigate' || SHELL_URLS.includes(url.pathname);
  if (isShell) {
    e.respondWith(
      fetch(req).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(url.pathname === '/' || req.mode === 'navigate' ? '/' : req, copy)); }
        return res;
      }).catch(async () => {
        tell({ type: 'offline' });
        return (await caches.match(req.mode === 'navigate' ? '/' : req)) || new Response('Offline', { status: 503 });
      })
    );
    return;
  }

  // Everything else (/listings, /profile, ...): network only, never cached.
  e.respondWith(
    fetch(req, { cache: 'no-store' }).catch(() => {
      tell({ type: 'offline' });
      return new Response(JSON.stringify({ offline: true, detail: 'You are offline; no cached listings are shown.' }),
        { status: 503, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
    })
  );
});
