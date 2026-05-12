// Ki service worker — push notifications only, no caching
const VERSION = '10'

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', event => {
  // Clear any old caches left from previous versions
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  )
})

// ── Push: show notification (skip if app is focused) ─────────────────────────
self.addEventListener('push', event => {
  let data = { title: 'Ki', body: 'New message' }
  try {
    data = event.data.json()
  } catch (_) {
    if (event.data) data.body = event.data.text()
  }

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (const client of windowClients) {
        if (client.focused) return  // app is open and focused — skip notification
      }
      return self.registration.showNotification(data.title || 'Ki', {
        body: data.body,
        icon: '/app/icon.png',
        badge: '/app/icon.png',
        tag: 'ki-message',
        renotify: true,
        vibrate: [100, 50, 100],
        data: { url: '/app/' },
      })
    })
  )
})

// ── Notification click: focus or open the app ────────────────────────────────
self.addEventListener('notificationclick', event => {
  event.notification.close()
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (const client of windowClients) {
        if (client.url.includes('/app/') && 'focus' in client) return client.focus()
      }
      if (clients.openWindow) return clients.openWindow('/app/')
    })
  )
})
