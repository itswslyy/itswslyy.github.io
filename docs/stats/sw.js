// Always load the newest version of the app (GitHub Pages lets phones cache pages for 10 min).
// Falls back to the last saved copy when there's no signal. Also shows push notifications from the stats worker.
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
self.addEventListener("push", e => {
  let d = {}; try { d = e.data.json(); } catch (_) { d = { title: "My Stats", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "My Stats", { body: d.body || "", icon: "icon.png", badge: "icon.png", data: { url: d.url || "./" } }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) if ("focus" in c) return c.focus();
    return self.clients.openWindow((e.notification.data && e.notification.data.url) || "./");
  }));
});
