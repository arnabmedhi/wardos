const SHELL = "wardos-shell-L3";
const ASSETS = ["/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png",
  "/icons/maskable-192.png", "/icons/maskable-512.png"];
const FONT = "https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700;800&display=swap";
self.addEventListener("install", (event) => event.waitUntil((async () => {
  const cache = await caches.open(SHELL);
  await cache.addAll(ASSETS);
  await self.skipWaiting();
})()));
self.addEventListener("activate", (event) => event.waitUntil((async () => {
  for (const key of await caches.keys()) if (key.startsWith("wardos-shell-") && key !== SHELL) await caches.delete(key);
  await self.clients.claim();
})()));
self.addEventListener("fetch", (event) => {
  const req = event.request, url = new URL(req.url);
  if (req.method !== "GET") return;
  const local = url.origin === self.location.origin;
  const page = local && url.pathname === "/" && !url.search && req.mode === "navigate";
  const asset = local && !url.search && ASSETS.includes(url.pathname);
  const font = url.href === FONT || (url.origin === "https://fonts.gstatic.com" && url.pathname.startsWith("/s/figtree/"));
  if (!page && !asset && !font) {
    if (local) event.respondWith(fetch(req, { cache: "no-store" }).catch(() => new Response(
      JSON.stringify({ detail: "No internet" }), { status: 503, headers: { "Content-Type": "application/json", "X-WardOS-Offline": "1" } })));
    return;
  }
  event.respondWith((async () => {
    const cache = await caches.open(SHELL);
    if (!page) { const hit = await cache.match(req); if (hit) return hit; }
    try {
      const response = await fetch(req);
      if (response.ok && response.type !== "opaque") await cache.put(req, response.clone());
      return response;
    } catch (error) {
      const hit = await cache.match(req);
      if (hit) return hit;
      throw error;
    }
  })());
});
self.addEventListener("push", (event) => event.waitUntil((async () => {
  let msg = {};
  try { msg = event.data.json(); } catch {  }
  await self.registration.showNotification(msg.title || "WardOS", {
    icon: "/icons/icon-192.png", badge: "/icons/icon-192.png", silent: !!msg.silent,
    tag: msg.tag || "wardos", data: { url: msg.url || "/" }
  });
})()));
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const url = new URL(event.notification.data?.url || "/", self.location.origin);
    if (url.origin !== self.location.origin) return;
    if (/^#\/a\/[^/]+$/.test(url.hash)) url.hash += "/alert";
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (new URL(client.url).origin === url.origin) {
        await client.navigate(url.href);
        await client.focus();
        return;
      }
    }
    await self.clients.openWindow(url.href);
  })());
});
