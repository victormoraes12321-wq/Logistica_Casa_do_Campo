const CACHE_NAME = 'driver-app-v3.9.0-prod';
const TILE_CACHE_NAME = 'driver-app-map-tiles-v1';
const APP_SHELL = [
  '/static/driver_app/index.html',
  '/static/driver_app/driver.js?v=3.9.0-prod',
  '/static/driver_app/manifest.json?v=3.6.0',
  '/static/leaflet/leaflet.js?v=1.9.4',
  '/static/leaflet/leaflet.css?v=1.9.4',
  '/static/logo.png?v=3.6.0'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async cache => {
      for (const url of APP_SHELL) {
        try {
          const req = new Request(url, { headers: { 'ngrok-skip-browser-warning': 'true' } });
          const res = await fetch(req);
          if (res.ok) await cache.put(url, res);
        } catch (_) {}
      }
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(key => key !== CACHE_NAME && key !== TILE_CACHE_NAME).map(key => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);

  // 1. Cache Inteligente de Blocos de Mapa (Tiles do Leaflet / Satélite / Ruas)
  const isMapTile = url.hostname.includes('arcgisonline.com') ||
                    url.hostname.includes('openstreetmap.org') ||
                    url.hostname.includes('google.com') ||
                    url.pathname.includes('/tile/');

  if (isMapTile) {
    event.respondWith((async () => {
      const tileCache = await caches.open(TILE_CACHE_NAME);
      const cached = await tileCache.match(event.request);
      if (cached) return cached;
      try {
        const response = await fetch(event.request);
        if (response.ok) {
          await tileCache.put(event.request, response.clone());
        }
        return response;
      } catch (_) {
        return cached || new Response('', { status: 408, statusText: 'Tile offline' });
      }
    })());
    return;
  }

  // 2. Recursos internos do app
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(new Request(event.request, { headers: { 'ngrok-skip-browser-warning': 'true' } }))
        .catch(() => caches.match('/static/driver_app/index.html'))
    );
    return;
  }
  event.respondWith((async () => {
    try {
      const headers = new Headers(event.request.headers);
      headers.set('ngrok-skip-browser-warning', 'true');
      const response = await fetch(new Request(event.request, { headers }));
      if (response.ok) {
        const ct = (response.headers.get('content-type') || '').toLowerCase();
        // Não coloca em cache se ngrok retornar tela de aviso text/plain no lugar de script/css/imagem
        const isMimeMismatch = ct.includes('text/plain') &&
          (url.pathname.endsWith('.png') || url.pathname.endsWith('.js') || url.pathname.endsWith('.css'));
        if (!isMimeMismatch) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put(event.request, response.clone());
        }
      }
      return response;
    } catch (_) {
      return caches.match(event.request);
    }
  })());
});
