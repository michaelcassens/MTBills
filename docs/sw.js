// Bill Watch service worker: the app works offline; data is network-first.
// Change CACHE when you edit index.html so phones pick up the new version.
const CACHE = "billwatch-v2";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin || e.request.method !== "GET") return;
  const p = url.pathname;
  if (p.endsWith(".json") || p.endsWith("config.js") || p.endsWith("/") || p.endsWith("index.html")) {
    // Network first; fall back to the last copy when offline.
    const cacheKey = p.split("/").pop() || "./";
    e.respondWith(fetch(e.request).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(cacheKey, copy)); }
      return r;
    }).catch(() => caches.match(cacheKey)));
    return;
  }
  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
});
