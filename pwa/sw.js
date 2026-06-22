const CACHE = 'sentiment-v1';

const SHELL = [
  '/sentiment/',
  '/sentiment/manifest.json',
  '/sentiment/icon-192.png',
  '/sentiment/icon-512.png',
  '/sentiment/apple-touch-icon.png',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // API-Calls nie cachen
  if (url.pathname.startsWith('/sentiment/api/')) return;
  // HTML: network-first
  if (e.request.destination === 'document') {
    e.respondWith(fetch(e.request).catch(() => caches.match('/sentiment/')));
    return;
  }
  e.respondWith(
    caches.match(e.request).then(c => c || fetch(e.request))
  );
});
