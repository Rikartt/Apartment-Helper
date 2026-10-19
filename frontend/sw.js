/* WGPendler service worker: caches the app shell only. API data is never cached. */
// Bump SHELL when the caching logic changes; the activate step deletes every other cache.
const SHELL = 'wgp-shell-v2';
const LIB = 'wgp-lib-v1';
// pwa.js, checklist.js and geo.js are loaded by index.html: without them the offline page has no banner
// and the draft checklist throws. They are fetched network-first like the page itself.
const SHELL_URLS = ['/', '/manifest.json', '/pwa.js', '/checklist.js', '/geo.js',
                    '/icons/icon-192.png', '/icons/icon-512.png'];
const SHELL_TIMEOUT_MS = 8000;   // flaky wifi: fall back to the cached shell instead of hanging

self.addEventListener('install', (e) => {
  // one missing file (or a 401 from the access guard) must not abort the whole install
  e.waitUntil(
    caches.open(SHELL)
      .then((c) => Promise.allSettled(SHELL_URLS.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
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
  // Only the real page ('/') is stored under '/': navigating to /docs or /openapi.json must
  // never overwrite the cached app shell.
  const isPage = req.mode === 'navigate';
  const isShell = SHELL_URLS.includes(url.pathname) || (isPage && url.pathname === '/');
  if (isShell) {
    const key = isPage ? '/' : url.pathname;
    e.respondWith(
      new Promise((resolve, reject) => {
        const t = setTimeout(() => reject(new Error('timeout')), SHELL_TIMEOUT_MS);
        fetch(req).then((r) => { clearTimeout(t); resolve(r); }, (err) => { clearTimeout(t); reject(err); });
      }).then((res) => {
        if (res.ok && res.status === 200) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(key, copy)); }
        return res;
      }).catch(async () => {
        tell({ type: 'offline' });
        return (await caches.match(key)) || new Response('Offline', { status: 503 });
      })
    );
    return;
  }
  if (isPage) return;   // any other page (/docs, ...): plain network, never cached

  // Everything else (/listings, /profile, ...): network only, never cached.
  e.respondWith(
    fetch(req, { cache: 'no-store' }).catch(() => {
      tell({ type: 'offline' });
      return new Response(JSON.stringify({ offline: true, detail: 'You are offline; no cached listings are shown.' }),
        { status: 503, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
    })
  );
});
