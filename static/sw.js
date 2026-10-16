/* CINEMA WORLD service worker: never cache pages, login, payments, APIs, or admin routes. */
const CACHE = "cinema-world-static-v2";

self.addEventListener("install", event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Always let Flask/Render handle page navigations and dynamic routes directly.
  // This prevents a cached homepage from appearing when /login or another page fails.
  if (request.mode === "navigate") return;
  if (
    url.pathname.startsWith("/login") ||
    url.pathname.startsWith("/admin") ||
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/payment") ||
    url.pathname.startsWith("/checkout") ||
    url.pathname.startsWith("/request-movie")
  ) return;

  // Cache only static assets; never store HTML or user-specific responses.
  if (!url.pathname.startsWith("/static/")) return;

  event.respondWith(
    fetch(request).then(response => {
      if (response && response.ok) {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(request, copy)).catch(() => {});
      }
      return response;
    }).catch(() => caches.match(request))
  );
});
