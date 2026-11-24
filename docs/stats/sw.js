// Always load the newest version of the app (GitHub Pages lets phones cache pages for 10 min).
// Falls back to the last saved copy when there's no signal.
const CACHE = "stats-app-v1";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;  // stats data etc. go straight to the network
  e.respondWith(
    fetch(e.request, { cache: "no-store" })
      .then(res => { const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return res; })
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
