// 最小限のService Worker（PWAとしてホーム画面に追加できるようにする）。
// 画面は常にサーバーから取得する（他ユーザーの更新を反映するため、HTMLはキャッシュしない）。
const CACHE = 'ops-static-v1';
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(['/static/app.css', '/static/vendor/htmx-2.0.4.min.js', '/static/icon-192.png'])));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method === 'GET' && url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});
