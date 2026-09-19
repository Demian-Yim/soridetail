// 한마디 서비스워커 — 최소 캐시. /api/ 요청은 절대 가로채지 않는다(항상 네트워크 그대로 통과).
const CACHE_NAME = "hanmadi-v1";
const CACHED_PATHS = ["/eye", "/static/voice.js", "/static/manifest.webmanifest", "/static/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(CACHED_PATHS))
      .catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // /api/ 요청은 캐시·가로채기 절대 금지 — 그대로 네트워크로 보낸다.
  if (url.pathname.startsWith("/api/")) return;

  // 지정된 정적 경로 외에는 관여하지 않는다(기본 네트워크 동작 유지).
  if (!CACHED_PATHS.includes(url.pathname)) return;

  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
